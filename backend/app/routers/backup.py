"""P4 数据备份路由：列表 / 手动备份 / 还原（暂存，重启生效）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..services.audit import audit
from ..services.backup import list_backups, restore_backup, run_backup

router = APIRouter(prefix="/api/backup", tags=["F4.4 备份"])


@router.get("")
def backup_list() -> dict[str, Any]:
    return {"items": list_backups(), "keep": 7, "schedule": "每日 02:00 自动备份"}


@router.post("")
def backup_now() -> dict[str, Any]:
    try:
        meta = run_backup("manual")
    except Exception as e:
        raise HTTPException(500, f"备份失败：{e}") from e
    audit("backup_created", detail=meta["name"])
    return meta


@router.post("/{name}/restore")
def backup_restore(name: str) -> dict[str, Any]:
    try:
        r = restore_backup(name)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    audit("backup_restore_staged", detail=name)
    return r
