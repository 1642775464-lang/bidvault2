"""拆解规则中心路由（契约《标库-拆解规则契约》v1.0 · Phase 1）。

范围：规则清单 / 新增 / 修改 / 删除 / 排序 / 恢复默认 / 变更留痕。
Preview（§七）与文档侧 extract-diff（§8.2）分属 Phase 4 / Phase 2，不在本阶段。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_session
from ..services.extract_rules import (
    KIND_LABELS,
    KINDS,
    ExtractRuleError,
    create_rule,
    current_version,
    delete_rule,
    ensure_rules,
    history,
    list_rules,
    reorder_rules,
    reset_rules,
    serialize_rule,
    stale_doc_count,
    update_rule,
)

router = APIRouter(prefix="/api/extract-rules", tags=["拆解规则中心"])


def _fail(exc: ExtractRuleError) -> HTTPException:
    return HTTPException(exc.http_status, exc.message)


@router.get("")
def list_all(session: Session = Depends(get_session)) -> dict[str, Any]:
    """按文档类型分组的规则清单 + 当前规则集版本 + 规则过期文档数。"""
    ensure_rules(session)
    groups: list[dict[str, Any]] = []
    for kind in KINDS:
        items = [serialize_rule(r) for r in list_rules(session, kind=kind)]
        groups.append({
            "kind": kind,
            "label": KIND_LABELS[kind],
            "items": items,
            "enabled_count": sum(1 for it in items if it["enabled"]),
        })
    return {
        "groups": groups,
        "ruleset_version": current_version(session),
        "stale_docs": stale_doc_count(session),
    }


class RuleCreateIn(BaseModel):
    kind: str
    name: str
    anchors: list[Any]
    group_name: str | None = None
    summary: str | None = None
    focus_fields: list[Any] | None = None
    enabled: bool | None = True
    seq: int | None = None


@router.post("")
def create(body: RuleCreateIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ensure_rules(session)
    try:
        return create_rule(session, body.model_dump())
    except ExtractRuleError as exc:
        raise _fail(exc) from exc


class RulePatchIn(BaseModel):
    name: str | None = None
    anchors: list[Any] | None = None
    group_name: str | None = None
    summary: str | None = None
    focus_fields: list[Any] | None = None
    enabled: bool | None = None
    seq: int | None = None


@router.patch("/{rule_id}")
def patch(rule_id: str, body: RulePatchIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ensure_rules(session)
    try:
        return update_rule(session, rule_id, body.model_dump(exclude_unset=True))
    except ExtractRuleError as exc:
        raise _fail(exc) from exc


@router.delete("/{rule_id}")
def remove(rule_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    ensure_rules(session)
    try:
        return delete_rule(session, rule_id)
    except ExtractRuleError as exc:
        raise _fail(exc) from exc


class ReorderIn(BaseModel):
    kind: str
    ids: list[str]


@router.post("/reorder")
def reorder(body: ReorderIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ensure_rules(session)
    try:
        return reorder_rules(session, body.model_dump())
    except ExtractRuleError as exc:
        raise _fail(exc) from exc


class ResetIn(BaseModel):
    kind: str | None = None


@router.post("/reset")
def reset(body: ResetIn | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    ensure_rules(session)
    try:
        return reset_rules(session, body.model_dump() if body else None)
    except ExtractRuleError as exc:
        raise _fail(exc) from exc


@router.get("/history")
def rule_history(limit: int = 30, session: Session = Depends(get_session)) -> dict[str, Any]:
    ensure_rules(session)
    return {"items": history(session, limit)}
