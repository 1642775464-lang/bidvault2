"""阶段六A 回归：表格行号槽 invariant + 表格连续性（空行/vMerge/表尾空行/真实17行表）。

根因回归防护：parse 阶段每个物理 <w:tr> 必须占一个行号槽（空行也占位），
否则表尾行号与表后段落行号碰撞，extractor 稳定排序把段落插进表尾。
"""

from __future__ import annotations

import io

from docx import Document as Docx

from app.services.ingest.extractor import extract_structured
from app.services.ingest.parser import parse_docx

ANCHOR = "投标人须知前附表"


def _docx(build) -> bytes:
    d = Docx()
    d.add_paragraph(ANCHOR)
    build(d)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _rows_order(data: bytes, kind: str = "tender") -> list[tuple[str, int]]:
    """抽取行序列：(类型, 摘要)。dict=表格行，list=段落行。"""
    parsed = parse_docx(data)
    groups = extract_structured(parsed.text, kind, parsed.doc_tables)
    out: list[tuple[str, int]] = []
    for g in groups:
        for r in g.rows:
                if isinstance(r, dict):
                    out.append(("T", hash(r["id"]) % 10000))
                else:
                    joined = " ".join(r)
                    if joined.strip():
                        out.append(("P", abs(hash(joined)) % 10000))
    return out


def _invariant_ok(data: bytes) -> bool:
    parsed = parse_docx(data)
    return all(
        (t.end_line - t.start_line + 1) == len(t.model["rows"])
        for t in parsed.doc_tables
        if t.model is not None
    )


def test_single_empty_row_kept_in_order():
    """单个空行：表格行保持连续，表后段落不插入表中间。"""

    def build(d: Docx) -> None:
        t = d.add_table(rows=3, cols=3)
        t.cell(0, 0).text = "1"
        t.cell(1, 0).text = ""  # 单个空行
        t.cell(2, 0).text = "3"
        d.add_paragraph("表后正文")

    data = _docx(build)
    assert _invariant_ok(data)
    parsed = parse_docx(data)
    rows = [r for g in extract_structured(parsed.text, "tender", parsed.doc_tables) for r in g.rows]
    tbl = [i for i, r in enumerate(rows) if isinstance(r, dict)]
    assert tbl, "表格行丢失"
    seq = rows[tbl[0]:tbl[-1] + 1]
    assert all(isinstance(r, dict) for r in seq), f"表格行被段落打断：{seq}"
    assert rows[-1] == ["表后正文"], f"表后段落不在表后：{rows[-3:]}"


def test_consecutive_empty_rows_kept_in_order():
    """连续空行：占位连续、不打断表格。"""

    def build(d: Docx) -> None:
        t = d.add_table(rows=5, cols=2)
        t.cell(0, 0).text = "开始"
        # 行 1-3 连续空行
        t.cell(4, 0).text = "结束"
        d.add_paragraph("表后正文")

    data = _docx(build)
    assert _invariant_ok(data)
    parsed = parse_docx(data)
    tb = parsed.doc_tables[0]
    assert (tb.end_line - tb.start_line + 1) == 5
    rows = [r for g in extract_structured(parsed.text, "tender", parsed.doc_tables) for r in g.rows]
    tbl = [i for i, r in enumerate(rows) if isinstance(r, dict)]
    seq = rows[tbl[0]:tbl[-1] + 1]
    assert all(isinstance(r, dict) for r in seq), f"连续空行场景表格被打断：{seq}"


def test_trailing_empty_row_and_paragraph_after():
    """表尾空行 + 空行后紧跟正文段落：段落必须在全部表格行之后。"""

    def build(d: Docx) -> None:
        t = d.add_table(rows=3, cols=2)
        t.cell(0, 0).text = "小计"
        # 行 1 表尾空行
        t.cell(2, 0).text = "投标总价（元）"
        d.add_paragraph("投标人：（盖单位章）")
        d.add_paragraph("年月日")

    data = _docx(build)
    assert _invariant_ok(data)
    parsed = parse_docx(data)
    rows = [r for g in extract_structured(parsed.text, "tender", parsed.doc_tables) for r in g.rows]
    tbl = [i for i, r in enumerate(rows) if isinstance(r, dict)]
    assert tbl and all(isinstance(r, dict) for r in rows[tbl[0]:tbl[-1] + 1])
    tail = rows[tbl[-1] + 1:]
    assert tail == [["投标人：（盖单位章）"], ["年月日"]], f"表后段落顺序错误：{tail}"


