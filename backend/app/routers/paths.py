"""P1.1 归档路由：路径管理+扫描。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import ScanReport, WatchPath
from ..services.archive.scanner import scan_watch_path

router = APIRouter(prefix="/api/paths", tags=["归档路径"])


class PathIn(BaseModel):
    path: str


@router.get("")
def list_paths(session: Session = Depends(get_session)) -> dict[str, Any]:
    rows = session.query(WatchPath).order_by(WatchPath.created_at).all()
    return {
        "items": [
            {
                "id": w.id, "path": w.path, "enabled": w.enabled,
                "last_scan_at": w.last_scan_at.isoformat() if w.last_scan_at else None,
            }
            for w in rows
        ]
    }


@router.post("")
def add_path(body: PathIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    p = body.path.strip()
    if not p or not Path(p).is_dir():
        raise HTTPException(400, f"路径不存在或不可访问：{p}")
    existing = session.query(WatchPath).filter(WatchPath.path == p).one_or_none()
    if existing:
        return {"id": existing.id, "existed": True}
    wp = WatchPath(path=p)
    session.add(wp)
    session.commit()
    return {"id": wp.id, "existed": False}


@router.delete("/{path_id}")
def remove_path(path_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    wp = session.get(WatchPath, path_id)
    if wp is None:
        raise HTTPException(404, "路径不存在")
    session.delete(wp)
    session.commit()
    return {"ok": True}


@router.post("/{path_id}/scan")
def scan_now(path_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    wp = session.get(WatchPath, path_id)
    if wp is None:
        raise HTTPException(404, "路径不存在")
    result = scan_watch_path(session, wp, trigger="manual")
    return {"added": result.added, "updated": result.updated,
            "skipped": result.skipped, "failed": result.failed, "errors": result.errors}


@router.get("/reports")
def list_reports(limit: int = 20, session: Session = Depends(get_session)) -> dict[str, Any]:
    rows = session.query(ScanReport).order_by(ScanReport.created_at.desc()).limit(limit).all()
    return {
        "items": [
            {
                "id": r.id, "path": r.path, "trigger": r.trigger,
                "added": r.added, "updated": r.updated, "skipped": r.skipped, "failed": r.failed,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }
