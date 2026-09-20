"""ONLYOFFICE 集成路由（阶段二）。

- GET /api/documents/{doc_id}/editor-config：签名编辑器配置（前端 DocsAPI 初始化用）
- GET /api/onlyoffice/files/{doc_id}?token=：DS 拉取原始 DOCX（短期签名，不暴露本地路径）
- GET /api/onlyoffice/health：DS 可达性健康检查

callback 路由属阶段三，本阶段未实现（阶段二范围内无保存回调）。
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import Document
from ..services import onlyoffice_svc

router = APIRouter(prefix="/api/onlyoffice", tags=["ONLYOFFICE 集成"])


@router.get("/health")
def ds_health() -> dict[str, Any]:
    """检查 Document Server 可达性（DS 官方 healthcheck 返回 true）。"""
    s = get_settings()
    if not s.onlyoffice_enabled:
        return {"enabled": False, "ok": False}
    try:
        r = httpx.get(f"{s.onlyoffice_public_url}/healthcheck", timeout=5)
        ok = r.status_code == 200 and "true" in r.text.lower()
        return {"enabled": True, "ok": ok, "status_code": r.status_code}
    except Exception:
        return {"enabled": True, "ok": False}


def _doc_or_404(doc_id: str, session: Session) -> Document:
    doc = session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    if not doc.stored_name:
        raise HTTPException(409, "该文档没有原始文件（非上传件）")
    return doc


def register_document_routes(documents_router: APIRouter) -> None:
    """editor-config 挂在 documents 前缀下（任务单定义 GET /api/documents/:id/editor-config）。"""

    @documents_router.get("/{doc_id}/editor-config")
    def editor_config(doc_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
        s = get_settings()
        if not s.onlyoffice_enabled:
            raise HTTPException(503, "ONLYOFFICE 未配置（缺少 ONLYOFFICE_DOCUMENT_SERVER_PUBLIC_URL）")
        doc = _doc_or_404(doc_id, session)
        if not doc.stored_name:
            raise HTTPException(409, "该文档没有原始文件")
        path = s.files_dir / doc.stored_name
        if not path.is_file():
            raise HTTPException(404, "原始文件已丢失，请重新上传")
        config = onlyoffice_svc.build_editor_config(doc.id, doc.filename, path.read_bytes())
        return {
            "scriptUrl": f"{s.onlyoffice_public_url}/web-apps/apps/api/documents/api.js",
            "config": config,
            "enabled": True,
        }


@router.get("/files/{doc_id}")
def fetch_file(doc_id: str, token: str, session: Session = Depends(get_session)) -> FileResponse:
    """DS 专用：短期签名 token 校验后返回原始 DOCX。"""
    if not onlyoffice_svc.verify_file_token(doc_id, token):
        raise HTTPException(403, "文件访问凭证无效或已过期")
    doc = _doc_or_404(doc_id, session)
    if not doc.stored_name:
        raise HTTPException(409, "该文档没有原始文件")
    path = get_settings().files_dir / doc.stored_name
    if not path.is_file():
        raise HTTPException(404, "原始文件已丢失")
    return FileResponse(path, filename=doc.filename, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
