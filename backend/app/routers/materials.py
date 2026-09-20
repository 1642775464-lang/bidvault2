"""P4 素材库路由：列表/CRUD/引用/删除。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import MATERIAL_CATS, Material, MaterialRef, Project
from ..services.audit import audit
from ..services.images import guess_cat

router = APIRouter(prefix="/api/materials", tags=["F2.6 素材库"])


def _m_dict(m: Material, session: Session) -> dict[str, Any]:
    proj = session.get(Project, m.project_id) if m.project_id else None
    return {
        "id": m.id, "title": m.title, "type": m.type, "cat": m.cat, "src": m.src,
        "doc_name": m.doc_name, "project_id": m.project_id,
        "project_name": proj.name if proj else None,
        "tags": json.loads(m.tags_json) if m.tags_json else [],
        "rows": json.loads(m.rows_json) if m.rows_json else None,
        "text": m.text, "file_name": m.file_name, "file_size": m.file_size, "source_page": m.source_page,
        "used": m.used,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


class MaterialIn(BaseModel):
    title: str
    type: str = "表格"
    cat: str = "资质文件"
    tags: list[str] = []
    rows: list[list[str]] | None = None
    text: str | None = None
    file_name: str | None = None
    file_size: str | None = None


@router.get("")
def list_materials(
    cat: str | None = None,
    q: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = session.query(Material)
    if cat and cat in MATERIAL_CATS:
        query = query.filter(Material.cat == cat)
    if q:
        like = f"%{q}%"
        query = query.filter(Material.title.like(like) | Material.tags_json.like(like))
    rows = query.order_by(Material.updated_at.desc()).all()
    return {"total": len(rows), "items": [_m_dict(m, session) for m in rows]}


@router.get("/meta")
def meta() -> dict[str, Any]:
    return {"cats": list(MATERIAL_CATS), "types": ["表格", "文本", "图片", "附件"]}


@router.post("/upload")
async def upload_material(
    file: UploadFile = File(...),
    project_id: str = Form(""),
    cat: str = Form(""),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """素材真实上传：图片走 OCR 识别文本，其余类型作为附件存档。"""
    filename = file.filename or "unnamed"
    suffix = Path(filename).suffix.lower()
    allowed = {".jpg", ".jpeg", ".png", ".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt", ".zip"}
    if suffix not in allowed:
        raise HTTPException(415, f"不支持的素材类型：{filename}")
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, f"{filename} 超过 50MB 限制")
    settings = get_settings()
    stored = uuid.uuid4().hex + suffix
    (settings.files_dir / stored).write_bytes(data)

    mtype = "附件"
    text: str | None = None
    context = filename
    if suffix in (".jpg", ".jpeg", ".png"):
        mtype = "图片"
        try:
            from ..services.ingest.parser import OCREngine

            parsed = OCREngine.get().ocr_image(data)
            text = parsed.text
            context = f"{filename} {text}"
        except Exception:
            text = ""
    mat = Material(
        title=Path(filename).stem[:300],
        type=mtype,
        cat=cat if cat in MATERIAL_CATS else guess_cat(context),
        src="手动上传",
        project_id=project_id or None,
        text=text,
        file_name=stored,
        file_size=f"{len(data) / 1024:.0f} KB",
    )
    session.add(mat)
    session.commit()
    audit("material_uploaded", detail=f"{filename} · {mtype}")
    return {"ok": True, "id": mat.id, "type": mtype, "cat": mat.cat}


@router.post("")
def create_material(body: MaterialIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    if not body.title.strip():
        raise HTTPException(400, "标题不能为空")
    if body.type not in ("表格", "文本", "图片", "附件"):
        raise HTTPException(400, "type 无效")
    m = Material(
        title=body.title.strip(), type=body.type, cat=body.cat if body.cat in MATERIAL_CATS else "资质文件",
        src="手动上传", tags_json=json.dumps(body.tags, ensure_ascii=False),
        rows_json=json.dumps(body.rows, ensure_ascii=False) if body.rows else None,
        text=body.text, file_name=body.file_name, file_size=body.file_size,
    )
    session.add(m)
    session.commit()
    audit("material_created", detail=m.title)
    return {"id": m.id}


@router.put("/{material_id}")
def update_material(material_id: str, body: MaterialIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    m = session.get(Material, material_id)
    if m is None:
        raise HTTPException(404, "素材不存在")
    m.title = body.title.strip()
    m.type = body.type
    m.cat = body.cat
    m.tags_json = json.dumps(body.tags, ensure_ascii=False)
    m.rows_json = json.dumps(body.rows, ensure_ascii=False) if body.rows else None
    m.text = body.text
    m.file_name = body.file_name
    m.file_size = body.file_size
    session.commit()
    audit("material_updated", detail=m.title)
    return {"ok": True}


@router.delete("/{material_id}")
def delete_material(material_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    m = session.get(Material, material_id)
    if m is None:
        raise HTTPException(404, "素材不存在")
    name = m.title
    refs = session.query(MaterialRef).filter(MaterialRef.material_id == m.id).count()
    session.query(MaterialRef).filter(MaterialRef.material_id == m.id).delete()
    session.delete(m)
    session.commit()
    audit("material_deleted", detail=f"{name} · 引用 {refs}")
    return {"ok": True, "had_refs": refs}


class RefIn(BaseModel):
    project_id: str


@router.post("/{material_id}/use")
def use_material(material_id: str, body: RefIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    m = session.get(Material, material_id)
    if m is None:
        raise HTTPException(404, "素材不存在")
    p = session.get(Project, body.project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    existing = (
        session.query(MaterialRef)
        .filter(MaterialRef.material_id == m.id, MaterialRef.project_id == body.project_id)
        .one_or_none()
    )
    if existing is None:
        session.add(MaterialRef(material_id=m.id, project_id=body.project_id))
        m.used += 1
    m.project_id = body.project_id
    session.commit()
    audit("material_used", detail=f"{m.title} → {p.name}")
    return {"ok": True, "used": m.used}