def test_vmerge_continuation_slots():
    """vMerge continuation 行（内容为空）也必须占行号槽。"""

    def build(d: Docx) -> None:
        t = d.add_table(rows=3, cols=2)
        t.cell(0, 0).merge(t.cell(1, 0)).text = "小计"
        t.cell(0, 1).text = "100"
        t.cell(1, 1).text = "200"
        t.cell(2, 0).text = "备注"
        t.cell(2, 1).text = "含税"
        d.add_paragraph("表后正文")

    data = _docx(build)
    assert _invariant_ok(data)
    parsed = parse_docx(data)
    rows = [r for g in extract_structured(parsed.text, "tender", parsed.doc_tables) for r in g.rows]
    tbl = [i for i, r in enumerate(rows) if isinstance(r, dict)]
    assert all(isinstance(r, dict) for r in rows[tbl[0]:tbl[-1] + 1])
    assert rows[-1] == ["表后正文"]


def test_real_other_quote_17_row_table_continuity():
    """真实『其他报价』17 行表等价构造：1→…→投标总价 连续，投标人才出现。"""
    cells = [
        ["产品明细报价", "", "", "", "", "", "", ""],
        ["序号", "产品名称", "型号与规格", "数量", "单价", "总价", "原产地", "备注"],
        [""] * 8,
        [""] * 8,
        [""] * 8,
        [""] * 8,
        ["…", "", "", "", "", "", "", ""],
        ["小计", ""],
        ["其他报价", "", ""],
        ["1", "专用工具", "", "", "", "", "", ""],
        ["2", "包装费", "", "", "", "", "", ""],
        ["3", "运输费、保险", "", "", "", "", "", ""],
        ["4", "安装、调试、检验", "", "", "", "", "", ""],
        ["5", "其他", "", "", "", "", "", ""],
        ["…", "", "", "", "", "", "", ""],
        ["小计", ""],
        ["投标总价（元）", ""],
    ]

    def build(d: Docx) -> None:
        t = d.add_table(rows=len(cells), cols=8)
        # 合并行：产品明细报价（行0 跨8列）
        t.cell(0, 0).merge(t.cell(0, 7)).text = "产品明细报价"
        # 其他报价（行8 跨3列：模拟源文件小计式合并）
        t.cell(8, 0).merge(t.cell(8, 2)).text = "其他报价"
        for ri, row in enumerate(cells):
            for ci, val in enumerate(row):
                if val and not (ri in (0, 8) and ci == 0):
                    if ri == 8 and ci == 0:
                        continue
                    t.cell(ri, ci).text = val
        # 行7/15 小计 2 格合并（跨列 2-7）
        t.cell(7, 1).merge(t.cell(7, 7))
        t.cell(7, 0).text = "小计"
        t.cell(15, 1).merge(t.cell(15, 7))
        t.cell(15, 0).text = "小计"
        t.cell(16, 1).merge(t.cell(16, 7))
        t.cell(16, 0).text = "投标总价（元）"
        d.add_paragraph("投标人：（盖单位章）")
        d.add_paragraph("法定代表人（单位负责人）或其委托代理人：（签字或签章）")
        d.add_paragraph("年月日")

    data = _docx(build)
    assert _invariant_ok(data)
    parsed = parse_docx(data)
    rows = [r for g in extract_structured(parsed.text, "tender", parsed.doc_tables) for r in g.rows]
    tbl = [i for i, r in enumerate(rows) if isinstance(r, dict)]
    assert tbl, "表格行全部丢失"
    seq = rows[tbl[0]:tbl[-1] + 1]
    # 关键不变量：表格行区间内不得出现任何段落行（连续完整）
    assert all(isinstance(r, dict) for r in seq), f"17 行表被段落打断：{[r for r in seq if isinstance(r, list)]}"
    # 顺序：1 专用工具 → 2 包装费 → … → 投标总价 连续递进，随后才是投标人
    joined_seq = [c["text"] for r in seq for c in r["cells"] if c["text"].strip()]
    order = [joined_seq.index(k) for k in ["1", "专用工具", "2", "包装费", "5", "其他", "投标总价（元）"]]
    assert order == sorted(order), f"表内行序错乱：{joined_seq}"
    tail = rows[tbl[-1] + 1:]
    assert [r[0] for r in tail][:3] == ["投标人：（盖单位章）", "法定代表人（单位负责人）或其委托代理人：（签字或签章）", "年月日"], f"投标人未在表后：{tail}"
