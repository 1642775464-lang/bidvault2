"""拆解结构定义（对齐原型 v1.2 文档库）：招标 4 组 / 投标 7 项。

正则锚点从拆解正文中提取「字段：值」行；提取不到的组仅定位章节标题。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

TENDER_GROUPS: list[tuple[str, list[dict[str, Any]]]] = [
    ("前附表与格式抽取", [
        {"name": "投标人须知前附表", "sum": "关键条款锚点", "anchors": ["投标人须知前附表", "前附表"],
         "fields": ["递交截止时间", "投标有效期", "投标保证金", "正副本份数"]},
        {"name": "评标办法前附表", "sum": "评分办法锚点", "anchors": ["评标办法前附表", "评标办法"],
         "fields": ["评标办法", "技术分", "商务分", "价格分"]},
        {"name": "投标文件格式", "sum": "格式清单锚点", "anchors": ["投标文件格式", "格式要求"],
         "fields": ["商务格式", "技术格式", "资格审查格式"]},
    ]),
    ("章节内容抽取", [
        {"name": "章节目录", "sum": "章节锚点", "anchors": ["章节目录", "目录", "第一章"],
         "fields": []},
    ]),
]

BID_GROUPS: list[tuple[str, list[dict[str, Any]]]] = [
    ("资格与身份文件", [
        {"name": "法定代表人（单位负责人）身份证明", "sum": "法代锚点",
         "anchors": ["法定代表人身份证明", "法定代表人资格证明"], "fields": ["法定代表人", "单位性质"]},
        {"name": "授权委托书", "sum": "代理人锚点", "anchors": ["授权委托书", "委托代理书"],
         "fields": ["委托代理人", "委托权限", "有效期"]},
    ]),
    ("单位与财务状况", [
        {"name": "基本情况表", "sum": "单位信息锚点", "anchors": ["基本情况表", "投标人基本情况"],
         "fields": ["单位名称", "资质等级", "注册资本", "质量体系"]},
        {"name": "近年财务状况", "sum": "财务锚点", "anchors": ["财务状况", "审计报告"],
         "fields": ["营收", "净利", "负债率"]},
    ]),
    ("业绩与履约情况", [
        {"name": "近年完成的类似项目情况表", "sum": "业绩锚点", "anchors": ["类似项目情况表", "类似项目一览表", "类似项目业绩"],
         "fields": ["项目名称", "合同额", "验收情况"]},
        {"name": "正在供货和新承接的项目情况表", "sum": "履约锚点", "anchors": ["正在供货", "新承接"],
         "fields": ["项目名称", "完成度"]},
    ]),
    ("项目团队", [
        {"name": "项目团队成员及相关人员简历", "sum": "团队锚点", "anchors": ["项目团队", "团队成员", "简历"],
         "fields": ["项目经理", "技术负责人", "商务负责人"]},
    ]),
]

_FIELD_LINE = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9（）()·/ ]{2,30})[:：]\s*(\S[^\n]{0,120})")


@dataclass
class ExtractedGroup:
    group_name: str
    name: str
    sum: str
    rows: list[tuple[str, str]] = field(default_factory=list)


def extract_structured(text: str, kind: str) -> list[ExtractedGroup]:
    """从正文提取结构组：锚点定位 + 字段行收割。返回按组聚合的列表。"""
    groups = TENDER_GROUPS if kind == "tender" else BID_GROUPS
    out: list[ExtractedGroup] = []
    for group_name, items in groups:
        for item in items:
            hit_line = None
            all_lines = text.splitlines()
            # 优先命中独立标题行（整行≈锚点），避免正文里「见XX前附表」引用抢先命中
            for anchor in item["anchors"]:
                for ln in all_lines:
                    st = ln.strip()
                    if st == anchor or (st.startswith(anchor) and len(st) <= len(anchor) + 6):
                        hit_line = st
                        break
                if hit_line:
                    break
            if hit_line is None:
                for anchor in item["anchors"]:
                    for ln in all_lines:
                        if anchor in ln:
                            hit_line = ln.strip()
                            break
                    if hit_line:
                        break
            if hit_line is None:
                continue
            # 锚点行之后收割字段行（取锚点后 40 行窗口）
            rows: list[tuple[str, str]] = []
            lines = all_lines
            try:
                start = next(
                    i for i, ln in enumerate(lines)
                    if hit_line is not None and ln.strip() == hit_line
                )
            except StopIteration:
                start = 0
            # 章节目录：仅收录「第X章」级条目（目录行或章标题），其余编号/键值一律不收
            if "章节目录" in item["name"]:
                for ln in lines[start: start + 60]:
                    m = re.match(r"^\s*(第[一二三四五六七八九十百零]+章)\s*(\S.{0,40}?)\s*$", ln.strip())
                    if m and m.group(2).strip():
                        rows.append((m.group(1), m.group(2).strip()))
                        continue
                    m2 = re.match(r"^\s*(第[一二三四五六七八九十百零]+章)([^\n]{0,40}?)\s*[·.\u2026\d\s]*\d+\s*$", ln.strip())
                    if m2 and m2.group(2).strip():
                        rows.append((m2.group(1), m2.group(2).strip()))
                rows = rows[:30]
                out.append(ExtractedGroup(group_name=group_name, name=item["name"], sum=item["sum"], rows=rows))
                continue

            # 表格块解析：锚点窗口内按连续 | 行切块，支持任意列数（含表头映射）
            table_rows, prose_rows = _harvest_tables(lines[start: start + 80], item["fields"])
            rows.extend(table_rows)
            for ln in lines[start: start + 80]:
                if "|" in ln:
                    continue  # 表格行已由 _harvest_tables 处理
                for m in _FIELD_LINE.finditer(ln):
                    fname = m.group(1).strip()
                    if item["fields"] and fname not in item["fields"]:
                        continue
                    if not item["fields"] and fname in ("第一章", "第二章", "第三章", "第四章", "第五章", "第六章"):
                        continue
                    rows.append((fname, m.group(2).strip()))
            rows.extend(prose_rows)
            # 指定字段但未匹配到的，用宽松搜索（必须有冒号分隔，防止把标题行当值）
            for fname in item["fields"]:
                if any(r[0] == fname for r in rows):
                    continue
                pat = re.compile(re.escape(fname) + r"\s*[:：]\s*([^\n]{1,120})")
                fm = pat.search(text)
                if fm:
                    rows.append((fname, fm.group(1).strip()))
            out.append(ExtractedGroup(
                group_name=group_name, name=item["name"], sum=item["sum"],
                rows=rows[:40],
            ))
    return out


def _harvest_tables(
    window: list[str], fields: list[str],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """解析窗口内表格：连续 | 行成块；两列→键值，多列→首行表头映射。

    返回 (表格字段行, 窗口内非表格正文里的字段:值行)。
    """
    table_rows: list[tuple[str, str]] = []
    prose_rows: list[tuple[str, str]] = []
    block: list[list[str]] = []
    prev_cols = 0

    def flush() -> None:
        nonlocal block
        if not block:
            return
        ncols = max(len(r) for r in block)
        header: list[str] = []
        first = block[0]
        # 首行是表头：各列均短且非空
        if len(first) == ncols and all(len(c) <= 16 and c for c in first):
            header = first
            data = block[1:]
        else:
            data = block
        for r in data:
            r = (r + [""] * ncols)[:ncols]
            if ncols == 2:
                if r[0] and r[1]:
                    table_rows.append((r[0][:60], r[1][:300]))
            elif len(r) >= 3:
                # 通用：倒数第二列做字段名（条款名称/评审因素），最后一列做值
                fname = r[-2] or (header[-2] if len(header) >= 2 else "")
                fval = r[-1]
                if fname and fval and fval != "/":
                    table_rows.append((fname[:60], fval[:400]))
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
                prose_rows.append((fname, m.group(2).strip()[:120]))
    flush()
    return table_rows, prose_rows
