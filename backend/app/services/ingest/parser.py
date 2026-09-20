"""F1.2 文档解析：docx / pdf / txt / 表格 / 扫描件分流。"""

from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from pathlib import Path
from typing import Any

# Windows CPU + paddle 3.x 兼容：mkldnn PIR 指令未实现，须在 paddle 首次导入前禁用
os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")


@dataclass
class TableBlock:
    """docx 表格结构化块：rows 行列文本 + 在正文行流中的行号范围 + 高亮颜色（有标注文档时）。

    model 为 TableModel V2（OOXML 网格直读：colSpan/rowSpan/logicalCol/列宽），
    仅 docx 且解析成功时存在；其余来源（xlsx/pdf）只有 rows。
    """

    rows: list[list[str]]
    hl: str = ""  # yellow | cyan | gray | ""（众数高亮色，仅标注过的文档有）
    start_line: int = 0  # 表格首行在正文 lines 中的位置
    end_line: int = 0  # 表格末行位置（含）
    model: dict[str, Any] | None = None


@dataclass(frozen=True)
class ParsedDoc:
    text: str
    pages: list[str]  # 每页文本（page_no = 下标+1）；docx/表格整篇一页
    page_conf: list[float] = dataclasses_field(default_factory=list)  # OCR 页均置信度；非 OCR 为空
    doc_tables: list[TableBlock] = dataclasses_field(default_factory=list)  # docx 专属


_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _w(tag: str) -> str:
    return _W_NS + tag


def _tc_text(tc: Any) -> str:
    parts: list[str] = []
    for p in tc.findall(_w("p")):
        t = "".join(node.text or "" for node in p.iter(_w("t")))
        if t.strip():
            parts.append(t.strip())
    return " ".join(parts).strip().replace("\n", " ")


