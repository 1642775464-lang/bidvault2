"""F1.2 文档解析：docx / pdf / txt / 表格 / 扫描件分流。"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ParsedDoc:
    text: str
    pages: list[str]  # 每页文本（page_no = 下标+1）；docx/表格整篇一页


def parse_docx(data: bytes) -> ParsedDoc:
    from docx import Document as DocxDocument

    doc = DocxDocument(io.BytesIO(data))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    whole = "\n".join(paragraphs)
    return ParsedDoc(text=whole, pages=[whole])


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
    """PaddleOCR mobile 懒加载单例。[ai] 未装时抛错，由管线落 failed。"""

    _instance: OCREngine | None = None
    _ocr: Any | None = None

    @classmethod
    def get(cls) -> OCREngine:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load(self) -> Any:  # pragma: no cover
        if self._ocr is None:
            from paddleocr import PaddleOCR

            self._ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        return self._ocr

    def ocr_pdf(self, data: bytes) -> ParsedDoc:  # pragma: no cover
        import fitz
        import numpy as np

        ocr = self._load()
        pages: list[str] = []
        with fitz.open(stream=data, filetype="pdf") as pdf:
            for page in pdf:
                pix = page.get_pixmap(dpi=200)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                if pix.n == 4:
                    img = img[:, :, :3]
                result = ocr.ocr(img, cls=True)
                lines: list[str] = []
                if result and result[0]:
                    lines.extend(str(line[1][0]) for line in result[0])
                pages.append("\n".join(lines))
        return ParsedDoc(text="\n".join(pages), pages=pages)


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
