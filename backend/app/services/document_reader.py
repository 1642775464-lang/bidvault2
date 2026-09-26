"""E-2B：结构保真阅读器——块模型构建（只读，不落库、不改解析链）。

输入：OfficeDocumentVersion 的 DOCX 字节；输出：按正文顺序的阅读块。
- heading/para/image/table 四类块；表格带 gridSpan/vMerge（与 TableModel 同源语义）；
- para_no：段落序号（与 DocumentOutline 的 para_index 同一口径），供章节导航映射；
- 结构保真：不含分页/页眉页脚（契约明确的局限）。

安全：python-docx 解析受控内部文件；外部输入一律经导入管线安全门禁进入。
"""

from __future__ import annotations

import io
from typing import Any

from .document_outline import _heading_level


def build_reading_blocks(docx_bytes: bytes) -> dict[str, Any]:
    from docx import Document as DocxDocument
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = DocxDocument(io.BytesIO(docx_bytes))
    body = doc.element.body
    blocks: list[dict[str, Any]] = []
    para_no = 0

    def _para_block(p: Paragraph) -> dict[str, Any] | None:
        nonlocal para_no
        text = (p.text or "").strip()
        has_img = child_has_image(p._p)
        level = _heading_level(p)
        out: dict[str, Any] | None = None
        if has_img and not text:
            out = {"type": "image", "text": "[图片]"}
        elif level > 0 and text:
            out = {"type": "heading", "level": level, "text": text}
        elif text or has_img:
            out = {"type": "para", "text": text or "[图片]"}
        if out is not None:
            out["para_no"] = para_no
        para_no += 1
        return out

    def child_has_image(p_el) -> bool:  # noqa: ANN001
        return p_el.find(".//" + qn("w:drawing")) is not None

    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            blk = _para_block(Paragraph(child, doc))
            if blk is not None:
                blk["i"] = len(blocks)
                blocks.append(blk)
        elif child.tag == qn("w:tbl"):
            rows: list[dict[str, Any]] = []
            for tr in Table(child, doc)._tbl.findall(qn("w:tr")):
                cells: list[dict[str, Any]] = []
                texts: list[str] = []
                for tc in tr.findall(qn("w:tc")):
                    tcpr = tc.find(qn("w:tcPr"))
                    gridspan = 1
                    vmerge = ""
                    if tcpr is not None:
                        gs = tcpr.find(qn("w:gridSpan"))
                        if gs is not None:
                            gridspan = int(gs.get(qn("w:val"), "1"))
                        vm = tcpr.find(qn("w:vMerge"))
                        if vm is not None:
                            vmerge = vm.get(qn("w:val"), "continue")
                    text = "".join(node.text or "" for node in tc.iter(qn("w:t"))).strip()
                    texts.append(text)
                    cells.append({"text": text, "gridspan": gridspan, "vmerge": vmerge})
                rows.append({"cells": cells, "blank": not any(t for t in texts)})
                _ = texts
            blocks.append({"i": len(blocks), "type": "table",
                           "para_no": para_no, "rows": rows})
            # 注意：doc.paragraphs 不计表格，此处不递增 para_no（与 outline 同口径）

    return {"version": 1, "blocks": blocks}
