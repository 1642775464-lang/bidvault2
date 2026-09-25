"""E-1A：标书工作区路由——创建/查询/骨架状态/事件列表。

所有写端点自动受本机鉴权中间件保护（D-1R4）；只读端点环回放行。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import WorkspaceEvent
from ..services import workspace_fs
from ..services.workspace_fs import WorkspaceError

router = APIRouter(prefix="/api/workspace", tags=["标书工作区"])


class WorkspaceIn(BaseModel):
    path: str
    name: str = ""


def _payload(session: Session, row) -> dict[str, Any]:
    from pathlib import Path

    root = Path(row.root_path)
    ok, repaired = workspace_fs.skeleton_status(root)
    return {
        "active": True,
        "id": row.id,
        "root": row.root_path,
        "name": row.name,
        "schema_version": row.schema_version,
        "skeleton_ok": ok,
        "repaired": repaired,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("")
def get_workspace(session: Session = Depends(get_session)) -> dict[str, Any]:
    row = workspace_fs.get_active(session)
    if row is None:
        return {"active": False}
    return _payload(session, row)


@router.post("", status_code=201)
def create_workspace(body: WorkspaceIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        row = workspace_fs.create_workspace(session, body.path, body.name)
    except WorkspaceError as e:
        code = 409 if "已存在活动工作区" in str(e) else 400
        raise HTTPException(code, {"code": "WORKSPACE_ERROR", "detail": str(e)}) from e
    return _payload(session, row)


@router.get("/events")
def list_events(status: str = "detected", limit: int = 100,
                session: Session = Depends(get_session)) -> dict[str, Any]:
    row = workspace_fs.get_active(session)
    if row is None:
        return {"items": []}
    q = (session.query(WorkspaceEvent)
         .filter(WorkspaceEvent.workspace_id == row.id, WorkspaceEvent.status == status)
         .order_by(WorkspaceEvent.created_at.desc()))
    items = [
        {"id": e.id, "rel_path": e.rel_path, "kind": e.kind, "file_hash": e.file_hash,
         "status": e.status, "created_at": e.created_at.isoformat() if e.created_at else None}
        for e in q.limit(max(1, min(limit, 500))).all()
    ]
    return {"items": items}


@router.post("/events/{event_id}/dismiss")
def dismiss_event(event_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    e = session.get(WorkspaceEvent, event_id)
    if e is None or e.status != "detected":
        raise HTTPException(404, "事件不存在或已处理")
    e.status = "dismissed"
    session.commit()
    return {"ok": True}
