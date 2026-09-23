"""ONLYOFFICE 集成路由（阶段二）。

- GET /api/documents/{doc_id}/editor-config：签名编辑器配置（前端 DocsAPI 初始化用）
- GET /api/onlyoffice/files/{doc_id}?token=：DS 拉取原始 DOCX（短期签名，不暴露本地路径）
- GET /api/onlyoffice/health：DS 可达性健康检查

callback 见下方 POST /callback/{doc_id}（阶段三）。
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from ..services.ai_proposal import AiProposalService

from ..config import get_settings
from ..db import get_session
from ..models import AiProposal, Document, OfficeDocument, OfficeDocumentVersion
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
    mode = "edit" if od.document_type in ("material", "workspace") else "view"
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


@office_router.post("/{oid}/material-insert")
def office_material_insert(oid: str, body: MaterialInsertIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    """C-3b：Material → Workspace 插入（OOXML 包级 mutation）。

    插入=创建新 Workspace 正式版本（source=material_insert），非编辑器操作。
    结构不支持 → 422 UNSUPPORTED_MATERIAL_STRUCTURE（features 列表）；并发 → 409；活跃编辑 → 423。
    """
    from ..services.ooxml_insert import OOXMLInsertEngine
    from ..services.workspace_mutation import (
        MaterialInsertRequest,
        UnsupportedMaterialStructureError,
        WorkspaceMutationError,
        WorkspaceMutationService,
    )
    from ..services.xml_safety import UnsafeXmlError

    od = session.get(OfficeDocument, oid)
    if od is None:
        raise HTTPException(404, "OfficeDocument 不存在")

    def _probe(doc_id: str) -> bool:
        state = _office_cb_log.get(doc_id, "")
        return any(k in state for k in _ACTIVE_EDIT_STATES)

    svc = WorkspaceMutationService(
        session=session,
        active_edit_probe=_probe,
        engine=OOXMLInsertEngine(),
        dispatch_fn=lambda od_id, version_id: _dispatch_async(od_id, version_id),
    )
    req = MaterialInsertRequest(
        workspace_office_document_id=oid,
        base_workspace_version_id=body.base_workspace_version_id,
        material_id=body.material_id,
        material_office_document_id=body.material_office_document_id,
        material_version_id=body.material_version_id,
        insert_mode=body.insert_mode,
        target_locator=body.target_locator,
    )
    try:
        return svc.insert_material(req)
    except UnsupportedMaterialStructureError as e:
        raise HTTPException(e.http_status, {"code": e.code, "features": e.features}) from e
    except UnsafeXmlError as e:
        raise HTTPException(422, {"code": "UNSAFE_XML", "features": [str(e)]}) from e
    except WorkspaceMutationError as e:
        raise HTTPException(e.http_status, e.code) from e


# ---------------- C-4b-1：AI 提案（确定性写入；真实云 AI 属 C-4b-2） ----------------


class AiProposalIn(BaseModel):
    base_workspace_version_id: str
    title: str = ""
    text: str
    operation: str = "append_end"
    model: str | None = None
    context: Any = None


def _ai_service(session: Session) -> AiProposalService:
    from ..services.ai_proposal import AiProposalService
    from ..services.ooxml_insert import AiTextMutator

    def _probe(doc_id: str) -> bool:
        state = _office_cb_log.get(doc_id, "")
        return any(k in state for k in _ACTIVE_EDIT_STATES)

    return AiProposalService(
        session=session,
        active_edit_probe=_probe,
        mutator=AiTextMutator(),
        dispatch_fn=lambda od_id, version_id: _dispatch_async(od_id, version_id),
    )


@office_router.post("/{oid}/ai-proposals")
def create_ai_proposal(oid: str, body: AiProposalIn,
                       session: Session = Depends(get_session)) -> dict[str, Any]:
    """创建 AI 修改提案（draft）。提案内容未经确认不进入任何正文落点。"""
    from ..services.ai_proposal import AiProposalRequest
    from ..services.workspace_mutation import WorkspaceMutationError

    if session.get(OfficeDocument, oid) is None:
        raise HTTPException(404, "OfficeDocument 不存在")
    svc = _ai_service(session)
    try:
        return svc.create_proposal(AiProposalRequest(
            workspace_office_document_id=oid,
            base_workspace_version_id=body.base_workspace_version_id,
            title=body.title,
            text=body.text,
            operation=body.operation,
            model=body.model,
            context=body.context,
        ))
    except WorkspaceMutationError as e:
        raise HTTPException(e.http_status, e.code) from e


@office_router.get("/{oid}/ai-proposals")
def list_ai_proposals(oid: str, limit: int = 50,
                      session: Session = Depends(get_session)) -> dict[str, Any]:
    rows = (
        session.query(AiProposal)
        .filter(AiProposal.workspace_office_document_id == oid)
        .order_by(AiProposal.created_at.desc())
        .limit(limit)
        .all()
    )
    return {"items": [
        {"id": p.id, "title": p.title, "operation": p.operation, "status": p.status,
         "base_workspace_version_id": p.base_workspace_version_id,
         "applied_version_id": p.applied_version_id, "model": p.model,
         "error": p.error, "created_at": p.created_at}
        for p in rows
    ]}


ai_router = APIRouter(prefix="/api/ai-proposals", tags=["AI 提案"])


def _proposal_service(session: Session) -> AiProposalService:
    return _ai_service(session)


@ai_router.post("/{pid}/approve")
def ai_proposal_approve(pid: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    from ..services.workspace_mutation import WorkspaceMutationError

    svc = _proposal_service(session)
    try:
        return svc.approve_proposal(pid)
    except WorkspaceMutationError as e:
        raise HTTPException(e.http_status, e.code) from e


@ai_router.post("/{pid}/reject")
def ai_proposal_reject(pid: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    from ..services.workspace_mutation import WorkspaceMutationError

    svc = _proposal_service(session)
    try:
        return svc.reject_proposal(pid)
    except WorkspaceMutationError as e:
        raise HTTPException(e.http_status, e.code) from e


@ai_router.post("/{pid}/expire")
def ai_proposal_expire(pid: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    from ..services.workspace_mutation import WorkspaceMutationError

    svc = _proposal_service(session)
    try:
        return svc.expire_proposal(pid)
    except WorkspaceMutationError as e:
        raise HTTPException(e.http_status, e.code) from e


@ai_router.post("/{pid}/apply")
def ai_proposal_apply(pid: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """应用提案：OOXML 写入 → 新 Workspace 正式版本（source=ai_generate）→ 派生异步分发。"""
    from ..services.workspace_mutation import (
        UnsupportedMaterialStructureError,
        WorkspaceMutationError,
    )
    from ..services.xml_safety import UnsafeXmlError

    svc = _proposal_service(session)
    try:
        return svc.apply_proposal(pid)
    except UnsupportedMaterialStructureError as e:
        raise HTTPException(e.http_status, {"code": e.code, "features": e.features}) from e
    except UnsafeXmlError as e:
        raise HTTPException(422, {"code": "UNSAFE_XML", "features": [str(e)]}) from e
    except WorkspaceMutationError as e:
        raise HTTPException(e.http_status, e.code) from e


# ---------------- C-4b-2A：AI Gateway（统一出网入口；Mock Provider 先行） ----------------


class AiGenerateIn(BaseModel):
    base_workspace_version_id: str
    instruction: str
    context: list[dict[str, Any]] = []


class AiGatewayConfigIn(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    timeout_s: int | None = None
    api_key_ref: str | None = None


def _gateway_error(e: Any) -> HTTPException:
    from ..services.ai_gateway import sanitize_error

    return HTTPException(e.http_status, {"code": e.code, "message": sanitize_error(e)})


@office_router.get("/{oid}/ai-gateway/config")
def get_ai_gateway_config(oid: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """AI 网关配置视图（永不返回密钥本体）。"""
    from ..services.ai_gateway import load_gateway_config, public_config_view, resolve_secret_store

    if session.get(OfficeDocument, oid) is None:
        raise HTTPException(404, "OfficeDocument 不存在")
    config = load_gateway_config(session)
    store = resolve_secret_store()
    has_key = config["provider"] == "mock" or bool(store.get_key(config["api_key_ref"]))
    return public_config_view(config, has_key)


@office_router.put("/{oid}/ai-gateway/config")
def put_ai_gateway_config(oid: str, body: AiGatewayConfigIn,
                          session: Session = Depends(get_session)) -> dict[str, Any]:
    """更新 AI 网关配置（密钥本体不落库，仅可更新 api_key_ref 引用）。"""
    from ..services.ai_gateway import (
        load_gateway_config,
        public_config_view,
        resolve_secret_store,
        save_gateway_config,
    )

    if session.get(OfficeDocument, oid) is None:
        raise HTTPException(404, "OfficeDocument 不存在")
    current = load_gateway_config(session)
    updates = {k: v for k, v in {
        "provider": body.provider, "base_url": body.base_url, "model": body.model,
        "timeout_s": body.timeout_s, "api_key_ref": body.api_key_ref,
    }.items() if v is not None}
    timeout = updates.get("timeout_s")
    if isinstance(timeout, int) and not (5 <= timeout <= 600):
        raise HTTPException(400, {"code": "GATEWAY_INVALID_TIMEOUT",
                                  "message": "timeout_s 取值范围 5~600"})
    config = save_gateway_config(session, {**current, **updates})
    store = resolve_secret_store()
    has_key = config["provider"] == "mock" or bool(store.get_key(config["api_key_ref"]))
    return public_config_view(config, has_key)


@office_router.post("/{oid}/ai-proposals/generate")
def generate_ai_proposal(oid: str, body: AiGenerateIn,
                         session: Session = Depends(get_session)) -> dict[str, Any]:
    """AI 生成提案候选（经 Gateway）：上下文显式提交 → 出网 → draft 提案（不进入任何正文落点）。"""
    from ..services.ai_context import ContextCollector
    from ..services.ai_gateway import (
        AiGateway,
        GatewayError,
        load_gateway_config,
        resolve_secret_store,
    )
    from ..services.ai_proposal import AiProposalRequest

    od = session.get(OfficeDocument, oid)
    if od is None or od.document_type != "workspace":
        raise HTTPException(404, "OfficeDocument 不存在")
    if not (body.instruction or "").strip():
        raise HTTPException(400, {"code": "GATEWAY_INVALID_INSTRUCTION",
                                  "message": "写作要求不能为空"})

    from ..models import WorkspaceDocument

    wd = session.query(WorkspaceDocument).filter(
        WorkspaceDocument.office_document_id == oid).first()
    project_id = wd.project_id if wd else None
    if project_id is None:
        raise HTTPException(404, "工作稿所属项目不存在")

    import json

    config = load_gateway_config(session)
    gateway = AiGateway(config, resolve_secret_store())
    try:
        blocks = ContextCollector(session, project_id).collect(body.context)
        text, manifest = gateway.generate_proposal_text(body.instruction.strip(), blocks)
    except GatewayError as e:
        raise _gateway_error(e) from e

    from ..services.workspace_mutation import WorkspaceMutationError

    svc = _ai_service(session)
    try:
        return {
            "proposal": svc.create_proposal(AiProposalRequest(
                workspace_office_document_id=oid,
                base_workspace_version_id=body.base_workspace_version_id,
                title=(body.instruction.strip()[:60]),
                text=text,
                operation="append_end",
                model=manifest.model,
                context=json.loads(manifest.to_json()),
            )),
            "egress_manifest": json.loads(manifest.to_json()),
        }
    except WorkspaceMutationError as e:
        raise HTTPException(e.http_status, e.code) from e


@office_router.post("/{oid}/retry-processor")
def office_retry_processor(oid: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """派生数据处理失败后重试（当前版本）。

    复用既有幂等语义：retry_processor_run 仅删除 failed 登记，随后 dispatch 重新抢占；
    已 done/running 的版本重试为无副作用 no-op（try_claim 失败 → already-dispatched）。
    """
    from ..services.office_document_svc import retry_processor_run

    od = session.get(OfficeDocument, oid)
    if od is None or not od.current_version_id:
        raise HTTPException(404, "OfficeDocument 不存在或尚无版本")
    v = session.get(OfficeDocumentVersion, od.current_version_id)
    if v is None:
        raise HTTPException(404, "当前版本不存在")
    retried = retry_processor_run(v.id, od.document_type)
    audit("oo_processor_retry", detail=f"office_document {oid} v{v.version} · {'重新入队' if retried else '无失败登记'}")
    # 覆盖上次的失败提示，避免前端状态条把重试误读成仍在失败
    _office_cb_log[oid] = f"正在重试 v{v.version} 派生数据处理"
    _dispatch_async(od.id, v.id)
    return {"ok": True, "version": v.version, "retried": retried}


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


class MaterialInsertIn(BaseModel):
    """C-3a：Material → Workspace 插入请求（版本精确固定；契约见 阶段六C3a-插入契约设计.md）。"""

    base_workspace_version_id: str
    material_id: str
    material_office_document_id: str
    material_version_id: str
    insert_mode: str = "append_end"
    target_locator: Any = None


# DS 会话态 → 活跃编辑（内存信号；持久信号=工作快照脏判定，在服务内）
_ACTIVE_EDIT_STATES = ("编辑中", "正在处理保存", "工作快照", "强制保存失败")


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
