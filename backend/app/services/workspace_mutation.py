"""C-3a：Material → Workspace 插入契约（模型、校验链、审计；OOXML 引擎 C-3b 注入）。

核心语义（阶段六C3a-插入契约设计.md）：
- 插入素材 = 创建一个新的 Workspace 正式版本（source=material_insert），
  与 ONLYOFFICE callback 链完全无关（禁伪造 status=2）。
- 乐观并发：base_workspace_version_id 必须 == workspace.current_version_id，否则 409。
- 活跃编辑保护：DS 会话态（注入探测）或工作快照脏（存在且 hash ≠ current）→ 拒绝。
- engine=None（引擎未注入）时校验通过后显式 501，零副作用。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..models import Material, OfficeDocument, OfficeDocumentVersion, WorkspaceMutation

ALLOWED_INSERT_MODES = ("append_end",)


class WorkspaceMutationError(Exception):
    """契约错误基类：code 即 API 错误码（HTTPException detail）。"""

    code = "WORKSPACE_MUTATION_ERROR"
    http_status = 400


class RelatedEntityNotFoundError(WorkspaceMutationError):
    code = "RELATED_ENTITY_NOT_FOUND"
    http_status = 404


class InvalidInsertModeError(WorkspaceMutationError):
    code = "INVALID_INSERT_MODE"
    http_status = 400


class MaterialVersionMismatchError(WorkspaceMutationError):
    code = "MATERIAL_VERSION_MISMATCH"
    http_status = 400


class WorkspaceVersionMismatchError(WorkspaceMutationError):
    code = "WORKSPACE_VERSION_MISMATCH"
    http_status = 400


class WorkspaceVersionConflictError(WorkspaceMutationError):
    code = "WORKSPACE_VERSION_CONFLICT"
    http_status = 409


class WorkspaceEditingError(WorkspaceMutationError):
    code = "WORKSPACE_EDITING"
    http_status = 423


class MutationNoChangeError(WorkspaceMutationError):
    code = "MUTATION_NO_CHANGE"
    http_status = 400


class MutationEngineNotImplementedError(WorkspaceMutationError):
    code = "MUTATION_ENGINE_NOT_IMPLEMENTED"
    http_status = 501


class UnsupportedMaterialStructureError(WorkspaceMutationError):
    """C-3b：素材包含引擎不支持的结构（显式拒绝，不静默降级）。detail 携带 features 列表。"""

    code = "UNSUPPORTED_MATERIAL_STRUCTURE"
    http_status = 422

    def __init__(self, features: list[str], message: str = "") -> None:
        self.features = list(dict.fromkeys(features))
        super().__init__(message or f"素材包含不支持的结构：{', '.join(self.features)}")


class MutationEngine(Protocol):
    """C-3b：OOXML 包级插入执行器（包级 clone / rel 重映射 / 不支持结构显式拒绝）。"""

    def mutate(self, plan: InsertPlan, workspace_bytes: bytes, material_bytes: bytes) -> bytes: ...


@dataclass
class MaterialInsertRequest:
    workspace_office_document_id: str
    base_workspace_version_id: str
    material_id: str
    material_office_document_id: str
    material_version_id: str
    insert_mode: str = "append_end"
    target_locator: Any = None


@dataclass
class InsertPlan:
    """prepare_insert 的产出：解析后的双方实体与精确版本（零副作用）。"""

    request: MaterialInsertRequest
    workspace_od: Any
    base_version: Any
    material: Any
    material_od: Any
    material_version: Any


def _version_bytes(version: Any) -> bytes:
    from ..config import get_settings

    p = get_settings().files_dir / version.file_path
    if not p.is_file():
        raise RelatedEntityNotFoundError(f"版本文件已丢失：{version.id}")
    data: bytes = p.read_bytes()
    return data


class WorkspaceMutationService:
    """Material → Workspace 版本变更契约。写入面收敛见设计文档 §六。"""

    def __init__(
        self,
        session: Any,
        active_edit_probe: Callable[[str], bool],
        engine: MutationEngine | None = None,
        dispatch_fn: Callable[[str, str], None] | None = None,
    ) -> None:
        self.session = session
        self._active_edit_probe = active_edit_probe
        self._engine = engine
        self._dispatch_fn = dispatch_fn

    # ---------------- 校验链（零副作用） ----------------

    def prepare_insert(self, req: MaterialInsertRequest) -> InsertPlan:
        if req.insert_mode not in ALLOWED_INSERT_MODES:
            raise InvalidInsertModeError(f"insert_mode 仅支持：{ALLOWED_INSERT_MODES}")

        ws_od = self.session.get(OfficeDocument, req.workspace_office_document_id)
        if ws_od is None:
            raise RelatedEntityNotFoundError("workspace OfficeDocument 不存在")
        if ws_od.document_type != "workspace":
            raise WorkspaceVersionMismatchError("目标 OfficeDocument 不是 workspace")
        material_od = self.session.get(OfficeDocument, req.material_office_document_id)
        if material_od is None:
            raise RelatedEntityNotFoundError("material OfficeDocument 不存在")
        if material_od.document_type != "material":
            raise MaterialVersionMismatchError("素材 OfficeDocument 类型不是 material")
        material = self.session.get(Material, req.material_id)
        if material is None:
            raise RelatedEntityNotFoundError("素材不存在")
        if material.office_document_id and material.office_document_id != req.material_office_document_id:
            raise MaterialVersionMismatchError("material_id 与 material_office_document_id 不匹配")

        base = self.session.get(OfficeDocumentVersion, req.base_workspace_version_id)
        if base is None or base.office_document_id != ws_od.id:
            raise WorkspaceVersionMismatchError("base_workspace_version_id 不属于该 workspace")
        mat_ver = self.session.get(OfficeDocumentVersion, req.material_version_id)
        if mat_ver is None or mat_ver.office_document_id != material_od.id:
            raise MaterialVersionMismatchError("material_version_id 不属于该 material OfficeDocument")

        if self._has_active_edit(ws_od):
            raise WorkspaceEditingError("存在活跃编辑会话或未落版本的工作快照，请先结束编辑")

        current = ws_od.current_version_id
        if current != req.base_workspace_version_id:
            raise WorkspaceVersionConflictError(
                f"workspace 已前移到 v{self._version_no(ws_od)}，请基于最新版本重试"
            )
        return InsertPlan(
            request=req, workspace_od=ws_od, base_version=base,
            material=material, material_od=material_od, material_version=mat_ver,
        )

    def _has_active_edit(self, ws_od: Any) -> bool:
        if self._active_edit_probe(ws_od.id):
            return True
        from ..config import get_settings

        # 脏快照 = 快照内容不属于该 workspace 的任何已持久化版本
        # （只比对 current 会误报：DS status=6 载荷与 status=2 落版文件逐字节不同，
        #   且 restore 后快照可能只是被超越的历史内容——那些都不是"未落版本的修改"）
        snap = get_settings().files_dir / "working" / f"{ws_od.id}.docx"
        if not snap.is_file():
            return False
        snap_hash = hashlib.sha256(snap.read_bytes()).hexdigest()
        versions = (
            self.session.query(OfficeDocumentVersion)
            .filter(OfficeDocumentVersion.office_document_id == ws_od.id)
            .all()
        )
        for v in versions:
            f = get_settings().files_dir / v.file_path
            if f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest() == snap_hash:
                return False
        return True

    def _version_no(self, ws_od: Any) -> int:
        v = (
            self.session.get(OfficeDocumentVersion, ws_od.current_version_id)
            if ws_od.current_version_id
            else None
        )
        return v.version if v else 0

    # ---------------- 执行（engine 为 C-3b 注入位） ----------------

    def insert_material(self, req: MaterialInsertRequest) -> dict[str, Any]:
        plan = self.prepare_insert(req)
        if self._engine is None:
            raise MutationEngineNotImplementedError("OOXML 插入引擎未实现（C-3b）")

        new_bytes = self._engine.mutate(
            plan, _version_bytes(plan.base_version), _version_bytes(plan.material_version)
        )
        from .office_document_svc import OfficeDocumentService, OfficeDocumentTableAdapter

        svc = OfficeDocumentService(session=self.session, adapter=OfficeDocumentTableAdapter(self.session))
        version = svc.create_version(
            plan.workspace_od, new_bytes, source="material_insert",
            comment=f"插入素材「{plan.material.title}」v{plan.material_version.version}",
        )
        if version.id == plan.base_version.id:
            raise MutationNoChangeError("插入未产生内容变化（引擎输出与 base 相同）")
        self.session.expire(plan.workspace_od)

        self.session.add(WorkspaceMutation(
            workspace_office_document_id=plan.workspace_od.id,
            workspace_base_version_id=plan.base_version.id,
            workspace_new_version_id=version.id,
            material_id=plan.material.id,
            material_office_document_id=plan.material_od.id,
            material_version_id=plan.material_version.id,
            insert_mode=plan.request.insert_mode,
            target_locator_json=(
                json.dumps(plan.request.target_locator, ensure_ascii=False)
                if plan.request.target_locator is not None else None
            ),
        ))
        self.session.commit()
        self.session.expire(plan.workspace_od)

        from ..services.audit import audit

        audit("workspace_material_insert", detail=(
            f"workspace {plan.workspace_od.id} v{plan.base_version.version}→v{version.version} "
            f"← material {plan.material.id} v{plan.material_version.version} · {plan.request.insert_mode}"
        ))
        if self._dispatch_fn is not None:
            self._dispatch_fn(plan.workspace_od.id, version.id)
        return {
            "ok": True,
            "workspace_office_document_id": plan.workspace_od.id,
            "version": version.version,
            "version_id": version.id,
            "material_version_id": plan.material_version.id,
            "insert_mode": plan.request.insert_mode,
        }
