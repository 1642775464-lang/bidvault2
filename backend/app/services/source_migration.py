"""B-1c：source → OfficeDocument 迁移收口（幂等回填）。

- 每份 Document 建 OfficeDocument(type=source, title=filename, file_type)
- document_versions **按原行 id** 复制到 office_document_versions
  （表不同无冲突 → current_version_id / restored_from_version_id 零重映射，文件不复制）
- documents.office_document_id 回填；OfficeDocument.current_version_id 指原 current
- 幂等：已有 office_document_id 的文档跳过；启动时执行 + 可手动触发

迁移后语义（关单条件）：业务层只认 office_document_id；
documents.current_version_id / stored_name 为 deprecated 兼容字段（回填后与真源一致，
仅由兼容写路径维持，新代码禁止读取）。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Document, DocumentVersion, OfficeDocument, OfficeDocumentVersion

logger = logging.getLogger(__name__)




def _file_size(files_dir: Any, file_path: str | None) -> int:
    if not file_path:
        return 0
    try:
        size: int = (files_dir / file_path).stat().st_size
        return size
    except OSError:
        return 0


def backfill_source_office_documents(session: Session) -> dict[str, int]:
    """幂等回填（补齐语义）：返回 {created, synced, skipped}。

    - 无 od：新建 OfficeDocument + 复制全部版本
    - 有 od：同步缺失的版本行（串行处理时文档可能在无版本阶段被抢先建档），
      并在 current 悬空时补为最新版本 id
    """
    created = synced = skipped = 0
    docs = session.query(Document).all()
    for d in docs:
        versions = (
            session.query(DocumentVersion)
            .filter(DocumentVersion.document_id == d.id)
            .order_by(DocumentVersion.version)
            .all()
        )
        latest_dv = versions[-1] if versions else None
        current = d.current_version_id or (latest_dv.id if latest_dv else None)

        od = (
            session.get(OfficeDocument, d.office_document_id)
            if d.office_document_id
            else None
        )
        if od is None:
            od = OfficeDocument(
                document_type="source",
                title=d.filename,
                file_type=d.file_type or "docx",
                current_version_id=current,
            )
            session.add(od)
            session.flush()
            d.office_document_id = od.id
            created += 1

        # 同步缺失的版本行（按原 id）
        existing = {
            v.id: v
            for v in session.query(OfficeDocumentVersion)
            .filter(OfficeDocumentVersion.office_document_id == od.id)
            .all()
        }
        changed = False
        for dv in versions:
            if dv.id in existing:
                continue
            session.add(OfficeDocumentVersion(
                id=dv.id,  # 原 id 直接沿用：restore 链/current 指针零重映射
                office_document_id=od.id,
                version=dv.version,
                file_path=dv.file_path,
                file_hash=dv.file_hash,
                file_size=_file_size(get_settings().files_dir, dv.file_path),
                source=dv.source,
                restored_from_version_id=dv.restored_from_version_id,
                editor_user=dv.editor_user,
                comment=dv.comment,
                created_at=dv.created_at,
            ))
            changed = True
        # current 悬空补齐
        if od.current_version_id is None and current is not None:
            od.current_version_id = current
            changed = True
        if changed:
            synced += 1
            session.commit()
        else:
            skipped += 1
    if created or synced:
        logger.info("source office documents backfilled: created=%d synced=%d skipped=%d", created, synced, skipped)
    return {"created": created, "synced": synced, "skipped": skipped}


# ---------------- B-1c-4：切换标记 ----------------

CUTOVER_KEY = "source_office_cutover"


def is_cutover_completed(session: Any) -> bool:
    from ..models import AppMeta

    return session.get(AppMeta, CUTOVER_KEY) is not None


def freeze_cutover(session: Any) -> dict[str, Any]:
    """一次性切换标记：记录每个 source 的 legacy 基线（max_version/version_ids/current）。

    幂等：marker 已存在时直接返回现有记录，不覆盖（基线冻结后不可变）。
    写入 marker 后：启动 backfill 停止；legacy 表进入 deprecated read-only。
    """
    import json as _json

    from ..models import AppMeta, Document, DocumentVersion

    existing = session.get(AppMeta, CUTOVER_KEY)
    if existing is not None:
        parsed: dict[str, Any] = _json.loads(existing.value)
        return parsed

    baseline: dict[str, Any] = {"completed_at": _now_iso(), "documents": {}}
    for d in session.query(Document).all():
        if not d.office_document_id:
            continue
        versions = (
            session.query(DocumentVersion)
            .filter(DocumentVersion.document_id == d.id)
            .order_by(DocumentVersion.version)
            .all()
        )
        latest = versions[-1] if versions else None
        baseline["documents"][d.id] = {
            "office_document_id": d.office_document_id,
            "legacy_max_version": latest.version if latest else 0,
            "legacy_version_ids": [v.id for v in versions],
            "legacy_current_version_id": d.current_version_id,
            "cutover_current_version_id": d.current_version_id or (latest.id if latest else None),
        }
    payload = _json.dumps(baseline, ensure_ascii=False)
    session.add(AppMeta(key=CUTOVER_KEY, value=payload))
    session.commit()
    return baseline


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def ensure_source_office_document(session: Any, doc: Any) -> Any:
    """B-1c-4 upload 建档：source Document → OfficeDocument + v1（直写 office 表）。

    与旧 ensure_version（deprecated，写 document_versions）职责替换；
    幂等：已有 od 直接返回。
    """
    import hashlib as _hashlib

    from ..models import OfficeDocument, OfficeDocumentVersion

    if doc.office_document_id:
        od = session.get(OfficeDocument, doc.office_document_id)
        if od is not None:
            return od
    settings = get_settings()
    digest = _hashlib.sha256((settings.files_dir / doc.stored_name).read_bytes()).hexdigest()
    od = OfficeDocument(
        document_type="source", title=doc.filename,
        file_type=doc.file_type or "docx",
    )
    session.add(od)
    session.flush()
    v = OfficeDocumentVersion(
        office_document_id=od.id, version=1,
        file_path=doc.stored_name, file_hash=digest,
        file_size=(settings.files_dir / doc.stored_name).stat().st_size,
        source="upload",
    )
    session.add(v)
    session.flush()
    od.current_version_id = v.id
    doc.office_document_id = od.id
    doc.current_version_id = v.id  # deprecated 兼容写（最后一次）
    session.commit()
    return od
