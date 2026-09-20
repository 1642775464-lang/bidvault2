"""数据备份（F4.4）：每日 02:00 自动备份 db+files，滚动保留 7 份；支持手动备份与还原。

还原采用「暂存 + 下次启动生效」：Windows 下 sqlite 文件被运行中服务占用，
无法热替换；还原先把备份放入 data/restore_staging/，lifespan 启动时最先应用。
"""

from __future__ import annotations

import contextlib
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import get_settings

KEEP = 7
_BACKUP_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")


def backups_dir() -> Path:
    p = get_settings().data_dir / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total


def run_backup(label: str = "manual") -> dict[str, Any]:
    """执行一次备份，返回备份元信息；超过 KEEP 份滚动清理。"""
    data = get_settings().data_dir
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = backups_dir() / stamp
    dest.mkdir(parents=True)
    for sub in ("db", "files"):
        src = data / sub
        if src.is_dir():
            shutil.copytree(src, dest / sub)
    _rotate()
    return {
        "name": stamp,
        "label": label,
        "size_mb": round(_dir_size(dest) / 1048576, 1),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def list_backups() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in sorted((x for x in backups_dir().iterdir() if x.is_dir()), reverse=True):
        if not _BACKUP_NAME_RE.match(d.name):
            continue
        meta_file = d / "meta.json"
        label = "manual"
        if meta_file.is_file():
            with contextlib.suppress(OSError):
                label = meta_file.read_text(encoding="utf-8").strip() or label
        out.append({
            "name": d.name,
            "label": label,
            "size_mb": round(_dir_size(d) / 1048576, 1),
        })
    return out


def _rotate() -> None:
    dirs = sorted((x for x in backups_dir().iterdir() if x.is_dir() and _BACKUP_NAME_RE.match(x.name)), reverse=True)
    for old in dirs[KEEP:]:
        shutil.rmtree(old, ignore_errors=True)


def restore_backup(name: str) -> dict[str, Any]:
    """暂存还原：备份内容复制到 restore_staging，下次启动服务时生效。"""
    if not _BACKUP_NAME_RE.match(name):
        raise ValueError("非法备份名")
    src = backups_dir() / name
    if not src.is_dir():
        raise ValueError("备份不存在")
    staging = get_settings().data_dir / "restore_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for sub in ("db", "files"):
        s = src / sub
        if s.is_dir():
            shutil.copytree(s, staging / sub)
    (staging / "meta.json").write_text(name, encoding="utf-8")
    return {"ok": True, "name": name, "restart_required": True}


def apply_pending_restore() -> bool:
    """启动时应用暂存还原（lifespan 最先调用，须在 make_engine 之前）。"""
    staging = get_settings().data_dir / "restore_staging"
    if not staging.is_dir():
        return False
    data = get_settings().data_dir
    for sub in ("db", "files"):
        s = staging / sub
        if not s.is_dir():
            continue
        dst = data / sub
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(s), str(dst))
    shutil.rmtree(staging, ignore_errors=True)
    return True


def start_backup_scheduler() -> threading.Thread:
    """每日 02:00 自动备份：守护线程每 30 分钟检查，当天已有备份则跳过。"""

    def loop() -> None:
        while True:
            try:
                now = datetime.now()
                if now.hour == 2:
                    today = now.strftime("%Y-%m-%d")
                    if not any(
                        x.is_dir() and x.name.startswith(today) for x in backups_dir().iterdir()
                    ):
                        run_backup("auto")
            except Exception:
                pass
            time.sleep(1800)

    t = threading.Thread(target=loop, daemon=True, name="backup-scheduler")
    t.start()
    return t
