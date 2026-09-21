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
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import Document, DocumentVersion, OfficeDocument, OfficeDocumentVersion
from ..services import onlyoffice_svc
from ..services.audit import audit
from ..services.office_document_svc import OfficeDocumentService

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
    def editor_config(
        doc_id: str,
        version_id: str | None = None,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """version_id 给定时：只读查看该历史版本（历史文件不可变，强制 view）。"""
        s = get_settings()
        if not s.onlyoffice_enabled:
            raise HTTPException(503, "ONLYOFFICE 未配置（缺少 ONLYOFFICE_DOCUMENT_SERVER_PUBLIC_URL）")
        doc = _doc_or_404(doc_id, session)
        if not doc.stored_name:
            raise HTTPException(409, "该文档没有原始文件")
        viewing = None
        mode = None
        token = onlyoffice_svc.sign_file_token(doc.id)
        if version_id:
            viewing = session.get(DocumentVersion, version_id)
            if viewing is None or viewing.document_id != doc.id:
                raise HTTPException(404, "历史版本不存在")
            path = s.files_dir / viewing.file_path
            if not path.is_file():
                raise HTTPException(404, "历史版本文件已丢失")
            mode = "view"
            token = onlyoffice_svc.sign_file_token(doc.id, vid=viewing.id)
        else:
            path = s.files_dir / doc.stored_name
            if not path.is_file():
                raise HTTPException(404, "原始文件已丢失，请重新上传")
        v = onlyoffice_svc.current_version(session, doc) if viewing is None else viewing
        session.commit()
        # key 与正式版本绑定：下次重新打开文档时才切换到新正式版本的 key
        key = (
            f"{doc.id}-v{v.version}-{v.file_hash[:12]}"
            if v is not None
            else onlyoffice_svc.document_key(doc.id, path.read_bytes())
        )
        config = onlyoffice_svc.build_editor_config(doc.id, doc.filename, path.read_bytes(), mode=mode, key=key)
        config["document"]["url"] = (
            f"{s.onlyoffice_backend_internal_url}/api/onlyoffice/files/{doc.id}?token={token}"
        )
        config["token"] = jwt_encode(config)
        return {
            "scriptUrl": f"{s.onlyoffice_public_url}/web-apps/apps/api/documents/api.js",
            "config": config,
            "mode": config["editorConfig"]["mode"],
            "version": v.version if v is not None else None,
            "viewing_version_id": viewing.id if viewing else None,
            "enabled": True,
        }


def jwt_encode(payload: dict[str, Any]) -> str:
    import jwt

    return jwt.encode(payload, get_settings().onlyoffice_jwt_secret, algorithm="HS256")


_callback_log: dict[str, str] = {}  # doc_id -> 最近一次保存状态描述（最小状态反馈）


@router.get("/callback-state/{doc_id}")
def callback_state(doc_id: str) -> dict[str, Any]:
    """最小保存状态反馈：最近一次 callback 处理结果描述。"""
    return {"doc_id": doc_id, "state": _callback_log.get(doc_id, "")}


class RestoreIn(BaseModel):
    comment: str | None = None
    editor_user: str | None = None


@router.get("/versions/{doc_id}")
def versions_list(doc_id: str, limit: int = 50, session: Session = Depends(get_session)) -> dict[str, Any]:
    """历史版本列表（新→旧，带 is_current 标记）。"""
    doc = _doc_or_404(doc_id, session)
    return {"items": onlyoffice_svc.list_versions(session, doc, limit=limit)}


@router.get("/versions/{doc_id}/{version_id}/file")
def version_file(doc_id: str, version_id: str, session: Session = Depends(get_session)) -> FileResponse:
    """下载指定历史版本的原文件（历史文件不可变，只读）。"""
    doc = _doc_or_404(doc_id, session)
    v = session.get(DocumentVersion, version_id)
    if v is None or v.document_id != doc.id:
        raise HTTPException(404, "历史版本不存在")
    path = get_settings().files_dir / v.file_path
    if not path.is_file():
        raise HTTPException(404, "历史版本文件已丢失")
    audit("version_file_downloaded", doc_id=doc.id, detail=f"v{v.version}")
    return FileResponse(path, filename=f"v{v.version}_{doc.filename}", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@router.post("/versions/{doc_id}/{version_id}/restore")
def restore_version(doc_id: str, version_id: str, body: RestoreIn | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    """恢复历史版本：以创建新版本方式落地（source=restore），并自动按新版本重拆。"""
    doc = _doc_or_404(doc_id, session)
    target = session.get(DocumentVersion, version_id)
    if target is None or target.document_id != doc.id:
        raise HTTPException(404, "历史版本不存在")
    comment = body.comment if body else None
    editor = (body.editor_user if body else None) or "本地用户"
    try:
        v = onlyoffice_svc.restore_version(session, doc, target, editor_user=editor, comment=comment)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    audit("version_restored", doc_id=doc.id, detail=f"v{v.version} ← 恢复自 v{target.version}")

    def _reparse() -> None:
        from sqlalchemy.orm import sessionmaker

        from ..db import get_engine
        from ..services.ingest.pipeline import process_document

        factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
        ses = factory()
        try:
            d = ses.get(Document, doc.id)
            if d is not None:
                process_document(ses, d, version_id=v.id)
        finally:
            ses.close()

    threading.Thread(target=_reparse, daemon=True).start()
    return {"ok": True, "version": v.version, "id": v.id, "restored_from": target.version}


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


# ---------------- B-1b：通用 OfficeDocument 只读入口（material 用；编辑 callback 属 B-2） ----------------

office_router = APIRouter(prefix="/api/office-documents", tags=["ONLYOFFICE 集成"])


@office_router.get("/{oid}/editor-config")
def office_document_editor_config(oid: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """通用 editor-config：按 OfficeDocument 组装。

    B-1b 阶段 material 一律 mode=view（无 callback，保存链路 B-2 接入）。
    """
    s = get_settings()
    if not s.onlyoffice_enabled:
        raise HTTPException(503, "ONLYOFFICE 未配置")
    od = session.get(OfficeDocument, oid)
    if od is None:
        raise HTTPException(404, "OfficeDocument 不存在")
    v = (
        session.get(OfficeDocumentVersion, od.current_version_id)
        if od.current_version_id
        else None
    )
    if v is None:
        raise HTTPException(409, "该 Office 文件尚无版本")
    path = s.files_dir / v.file_path
    if not path.is_file():
        raise HTTPException(404, "版本文件已丢失")
    data = path.read_bytes()
    key = f"{od.id}-v{v.version}-{v.file_hash[:12]}"
    token = OfficeDocumentService.generate_file_token(od.id, vid=v.id)
    config = onlyoffice_svc.build_editor_config(od.id, od.title or od.id, data, mode="view", key=key)
    config["document"]["url"] = (
        f"{s.onlyoffice_backend_internal_url}/api/office-documents/{od.id}/files?token={token}"
    )
    config["token"] = svc_jwt(config)
    return {
        "scriptUrl": f"{s.onlyoffice_public_url}/web-apps/apps/api/documents/api.js",
        "config": config,
        "mode": "view",
        "version": v.version,
        "enabled": True,
    }


def svc_jwt(payload: dict[str, Any]) -> str:
    import jwt

    return jwt.encode(payload, get_settings().onlyoffice_jwt_secret, algorithm="HS256")


@office_router.get("/{oid}/files")
def office_document_file(oid: str, token: str, session: Session = Depends(get_session)) -> FileResponse:
    """通用文件读取：token（含 vid）校验后返回对应版本文件。"""
    ok, vid = onlyoffice_svc.verify_file_token(oid, token)
    if not ok:
        raise HTTPException(403, "文件访问凭证无效或已过期")
    od = session.get(OfficeDocument, oid)
    if od is None:
        raise HTTPException(404, "OfficeDocument 不存在")
    v = session.get(OfficeDocumentVersion, vid) if vid else (
        session.get(OfficeDocumentVersion, od.current_version_id) if od.current_version_id else None
    )
    if v is None:
        raise HTTPException(404, "版本不存在")
    path = get_settings().files_dir / v.file_path
    if not path.is_file():
        raise HTTPException(404, "版本文件已丢失")
    return FileResponse(path, filename=od.title or od.id, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@router.get("/files/{doc_id}")
def fetch_file(doc_id: str, token: str, session: Session = Depends(get_session)) -> FileResponse:
    """DS 专用：短期签名 token 校验后返回原始 DOCX（token 带 vid 时返回对应历史版本文件）。"""
    ok, vid = onlyoffice_svc.verify_file_token(doc_id, token)
    if not ok:
        raise HTTPException(403, "文件访问凭证无效或已过期")
    doc = _doc_or_404(doc_id, session)
    if vid:
        v = session.get(DocumentVersion, vid)
        if v is None or v.document_id != doc.id:
            raise HTTPException(404, "历史版本不存在")
        path = get_settings().files_dir / v.file_path
    else:
        if not doc.stored_name:
            raise HTTPException(409, "该文档没有原始文件")
        path = get_settings().files_dir / doc.stored_name
    if not path.is_file():
        raise HTTPException(404, "原始文件已丢失")
    return FileResponse(path, filename=doc.filename, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
