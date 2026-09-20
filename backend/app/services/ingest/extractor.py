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


def _find_anchor_line(lines: list[str], anchors: list[str]) -> int | None:
    """锚点行号：仅命中独立标题行。标题允许带章节号前缀（第一章/1.2/一、/附件1），
    去前缀后整行须与锚点全等（或锚点+≤2字尾且不以句读结尾）。

    正文里的「见/按XX前附表」引用句以标点结尾或含引用动词，天然被排除；
    目录行带页码尾，去前缀后也不可能与锚点全等。
    """
    anchors_n = [_norm(a) for a in anchors]
    for an in anchors_n:
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
                return i
            if core.startswith(an):
                tail = core[len(an):]
                if len(tail) <= 2 and not re.search(r"[；。，、：,（(]", tail):
                    return i
    return None


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


def extract_structured(
    text: str, kind: str, doc_tables: list[Any] | None = None,
) -> list[ExtractedGroup]:
    """抽取 = 按标题锚点定位章节区间，区间内容原样复制（表格还原表格、段落还原段落）。"""
    lines = text.splitlines()
    tables = doc_tables or []
    if kind == "tender":
        specs = [(it["name"], it["anchors"].split("|"), it.get("sum", "")) for it in TENDER_ITEMS]
    else:
        specs = [(it["name"], it["anchors"].split("|"), it["name"]) for it in BID_ITEMS]

    # 分界行：章标题 + 各锚点行
    bounds: set[int] = set()
    anchor_at: dict[int, str] = {}
    for name, anchors, _sum in specs:
        ai = _find_anchor_line(lines, anchors)
        if ai is not None:
            anchor_at[ai] = name
            bounds.add(ai)
    for i, ln in enumerate(lines):
        if _CHAPTER_RE.match(_norm(ln)):
            bounds.add(i)
    ordered = sorted(bounds)

    out: list[ExtractedGroup] = []
    used_tables: set[int] = set()
    for name, _anchors, _sum in specs:
        ai = next((i for i, nm in anchor_at.items() if nm == name), None)
        if ai is None:
            out.append(ExtractedGroup(group_name=name, name=name, sum=_sum, rows=[]))
            continue
        hi = next((b for b in ordered if b > ai), len(lines))
        rows = _copy_region(lines, tables, ai, hi, used_tables)
        out.append(ExtractedGroup(group_name=name, name=name, sum=_sum, rows=rows[:500]))
    return out


def _copy_region(
    lines: list[str], tables: list[Any], lo: int, hi: int, used_tables: set[int],
) -> list[list[str]]:
    """区间 [lo, hi) 按文档顺序原样复制：表格=全列行，段落=单列行，行号交织保序。"""
    tbs = sorted(
        (tb for ti, tb in enumerate(tables) if lo <= tb.start_line < hi and ti not in used_tables),
        key=lambda tb: tb.start_line,
    )
    for tb in tbs:
        used_tables.add(id(tb))

    spans = [(tb.start_line, tb.end_line) for tb in tbs]

    def in_table(i: int) -> bool:
        return any(sp[0] <= i <= sp[1] for sp in spans)

    events: list[tuple[int, list[str]]] = []
    for i in range(lo, min(hi, len(lines))):
        if in_table(i) or _CHAPTER_RE.match(_norm(lines[i])):
            continue
        st = lines[i].strip()
        if st:
            events.append((i, [st]))
    for tb in tbs:
        table_rows = _table_to_rows(tb.rows)
        events.extend((tb.start_line + k, r) for k, r in enumerate(table_rows))
    events.sort(key=lambda x: x[0])
    return [r for _, r in events]
