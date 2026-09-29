"""拆解抽取引擎 v3：按标题锚点定位章节区间，区间内容原样复制（表格还原表格、段落还原段落）。

招标文件：三项（不分组）——投标人须知前附表 / 评标办法前附表 / 投标文件格式。
投标文件：两项区间——近年完成的类似项目情况表（业绩区）/ 项目团队成员及相关人员简历（团队区）。

项目负责人 2026-09-16/17 规则定稿：源文件里是表格就抽表格、是文本就抽文本，
内容完整原样复制，不做字段加工；LLM 不参与拆解（仅用于图片识别）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 招标三项（锚点用 | 分隔备选）
TENDER_ITEMS: list[dict[str, str]] = [
    {"name": "投标人须知前附表", "anchors": "投标人须知前附表", "sum": "关键条款表"},
    {"name": "评标办法前附表", "anchors": "评标办法前附表", "sum": "评分办法表"},
    {"name": "投标文件格式", "anchors": "第六章投标文件格式|投标文件格式", "sum": "格式章节"},
]

# 投标两项区间（黄区=业绩、青区=团队）
BID_ITEMS: list[dict[str, str]] = [
    {"name": "近年完成的类似项目情况表", "group": "业绩与履约情况",
     "anchors": "近年完成的类似项目情况表|三年内成功实施类似设备|类似项目业绩"},
    {"name": "项目团队成员及相关人员简历", "group": "项目团队",
     "anchors": "项目团队成员及相关人员简历|项目团队成员"},
]

_CHAPTER_RE = re.compile(r"^\s*第[0-9一二三四五六七八九十百零]+[章节部分篇]")


@dataclass
class ExtractedGroup:
    group_name: str
    name: str
    sum: str
    rows: list[list[str]] = field(default_factory=list)


def _norm(s: str) -> str:
    """标题归一化：去空白，使「第六章投标文件格式」与「第六章 投标文件格式」等价。"""
    return re.sub(r"[\s\u3000]+", "", s)


_TITLE_PREFIX_RE = re.compile(
    r"^(?:第[0-9一二三四五六七八九十百零]+[章节部分篇]"
    r"|[0-9]+(?:[.、][0-9]+)*[.、]?"
    r"|[一二三四五六七八九十]+[、.]"
    r"|[（(][一二三四五六七八九十]+[）)]"
    r"|附件[0-9一二三四五六七八九十]+[:：]?)"
)


def _find_anchor_with_text(lines: list[str], anchors: list[str]) -> tuple[int | None, str | None]:
    """锚点行号 + 命中锚点原文（供 preview 证据使用）。匹配语义与历史实现完全一致。"""
    norm_to_orig: dict[str, str] = {}
    for a in anchors:
        na = _norm(a)
        if na and na not in norm_to_orig:
            norm_to_orig[na] = a
    for an in norm_to_orig:
        for i, ln in enumerate(lines):
            st = _norm(ln)
            if len(st) < len(an):
                continue
            core = st
            m = _TITLE_PREFIX_RE.match(st)
            if m:
                core = st[m.end():]
            if len(core) < len(an):
                continue
            if core == an:
                return i, norm_to_orig[an]
            if core.startswith(an):
                tail = core[len(an):]
                if len(tail) <= 2 and not re.search(r"[；。，、：,（(]", tail):
                    return i, norm_to_orig[an]
    return None, None


def _find_anchor_line(lines: list[str], anchors: list[str]) -> int | None:
    """锚点行号：仅命中独立标题行。标题允许带章节号前缀（第一章/1.2/一、/附件1），
    去前缀后整行须与锚点全等（或锚点+≤2字尾且不以句读结尾）。

    正文里的「见/按XX前附表」引用句以标点结尾或含引用动词，天然被排除；
    目录行带页码尾，去前缀后也不可能与锚点全等。
    """
    return _find_anchor_with_text(lines, anchors)[0]


def _table_to_rows(rows: list[list[str]]) -> list[list[str]]:
    """表格行列 → 全列行：保留原表结构，过滤全空行。"""
    out: list[list[str]] = []
    for r in rows:
        cells = [c.strip() for c in r]
        while cells and not cells[-1]:
            cells.pop()
        if any(cells):
            out.append(cells)
    return out


def has_title_prefix(text: str) -> bool:
    """锚点是否自带章节号前缀（如「第四章 合同条款」）。

    匹配时剥离的是**正文标题**的章节号，锚点自身带前缀将永远匹配不上；
    规则保存处据此提前拦截，避免用户静默配出永不命中的锚点。
    """
    return bool(_TITLE_PREFIX_RE.match(_norm(text)))


def _spec_from_rule(rule: dict[str, Any]) -> tuple[str, list[str], str]:
    """规则项 → 抽取规格 (name, anchors, sum)。

    锚点按 priority 升序（契约 §2.3：等价于既有「锚点顺序优先」语义）；
    sum 取规则说明，缺省回落规则名（与既有投标侧 sum=name 行为一致）。
    group_name 由调用处取 name（既有实现即如此，BID_ITEMS 的 group 字段未参与抽取）。
    """
    name = str(rule.get("name") or "")
    pairs: list[tuple[int, int, str]] = []
    for idx, a in enumerate(rule.get("anchors") or []):
        if isinstance(a, dict):
            text = str(a.get("text") or "").strip()
            try:
                prio = int(a.get("priority", idx + 1))
            except (TypeError, ValueError):
                prio = idx + 1
        else:
            text, prio = str(a).strip(), idx + 1
        if text:
            pairs.append((prio, idx, text))
    pairs.sort()
    summary = rule.get("summary")
    return (name, [t for _p, _i, t in pairs], str(summary) if summary else name)


@dataclass
class ExtractedRegion:
    """抽取区间及证据定位（契约 §十一）：group 为原样复制结果。

    行号 0-based；`anchor_line` = 命中锚点行，`end_line` = 区间排他上界（区间最后一行
    为 end_line-1）；未命中时两者与 `anchor_hit` 均为 None。
    """

    group: ExtractedGroup
    anchor_line: int | None
    end_line: int | None
    anchor_hit: str | None


def extract_structured_with_regions(
    text: str, kind: str, doc_tables: list[Any] | None = None,
    rules: list[dict[str, Any]] | None = None,
) -> list[ExtractedRegion]:
    """抽取核心（带证据定位）：按标题锚点定位章节区间，区间内容原样复制。

    `extract_structured` 为其薄包装（只取 group），两条路径共享同一实现——
    preview 的 location/rows_count/内容与正式拆解天然一致（契约 §11.4）。
    """
    if rules is None:
        if kind not in ("tender", "bid"):
            return []  # 未配置规则的招标公告等类型不参与结构拆解（默认行为不变）
        if kind == "tender":
            specs = [(it["name"], it["anchors"].split("|"), it.get("sum", "")) for it in TENDER_ITEMS]
        else:
            specs = [(it["name"], it["anchors"].split("|"), it["name"]) for it in BID_ITEMS]
    else:
        specs = [_spec_from_rule(r) for r in rules]

    lines = text.splitlines()
    tables = doc_tables or []
    tid_of = {id(tb): i + 1 for i, tb in enumerate(tables)}  # 表格编号（V2 行对象携带）

    # 分界行：章标题 + 各锚点行
    bounds: set[int] = set()
    anchor_at: dict[int, str] = {}
    hit_of: dict[str, tuple[int, str]] = {}
    for name, anchors, _sum in specs:
        ai, hit = _find_anchor_with_text(lines, anchors)
        if ai is not None:
            anchor_at[ai] = name
            bounds.add(ai)
            hit_of[name] = (ai, hit or "")
    for i, ln in enumerate(lines):
        if _CHAPTER_RE.match(_norm(ln)):
            bounds.add(i)
    ordered = sorted(bounds)

    out: list[ExtractedRegion] = []
    used_tables: set[int] = set()
    for name, _anchors, _sum in specs:
        ai = next((i for i, nm in anchor_at.items() if nm == name), None)
        if ai is None:
            out.append(ExtractedRegion(
                group=ExtractedGroup(group_name=name, name=name, sum=_sum, rows=[]),
                anchor_line=None, end_line=None, anchor_hit=None,
            ))
            continue
        hi = next((b for b in ordered if b > ai), len(lines))
        rows = _copy_region(lines, tables, ai, hi, used_tables, tid_of)
        hit = hit_of.get(name, (ai, ""))[1]
        out.append(ExtractedRegion(
            group=ExtractedGroup(group_name=name, name=name, sum=_sum, rows=rows[:800]),
            anchor_line=ai, end_line=hi, anchor_hit=hit,
        ))
    return out


def extract_structured(
    text: str, kind: str, doc_tables: list[Any] | None = None,
    rules: list[dict[str, Any]] | None = None,
) -> list[ExtractedGroup]:
    """抽取 = 按标题锚点定位章节区间，区间内容原样复制（表格还原表格、段落还原段落）。

    rules=None（契约 §9.1 安全阀）：走内置常量，行为与规则中心上线前逐行一致；
    rules 给定时按规则集执行（空列表 = 不抽取），kind 不再限制为 tender/bid，
    以便公告等类型配置自有规则后同样生效。
    """
    return [r.group for r in extract_structured_with_regions(text, kind, doc_tables, rules)]


def _copy_region(
    lines: list[str], tables: list[Any], lo: int, hi: int, used_tables: set[int],
    tid_of: dict[int, int] | None = None,
) -> list[Any]:
    """区间 [lo, hi) 按文档顺序原样复制：段落=单列行；docx 表格=V2 行对象（保留合并结构）。"""
    tid_of = tid_of or {}
    tbs = sorted(
        (tb for tb in tables if lo <= tb.start_line < hi and id(tb) not in used_tables),
        key=lambda tb: tb.start_line,
    )
    for tb in tbs:
        used_tables.add(id(tb))

    spans = [(tb.start_line, tb.end_line) for tb in tbs]

    def in_table(i: int) -> bool:
        return any(sp[0] <= i <= sp[1] for sp in spans)

    events: list[tuple[int, Any]] = []
    for i in range(lo, min(hi, len(lines))):
        if in_table(i) or _CHAPTER_RE.match(_norm(lines[i])):
            continue
        st = lines[i].strip()
        if st:
            events.append((i, [st]))
    for tb in tbs:
        model = getattr(tb, "model", None)
        if model:
            # V2：每个表格行一个对象（自带表头信息），空行/合并结构原样保留
            for k, trow in enumerate(model["rows"]):
                events.append((tb.start_line + k, {
                    "type": "table",
                    "schemaVersion": 2,
                    "tid": tid_of.get(id(tb), 0),
                    "columnCount": model["columnCount"],
                    "columnWidthsTwips": model["columnWidthsTwips"],
                    "cells": trow["cells"],
                }))
        else:
            table_rows = _table_to_rows(tb.rows)
            events.extend((tb.start_line + k, r) for k, r in enumerate(table_rows))
    events.sort(key=lambda x: x[0])
    return [r for _, r in events]
