"""拆解规则中心（契约《标库-拆解规则契约》v1.0 · Phase 1）。

职责边界（Phase 1）：
- 规则入库与治理：播种（幂等）、CRUD、排序、恢复默认、校验、规则集版本、变更留痕、审计；
- **不改造拆解行为**：extractor 接入、执行记录落库、provenance 与状态机属 Phase 2。

锚点语义见契约 §2.3：priority 升序即现有「锚点顺序优先」；match_mode 仅实现 contains，
exact/prefix/normalized 仅预留（写入即拒，避免接受未实现的语义）。

SQL 纪律：全部经 SQLAlchemy ORM 参数绑定，禁止拼接 SQL。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from ..models import ExtractRule, ExtractRuleVersion, ExtractRulesetMeta

KINDS: tuple[str, ...] = ("tender", "bid", "notice")
SCOPE_DEFAULT = "default"
MATCH_MODES: tuple[str, ...] = ("contains",)  # 仅实现项；其余仅预留（契约 §2.3）
ALLOWED_MATCH_MODES_RESERVED: tuple[str, ...] = ("exact", "prefix", "normalized")

KIND_LABELS: dict[str, str] = {
    "tender": "招标文件",
    "bid": "投标文件",
    "notice": "招标公告",
}

_NAME_MAX = 300
_SUMMARY_MAX = 300
_GROUP_MAX = 50
_ANCHOR_TEXT_MAX = 120
_FOCUS_MAX = 40
_FOCUS_ITEMS_MAX = 30
_META_ID = "ruleset"


class ExtractRuleError(Exception):
    """规则校验/治理错误：http_status + 可读消息。"""

    def __init__(self, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.message = message


# ---------------------------------------------------------------------------
# 内置规则（由 services/ingest/extractor.py 的 TENDER_ITEMS / BID_ITEMS 迁移而来）
# ---------------------------------------------------------------------------
# 说明：tender 的 summary 即现有 sum（关键条款表/评分办法表/格式章节）；
# bid 现有实现 sum=规则名，故内置项 summary 留空，由 Phase 2 取「summary 或 name」保持输出逐行一致。
# focus_fields 取设置页既有展示字段，仅作说明（契约 §2.5，不参与抽取判定）。


def _a(text: str, priority: int) -> dict[str, Any]:
    return {"text": text, "priority": priority, "match_mode": "contains"}


BUILTIN_EXTRACT_RULES: list[dict[str, Any]] = [
    {
        "id": "tender_notice_front",
        "kind": "tender",
        "group_name": "",
        "name": "投标人须知前附表",
        "anchors": [_a("投标人须知前附表", 1)],
        "summary": "关键条款表",
        "focus_fields": [
            "递交截止时间", "投标有效期", "投标保证金", "正副本份数",
            "资质/财务/业绩/信誉要求", "招标人/代理机构/交货期",
        ],
        "seq": 0,
    },
    {
        "id": "tender_eval_front",
        "kind": "tender",
        "group_name": "",
        "name": "评标办法前附表",
        "anchors": [_a("评标办法前附表", 1)],
        "summary": "评分办法表",
        "focus_fields": ["评标方法与排序规则", "形式评审标准", "资格评审标准", "评分项及分值"],
        "seq": 1,
    },
    {
        "id": "tender_bid_format",
        "kind": "tender",
        "group_name": "",
        "name": "投标文件格式",
        "anchors": [_a("第六章投标文件格式", 1), _a("投标文件格式", 2)],
        "summary": "格式章节",
        "focus_fields": ["开标一览表", "产品明细报价", "商务/技术偏差表", "资格文件", "分项报价表"],
        "seq": 2,
    },
    {
        "id": "bid_similar_projects",
        "kind": "bid",
        "group_name": "业绩与履约情况",
        "name": "近年完成的类似项目情况表",
        "anchors": [
            _a("近年完成的类似项目情况表", 1),
            _a("三年内成功实施类似设备", 2),
            _a("类似项目业绩", 3),
        ],
        "summary": None,
        "focus_fields": ["项目名称", "合同金额", "实施时间", "验收情况", "设备/货物配置明细", "业绩描述段落"],
        "seq": 0,
    },
    {
        "id": "bid_team_resume",
        "kind": "bid",
        "group_name": "项目团队",
        "name": "项目团队成员及相关人员简历",
        "anchors": [_a("项目团队成员及相关人员简历", 1), _a("项目团队成员", 2)],
        "summary": None,
        "focus_fields": ["姓名", "职务/层级", "职称", "执业资格及证书编号", "简历正文"],
        "seq": 1,
    },
]

_BUILTIN_BY_ID = {r["id"]: r for r in BUILTIN_EXTRACT_RULES}


# ---------------------------------------------------------------------------
# 版本与播种
# ---------------------------------------------------------------------------


def _meta(session: Session) -> ExtractRulesetMeta:
    row = session.get(ExtractRulesetMeta, _META_ID)
    if row is None:
        row = ExtractRulesetMeta(id=_META_ID, version=0)
        session.add(row)
        session.commit()
    return row


def current_version(session: Session) -> int:
    """当前规则集版本。"""
    return int(_meta(session).version)


def _bump_version(session: Session) -> int:
    """规则集版本 +1（任何规则变更调用）。"""
    row = _meta(session)
    row.version = int(row.version) + 1
    session.commit()
    return int(row.version)


def ensure_rules(session: Session) -> None:
    """内置规则入库（幂等）：缺哪条补哪条；已有行的编辑与启停以库为准。"""
    existing = {r.id for r in session.query(ExtractRule).all()}
    changed = False
    for spec in BUILTIN_EXTRACT_RULES:
        if spec["id"] in existing:
            continue
        session.add(ExtractRule(
            id=spec["id"],
            kind=spec["kind"],
            scope=SCOPE_DEFAULT,
            group_name=spec["group_name"],
            name=spec["name"],
            anchors_json=json.dumps(spec["anchors"], ensure_ascii=False),
            focus_fields_json=json.dumps(spec["focus_fields"], ensure_ascii=False),
            summary=spec["summary"],
            enabled=True,
            seq=spec["seq"],
            builtin=True,
        ))
        changed = True
    _meta(session)
    if changed:
        session.commit()


# ---------------------------------------------------------------------------
# 读写与序列化
# ---------------------------------------------------------------------------


def _norm_text(s: str) -> str:
    """锚点去空白归一（与 extractor._norm 同口径），用于重复检测。"""
    return re.sub(r"[\s\u3000]+", "", s or "")


def _loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        val = json.loads(raw)
    except ValueError:
        return fallback
    return val if isinstance(val, type(fallback)) else fallback


def serialize_rule(row: ExtractRule) -> dict[str, Any]:
    """规则项 → API 形状（anchors/focus_fields 解析为数组）。"""
    anchors = _loads(row.anchors_json, [])
    norm_anchors: list[dict[str, Any]] = []
    for idx, a in enumerate(anchors):
        if isinstance(a, dict):
            norm_anchors.append({
                "text": str(a.get("text", "")),
                "priority": int(a.get("priority", idx + 1)),
                "match_mode": str(a.get("match_mode", "contains")),
            })
        else:  # 兼容纯字符串数组
            norm_anchors.append({"text": str(a), "priority": idx + 1, "match_mode": "contains"})
    return {
        "id": row.id,
        "kind": row.kind,
        "scope": row.scope,
        "group_name": row.group_name,
        "name": row.name,
        "anchors": norm_anchors,
        "focus_fields": _loads(row.focus_fields_json, []),
        "summary": row.summary,
        "enabled": bool(row.enabled),
        "seq": int(row.seq),
        "builtin": bool(row.builtin),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_rules(session: Session, *, kind: str | None = None, scope: str = SCOPE_DEFAULT) -> list[ExtractRule]:
    """按 kind/scope 取规则（seq 升序，其次内置顺序）。"""
    q = session.query(ExtractRule).filter(ExtractRule.scope == scope)
    if kind:
        q = q.filter(ExtractRule.kind == kind)
    rows = q.order_by(ExtractRule.kind, ExtractRule.seq, ExtractRule.name).all()
    return list(rows)


def resolve_rules(session: Session, kind: str, *, scope: str = SCOPE_DEFAULT) -> list[dict[str, Any]]:
    """启用规则集（seq 升序）——Phase 2 由 pipeline 消费；Phase 1 仅供查询与测试。"""
    rows = [
        r for r in list_rules(session, kind=kind, scope=scope) if bool(r.enabled)
    ]
    return [serialize_rule(r) for r in rows]


def ruleset_snapshot(session: Session, *, scope: str = SCOPE_DEFAULT) -> list[dict[str, Any]]:
    """全量启用规则集快照（契约 §6.4：执行开始时冻结，写入执行记录后不可变）。"""
    return [
        serialize_rule(r)
        for r in list_rules(session, scope=scope)
        if bool(r.enabled)
    ]


def rules_hash(snapshot: list[dict[str, Any]]) -> str:
    """快照摘要（canonical JSON → sha256），用于 provenance 快速比对。"""
    canon = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def stale_doc_count(session: Session) -> int:
    """已拆解但规则已过期/无 provenance 的文档数（只读统计，契约 §5.3）。"""
    from ..models import Document

    cur = current_version(session)
    return int(
        session.query(Document)
        .filter(Document.status == 2)
        .filter(
            (Document.extract_ruleset_version.is_(None))
            | (Document.extract_ruleset_version < cur)
        )
        .count()
    )


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _validate_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ExtractRuleError(f"kind 须为 {'/'.join(KINDS)}")
    return kind


def _validate_anchors(anchors: Any) -> list[dict[str, Any]]:
    if not isinstance(anchors, list) or not anchors:
        raise ExtractRuleError("锚点至少 1 个")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, raw in enumerate(anchors):
        if isinstance(raw, dict):
            text = str(raw.get("text", "")).strip()
            mode = str(raw.get("match_mode", "contains"))
            try:
                priority = int(raw.get("priority", idx + 1))
            except (TypeError, ValueError):
                raise ExtractRuleError("锚点 priority 须为整数")
        else:
            text, mode, priority = str(raw).strip(), "contains", idx + 1
        if not text:
            raise ExtractRuleError("锚点内容不能为空")
        if len(text) > _ANCHOR_TEXT_MAX:
            raise ExtractRuleError(f"锚点内容不能超过 {_ANCHOR_TEXT_MAX} 字")
        if mode not in MATCH_MODES:
            raise ExtractRuleError(
                f"match_mode 暂仅支持 {'/'.join(MATCH_MODES)}（{('/'.join(ALLOWED_MATCH_MODES_RESERVED))} 为预留）"
            )
        key = _norm_text(text)
        if key in seen:
            raise ExtractRuleError(f"同一规则内锚点重复：{text}", http_status=409)
        seen.add(key)
        out.append({"text": text, "priority": priority, "match_mode": mode})
    return out


def _validate_focus(fields: Any) -> list[str]:
    if fields is None:
        return []
    if not isinstance(fields, list):
        raise ExtractRuleError("关注字段须为数组")
    if len(fields) > _FOCUS_ITEMS_MAX:
        raise ExtractRuleError(f"关注字段最多 {_FOCUS_ITEMS_MAX} 个")
    out: list[str] = []
    for f in fields:
        s = str(f).strip()
        if not s:
            continue
        if len(s) > _FOCUS_MAX:
            raise ExtractRuleError(f"关注字段不能超过 {_FOCUS_MAX} 字：{s[:20]}…")
        out.append(s)
    return out


def _validate_name(name: Any) -> str:
    s = str(name or "").strip()
    if not s:
        raise ExtractRuleError("规则名称不能为空")
    if len(s) > _NAME_MAX:
        raise ExtractRuleError(f"规则名称不能超过 {_NAME_MAX} 字")
    return s


def _check_anchor_conflict(
    session: Session, *, kind: str, scope: str, anchors: list[dict[str, Any]], exclude_id: str | None = None
) -> None:
    """同 kind+scope 内锚点不得重复（契约 §8.3 → 409）。"""
    mine = {_norm_text(a["text"]) for a in anchors}
    for row in list_rules(session, kind=kind, scope=scope):
        if exclude_id and row.id == exclude_id:
            continue
        for a in serialize_rule(row)["anchors"]:
            other = _norm_text(a["text"])
            if other and other in mine:
                raise ExtractRuleError(
                    f"锚点「{a['text']}」与规则「{row.name}」重复", http_status=409
                )


# ---------------------------------------------------------------------------
# 变更（均写留痕 + 审计 + 版本 +1）
# ---------------------------------------------------------------------------


def _write_history(session: Session, rule_id: str, note: str, snapshot: dict[str, Any]) -> None:
    session.add(ExtractRuleVersion(
        rule_id=rule_id,
        note=note[:300],
        snapshot_json=json.dumps(snapshot, ensure_ascii=False),
    ))


def create_rule(session: Session, body: dict[str, Any]) -> dict[str, Any]:
    from ..services.audit import audit

    kind = _validate_kind(str(body.get("kind", "")))
    name = _validate_name(body.get("name"))
    anchors = _validate_anchors(body.get("anchors"))
    focus = _validate_focus(body.get("focus_fields"))
    group_name = str(body.get("group_name") or "").strip()
    if len(group_name) > _GROUP_MAX:
        raise ExtractRuleError(f"分组名不能超过 {_GROUP_MAX} 字")
    summary = body.get("summary")
    summary = str(summary).strip() if summary is not None else None
    if summary and len(summary) > _SUMMARY_MAX:
        raise ExtractRuleError(f"说明不能超过 {_SUMMARY_MAX} 字")

    _check_anchor_conflict(session, kind=kind, scope=SCOPE_DEFAULT, anchors=anchors)

    max_seq = max([r.seq for r in list_rules(session, kind=kind)] or [-1])
    seq_raw = body.get("seq")
    try:
        seq = int(seq_raw) if seq_raw is not None else max_seq + 1
    except (TypeError, ValueError):
        raise ExtractRuleError("seq 须为整数")
    enabled_raw = body.get("enabled")
    row = ExtractRule(
        kind=kind,
        scope=SCOPE_DEFAULT,
        group_name=group_name,
        name=name,
        anchors_json=json.dumps(anchors, ensure_ascii=False),
        focus_fields_json=json.dumps(focus, ensure_ascii=False),
        summary=summary or None,
        enabled=True if enabled_raw is None else bool(enabled_raw),
        seq=seq,
        builtin=False,
    )
    session.add(row)
    session.commit()
    _write_history(session, row.id, f"新增规则（{KIND_LABELS.get(kind, kind)}）", serialize_rule(row))
    session.commit()
    version = _bump_version(session)
    audit("extract_rule_created", detail=f"{row.id} · {kind} · {name}")
    return {"ok": True, "rule": serialize_rule(row), "ruleset_version": version}


def update_rule(session: Session, rule_id: str, body: dict[str, Any]) -> dict[str, Any]:
    from ..services.audit import audit

    row = session.get(ExtractRule, rule_id)
    if row is None:
        raise ExtractRuleError("规则不存在", http_status=404)

    changes: list[str] = []
    if body.get("name") is not None:
        name = _validate_name(body.get("name"))
        if name != row.name:
            row.name = name
            changes.append("名称")
    if body.get("anchors") is not None:
        anchors = _validate_anchors(body.get("anchors"))
        _check_anchor_conflict(
            session, kind=row.kind, scope=row.scope, anchors=anchors, exclude_id=row.id
        )
        new_json = json.dumps(anchors, ensure_ascii=False)
        if new_json != row.anchors_json:
            row.anchors_json = new_json
            changes.append("锚点")
    if body.get("focus_fields") is not None:
        focus = _validate_focus(body.get("focus_fields"))
        new_focus = json.dumps(focus, ensure_ascii=False)
        if new_focus != row.focus_fields_json:
            row.focus_fields_json = new_focus
            changes.append("关注字段")
    if body.get("group_name") is not None:
        group_name = str(body.get("group_name") or "").strip()
        if len(group_name) > _GROUP_MAX:
            raise ExtractRuleError(f"分组名不能超过 {_GROUP_MAX} 字")
        if group_name != row.group_name:
            row.group_name = group_name
            changes.append("分组")
    if body.get("summary") is not None:
        summary = str(body.get("summary") or "").strip() or None
        if summary and len(summary) > _SUMMARY_MAX:
            raise ExtractRuleError(f"说明不能超过 {_SUMMARY_MAX} 字")
        if summary != row.summary:
            row.summary = summary
            changes.append("说明")
    if body.get("enabled") is not None and bool(body["enabled"]) != bool(row.enabled):
        row.enabled = bool(body["enabled"])
        changes.append("启用" if row.enabled else "停用")
    if body.get("seq") is not None:
        try:
            seq = int(body["seq"])
        except (TypeError, ValueError):
            raise ExtractRuleError("seq 须为整数")
        if seq != row.seq:
            row.seq = seq
            changes.append("顺序")

    if not changes:
        return {"ok": True, "changed": False, "rule": serialize_rule(row)}

    session.commit()
    _write_history(session, row.id, "；".join(changes), serialize_rule(row))
    session.commit()
    version = _bump_version(session)
    audit("extract_rule_updated", detail=f"{rule_id} · {'；'.join(changes)}")
    return {"ok": True, "changed": True, "rule": serialize_rule(row), "ruleset_version": version}


def delete_rule(session: Session, rule_id: str) -> dict[str, Any]:
    from ..services.audit import audit

    row = session.get(ExtractRule, rule_id)
    if row is None:
        raise ExtractRuleError("规则不存在", http_status=404)
    if bool(row.builtin):
        raise ExtractRuleError("内置规则不可删除，可用「恢复默认」还原", http_status=409)

    snapshot = serialize_rule(row)
    session.delete(row)
    session.commit()
    _write_history(session, rule_id, "删除规则", snapshot)
    session.commit()
    version = _bump_version(session)
    audit("extract_rule_deleted", detail=f"{rule_id} · {snapshot['name']}")
    return {"ok": True, "ruleset_version": version}


def reorder_rules(session: Session, body: dict[str, Any]) -> dict[str, Any]:
    """同 kind（+scope）内排序：ids 为期望顺序；未列出的规则按原相对顺序排在后面。"""
    from ..services.audit import audit

    kind = _validate_kind(str(body.get("kind", "")))
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise ExtractRuleError("ids 须为非空数组")
    ids = [str(i) for i in ids]
    if len(set(ids)) != len(ids):
        raise ExtractRuleError("ids 存在重复")

    rows = list_rules(session, kind=kind)
    by_id = {r.id: r for r in rows}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ExtractRuleError(f"规则不属于该类型：{', '.join(unknown[:3])}", http_status=409)

    ordered = [by_id[i] for i in ids] + [r for r in rows if r.id not in set(ids)]
    for idx, row in enumerate(ordered):
        row.seq = idx
    session.commit()
    _write_history(
        session, ids[0], f"排序调整（{KIND_LABELS.get(kind, kind)}）",
        {"kind": kind, "order": [r.id for r in ordered]},
    )
    session.commit()
    version = _bump_version(session)
    audit("extract_rule_reordered", detail=f"{kind} · {','.join(ids[:5])}")
    return {"ok": True, "order": [r.id for r in ordered], "ruleset_version": version}


def reset_rules(session: Session, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """恢复内置默认：内置项字段还原、启用；非内置项删除（按 kind 可选）。"""
    from ..services.audit import audit

    kind = (body or {}).get("kind")
    if kind is not None:
        kind = _validate_kind(str(kind))

    rows = list_rules(session, kind=kind)
    removed = 0
    restored = 0
    for row in rows:
        spec = _BUILTIN_BY_ID.get(row.id)
        if spec is None:
            session.delete(row)
            removed += 1
            continue
        row.kind = spec["kind"]
        row.scope = SCOPE_DEFAULT
        row.group_name = spec["group_name"]
        row.name = spec["name"]
        row.anchors_json = json.dumps(spec["anchors"], ensure_ascii=False)
        row.focus_fields_json = json.dumps(spec["focus_fields"], ensure_ascii=False)
        row.summary = spec["summary"]
        row.enabled = True
        row.seq = spec["seq"]
        row.builtin = True
        restored += 1
    session.commit()

    # 内置项若被删过（历史遗留），补齐
    ensure_rules(session)

    _write_history(
        session, "builtin", f"恢复默认（{'全部' if kind is None else KIND_LABELS.get(kind, kind)}）",
        {"restored": restored, "removed": removed},
    )
    session.commit()
    version = _bump_version(session)
    audit("extract_rule_reset", detail=f"kind={kind or 'all'} restored={restored} removed={removed}")
    return {"ok": True, "restored": restored, "removed": removed, "ruleset_version": version}


def history(session: Session, limit: int = 30) -> list[dict[str, Any]]:
    """变更留痕（时间倒序）。"""
    rows = (
        session.query(ExtractRuleVersion)
        .order_by(ExtractRuleVersion.created_at.desc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
    names = {r.id: r.name for r in session.query(ExtractRule).all()}
    return [
        {
            "rule_id": v.rule_id,
            "rule_name": names.get(v.rule_id, "（已删除）" if v.rule_id not in _BUILTIN_BY_ID else "内置规则"),
            "note": v.note,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in rows
    ]
