"""D-1R3：原生编辑器会话路由——DX 编辑器的打开/心跳/读取/保存/关闭协议端点。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..db import get_session
from ..services.editor_session import (
    EditorSessionConflictError,
    EditorSessionError,
    EditorSessionService,
)

router = APIRouter(prefix="/api", tags=["原生编辑会话"])


def _svc(session: Session) -> EditorSessionService:
    return EditorSessionService(session=session)


@router.post("/office-documents/{oid}/editor-session", status_code=201)
def open_editor_session(oid: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """打开编辑会话=取编辑锁；并发（另一活跃会话）→ 409。"""
    try:
        s, cur = _svc(session).open(oid)
    except EditorSessionConflictError as e:
        raise HTTPException(409, {"code": e.code, "detail": str(e)}) from e
    except EditorSessionError as e:
        raise HTTPException(409, {"code": e.code, "detail": str(e)}) from e
    return {
        "session_id": s.id,
        "office_document_id": oid,
        "base_version_id": cur.id,
        "base_version": cur.version,
        "state": s.state,
    }


@router.get("/office-documents/{oid}/editor-session")
def get_editor_session(oid: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    from ..models import EditorSession

    _svc(session)._reap_expired(oid)
    row = (
        session.query(EditorSession)
        .filter(EditorSession.office_document_id == oid, EditorSession.state == "active")
        .first()
    )
    if row is None:
        return {"active": False}
    return {"active": True, "session_id": row.id, "base_version": row.base_version,
            "editor_user": row.editor_user}


@router.post("/editor-sessions/{sid}/heartbeat")
def editor_heartbeat(sid: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        s = _svc(session).heartbeat(sid)
    except EditorSessionError as e:
        raise HTTPException(409, {"code": e.code, "detail": str(e)}) from e
    return {"session_id": s.id, "state": s.state, "base_version": s.base_version}


@router.get("/editor-sessions/{sid}/file")
def editor_read_file(sid: str, session: Session = Depends(get_session)) -> Response:
    try:
        _, data = _svc(session).read_file(sid)
    except EditorSessionError as e:
        raise HTTPException(409, {"code": e.code, "detail": str(e)}) from e
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="editor-base.docx"'},
    )


@router.put("/editor-sessions/{sid}/file")
async def editor_save_file(sid: str, request: Request, session: Session = Depends(get_session)) -> dict[str, Any]:
    """受控保存：包校验 → fence → 新正式版本（source=native_editor）→ Processor 追平。"""
    data = await request.body()
    if not data:
        raise HTTPException(400, {"code": "EDITOR_SESSION_ERROR", "detail": "保存内容为空"})
    try:
        _, v = _svc(session).save(sid, data)
    except EditorSessionConflictError as e:
        raise HTTPException(409, {"code": e.code, "detail": str(e)}) from e
    except EditorSessionError as e:
        raise HTTPException(409, {"code": e.code, "detail": str(e)}) from e
    return {"version_id": v.id, "version": v.version, "source": v.source}


@router.post("/editor-sessions/{sid}/close")
def editor_close(sid: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        _svc(session).close(sid)
    except EditorSessionError as e:
        raise HTTPException(409, {"code": e.code, "detail": str(e)}) from e
    return {"closed": True}
