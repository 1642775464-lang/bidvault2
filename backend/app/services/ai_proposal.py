"""C-4a：AI 修改提案的契约与审批模型（无真实 AI 调用；mutator 为 C-4b 注入位）。

核心语义（阶段六C4a-AI审批契约设计.md）：
- AI 内容在用户 approve 之前只存在于 ai_proposals——禁止进 derived_text/content_text/版本表；
- apply = 与素材插入同一持久化链：OOXML mutation（注入）→ create_version(source="ai_insert")
  → current 前移 → 派生追平；拒绝伪造 ONLYOFFICE callback；
- CAS：apply 时 base_workspace_version_id 必须 == current，失效则提案转 expired（409）；
- 状态机：draft → approved → applied | failed；draft → rejected | expired；
  rejected/expired/applied 不可应用；并发重复 apply 由状态机拒绝（不产生两个新版本）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Protocol

from ..models import AiProposal, OfficeDocument, OfficeDocumentVersion, _now
from .workspace_mutation import (
    MutationEngineNotImplementedError,
    RelatedEntityNotFoundError,
    WorkspaceEditingError,
    WorkspaceMutationError,
    WorkspaceVersionConflictError,
)

ALLOWED_AI_OPERATIONS = ("append_end",)
# 人工审批边界在 DB 层强制：draft 不在抢占条件中，未经 approve 的提案任何路径都不能 claim
PROPOSAL_CLAIMABLE_STATES = ("approved", "failed")  # failed 可重试（明确重试策略）


class ProposalApplyingError(WorkspaceMutationError):
    code = "PROPOSAL_APPLYING"
    http_status = 409


class InvalidProposalOperationError(WorkspaceMutationError):
    code = "PROPOSAL_INVALID_OPERATION"
    http_status = 400


class InvalidProposalContentError(WorkspaceMutationError):
    code = "PROPOSAL_INVALID_CONTENT"
    http_status = 400


class InvalidProposalStateError(WorkspaceMutationError):
    code = "PROPOSAL_INVALID_STATE"
    http_status = 409


class AiContentMutator(Protocol):
    """C-4b：AI 内容 → 新 Workspace DOCX bytes（复用 C-3b 包级 append 机制）。"""

    def mutate_append(self, proposal: AiProposal, workspace_bytes: bytes) -> bytes: ...


@dataclass
class AiProposalRequest:
    workspace_office_document_id: str
    base_workspace_version_id: str
    title: str
    text: str
    operation: str = "append_end"
    model: str | None = None
    context: Any = None  # 出网边界：发送给云模型的上下文来源（JSON 可序列化）


def _validate_content(req: AiProposalRequest) -> str:
    if req.operation not in ALLOWED_AI_OPERATIONS:
        raise InvalidProposalOperationError(f"operation 仅支持：{ALLOWED_AI_OPERATIONS}")
    if not isinstance(req.text, str) or not req.text.strip():
        raise InvalidProposalContentError("AI 内容 text 不能为空")
    return json.dumps({"text": req.text}, ensure_ascii=False)


class AiProposalService:
    """AI 提案生命周期：create(draft) → approve → apply；reject / expire。"""

    def __init__(
        self,
        session: Any,
        active_edit_probe: Callable[[str], bool],
        mutator: AiContentMutator | None = None,
        dispatch_fn: Callable[[str, str], None] | None = None,
    ) -> None:
        self.session = session
        self._active_edit_probe = active_edit_probe
        self._mutator = mutator
        self._dispatch_fn = dispatch_fn

    # ---------------- 提案生成（draft；不触碰正文任何落点） ----------------

    def create_proposal(self, req: AiProposalRequest) -> dict[str, Any]:
        content_json = _validate_content(req)
        od = self.session.get(OfficeDocument, req.workspace_office_document_id)
        if od is None:
            raise RelatedEntityNotFoundError("workspace OfficeDocument 不存在")
        if od.document_type != "workspace":
            raise WorkspaceVersionConflictError("目标 OfficeDocument 不是 workspace")
        base = self.session.get(OfficeDocumentVersion, req.base_workspace_version_id)
        if base is None or base.office_document_id != od.id:
            raise WorkspaceVersionConflictError("base_workspace_version_id 不属于该 workspace")

        p = AiProposal(
            workspace_office_document_id=od.id,
            base_workspace_version_id=req.base_workspace_version_id,
            operation=req.operation,
            title=(req.title or req.text.strip()[:60]),
            content_json=content_json,
            context_json=(json.dumps(req.context, ensure_ascii=False)
                          if req.context is not None else None),
            model=req.model,
            status="draft",
        )
        self.session.add(p)
        self.session.commit()
        self._audit("ai_proposal_created", p, f"base v{base.version} · {req.operation} · {len(req.text)} 字")
        return self._view(p)

    # ---------------- 状态迁移 ----------------

    def approve_proposal(self, proposal_id: str) -> dict[str, Any]:
        return self._decide(proposal_id, "approved")

    def reject_proposal(self, proposal_id: str) -> dict[str, Any]:
        return self._decide(proposal_id, "rejected")

    def expire_proposal(self, proposal_id: str) -> dict[str, Any]:
        p = self._get(proposal_id)
        # expire 不是清理已成功建版提案的捷径：已建版必须走恢复认领（保持文档与审计一致）
        if p.status == "applying" and self._find_applied_version(p) is not None:
            raise InvalidProposalStateError("提案已成功建版，不可作废（应通过 apply 走恢复认领）")
        return self._decide(proposal_id, "expired")

    def _decide(self, proposal_id: str, target: str) -> dict[str, Any]:
        p = self._get(proposal_id)
        if p.status != "draft":
            raise InvalidProposalStateError(f"仅 draft 可变更为 {target}，当前 {p.status}")
        p.status = target
        p.decided_at = _now()
        self.session.commit()
        self._audit(f"ai_proposal_{target}", p)
        return self._view(p)

    # ---------------- 应用（真实版本变更；mutator 为 C-4b 注入位） ----------------

    def apply_proposal(self, proposal_id: str) -> dict[str, Any]:
        """应用提案。幂等/并发语义（C-4a 补正）：

        - applied   → 幂等返回既有 applied_version_id（响应丢失后的安全重试）
        - applying  → 409 PROPOSAL_APPLYING（另一请求已抢占，不启动第二次 mutator）
        - draft → 409 拒绝（尚未审批，DB 层强制人工确认边界）
  - approved/failed → 数据库条件更新抢占（claim）后执行
        - rejected/expired → 409 PROPOSAL_INVALID_STATE
        - failed 可重试：claim 的 CAS 条件包含 failed（重试策略=用户再次发起 apply）

        事务一致性：claim 先行提交（持久），此后任何时刻崩溃提案都停在 applying，
        绝不出现「正式版本已创建而提案仍在 approved」的重复应用窗口；
        版本 comment 携带提案 id，供后续自动认领恢复（当前 expire 为兜底出口）。
        """
        p = self._get(proposal_id)

        # 幂等：applied → 返回既有结果
        if p.status == "applied":
            return self._view(p)
        if p.status == "applying":
            # 崩溃恢复（C-4b-1）：版本 comment 携带提案 id——已建版则认领；未建版且超时则转 failed 可重试
            linked = self._find_applied_version(p)
            if linked is not None:
                p.status = "applied"
                p.applied_version_id = linked.id
                if p.applied_at is None:
                    p.applied_at = _now()
                self.session.commit()
                self._audit("ai_proposal_applied", p,
                            f"崩溃恢复认领 v{linked.version} ({linked.id})")
                return self._view(p)
            if not self._applying_stale(p):
                raise ProposalApplyingError("提案正在处理中，请稍后查询结果")
            p.status = "failed"
            p.error = "处理超时（疑似中断于建版前），已转为可重试"
            self.session.commit()
            self._audit("ai_proposal_failed", p, p.error)
            # 落入 failed → 下方 claim 路径接管重试
        if p.status not in PROPOSAL_CLAIMABLE_STATES:
            if p.status == "draft":
                raise InvalidProposalStateError("提案尚未审批，不可应用")
            raise InvalidProposalStateError(f"提案状态为 {p.status}，不可应用")

        prior_status = p.status
        claimed = (
            self.session.query(AiProposal)
            .filter(AiProposal.id == p.id,
                    AiProposal.status.in_(PROPOSAL_CLAIMABLE_STATES))
            .update({"status": "applying", "decided_at": _now()},
                    synchronize_session=False)
        )
        self.session.commit()
        if claimed != 1:
            # 并发下抢占失败：重读并以最新状态回应
            latest = self._get(proposal_id)
            if latest.status == "applied":
                return self._view(latest)
            if latest.status == "applying":
                raise ProposalApplyingError("提案正在处理中，请稍后查询结果")
            raise InvalidProposalStateError(f"提案状态为 {latest.status}，不可应用")
        p.status = "applying"

        od = self.session.get(OfficeDocument, p.workspace_office_document_id)
        if od is None or od.document_type != "workspace":
            self._release(p, prior_status)
            raise RelatedEntityNotFoundError("workspace OfficeDocument 不存在或类型不符")

        # 活跃编辑是瞬态条件：423 时释放抢占，提案回到原状态（可重试）
        if self._active_edit_probe(od.id):
            self._release(p, prior_status)
            raise WorkspaceEditingError("工作稿处于活跃编辑状态，请先结束编辑")

        current = od.current_version_id
        if current != p.base_workspace_version_id:
            v = (self.session.get(OfficeDocumentVersion, current) if current else None)
            p.status = "expired"
            p.error = f"工作稿已前移至 v{v.version if v else '?'}，提案基于的版本已失效"
            self.session.commit()
            self._audit("ai_proposal_expired", p, p.error)
            raise WorkspaceVersionConflictError(p.error)

        if self._mutator is None:
            self._release(p, prior_status)
            raise MutationEngineNotImplementedError("AI 内容 mutator 未实现（C-4b）")

        ws_bytes = self._read_version_bytes(current)
        try:
            new_bytes = self._mutator.mutate_append(p, ws_bytes)
        except WorkspaceMutationError:
            raise
        except Exception as e:  # mutator 内部失败 → 提案 failed（可重试），零副作用
            p.status = "failed"
            p.error = str(e)[:500]
            self.session.commit()
            self._audit("ai_proposal_failed", p, p.error)
            raise WorkspaceMutationError(f"AI 内容写入失败：{e}") from e

        from .office_document_svc import OfficeDocumentService, OfficeDocumentTableAdapter

        svc = OfficeDocumentService(session=self.session, adapter=OfficeDocumentTableAdapter(self.session))
        version = svc.create_version(
            od, new_bytes, source="ai_insert",
            # comment 携带提案 id：崩溃恢复时可由 applying 提案认领（防重复应用）
            comment=f"AI 提案 {p.id}：{p.title[:60]}")
        if version.id == p.base_workspace_version_id:
            p.status = "failed"
            p.error = "mutator 输出与 base 相同（无内容变化）"
            self.session.commit()
            self._audit("ai_proposal_failed", p, p.error)
            raise WorkspaceMutationError("AI 内容写入失败：输出与 base 相同")

        p.status = "applied"
        p.applied_version_id = version.id
        p.applied_at = _now()
        self.session.commit()
        self.session.expire(od)
        self._audit("ai_proposal_applied", p,
                    f"v{version.version} ({version.id}) · sha256 {version.file_hash[:12]}")
        if self._dispatch_fn is not None:
            self._dispatch_fn(od.id, version.id)
        return self._view(p)

    def _release(self, p: AiProposal, prior_status: str) -> None:
        """瞬态错误（423 等）时释放抢占，提案回到原状态保持可重试。"""
        p.status = prior_status
        self.session.commit()

    def _find_applied_version(self, p: AiProposal) -> OfficeDocumentVersion | None:
        """崩溃恢复：按提案 id（版本 comment 中的稳定操作标识）唯一关联已创建的正式版本。"""
        linked: list[OfficeDocumentVersion] = list(
            self.session.query(OfficeDocumentVersion)
            .filter(OfficeDocumentVersion.office_document_id == p.workspace_office_document_id,
                    OfficeDocumentVersion.source == "ai_insert",
                    OfficeDocumentVersion.comment.like(f"%{p.id}%"))
            .all()
        )
        assert len(linked) <= 1, f"提案 {p.id} 关联到多个版本（恢复约束被破坏）"
        found: OfficeDocumentVersion | None = linked[0] if linked else None
        return found

    def _applying_stale(self, p: AiProposal) -> bool:
        """applying 是否已超时（超过 mutator 处理预算 → 判定中断于建版前，转可重试）。"""
        from datetime import datetime

        if p.decided_at is None:
            return True
        decided = p.decided_at
        if decided.tzinfo is None:
            decided = decided.replace(tzinfo=UTC)
        return (datetime.now(UTC) - decided).total_seconds() > 180

    # ---------------- 内部 ----------------

    def _get(self, proposal_id: str) -> AiProposal:
        p: AiProposal | None = self.session.get(AiProposal, proposal_id)
        if p is None:
            raise RelatedEntityNotFoundError("AI 提案不存在")
        return p

    def _read_version_bytes(self, version_id: str) -> bytes:
        from ..config import get_settings

        v = self.session.get(OfficeDocumentVersion, version_id)
        if v is None:
            raise RelatedEntityNotFoundError("版本不存在")
        p = get_settings().files_dir / v.file_path
        if not p.is_file():
            raise RelatedEntityNotFoundError("版本文件已丢失")
        data: bytes = p.read_bytes()
        return data

    def _audit(self, action: str, p: AiProposal, detail: str = "") -> None:
        from ..services.audit import audit

        audit(action, detail=(
            f"ai_proposal {p.id[:8]} · workspace {p.workspace_office_document_id} · "
            f"{p.operation} · {p.status}" + (f" · {detail}" if detail else "")
        ))

    def _view(self, p: AiProposal) -> dict[str, Any]:
        return {
            "id": p.id,
            "workspace_office_document_id": p.workspace_office_document_id,
            "base_workspace_version_id": p.base_workspace_version_id,
            "operation": p.operation,
            "title": p.title,
            "status": p.status,
            "model": p.model,
            "applied_version_id": p.applied_version_id,
            "error": p.error,
        }
