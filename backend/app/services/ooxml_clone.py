"""OOXMLCloneService（阶段六 B-1b）：Material 独立 DOCX 高保真生成。

策略（批准版）：完整 clone source DOCX package → 仅裁剪 word/document.xml body
（保留素材范围节点 + section 边界 + 合法 final sectPr）→ 其余 parts 全量保留
（styles/numbering/theme/fontTable/settings/rels/media/headers/footers/footnotes/customXml，
第一版冗余安全，不做 relationship/media 裁剪优化）。

禁止 python-docx / cells_json 重建素材正文。
"""

from __future__ import annotations

import copy
import io
import re
import zipfile
from typing import Any

from lxml import etree

from .xml_safety import safe_parse_xml

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _find_sectpr_in_p(el: Any) -> Any | None:
    ppr = el.find(_W + "pPr")
    if ppr is not None:
        return ppr.find(_W + "sectPr")
    return None


def clone_material(docx_bytes: bytes, child_range: tuple[int, int],
                   title: str) -> bytes:
    """从 source DOCX 复制出素材独立 DOCX。

    child_range：body 顶层节点索引（含两端）；历史版本文件不可变——只读取，
    产物写入新文件（由调用方负责）。
    """
    zin = zipfile.ZipFile(io.BytesIO(docx_bytes))
    doc_xml = zin.read("word/document.xml")
    root = safe_parse_xml(doc_xml)
    body = root.find(_W + "body")
    children = list(body)
    lo, hi = child_range
    if lo < 0 or hi >= len(children) or lo > hi:
        raise ValueError(f"非法 child_range：({lo}, {hi})，body 共 {len(children)} 个顶层节点")

    keep_nodes: list[Any] = []
    final_sectpr: Any | None = None
    inner_sectprs: list[Any] = []
    # 素材起点所在 section 的页面设置 = 素材首节点之后遇到的第一个 sectPr
    start_sectpr: Any | None = None
    for idx in range(lo, len(children)):
        child = children[idx]
        if idx <= hi:
            sp = _find_sectpr_in_p(child)
            if sp is not None:
                inner_sectprs.append(copy.deepcopy(sp))
            keep_nodes.append(child)
        else:
            if start_sectpr is None:
                sp = _find_sectpr_in_p(child)
                if sp is not None:
                    start_sectpr = copy.deepcopy(sp)
    # body 末尾直接子元素 sectPr（文档 final sectPr）
    tail_sectpr = body.find(_W + "sectPr")
    if tail_sectpr is not None:
        final_sectpr = copy.deepcopy(tail_sectpr)
    # final sectPr 优先级：源文档 body 尾 sectPr > 素材起点所在节 sectPr > 范围内最后一个 sectPr > 默认 A4
    chosen = final_sectpr if final_sectpr is not None else start_sectpr
    if chosen is None and inner_sectprs:
        chosen = inner_sectprs[-1]
    if chosen is None:
        chosen = safe_parse_xml(
            f'<w:sectPr xmlns:w="{_W}"><w:pgSz w:w="11906" w:h="16838"/>'
            f'<w:pgMar w:top="1440" w:right="1800" w:bottom="1440" w:left="1800"/></w:sectPr>'
        )

    # 重建 body：保留节点（含范围内 inner sectPr 原位）+ final sectPr
    for child in list(body):
        body.remove(child)
    for node in keep_nodes:
        body.append(node)
    body.append(copy.deepcopy(chosen))

    new_doc_xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    # core.xml 更新标题（若存在）
    new_core: bytes | None = None
    if "docProps/core.xml" in zin.namelist():
        core = zin.read("docProps/core.xml").decode("utf-8", errors="ignore")
        if "<dc:title>" in core:
            core = re.sub(r"<dc:title>.*?</dc:title>", f"<dc:title>{title}</dc:title>", core, flags=re.S)
        elif "</cp:coreProperties>" in core:
            core = core.replace("</cp:coreProperties>", f"<dc:title>{title}</dc:title></cp:coreProperties>")
        new_core = core.encode("utf-8")

    # 重打包：document.xml 用新内容，其余 parts 原样保留（冗余安全策略）
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == "word/document.xml":
                zout.writestr(item, new_doc_xml)
            elif item.filename == "docProps/core.xml" and new_core is not None:
                zout.writestr(item, new_core)
            else:
                zout.writestr(item, zin.read(item.filename))
    return out.getvalue()


def table_structure_summary(docx_bytes: bytes, tbl_index: int = 0) -> dict[str, Any]:
    """clone 产物结构断言辅助：第 tbl_index 张表的行/格/合并描述。"""
    z = zipfile.ZipFile(io.BytesIO(docx_bytes))
    root = safe_parse_xml(z.read("word/document.xml"))
    body = root.find(_W + "body")
    tbls = body.findall(_W + "tbl")
    if tbl_index >= len(tbls):
        raise IndexError(f"clone 产物只有 {len(tbls)} 张表")
    tbl = tbls[tbl_index]
    rows = []
    for tr in tbl.findall(_W + "tr"):
        cells = []
        for tc in tr.findall(_W + "tc"):
            span_el = tc.find(f"{_W}tcPr/{_W}gridSpan")
            span = int(span_el.get(_W + "val", "1")) if span_el is not None else 1
            vm_el = tc.find(f"{_W}tcPr/{_W}vMerge")
            vm = "restart" if (vm_el is not None and (vm_el.get(_W + "val") or "") == "restart") else (
                "cont" if vm_el is not None else "")
            cells.append({"text": "".join(t.text or "" for t in tc.iter(_W + "t")).strip()[:20],
                          "span": span, "vmerge": vm})
        rows.append(cells)
    grid = [int(g.get(_W + "w", "0")) for g in tbl.findall(f"{_W}tblGrid/{_W}gridCol")]
    return {"rows": rows, "grid_cols": len(grid)}
