"""P7 预览弹层支撑：PDF 按页 PNG 渲染（缓存）+ 页数 + 证据图片文件 + AI 识别。"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session, object_session

from ..config import get_settings
from ..db import get_session
from ..models import Document, Material
from ..services.images import img_file_path, safe_img_rel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["F2.4 预览/图片证据"])


def _get_doc(session: Session, doc_id: str) -> Document:
    doc = session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    return doc


@router.get("/documents/{doc_id}/page_count")
def page_count(doc_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    doc = _get_doc(session, doc_id)
    if doc.file_type != "pdf":
        return {"pages": 1, "previewable": False}
    import fitz

    data = _doc_bytes(doc)
    with fitz.open(stream=data, filetype="pdf") as pdf:
        return {"pages": len(pdf), "previewable": True}


def _doc_bytes(doc: Document) -> bytes:
    """B-1c-4：页图渲染基于 OfficeDocument.current_version（不再读 deprecated stored_name）。"""
    from ..config import get_settings
    from ..models import OfficeDocument, OfficeDocumentVersion

    if doc.office_document_id:
        session = object_session(doc)
        if session is None:
            session = None
        od = session.get(OfficeDocument, doc.office_document_id) if session is not None else None
        if od is not None and od.current_version_id and session is not None:
            v = session.get(OfficeDocumentVersion, od.current_version_id)
            if v is not None:
                path = get_settings().files_dir / v.file_path
                if path.is_file():
                    return path.read_bytes()
    from ..services.ingest.parser import read_source

    return read_source(doc.source, doc.origin_path, doc.stored_name)  # deprecated 兜底


@router.get("/documents/{doc_id}/pages")
def render_page(doc_id: str, page_no: int = 1, session: Session = Depends(get_session)) -> Response:
    """按页渲染 PNG（缓存到 cache/pages/{doc_id}/）。判据：缓存命中 ≤3 秒。"""
    doc = _get_doc(session, doc_id)
    if doc.file_type != "pdf":
        raise HTTPException(400, "仅 PDF 支持按页预览")
    if page_no < 1:
        raise HTTPException(400, "页码无效")

    settings = get_settings()
    cache = settings.cache_pages_dir / doc_id / f"p{page_no}.png"
    if cache.is_file():
        return Response(content=cache.read_bytes(), media_type="image/png")

    import fitz

    data = _doc_bytes(doc)
    with fitz.open(stream=data, filetype="pdf") as pdf:
        if page_no > len(pdf):
            raise HTTPException(404, f"超出页数（共 {len(pdf)} 页）")
        pix = pdf[page_no - 1].get_pixmap(dpi=140)
        png = pix.tobytes("png")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(png)
    return Response(content=png, media_type="image/png")


@router.get("/files/img/{doc_id}/{name}")
def image_file(doc_id: str, name: str) -> FileResponse:
    """证据图片静态访问（防穿越）。"""
    rel = safe_img_rel(doc_id, name)
    if rel is None:
        raise HTTPException(404, "路径无效")
    path = img_file_path(rel)
    if path is None:
        raise HTTPException(404, "图片不存在")
    media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(path, media_type=media)


@router.post("/documents/{doc_id}/analyze_images")
def analyze_images(doc_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """对文档的图片素材批量调 GLM-4V-Flash 识别字段并回写。"""
    doc = _get_doc(session, doc_id)
    from ..services.glm_vision import analyze_image

    mats = (
        session.query(Material)
        .filter(Material.doc_name == doc.filename, Material.type == "图片")
        .all()
    )
    done = failed = 0
    for m in mats:
        rel = m.file_name or ""
        path = img_file_path(rel)
        if path is None:
            failed += 1
            continue
        try:
            result = analyze_image(path.read_bytes(), path.suffix.lstrip("."))
            rows = [[k, str(v)] for k, v in (result.get("fields") or {}).items()]
            if rows:
                m.rows_json = json.dumps(rows, ensure_ascii=False)
            m.text = str(result.get("summary") or "") or m.text
            m.tags_json = json.dumps(["图片证据", str(result.get("type") or "未识别")], ensure_ascii=False)
            done += 1
        except Exception:
            logger.warning("analyze image failed: %s", rel, exc_info=True)
            failed += 1
    session.commit()
    return {"analyzed": done, "failed": failed, "total": len(mats)}