def parse_table_model(tbl_el: Any) -> dict[str, Any]:
    """w:tbl 元素 → TableModel V2（OOXML 网格直读，禁止 row.cells 展开）。

    - gridSpan → colSpan（不展开成重复格）；空 cell 一律保留（text=""）
    - vMerge restart=起点累计 rowSpan；continue 标 merged + mergeParentId，text 置空不复制
    - tblGrid/gridCol → columnWidthsTwips；tcW 仅接受 dxa（pct/auto 不误读为 twips）
    - gridBefore/gridAfter 计入逻辑列占位（当前文档未出现，防御性支持）
    """
    grid = tbl_el.find(_w("tblGrid"))
    col_widths = [int(g.get(_w("w"), "0") or 0) for g in grid.findall(_w("gridCol"))] if grid is not None else []

    raw_rows: list[dict[str, Any]] = []
    for ri, tr in enumerate(tbl_el.findall(_w("tr"))):
        trpr = tr.find(_w("trPr"))
        grid_before = 0
        if trpr is not None:
            gb = trpr.find(_w("gridBefore"))
            if gb is not None:
                grid_before = max(0, int(gb.get(_w("val"), "0") or 0))
        cursor = grid_before
        cells: list[dict[str, Any]] = []
        for tc in tr.findall(_w("tc")):
            col_span, vmerge, width_tw = 1, None, None
            tcpr = tc.find(_w("tcPr"))
            if tcpr is not None:
                gs = tcpr.find(_w("gridSpan"))
                if gs is not None:
                    col_span = max(1, int(gs.get(_w("val"), "1") or 1))
                vm = tcpr.find(_w("vMerge"))
                if vm is not None:
                    vmerge = vm.get(_w("val")) or "continue"  # 无 val 属性 = continue
                tcw = tcpr.find(_w("tcW"))
                if tcw is not None:
                    wtype = tcw.get(_w("type")) or "auto"
                    if wtype == "dxa":
                        try:
                            width_tw = int(float(tcw.get(_w("w"), "0") or 0))
                        except ValueError:
                            width_tw = None
                    # pct / auto：不误读为 twips，宽度留空走 tblGrid
            cells.append({
                "text": _tc_text(tc), "logical": cursor, "col_span": col_span,
                "vmerge": vmerge, "width": width_tw, "ri": ri,
            })
            cursor += col_span
        ga = trpr.find(_w("gridAfter")) if trpr is not None else None
        grid_after = max(0, int(ga.get(_w("val"), "0") or 0)) if ga is not None else 0
        raw_rows.append({"cells": cells, "grid_before": grid_before, "grid_after": grid_after})

    if not col_widths:
        column_count = max(
            (sum(c["col_span"] for c in r["cells"]) + r["grid_before"] + r["grid_after"] for r in raw_rows),
            default=0,
        )
        col_widths = [0] * column_count
    column_count = len(col_widths)

    # vMerge 配对：逐逻辑列跟踪 open 起点；restart 记 origin，continue 挂 parent
    open_v: dict[int, dict[str, Any]] = {}
    for r in raw_rows:
        for c in r["cells"]:
            cols = range(c["logical"], c["logical"] + c["col_span"])
            if c["vmerge"] == "restart":
                c["row_span"] = 1
                for col in cols:
                    open_v[col] = c
            elif c["vmerge"] == "continue":
                origin = next((open_v[col] for col in cols if col in open_v), None)
                if origin is not None:
                    c["merged"] = True
                    c["merge_parent"] = origin
                    c["text"] = ""  # continuation 不复制原格内容
                    c["col_span"] = origin["col_span"]
                    c["logical"] = origin["logical"]
                    origin["row_span"] = origin.get("row_span", 1) + 1
                else:
                    c["vmerge"] = None  # 异常：无起点的 continue 按普通格处理
            else:
                for col in cols:
                    open_v.pop(col, None)

    rows_out: list[dict[str, Any]] = []
    for ri, r in enumerate(raw_rows):
        cells_out: list[dict[str, Any]] = []
        for c in r["cells"]:
            cell: dict[str, Any] = {
                "id": f"r{ri}c{c['logical']}",
                "text": c["text"],
                "logicalCol": c["logical"],
                "colSpan": c["col_span"],
                "rowSpan": c.get("row_span", 1),
            }
            if c.get("merged"):
                cell["merged"] = True
                cell["mergeParentId"] = f"r{c['merge_parent']['ri']}c{c['merge_parent']['logical']}"
            if c.get("width"):
                cell["widthTwips"] = c["width"]
            cells_out.append(cell)
        rows_out.append({"cells": cells_out})

    return {
        "type": "table",
        "schemaVersion": 2,
        "columnCount": column_count,
        "columnWidthsTwips": col_widths,
        "rows": rows_out,
    }


def validate_table_model(model: dict[str, Any]) -> list[str]:
    """表格完整性校验器：第一层 sum(colSpan) 快断言，第二层逻辑列 occupancy 精确断言。

    occupancy 口径镜像 HTML 渲染：merged continuation 不占位（渲染不生成 td），
    非合并格的 rowSpan>1 向下逐行承载占用。
    """
    warns: list[str] = []
    n = model.get("columnCount", 0)
    for ri, row in enumerate(model.get("rows", [])):
        total = sum(c.get("colSpan", 1) for c in row["cells"] if not c.get("merged"))
        if total + _carried_width(model, ri) != n:
            warns.append(f"row {ri}: span sum {total} != columnCount {n}")
    carried: dict[int, int] = {}  # 逻辑列 → 上方 rowspan 剩余行数
    for ri, row in enumerate(model.get("rows", [])):
        occupancy: dict[int, int] = {}
        for c in row["cells"]:
            if c.get("merged"):
                continue  # 渲染时不生成 td，由上方 rowspan 承载
            for col in range(c["logicalCol"], c["logicalCol"] + c.get("colSpan", 1)):
                occupancy[col] = occupancy.get(col, 0) + 1
        for col in list(carried):
            occupancy[col] = occupancy.get(col, 0) + 1
            carried[col] -= 1
            if carried[col] <= 0:
                del carried[col]
        missing = [col for col in range(n) if occupancy.get(col, 0) == 0]
        dup = [col for col, cnt in occupancy.items() if cnt > 1]
        if missing:
            warns.append(f"row {ri}: logical columns {missing[:8]} not covered")
        if dup:
            warns.append(f"row {ri}: logical columns {dup[:8]} covered more than once")
        for c in row["cells"]:
            if not c.get("merged") and c.get("rowSpan", 1) > 1:
                for col in range(c["logicalCol"], c["logicalCol"] + c.get("colSpan", 1)):
                    carried[col] = c["rowSpan"] - 1
    return warns


