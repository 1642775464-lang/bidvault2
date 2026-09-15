"""P3 文档库路由：列表/详情/拆解/上传/绑定。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import Document, ExtractItem, ExtractRow, Project
from ..services.audit import audit
from ..services.ingest.pipeline import process_document

router = APIRouter(prefix="/api/documents", tags=["F1 文档库"])

_MAX_SIZE = 200 * 1024 * 1024


class UploadIn(BaseModel):
    doc_ids: list[str] = []


@router.get("")
def list_docs(
    kind: str | None = None,
    project_id: str | None = None,
    q: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    from ..models import Document

    query = session.query(Document)
    if kind and kind in ("tender", "bid"):
        query = query.filter(Document.kind == kind)
    if project_id:
        query = query.filter(Document.project_id == project_id)
    if q:
        query = query.filter(Document.filename.contains(q))
    rows = query.order_by(Document.created_at.desc()).all()
    return {
        "total": len(rows),
        "items": [
            {
                "id": d.id, "filename": d.filename, "kind": d.kind,
                "file_type": d.file_type, "size": d.size,
                "status": d.status, "project_id": d.project_id,
                "is_secret": d.is_secret, "error_msg": d.error_msg,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in rows
        ],
    }


@router.get("/{doc_id}/extract")
def get_extract(doc_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """返回拆解结构（按组聚合）。"""
    from ..models import Document

    doc = session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    if doc.status != 2:
        return {"groups": [], "status": doc.status}
    items = (
        session.query(ExtractItem)
        .filter(ExtractItem.doc_id == doc_id)
        .order_by(ExtractItem.seq)
        .all()
    )
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        rows = (
            session.query(ExtractRow)
            .filter(ExtractRow.item_id == item.id)
            .order_by(ExtractRow.seq)
            .all()
        )
        groups.setdefault(item.group_name, []).append({
            "id": item.id, "name": item.name, "sum": item.sum,
            "rows": [[r.field, r.value or ""] for r in rows],
        })
    return {
        "status": doc.status,
        "groups": [{"name": g, "items": items_} for g, items_ in groups.items()],
    }


@router.post("/{doc_id}/extract")
def run_extract(doc_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    doc = session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    if doc.status == 1:
        raise HTTPException(409, "拆解进行中")
    result = process_document(session, doc)
    return {"status": result.status, "extracted": result.extracted, "materials": result.materials}


class BindIn(BaseModel):
    project_id: str | None = None  # None = 解绑
    new_project_name: str | None = None
    new_project_industry: str | None = None


@router.post("/{doc_id}/bind")
def bind_project(doc_id: str, body: BindIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    from ..models import BUILTIN_INDUSTRIES

    doc = session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    if body.new_project_name:
        np = Project(
            code="NEW-" + body.new_project_name[:8],
            name=body.new_project_name.strip(),
            industry=body.new_project_industry or BUILTIN_INDUSTRIES[0],
        )
        session.add(np)
        session.flush()
        doc.project_id = np.id
        audit("doc_bound_new_project", doc_id=doc.id, detail=np.name)
    elif body.project_id:
        doc.project_id = body.project_id
        audit("doc_bound", doc_id=doc.id, detail=body.project_id)
    else:
        doc.project_id = None
        audit("doc_unbound", doc_id=doc.id)
    session.commit()
    return {"ok": True, "project_id": doc.project_id}


class SecretIn(BaseModel):
    is_secret: bool


@router.patch("/{doc_id}/secret")
def set_secret(doc_id: str, body: SecretIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    """涉密打标：默认全涉密，确认后才允许在线模型（闸门依据）。"""
    doc = session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    doc.is_secret = 1 if body.is_secret else 0
    session.commit()
    audit("doc_secret_marked" if body.is_secret else "doc_secret_cleared", doc_id=doc.id)
    return {"ok": True, "is_secret": bool(doc.is_secret)}


@router.get("/{doc_id}/content")
def doc_content(doc_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """正文预览（按切片序拼接，工作台编辑区用）。"""
    from ..models import Chunk

    doc = session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    rows = (
        session.query(Chunk)
        .filter(Chunk.document_id == doc_id)
        .order_by(Chunk.chunk_index)
        .all()
    )
    return {"filename": doc.filename, "text": "\n".join(r.content for r in rows)[:60000]}


@router.post("/upload")
async def upload(
    files: list[UploadFile] = File(...),
    kind: str = Form("tender"),
    project_id: str | None = Form(None),
    is_secret: bool = Form(True),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if kind not in ("tender", "bid"):
        raise HTTPException(400, "kind 须为 tender/bid")
    settings = get_settings()
    created: list[dict[str, Any]] = []
    for f in files:
        data = await f.read()
        if len(data) > 200 * 1024 * 1024:
            raise HTTPException(413, f"{f.filename} 超过 200MB 限制")
        stored = uuid.uuid4().hex + Path(f.filename or "unnamed").suffix.lower()
        (settings.files_dir / stored).write_bytes(data)
        from ..models import Document as Doc

        doc = Doc(
            filename=(f.filename or "unnamed")[:500],
            stored_name=stored,
            source="upload",
            kind=kind,
            file_type=detect_file_type(f.filename or ""),
            size=len(data),
            project_id=project_id or None,
            is_secret=1 if is_secret else 0,
            status=0,
        )
        session.add(doc)
        session.commit()
        created.append({"id": doc.id, "filename": doc.filename, "status": doc.status})
    audit("upload", detail=f"{len(created)} 份{('招标' if kind == 'tender' else '投标')}文件")
    return {"created": created, "queued": len(created)}


def detect_file_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png"}:
        return "scan"
    if ext == ".txt":
        return "txt"
    return ext.lstrip(".")
