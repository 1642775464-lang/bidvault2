"""拆解 provenance：状态机派生、历史快照差异、孤儿执行回收（契约 §五 / §8.2）。

原则（契约 §三）：历史解释只读 ExtractExecution.rule_snapshot_json，绝不回查规则表重算。

状态派生（契约 §5.2，Execution 优先于 document.status）：
    running execution                → BUILDING
    最近一次 execution = failed       → FAILED
    provenance 与当前同类规则集一致    → NORMAL
    provenance 落后（同类规则已变）    → RULE_CHANGED
    无 provenance                    → NEED_REBUILD（禁止标 NORMAL）

比对口径说明：以**同类规则集 hash** 判定过期（而非全局版本号）——否则改一条招标规则
会把所有投标文档误标为过期；全局规则集版本仅用于展示「v3 → 当前 v5」。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..models import Document, ExtractExecution
from .extract_rules import KINDS, current_version, list_rules, resolve_rules, rules_hash, serialize_rule

STATUS_BUILDING = "BUILDING"
STATUS_FAILED = "FAILED"
STATUS_NORMAL = "NORMAL"
STATUS_RULE_CHANGED = "RULE_CHANGED"
STATUS_NEED_REBUILD = "NEED_REBUILD"


def kind_hash(session: Session, kind: str) -> str:
    """当前该文档类型的启用规则集摘要。"""
    return rules_hash(resolve_rules(session, kind))


def _kind_hashes(session: Session) -> dict[str, str]:
    return {k: kind_hash(session, k) for k in KINDS}


def status_map(session: Session, docs: list[Document]) -> dict[str, dict[str, Any]]:
    """批量派生 extract_status（避免逐文档 N+1）。"""
    out: dict[str, dict[str, Any]] = {}
    if not docs:
        return out
    ids = [d.id for d in docs]
    hashes = _kind_hashes(session)
    cur_version = current_version(session)

    running: set[str] = set()
    latest: dict[str, str] = {}
    rows = (
        session.query(ExtractExecution)
        .filter(ExtractExecution.document_id.in_(ids))
        .order_by(ExtractExecution.started_at)
        .all()
    )
    for ex in rows:
        if ex.status == "running":
            running.add(ex.document_id)
        latest[ex.document_id] = ex.status  # 有序遍历 → 最后一条即最近

    for d in docs:
        cur_hash = hashes.get(d.kind, "")
        if d.id in running:
            status = STATUS_BUILDING
        elif latest.get(d.id) == "failed":
            status = STATUS_FAILED
        elif d.extract_ruleset_version is None and d.extract_rules_hash is None:
            status = STATUS_NEED_REBUILD
        elif d.extract_rules_hash == cur_hash:
            status = STATUS_NORMAL
        else:
            status = STATUS_RULE_CHANGED
        out[d.id] = {
            "extract_status": status,
            "extract_ruleset_version": d.extract_ruleset_version,
            "ruleset_version": cur_version,
        }
    return out


def status_for(session: Session, doc: Document) -> dict[str, Any]:
    return status_map(session, [doc])[doc.id]


def stale_doc_count(session: Session) -> int:
    """已拆解但规则已过期/无 provenance 的文档数（同类 hash 口径）。"""
    hashes = _kind_hashes(session)
    rows = session.query(Document).filter(Document.status == 2).all()
    n = 0
    for d in rows:
        if d.extract_rules_hash is None or d.extract_rules_hash != hashes.get(d.kind, ""):
            n += 1
    return n


def _anchors_key(rule: dict[str, Any]) -> list[tuple[int, str, str]]:
    return sorted(
        (
            int(a.get("priority", 0) or 0),
            str(a.get("text", "")),
            str(a.get("match_mode", "contains")),
        )
        for a in (rule.get("anchors") or [])
    )


def _rule_fields(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": rule.get("name"),
        "anchors": _anchors_key(rule),
        "enabled": bool(rule.get("enabled", True)),
        "group_name": rule.get("group_name") or "",
        "summary": rule.get("summary"),
        "focus_fields": list(rule.get("focus_fields") or []),
    }


def diff_for_document(session: Session, doc: Document) -> dict[str, Any]:
    """[查看差异]：最近一次执行快照 vs 当前同类规则集（只读快照，不回查历史规则表）。"""
    ex = (
        session.query(ExtractExecution)
        .filter(ExtractExecution.document_id == doc.id)
        .order_by(ExtractExecution.started_at.desc())
        .first()
    )
    if ex is None:
        return {
            "available": False,
            "reason": "规则中心上线前拆解，无快照可比",
            "document_id": doc.id,
            "kind": doc.kind,
        }

    try:
        snapshot: list[dict[str, Any]] = json.loads(ex.rule_snapshot_json or "[]")
    except ValueError:
        snapshot = []
    # 当前侧取该类型**全部**规则（含停用）：停用应显示为「已修改·enabled」，
    # 而不是被误报为「已删除」（用户会以为规则没了，实际只是不生效）。
    current = [serialize_rule(r) for r in list_rules(session, kind=doc.kind)]

    old_by_id = {r.get("id"): r for r in snapshot}
    new_by_id = {r.get("id"): r for r in current}

    added = [{"id": i, "name": new_by_id[i].get("name")} for i in new_by_id if i not in old_by_id]
    removed = [{"id": i, "name": old_by_id[i].get("name")} for i in old_by_id if i not in new_by_id]

    changed: list[dict[str, Any]] = []
    for rid in new_by_id:
        if rid not in old_by_id:
            continue
        before, after = _rule_fields(old_by_id[rid]), _rule_fields(new_by_id[rid])
        fields = [k for k in before if before[k] != after[k]]
        if fields:
            changed.append({"id": rid, "name": after["name"], "fields": fields})

    common_old = [i for i in old_by_id if i in new_by_id]
    common_new = [i for i in new_by_id if i in old_by_id]
    order_changed = common_old != common_new

    return {
        "available": True,
        "document_id": doc.id,
        "kind": doc.kind,
        "from_version": ex.ruleset_version,
        "to_version": current_version(session),
        "from_ruleset_hash": ex.rules_hash,
        "to_ruleset_hash": rules_hash(current),
        "executed_at": ex.started_at.isoformat() if ex.started_at else None,
        "added": added,
        "removed": removed,
        "changed": changed,
        "order_changed": order_changed,
        "unchanged": len(common_new) - len(changed),
    }


def reconcile_orphan_executions(session: Session) -> int:
    """启动回收：进程被杀等导致的 running 执行记录置 failed（快照不动，条款 A）。

    单进程桌面形态下，启动时不存在活跃执行，故所有 running 记录均为孤儿。
    """
    rows = session.query(ExtractExecution).filter(ExtractExecution.status == "running").all()
    for ex in rows:
        ex.status = "failed"
        ex.error_msg = "进程中断，执行未完成"
        ex.finished_at = datetime.now(UTC)
    if rows:
        session.commit()
    return len(rows)