def _carried_width(model: dict[str, Any], upto_row: int) -> int:
    """第 upto_row 行之前尚未结束的 rowspan 所覆盖的列数（快断言用）。"""
    carried: dict[int, int] = {}
    for ri, row in enumerate(model.get("rows", [])):
        if ri >= upto_row:
            break
        for c in row["cells"]:
            if c.get("merged"):
                continue
            if c.get("rowSpan", 1) > 1:
                for col in range(c["logicalCol"], c["logicalCol"] + c.get("colSpan", 1)):
                    carried[col] = ri + c["rowSpan"]  # 结束行号（含）
        carried = {col: end for col, end in carried.items() if end > upto_row}
    return len(carried)


def parse_docx(data: bytes) -> ParsedDoc:
    """按文档顺序抽取段落与表格（前附表多为表格，python-docx 的 .paragraphs 会丢表格内容）。

    同时输出结构化表格块及其高亮颜色（标注规则：黄=须知前附表，蓝/青=评标办法前附表，灰=投标文件格式）。
    """
    from collections import Counter

    from docx import Document as DocxDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = DocxDocument(io.BytesIO(data))
    lines: list[str] = []
    doc_tables: list[TableBlock] = []

    def _cell_hl(cell: Any) -> str:
        cnt: Counter[str] = Counter()
        for para in cell.paragraphs:
            for run in para.runs:
                rpr = run._element.rPr
                if rpr is None:
                    continue
                hl = rpr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}highlight")
                if hl is not None:
                    val = hl.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val")
                    if val and val != "none":
                        cnt[{"yellow": "yellow", "cyan": "cyan", "blue": "cyan", "lightGray": "gray", "gray": "gray"}.get(val, val)] += 1
        return cnt.most_common(1)[0][0] if cnt else ""

    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            para = Paragraph(child, doc)
            if para.text.strip():
                lines.append(para.text.strip())
        elif child.tag.endswith("}tbl"):
            t = Table(child, doc)
            hl_counter: Counter[str] = Counter()
            model = parse_table_model(child)
            table = TableBlock(rows=[], start_line=len(lines), model=model)
            for row in t.rows:
                cells: list[str] = []
                for c in row.cells:
                    cells.append(c.text.strip().replace("\n", " "))
                    h = _cell_hl(c)
                    if h:
                        hl_counter[h] += 1
                table.rows.append(cells)
                flat = [c for c in cells if c]
                if flat:
                    lines.append(" | ".join(flat))
            if hl_counter:
                table.hl = hl_counter.most_common(1)[0][0]
            table.end_line = len(lines) - 1
            doc_tables.append(table)
    whole = "\n".join(lines)
    return ParsedDoc(text=whole, pages=[whole], doc_tables=doc_tables)


def parse_pdf(data: bytes) -> ParsedDoc:
    import fitz

    pages: list[str] = []
    with fitz.open(stream=data, filetype="pdf") as pdf:
        pages.extend(page.get_text("text") for page in pdf)
    return ParsedDoc(text="\n".join(pages), pages=pages)


def pdf_has_text(data: bytes, sample_pages: int = 3) -> bool:
    import fitz

    with fitz.open(stream=data, filetype="pdf") as pdf:
        n = min(len(pdf), sample_pages)
        for i in range(n):
            if pdf[i].get_text("text").strip():
                return True
    return n == 0


def parse_txt(data: bytes) -> ParsedDoc:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("gbk", errors="replace")
    return ParsedDoc(text=text, pages=[text])


