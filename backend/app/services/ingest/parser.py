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
    """docx 表格结构化块：rows 行列文本 + 在正文行流中的行号范围 + 高亮颜色（有标注文档时）。"""

    rows: list[list[str]]
    hl: str = ""  # yellow | cyan | gray | ""（众数高亮色，仅标注过的文档有）
    start_line: int = 0  # 表格首行在正文 lines 中的位置
    end_line: int = 0  # 表格末行位置（含）


@dataclass(frozen=True)
class ParsedDoc:
    text: str
    pages: list[str]  # 每页文本（page_no = 下标+1）；docx/表格整篇一页
    page_conf: list[float] = dataclasses_field(default_factory=list)  # OCR 页均置信度；非 OCR 为空
    doc_tables: list[TableBlock] = dataclasses_field(default_factory=list)  # docx 专属


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
            table = TableBlock(rows=[], start_line=len(lines))
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
    if ext in (".xlsx", ".xls"):
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
