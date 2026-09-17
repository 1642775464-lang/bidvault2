"""拆解结构定义与抽取引擎。

招标文件：三项（不分组）——投标人须知前附表 / 评标办法前附表 / 投标文件格式。
锚点定位后直接取对应 docx 表格（项目负责人 2026-09-16 标注定稿：黄表=须知前附表、
蓝表=评标办法前附表、灰表区=投标文件格式；真实文档无颜色，按锚点+表头特征分类）。

投标文件：两项区间法——近年完成的类似项目情况表（业绩区）/ 项目团队成员及相关人员简历（团队区）。
表格行保留全部列（rows 元素为 list，列数 2~N），还原原表结构供页面表格渲染。
LLM 分段抽取与规则结果合并（pipeline 层），失败回退纯规则。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 招标三项（锚点用 | 分隔备选）
TENDER_ITEMS: list[dict[str, str]] = [
    {"name": "投标人须知前附表", "anchors": "投标人须知前附表", "sum": "关键条款锚点"},
    {"name": "评标办法前附表", "anchors": "评标办法前附表", "sum": "评分办法锚点"},
    {"name": "投标文件格式", "anchors": "投标文件格式|第六章投标文件格式|第六章 投标文件格式", "sum": "格式表格锚点"},
]

# 投标两项区间（项目负责人 2026-09-16 标注定稿：黄区=业绩、青区=团队）
BID_ITEMS: list[dict[str, str]] = [
    {"name": "近年完成的类似项目情况表", "group": "业绩与履约情况",
     "anchors": "近年完成的类似项目情况表|三年内成功实施类似设备|类似项目业绩|类似项目一览表"},
    {"name": "项目团队成员及相关人员简历", "group": "项目团队",
     "anchors": "项目团队成员及相关人员简历|项目团队成员|项目团队"},
]

_FIELD_LINE = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9（）()·/ ]{2,30})[:：]\s*(\S[^\n]{0,120})")


@dataclass
class ExtractedGroup:
    group_name: str
    name: str
    sum: str
    rows: list[list[str]] = field(default_factory=list)


def extract_structured(
    text: str, kind: str, doc_tables: list[Any] | None = None,
) -> list[ExtractedGroup]:
    """抽取入口：招标=三项锚点分配；投标=两项区间法。"""
    if kind == "tender":
        return _extract_tender(text, doc_tables or [])
    return _extract_bid_regions(text, doc_tables or [])


def _norm(s: str) -> str:
    """标题归一化：去空白，使「第六章投标文件格式」与「第六章 投标文件格式」等价。"""
    return re.sub(r"[\s\u3000]+", "", s)


def _find_anchor_line(lines: list[str], anchors: list[str]) -> int | None:
    """锚点行号：仅命中独立标题行（归一化后整行=锚点，或锚点+≤2字且不以句读结尾）。

    正文里的「见/按XX前附表」引用句以标点结尾或含引用动词，天然被排除。
    """
    anchors_n = [_norm(a) for a in anchors]
    for an in anchors_n:
        for i, ln in enumerate(lines):
            st = _norm(ln)
            if len(st) < len(an):
                continue
            if st == an:
                return i
            tail = st[len(an):]
            if st.startswith(an) and len(tail) <= 2 and not re.search(r"[；。，、：,]", tail):
                return i
    return None


def _table_to_rows(rows: list[list[str]]) -> list[list[str]]:
    """表格行列 → 全列行：保留原表结构（条款号|条款名称|编列内容 全部列）。

    过滤全空行与空尾列。
    """
    out: list[list[str]] = []
    for r in rows:
        cells = [c.strip() for c in r]
        while cells and not cells[-1]:
            cells.pop()
        # 单列行（仅条款名、无编列内容的对照行）不构成表格数据，丢弃
        if len(cells) < 2 or not any(cells):
            continue
        out.append(cells)
    return out


def _extract_tender(text: str, doc_tables: list[Any]) -> list[ExtractedGroup]:
    """招标三项：须知/评标办法=锚点后第一张表；投标文件格式=格式锚点后全部表。

    docx 有结构化表格时直接分配；无表格（pdf/纯文本）时回退窗口法抽冒号行，并靠 LLM 补充。
    """
    lines = text.splitlines()
    out: list[ExtractedGroup] = []
    used: set[int] = set()
    last_end = 0

    for item in TENDER_ITEMS:
        ai = _find_anchor_line(lines, item["anchors"].split("|"))
        rows: list[list[str]] = []
        if ai is not None:
            if item["name"] == "投标文件格式":
                tables = [tb for ti, tb in enumerate(doc_tables) if tb.start_line > ai and ti not in used]
            else:
                cand = sorted(
                    (tb for ti, tb in enumerate(doc_tables) if tb.start_line > ai and ti not in used),
                    key=lambda tb: tb.start_line,
                )
                tables = cand[:1]
            for tb in tables:
                used.add(id(tb))
                rows.extend(_table_to_rows(tb.rows))
                last_end = max(last_end, tb.end_line)
        if not rows:
            # 无结构化表格：锚点窗口内抽冒号行兜底（LLM 会再补）
            win = lines[ai + 1: ai + 60] if ai is not None else []
            for ln in win:
                if "|" in ln:
                    parts = [c.strip() for c in ln.split("|") if c.strip()]
                    if len(parts) >= 2:
                        rows.append([parts[-2][:60], parts[-1][:400]])
                    continue
                rows.extend([m.group(1).strip(), m.group(2).strip()] for m in _FIELD_LINE.finditer(ln))
        rows = rows[:80]
        out.append(ExtractedGroup(group_name=item["name"], name=item["name"], sum=item["sum"], rows=rows))

    # 格式锚点未命中或无表时，把锚点区之后剩余的未使用表归入投标文件格式
    fmt = out[-1]
    if fmt.name == "投标文件格式" and not fmt.rows:
        remaining = [tb for tb in doc_tables if id(tb) not in used and tb.start_line > last_end]
        for tb in remaining:
            fmt.rows.extend(_table_to_rows(tb.rows))
        fmt.rows = fmt.rows[:80]
    return out


def _extract_bid_regions(text: str, doc_tables: list[Any]) -> list[ExtractedGroup]:
    """投标两项：区间法——业绩锚点到团队锚点之间为业绩区，团队锚点到文末为团队区。

    区间内全部表格（全列保留）与正文段落（每段一行）都归入对应项。
    """
    lines = text.splitlines()
    a_perf = _find_anchor_line(lines, BID_ITEMS[0]["anchors"].split("|"))
    a_team = _find_anchor_line(lines, BID_ITEMS[1]["anchors"].split("|"))

    regions: list[tuple[int, int, dict[str, str]]] = []
    if a_perf is not None:
        regions.append((a_perf, a_team if a_team is not None and a_team > a_perf else len(lines), BID_ITEMS[0]))
    if a_team is not None:
        regions.append((a_team, len(lines), BID_ITEMS[1]))
    if not regions:
        mid = len(lines) // 2
        regions = [(0, mid, BID_ITEMS[0]), (mid, len(lines), BID_ITEMS[1])]

    out: list[ExtractedGroup] = []
    used_tables: set[int] = set()
    for lo, hi, item in regions:
        rows: list[list[str]] = []
        for ti, tb in enumerate(doc_tables):
            if ti in used_tables or not (lo <= tb.start_line < hi):
                continue
            used_tables.add(ti)
            rows.extend(_table_to_rows(tb.rows))
        para_n = 0
        for ln in lines[lo + 1: hi]:
            st = ln.strip()
            if not st or "|" in st or re.match(r"^第[一二三四五六七八九十百零]+章", st) or len(st) < 8:
                continue
            para_n += 1
            if para_n > 30:
                break
            m = re.match(r"^([^：:]{2,20})[:：]\s*(.+)$", st)
            if m:
                rows.append([m.group(1).strip(), m.group(2).strip()[:400]])
            else:
                rows.append([st[:12], st[:400]])
        seen: set[str] = set()
        uniq: list[list[str]] = []
        for r in rows:
            key = "|".join(c[:60] for c in r)
            if key not in seen:
                seen.add(key)
                uniq.append(r)
        out.append(ExtractedGroup(group_name=item["group"], name=item["name"], sum=item["group"], rows=uniq[:80]))
    return out


def _harvest_tables(
    window: list[str], fields: list[str],
) -> tuple[list[list[str]], list[list[str]]]:
    """解析窗口内表格：连续 | 行成块（全列保留）。

    返回 (表格行, 窗口内非表格正文里的字段:值行)。
    """
    table_rows: list[list[str]] = []
    prose_rows: list[list[str]] = []
    block: list[list[str]] = []
    prev_cols = 0

    def flush() -> None:
        nonlocal block
        if not block:
            return
        table_rows.extend(_table_to_rows(block))
        block = []

    for ln in window:
        if "|" in ln:
            parts = [c.strip() for c in ln.split("|") if c.strip()]
            if len(parts) >= 2:
                if block and len(parts) != prev_cols:
                    flush()
                block.append(parts)
                prev_cols = len(parts)
                continue
        flush()
        for m in _FIELD_LINE.finditer(ln):
            fname = m.group(1).strip()
            if not fields or fname in fields:
                prose_rows.append([fname, m.group(2).strip()[:120]])
    flush()
    return table_rows, prose_rows