def parse_table(data: bytes, ext: str) -> ParsedDoc:
    lines: list[str] = []
    if ext in ("xlsx", "xls"):
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for ws in wb.worksheets:
            lines.append(f"[工作表 {ws.title}]")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    lines.append(" | ".join(cells))
        wb.close()
    else:
        text = data.decode("utf-8-sig", errors="replace")
        for row in csv.reader(io.StringIO(text)):
            cells = [c for c in row if c.strip()]
            if cells:
                lines.append(" | ".join(cells))
    whole = "\n".join(lines)
    return ParsedDoc(text=whole, pages=[whole])


class OCREngine:
    """PaddleOCR（v3 API）懒加载单例。CPU 纪律：mobile 模型、关方向分类/扭曲矫正。

    低置信度复核：单页均分 < 0.85 记入 page_conf，由管线落 needs_review。
    """

    _instance: OCREngine | None = None
    _ocr: Any | None = None

    LOW_CONF = 0.85

    @classmethod
    def get(cls) -> OCREngine:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load(self) -> Any:
        if self._ocr is None:
            import os

            # Windows CPU + paddle 3.x：mkldnn PIR 指令未实现（ConvertPirAttribute2RuntimeAttribute），禁用后走普通核
            os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
            from paddleocr import PaddleOCR

            self._ocr = PaddleOCR(
                lang="ch",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                text_detection_model_name="PP-OCRv5_mobile_det",
                text_recognition_model_name="PP-OCRv5_mobile_rec",
                cpu_threads=4,
            )
        return self._ocr

    def ocr_image(self, data: bytes) -> ParsedDoc:
        """单张图片 OCR（JPG/PNG 上传链路）。"""
        import cv2
        import numpy as np

        ocr = self._load()
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return ParsedDoc(text="", pages=[""], page_conf=[0.0])
        result = ocr.predict(img)
        texts: list[str] = []
        scores: list[float] = []
        for res in result:
            texts.extend(str(t) for t in res.get("rec_texts", []))
            scores.extend(float(s) for s in res.get("rec_scores", []))
        return ParsedDoc(
            text="\n".join(texts), pages=["\n".join(texts)],
            page_conf=[round(sum(scores) / len(scores), 4) if scores else 0.0],
        )

    def ocr_pdf(self, data: bytes) -> ParsedDoc:
        import fitz
        import numpy as np

        ocr = self._load()
        pages: list[str] = []
        page_conf: list[float] = []
        with fitz.open(stream=data, filetype="pdf") as pdf:
            for page in pdf:
                pix = page.get_pixmap(dpi=150)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                if pix.n == 4:
                    img = img[:, :, :3]
                result = ocr.predict(img)
                texts: list[str] = []
                scores: list[float] = []
                for res in result:
                    texts.extend(str(t) for t in res.get("rec_texts", []))
                    scores.extend(float(s) for s in res.get("rec_scores", []))
                pages.append("\n".join(texts))
                page_conf.append(round(sum(scores) / len(scores), 4) if scores else 0.0)
        return ParsedDoc(text="\n".join(pages), pages=pages, page_conf=page_conf)


def parse_any(data: bytes, file_type: str) -> ParsedDoc:
    if file_type == "docx":
        return parse_docx(data)
    if file_type == "pdf":
        if pdf_has_text(data):
            return parse_pdf(data)
        return OCREngine.get().ocr_pdf(data)
    if file_type == "scan":
        if data[:4] == b"\x89PNG" or data[:2] == b"\xff\xd8":
            return OCREngine.get().ocr_image(data)
        return OCREngine.get().ocr_pdf(data)
    if file_type == "txt":
        return parse_txt(data)
    if file_type in {".xlsx", ".xls", ".csv"}:
        return parse_table(data, file_type)
    msg = f"不支持的文件类型: {file_type}"
    raise ValueError(msg)


def read_source(doc_source: str, origin_path: str | None, stored_name: str | None) -> bytes:
    if doc_source == "path":
        if not origin_path:
            msg = "归档文档缺少 origin_path"
            raise ValueError(msg)
        data: bytes = Path(origin_path).read_bytes()
        return data
    if not stored_name:
        msg = "上传文档缺少 stored_name"
        raise ValueError(msg)
    from ...config import get_settings

    uploaded: bytes = (get_settings().files_dir / stored_name).read_bytes()
    return uploaded
