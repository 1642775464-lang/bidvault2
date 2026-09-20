"""ONLYOFFICE 集成路由（阶段二）。

- GET /api/documents/{doc_id}/editor-config：签名编辑器配置（前端 DocsAPI 初始化用）
- GET /api/onlyoffice/files/{doc_id}?token=：DS 拉取原始 DOCX（短期签名，不暴露本地路径）
- GET /api/onlyoffice/health：DS 可达性健康检查

callback 见下方 POST /callback/{doc_id}（阶段三）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import Document
from ..services import onlyoffice_svc
from ..services.audit import audit

router = APIRouter(prefix="/api/onlyoffice", tags=["ONLYOFFICE 集成"])

logger = logging.getLogger(__name__)


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


@router.get("/version/{doc_id}")
def version_state(doc_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """当前正式版本摘要（前端最小保存状态反馈用）。"""
    doc = _doc_or_404(doc_id, session)
    v = onlyoffice_svc.current_version(session, doc)
    session.commit()
    if v is None:
        return {"version": None}
    return {
        "version": v.version,
        "file_hash": v.file_hash,
        "source": v.source,
        "editor_user": v.editor_user,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


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
        v = onlyoffice_svc.current_version(session, doc)
        session.commit()
        # key 与正式版本绑定：下次重新打开文档时才切换到新正式版本的 key
        key = (
            f"{doc.id}-v{v.version}-{v.file_hash[:12]}"
            if v is not None
            else onlyoffice_svc.document_key(doc.id, path.read_bytes())
        )
        config = onlyoffice_svc.build_editor_config(doc.id, doc.filename, path.read_bytes(), key=key)
        return {
            "scriptUrl": f"{s.onlyoffice_public_url}/web-apps/apps/api/documents/api.js",
            "config": config,
            "mode": config["editorConfig"]["mode"],
            "version": v.version if v is not None else None,
            "enabled": True,
        }


_callback_log: dict[str, str] = {}  # doc_id -> 最近一次保存状态描述（最小状态反馈）


@router.get("/callback-state/{doc_id}")
def callback_state(doc_id: str) -> dict[str, Any]:
    """最小保存状态反馈：最近一次 callback 处理结果描述。"""
    return {"doc_id": doc_id, "state": _callback_log.get(doc_id, "")}


def _persist_callback(doc_id: str, url: str, status: int, editor_user: str | None) -> None:
    """后台执行：下载 DS 回传内容。status=6 仅工作快照；status=2 正式版本+异步重拆。"""
    from sqlalchemy.orm import sessionmaker

    from ..db import get_engine
    from ..services.ingest.pipeline import process_document

    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    s = factory()
    try:
        doc = s.get(Document, doc_id)
        if doc is None:
            return
        tmp = onlyoffice_svc.working_snapshot_path(doc_id)
        onlyoffice_svc.download_to(url, tmp)
        data = tmp.read_bytes()
        if status == 6:
            tmp.replace(onlyoffice_svc.working_snapshot_path(doc_id))
            _callback_log[doc_id] = "已保存工作快照（编辑中，未生成正式版本）"
            audit("oo_forcesave_snapshot", doc_id=doc_id, detail=f"{len(data)} 字节")
            return
        # status == 2：正式版本
        v = onlyoffice_svc.create_version_from_data(
            s, doc, data, editor_user=editor_user, source="onlyoffice"
        )
        s.expire(doc)
        _callback_log[doc_id] = f"已保存正式版本 v{v.version}"
        audit("oo_version_saved", doc_id=doc_id, detail=f"v{v.version} · sha256 {v.file_hash[:12]}")
        # 异步重拆：按版本化解析（防串写在管线内）
        try:
            process_document(s, doc, version_id=v.id)
            _callback_log[doc_id] = f"已保存并重拆 v{v.version}"
        except Exception as e:
            _callback_log[doc_id] = f"v{v.version} 已保存，重拆失败：{e}"
            logger.exception("version re-extract failed")
    finally:
        s.close()


@router.post("/callback/{doc_id}")
def callback(doc_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """ONLYOFFICE 官方回调生命周期：

    status=1 编辑中（无动作）；2 可保存（正式版本）；4 无修改关闭；
    6 force-save（工作快照，不产生正式版本、不换 key）；7 force-save 失败（审计）。
    """
    status = body.get("status")
    editor = (body.get("users") or [None])[0] if isinstance(body.get("users"), list) else None
    if status == 1:
        _callback_log[doc_id] = "编辑中"
    elif status == 4:
        _callback_log[doc_id] = "关闭且无修改"
    elif status == 7:
        _callback_log[doc_id] = "强制保存失败"
        audit("oo_forcesave_error", doc_id=doc_id)
    elif status in (2, 6):
        url = body.get("url")
        if url:
            threading.Thread(
                target=_persist_callback, args=(doc_id, url, int(status), editor), daemon=True
            ).start()
            _callback_log[doc_id] = "正在处理保存…"
    return {"error": 0}


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
