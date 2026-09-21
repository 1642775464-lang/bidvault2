"""SourceLocatorService（阶段六 B-1b）：来源文档 body 区间定位 + 语义规范化 fingerprint。

定位模型：body 顶层节点索引（child_range），不经行号（源重编辑后行号漂移）。
fingerprint 只纳入稳定语义（block 类型 / 文本序列 / 表格行列结构 / gridSpan+vMerge / tblStyle），
明确忽略 w:rsid*、w14:paraId、w14:textId、revision 元数据、relationship id 等易变属性。
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from typing import Any

from lxml import etree

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _text_of(el: Any) -> str:
    return "".join(t.text or "" for t in el.iter(_W + "t"))


def _norm(s: str) -> str:
    return "".join(s.split())


def body_slots(docx_bytes: bytes) -> list[dict[str, Any]]:
    """解析 document.xml，返回 body 顶层节点槽位：
    [{child_index, kind: 'p'|'tbl'|'sectpr'|'other', text, has_sectpr}]。
    """
    root = etree.fromstring(docx_bytes)
    body = root.find(_W + "body")
    slots: list[dict[str, Any]] = []
    for idx, child in enumerate(body):
        tag = child.tag
        if tag == _W + "p":
            slots.append({"child_index": idx, "kind": "p",
                          "text": _norm(_text_of(child)),
                          "has_sectpr": child.find(f"{_W}pPr/{_W}sectPr") is not None})
        elif tag == _W + "tbl":
            slots.append({"child_index": idx, "kind": "tbl",
                          "text": _norm(_text_of(child)), "has_sectpr": False})
        elif tag == _W + "sectPr":
            slots.append({"child_index": idx, "kind": "sectpr", "text": "", "has_sectpr": True})
        else:
            slots.append({"child_index": idx, "kind": "other", "text": "", "has_sectpr": False})
    return slots


def _document_root(docx_bytes: bytes) -> Any:
    """从 DOCX 包读取并解析 word/document.xml（输入是完整包，不是裸 XML）。"""
    z = zipfile.ZipFile(io.BytesIO(docx_bytes))
    return etree.fromstring(z.read("word/document.xml"))


def semantic_fingerprint(docx_bytes: bytes, child_range: tuple[int, int]) -> str:
    """语义规范化 fingerprint：范围内节点的稳定语义哈希。

    纳入：节点类型、规范化文本序列、表格行列结构（每行格数/gridSpan/vMerge）、tblStyle、行 pStyle。
    忽略：w:rsid*、w14:paraId、w14:textId、revision 元数据、relationship id。
    """
    root = _document_root(docx_bytes)
    body = root.find(_W + "body")
    children = list(body)
    lo, hi = child_range
    parts: list[str] = []
    for child in children[lo:hi + 1]:
        if child.tag == _W + "tbl":
            rows_desc = []
            for tr in child.findall(_W + "tr"):
                cells_desc = []
                for tc in tr.findall(_W + "tc"):
                    tcpr = tc.find(_W + "tcPr")
                    span, vmerge = 1, 0
                    if tcpr is not None:
                        gs = tcpr.find(_W + "gridSpan")
                        if gs is not None:
                            span = int(gs.get(_W + "val", "1") or 1)
                        vm = tcpr.find(_W + "vMerge")
                        if vm is not None:
                            vmerge = 2 if (vm.get(_W + "val") or "continue") == "restart" else 1
                    texts = _norm(_text_of(tc))
                    pstyles = "|".join(
                        ps.get(_W + "val", "")
                        for p in tc.findall(_W + "p")
                        for ps in [p.find(f"{_W}pPr/{_W}pStyle")]
                        if ps is not None
                    )
                    cells_desc.append(f"{texts}#{span}#{vmerge}#{pstyles}")
                rows_desc.append("[" + ";".join(cells_desc) + "]")
            tblpr = child.find(_W + "tblPr")
            tbl_style = ""
            if tblpr is not None:
                ts = tblpr.find(_W + "tblStyle")
                if ts is not None:
                    tbl_style = ts.get(_W + "val", "")
            parts.append(f"TBL{tbl_style}:" + "".join(rows_desc))
        elif child.tag == _W + "p":
            parts.append("P:" + _norm(_text_of(child)))
        else:
            parts.append("X")
    payload = f"RANGE{lo}-{hi}::" + "\n".join(parts)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def locate_by_child_range(docx_bytes: bytes, child_range: tuple[int, int]) -> dict[str, Any]:
    """按 child_range 生成 locator（含 fingerprint）。"""
    fp = semantic_fingerprint(docx_bytes, child_range)
    return {"child_range": list(child_range), "fingerprint": fp}


def resolve(docx_bytes: bytes, locator: dict[str, Any]) -> dict[str, Any]:
    """校验 locator 在目标版本上是否仍成立。

    返回 {matched, fingerprint_current, fingerprint_expected, child_range}。
    matched=False 时调用方应提示「来源已变化」，不得静默用错误范围。
    """
    cr = locator.get("child_range")
    expected = locator.get("fingerprint", "")
    if not isinstance(cr, list) or len(cr) != 2:
        raise ValueError("locator 缺少合法 child_range")
    current = semantic_fingerprint(docx_bytes, (int(cr[0]), int(cr[1])))
    return {"matched": current == expected, "fingerprint_current": current,
            "fingerprint_expected": expected, "child_range": [int(cr[0]), int(cr[1])]}


def list_table_ranges(docx_bytes: bytes, min_rows: int = 1) -> list[dict[str, Any]]:
    """列出全部表格及其 child_index（辅助选择素材范围/验收用）。"""
    root = etree.fromstring(docx_bytes)
    body = root.find(_W + "body")
    out = []
    for idx, child in enumerate(body):
        if child.tag == _W + "tbl":
            rows = child.findall(_W + "tr")
            if len(rows) >= min_rows:
                first = _norm(_text_of(rows[0]))[:30]
                out.append({"child_index": idx, "rows": len(rows), "first_row": first})
    return out


def read_package_part(docx_bytes: bytes, part: str) -> bytes:
    z = zipfile.ZipFile(io.BytesIO(docx_bytes))
    return z.read(part)
