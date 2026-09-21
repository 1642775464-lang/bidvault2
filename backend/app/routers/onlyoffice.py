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
from ..models import Document, OfficeDocument, OfficeDocumentVersion
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
    return doc


def _od_of_doc_or_404(doc_id: str, session: Session) -> OfficeDocument:
    """B-1c-3：source 文档 id → OfficeDocument（读取链唯一身份）。"""
    doc = _doc_or_404(doc_id, session)
    if not doc.office_document_id:
        raise HTTPException(409, "文档尚未建立 Office 档案，请先完成一次拆解或重新上传")
    od = session.get(OfficeDocument, doc.office_document_id)
    if od is None:
        raise HTTPException(409, "Office 档案丢失，请重新上传")
    return od


def _maintenance_locked() -> bool:
    """B-1c-3 维护模式：禁止 Source 编辑/保存/恢复。"""
    return get_settings().source_maintenance_mode


def _office_version_state(od: OfficeDocument, session: Session) -> dict[str, Any]:
    v = (
        session.get(OfficeDocumentVersion, od.current_version_id)
        if od.current_version_id
        else None
    )
    if v is None:
        return {"version": None}
    return {
        "version": v.version,
        "file_hash": v.file_hash,
        "source": v.source,
        "editor_user": v.editor_user,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


@router.get("/version/{doc_id}")
def version_state(doc_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """当前正式版本摘要（适配转发：doc_id → office_document_id，B-1c-3 读链切换）。"""
    od = _od_of_doc_or_404(doc_id, session)
    return _office_version_state(od, session)


def register_document_routes(documents_router: APIRouter) -> None:
    """editor-config 挂在 documents 前缀下（任务单定义 GET /api/documents/:id/editor-config）。"""

    @documents_router.get("/{doc_id}/editor-config")
    def editor_config(
        doc_id: str,
        version_id: str | None = None,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """适配转发（B-1c-3 读链切换）：doc_id → office_document_id，全部数据来自通用层。

        version_id 给定时：只读查看该历史版本（强制 view）。
        维护模式：mode 强制 view（禁止 Source 编辑产生新写入）。
        key = {office_document_id}-v{version}-{hash12}。
        """
        s = get_settings()
        if not s.onlyoffice_enabled:
            raise HTTPException(503, "ONLYOFFICE 未配置（缺少 ONLYOFFICE_DOCUMENT_SERVER_PUBLIC_URL）")
        od = _od_of_doc_or_404(doc_id, session)
        v = (
            session.get(OfficeDocumentVersion, od.current_version_id)
            if (version_id is None and od.current_version_id)
            else None
        )
        viewing = None
        if version_id:
            viewing = session.get(OfficeDocumentVersion, version_id)
            if viewing is None or viewing.office_document_id != od.id:
                raise HTTPException(404, "历史版本不存在")
            v = viewing
        if v is None:
            raise HTTPException(409, "该文档尚无正式版本")
        path = s.files_dir / v.file_path
        if not path.is_file():
            raise HTTPException(404, "版本文件已丢失")
        mode = "view" if (version_id or _maintenance_locked()) else None
        token = OfficeDocumentService.generate_file_token(od.id, vid=v.id)
        key = f"{od.id}-v{v.version}-{v.file_hash[:12]}"
        config = onlyoffice_svc.build_editor_config(od.id, od.title, path.read_bytes(), mode=mode, key=key)
        config["document"]["url"] = (
            f"{s.onlyoffice_backend_internal_url}/api/office-documents/{od.id}/files?token={token}"
        )
        config["editorConfig"]["callbackUrl"] = (
            f"{s.onlyoffice_backend_internal_url}/api/office-documents/{od.id}/callback"
        )
        config["token"] = jwt_encode(config)
        return {
            "scriptUrl": f"{s.onlyoffice_public_url}/web-apps/apps/api/documents/api.js",
            "config": config,
            "mode": config["editorConfig"]["mode"],
            "version": v.version,
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
    """历史版本列表（B-1c-3 读链切换：office_document_versions，新→旧，带 is_current）。"""
    od = _od_of_doc_or_404(doc_id, session)
    rows = (
        session.query(OfficeDocumentVersion)
        .filter(OfficeDocumentVersion.office_document_id == od.id)
        .order_by(OfficeDocumentVersion.version.desc())
        .limit(limit)
        .all()
    )
    return {"items": [
        {
            "id": v.id, "version": v.version, "file_hash": v.file_hash,
            "source": v.source, "editor_user": v.editor_user,
            "restored_from_version_id": v.restored_from_version_id,
            "comment": v.comment,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "is_current": v.id == od.current_version_id,
            "size": v.file_size,
        }
        for v in rows
    ]}


@router.get("/versions/{doc_id}/{version_id}/file")
def version_file(doc_id: str, version_id: str, session: Session = Depends(get_session)) -> FileResponse:
    """下载指定历史版本的原文件（B-1c-3：office_document_versions；历史文件不可变，只读）。"""
    od = _od_of_doc_or_404(doc_id, session)
    v = session.get(OfficeDocumentVersion, version_id)
    if v is None or v.office_document_id != od.id:
        raise HTTPException(404, "历史版本不存在")
    path = get_settings().files_dir / v.file_path
    if not path.is_file():
        raise HTTPException(404, "历史版本文件已丢失")
    audit("version_file_downloaded", doc_id=od.id, detail=f"v{v.version}")
    return FileResponse(path, filename=f"v{v.version}_{od.title or od.id}", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@router.post("/versions/{doc_id}/{version_id}/restore")
def restore_version(doc_id: str, version_id: str, body: RestoreIn | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    """恢复历史版本（B-1c-4 切写链）：通用 Service 建新版本（source=restore）+ dispatch。"""
    from ..services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )

    od = _od_of_doc_or_404(doc_id, session)
    target = session.get(OfficeDocumentVersion, version_id)
    if target is None or target.office_document_id != od.id:
        raise HTTPException(404, "历史版本不存在")
    comment = body.comment if body else None
    editor = (body.editor_user if body else None) or "本地用户"
    svc = OfficeDocumentService(session=session, adapter=OfficeDocumentTableAdapter(session))
    try:
        v = svc.restore_version(od, target, editor_user=editor, comment=comment)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    session.commit()
    audit("version_restored", doc_id=od.id, detail=f"v{v.version} ← 恢复自 v{target.version}")
    _dispatch_async(od.id, v.id)
    return {"ok": True, "version": v.version, "id": v.id, "restored_from": target.version}




@router.post("/callback/{doc_id}")
def callback(doc_id: str, body: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
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
        # B-1c-4：source 写链正式切换——与 Material 共用通用持久化
        # （OfficeDocumentService 建版本 → dispatch SourceDocumentProcessor），禁止旧表双写
        url = body.get("url")
        if url:
            od = _od_of_doc_or_404(doc_id, session)
            threading.Thread(
                target=_office_persist, args=(od.id, url, int(status), editor), daemon=True
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
    # material 走通用 callback（status=2/6 全语义）；workspace 同；mode 按类型：
    # material=edit（B-2 编辑闭环），workspace 暂 view（C 阶段）
    mode = "edit" if od.document_type == "material" else "view"
    config = onlyoffice_svc.build_editor_config(od.id, od.title or od.id, data, mode=mode, key=key)
    config["document"]["url"] = (
        f"{s.onlyoffice_backend_internal_url}/api/office-documents/{od.id}/files?token={token}"
    )
    config["editorConfig"]["callbackUrl"] = (
        f"{s.onlyoffice_backend_internal_url}/api/office-documents/{od.id}/callback"
    )
    config["token"] = svc_jwt(config)
    return {
        "scriptUrl": f"{s.onlyoffice_public_url}/web-apps/apps/api/documents/api.js",
        "config": config,
        "mode": mode,
        "version": v.version,
        "enabled": True,
    }


def svc_jwt(payload: dict[str, Any]) -> str:
    import jwt

    return jwt.encode(payload, get_settings().onlyoffice_jwt_secret, algorithm="HS256")


_office_cb_log: dict[str, str] = {}


def _dispatch_async(od_id: str, version_id: str) -> None:
    """后台执行 post-save processor（source 全量解析耗时长，避免 HTTP 超时）。"""

    def _run() -> None:
        from sqlalchemy.orm import sessionmaker

        from ..db import get_engine
        from ..services.office_document_svc import dispatch_post_save

        factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
        ses = factory()
        try:
            od = ses.get(OfficeDocument, od_id)
            v = ses.get(OfficeDocumentVersion, version_id)
            if od is not None and v is not None:
                result = dispatch_post_save(od, v)
                _office_cb_log[od_id] = f"v{v.version} processor={result}"
        except Exception:
            logger.exception("async dispatch failed: od=%s v=%s", od_id, version_id)
        finally:
            ses.close()

    threading.Thread(target=_run, daemon=True).start()


@office_router.get("/reconcile-summary")
def reconcile_summary(session: Session = Depends(get_session)) -> dict[str, Any]:
    """B-1c-2 只读双读对账摘要：legacy vs office 逐字段 explicit compare。"""
    from ..services.version_stores import reconcile_source_documents

    return reconcile_source_documents(session)


@office_router.get("/{oid}/callback-state")
def office_callback_state(oid: str) -> dict[str, Any]:
    return {"doc_id": oid, "state": _office_cb_log.get(oid, "")}


def _office_persist(oid: str, url: str, status: int, editor_user: str | None) -> None:
    """通用保存持久化：status=6 仅工作快照；status=2 正式版本 + post-save 分发。

    素材/工作台的保存语义与 source 完全一致（阶段三），全部经由
    OfficeDocumentService + dispatch_post_save——不复制三份 callback。
    """
    from sqlalchemy.orm import sessionmaker

    from ..db import get_engine
    from ..services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
        dispatch_post_save,
    )

    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    ses = factory()
    try:
        od = ses.get(OfficeDocument, oid)
        if od is None:
            return
        adapter = OfficeDocumentTableAdapter(ses)
        svc = OfficeDocumentService(session=ses, adapter=adapter)
        tmp = onlyoffice_svc.working_snapshot_path(oid)
        onlyoffice_svc.download_to(url, tmp)
        data = tmp.read_bytes()
        if status == 6:
            svc.create_working_snapshot(od, data)
            _office_cb_log[oid] = "已保存工作快照（编辑中，未生成正式版本）"
            audit("oo_forcesave_snapshot", detail=f"office_document {oid} · {len(data)} 字节")
            return
        v = svc.create_version(od, data, source="onlyoffice", editor_user=editor_user)
        ses.expire(od)
        _office_cb_log[oid] = f"已保存正式版本 v{v.version}"
        audit("oo_office_version_saved", detail=f"office_document {oid} v{v.version} · sha256 {v.file_hash[:12]}")
        result = dispatch_post_save(od, v)
        _office_cb_log[oid] = f"已保存正式版本 v{v.version} · processor={result}"
    except Exception as e:
        _office_cb_log[oid] = f"保存处理失败：{e}"
        logger.exception("office callback persist failed: %s", oid)
    finally:
        ses.close()


@office_router.post("/{oid}/callback")
def office_callback(oid: str, body: dict[str, Any]) -> dict[str, Any]:
    """通用 ONLYOFFICE 回调（material/workspace；source 走旧端点至 B-1c）。"""
    status = body.get("status")
    editor = (body.get("users") or [None])[0] if isinstance(body.get("users"), list) else None
    if status == 1:
        _office_cb_log[oid] = "编辑中"
    elif status == 4:
        _office_cb_log[oid] = "关闭且无修改"
    elif status == 7:
        _office_cb_log[oid] = "强制保存失败"
        audit("oo_forcesave_error", detail=f"office_document {oid}")
    elif status in (2, 6):
        url = body.get("url")
        if url:
            threading.Thread(
                target=_office_persist, args=(oid, url, int(status), editor), daemon=True
            ).start()
            _office_cb_log[oid] = "正在处理保存…"
    return {"error": 0}


@office_router.get("/{oid}/versions")
def office_versions_list(oid: str, limit: int = 50, session: Session = Depends(get_session)) -> dict[str, Any]:
    """通用版本列表（material/workspace；source 走旧端点至 B-1c）。"""
    od = session.get(OfficeDocument, oid)
    if od is None:
        raise HTTPException(404, "OfficeDocument 不存在")
    rows = (
        session.query(OfficeDocumentVersion)
        .filter(OfficeDocumentVersion.office_document_id == oid)
        .order_by(OfficeDocumentVersion.version.desc())
        .limit(limit)
        .all()
    )
    return {"items": [
        {
            "id": v.id, "version": v.version, "file_hash": v.file_hash,
            "source": v.source, "editor_user": v.editor_user,
            "restored_from_version_id": v.restored_from_version_id,
            "comment": v.comment,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "is_current": v.id == od.current_version_id,
            "size": v.file_size,
        }
        for v in rows
    ]}


@office_router.get("/{oid}/versions/{vid}/file")
def office_version_file(oid: str, vid: str, session: Session = Depends(get_session)) -> FileResponse:
    """下载指定历史版本文件（不可变，只读）。"""
    od = session.get(OfficeDocument, oid)
    if od is None:
        raise HTTPException(404, "OfficeDocument 不存在")
    v = session.get(OfficeDocumentVersion, vid)
    if v is None or v.office_document_id != oid:
        raise HTTPException(404, "历史版本不存在")
    path = get_settings().files_dir / v.file_path
    if not path.is_file():
        raise HTTPException(404, "历史版本文件已丢失")
    audit("office_version_downloaded", detail=f"office_document {oid} v{v.version}")
    return FileResponse(path, filename=f"v{v.version}_{od.title or oid}.docx", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


class OfficeRestoreIn(BaseModel):
    comment: str | None = None
    editor_user: str | None = None


@office_router.post("/{oid}/versions/{vid}/restore")
def office_restore_version(oid: str, vid: str, body: OfficeRestoreIn | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    """恢复历史版本=创建新版本（source=restore）+ post-save 分发（派生数据同步回滚）。"""
    from ..services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )

    od = session.get(OfficeDocument, oid)
    if od is None:
        raise HTTPException(404, "OfficeDocument 不存在")
    target = session.get(OfficeDocumentVersion, vid)
    if target is None or target.office_document_id != oid:
        raise HTTPException(404, "历史版本不存在")
    svc = OfficeDocumentService(session=session, adapter=OfficeDocumentTableAdapter(session))
    editor = (body.editor_user if body else None) or "本地用户"
    try:
        v = svc.restore_version(od, target, editor_user=editor, comment=(body.comment if body else None))
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    session.commit()
    audit("office_version_restored", detail=f"office_document {oid} v{v.version} ← v{target.version}")
    _dispatch_async(od.id, v.id)
    return {"ok": True, "version": v.version, "id": v.id, "restored_from": target.version}


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
    if not doc.office_document_id:
        raise HTTPException(409, "文档尚未建立 Office 档案")
    od = session.get(OfficeDocument, doc.office_document_id)
    if od is None:
        raise HTTPException(409, "Office 档案丢失")
    if vid:
        v = session.get(OfficeDocumentVersion, vid)
        if v is None or v.office_document_id != od.id:
            raise HTTPException(404, "历史版本不存在")
    elif od.current_version_id:
        v = session.get(OfficeDocumentVersion, od.current_version_id)
    else:
        v = None
    if v is None:
        raise HTTPException(404, "版本不存在")
    path = get_settings().files_dir / v.file_path
    if not path.is_file():
        raise HTTPException(404, "原始文件已丢失")
    return FileResponse(path, filename=doc.filename, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
