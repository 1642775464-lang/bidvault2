"""F1.1 归档：扫描（手动/定时/watchdog）+ 去重 + 扫描报告。"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ...config import get_settings
from ...models import Document, ScanReport, WatchPath

logger = logging.getLogger(__name__)

# 归档白名单（项目负责人 09-13 收口：文本格式）
ARCHIVE_EXT = {".docx", ".pdf", ".txt", ".xlsx", ".xls", ".csv"}
UPLOAD_EXT = ARCHIVE_EXT | {".zip"}


def detect_file_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png"}:
        return "scan"
    if ext == ".txt":
        return "txt"
    return ext.lstrip(".")


@dataclass
class ScanResult:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _ingest_entry(session: Session, p: Path, result: ScanResult) -> None:
    try:
        stat = p.stat()
        existing = session.query(Document).filter(Document.origin_path == str(p)).one_or_none()
        if existing is not None:
            if existing.size != stat.st_size or abs(existing.mtime - stat.st_mtime) > 1e-6:
                existing.size = stat.st_size
                existing.mtime = stat.st_mtime
                existing.status = 0
                existing.error_msg = None
                existing.updated_at = datetime.now(UTC)
                result.updated += 1
            else:
                result.skipped += 1
            return
        doc = Document(
            filename=p.name,
            source="path",
            origin_path=str(p),
            kind="tender" if "招标" in p.name else "bid",
            file_type=detect_file_type(p.name),
            size=stat.st_size,
            mtime=stat.st_mtime,
            status=0,
        )
        session.add(doc)
        session.commit()
        result.added += 1
    except Exception as exc:
        session.rollback()
        result.failed += 1
        result.errors.append(f"{p}: {exc}")
        logger.warning("scan entry failed: %s (%s)", p, exc)


def _iter_files(root: Path) -> Iterator[Path]:
    data_root = get_settings().data_dir.resolve()
    for dirpath, dirnames, filenames in root.walk():
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "$"))]
        for fn in filenames:
            if fn.startswith("~$"):
                continue
            if Path(fn).suffix.lower() not in ARCHIVE_EXT:
                continue
            p = Path(dirpath) / fn
            try:
                if p.resolve().is_relative_to(data_root):
                    continue
            except (OSError, ValueError):
                pass
            yield p


def scan_watch_path(session: Session, wp: WatchPath, *, trigger: str = "manual") -> ScanResult:
    result = ScanResult()
    root = Path(wp.path)
    if not root.is_dir():
        result.failed += 1
        result.errors.append(f"路径不存在或不可访问：{wp.path}")
    else:
        for p in _iter_files(root):
            _ingest_entry(session, p, result)

    report = ScanReport(
        path_id=wp.id, path=wp.path, trigger=trigger,
        added=result.added, updated=result.updated,
        skipped=result.skipped, failed=result.failed,
        detail_json=json.dumps(result.errors, ensure_ascii=False) if result.errors else None,
    )
    session.add(report)
    wp.last_scan_at = datetime.now(UTC)
    session.commit()
    return result


def ingest_single_file(session: Session, p: Path) -> bool:
    result = ScanResult()
    _ingest_entry(session, p, result)
    if result.added or result.updated:
        session.add(ScanReport(
            path=str(p), trigger="watch",
            added=result.added, updated=result.updated,
            skipped=result.skipped, failed=result.failed,
        ))
        session.commit()
    return bool(result.added + result.updated)
