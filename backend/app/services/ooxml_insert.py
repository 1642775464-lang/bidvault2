"""C-3b：Material → Workspace OOXML 包级插入引擎。

原则（阶段六C3a-插入契约设计.md §七）：
- 包级 clone：Material body 子树整体移植进 Workspace 包，不重画内容（禁 python-docx 重建表格）；
- 依赖重建：relationship 重分配（禁复用 Material rId、禁覆盖 Workspace rId）、media 落盘不覆盖、
  styles 按「styleId + 归一化定义」判冲突（冲突→改名复制）、numbering 一律分配新 id；
- section：Material 最终 body sectPr 不插入；正文出现段级/中间分节 → 422 显式拒绝；
  Workspace final sectPr 原样保留，插入点=final sectPr 之前；
- 显式拒绝而非生成损坏文档：所有解析走 xml_safety（拒绝 DOCTYPE/ENTITY）。

版本与审计的持久化在 WorkspaceMutationService（本引擎只产出新 DOCX bytes，失败即无副作用）。
"""

from __future__ import annotations

import copy
import hashlib
import io
import re
import zipfile
from typing import Any

from lxml import etree

from .workspace_mutation import InsertPlan, UnsupportedMaterialStructureError
from .xml_safety import safe_parse_xml

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_RID_ATTRS = tuple(f"{_R}{name}" for name in ("id", "embed", "link", "href", "pict", "dm", "lo", "qs", "cs"))
_STYLE_REF_TAGS = (_W + "pStyle", _W + "rStyle", _W + "tblStyle")

_MEDIA_CT = {
    "png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "gif": "image/gif",
    "bmp": "image/bmp", "tiff": "image/tiff", "emf": "image/x-emf", "wmf": "image/x-wmf",
}


def _rid_numeric(rid: str) -> int:
    m = re.fullmatch(r"rId(\d+)", rid or "")
    return int(m.group(1)) if m else 0


def _norm_def(el: Any) -> tuple[Any, ...]:
    """style 定义的归一化形态（判同用）：递归 (tag, attrs, text, children)。"""
    attrs = tuple(sorted(
        (k, v) for k, v in el.attrib.items()
        if not k.startswith("{http://www.w3.org/2000/xmlns/}")
    ))
    return (el.tag, attrs, (el.text or "").strip(), tuple(_norm_def(c) for c in el))


def _part_path(target: str) -> str:
    """word/ 内 rel target → 包内绝对路径（`../` 合法，如 customXml；仅用于读/校验）。"""
    if target.startswith("/"):
        return target[1:]
    out = ["word"]
    for seg in target.split("/"):
        if seg in (".", ""):
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    return "/".join(out)


def _alloc_media_name(existing: set[str], part_path: str) -> str:
    """media 落盘名：保留扩展名，序号递增，绝不覆盖 Workspace 已有文件。"""
    ext = part_path.rsplit(".", 1)[-1]
    idx = 1
    while True:
        candidate = f"word/media/material_image{idx}.{ext}"
        if candidate not in existing:
            return candidate
        idx += 1


def _add_rel(rels_root: Any, rid: str, rel_type: str, target: str, mode: str) -> None:
    # nsmap={None: uri}：沿用包内默认命名空间前缀（无前缀序列化），避免 lxml 生成 ns0: 别名
    rel = etree.SubElement(rels_root, f"{{{_PKG_REL}}}Relationship",
                           nsmap={None: _PKG_REL})
    rel.set("Id", rid)
    rel.set("Type", rel_type)
    rel.set("Target", target)
    if mode == "External":
        rel.set("TargetMode", "External")


class OOXMLInsertEngine:
    """MutationEngine 协议实现：mutate(plan, workspace_bytes, material_bytes) -> 新 DOCX bytes。"""

    def mutate(self, plan: InsertPlan, workspace_bytes: bytes, material_bytes: bytes) -> bytes:  # noqa: ARG002
        ws_zip = zipfile.ZipFile(io.BytesIO(workspace_bytes))
        mat_zip = zipfile.ZipFile(io.BytesIO(material_bytes))
        ws_names = set(ws_zip.namelist())

        # ---------------- 1. 解析双方 document.xml ----------------
        ws_root = safe_parse_xml(ws_zip.read("word/document.xml"))
        ws_body = ws_root.find(_W + "body")
        ws_children = list(ws_body)
        if not ws_children or ws_children[-1].tag != _W + "sectPr":
            raise UnsupportedMaterialStructureError(
                ["workspace_final_sectpr_missing"], "Workspace 缺少合法 final sectPr，拒绝盲插")
        final_sectpr = ws_children[-1]
        ws_grid_before = sum(1 for _ in ws_body.iter(_W + "gridSpan"))
        ws_vmerge_before = sum(1 for _ in ws_body.iter(_W + "vMerge"))

        mat_root = safe_parse_xml(mat_zip.read("word/document.xml"))
        mat_body = mat_root.find(_W + "body")
        mat_children = list(mat_body)
        if mat_children and mat_children[-1].tag == _W + "sectPr":
            mat_children = mat_children[:-1]  # Material 自身 final sectPr 不插入
        if not mat_children:
            return workspace_bytes  # 空素材：无内容变化（由上层 MutationNoChange 兜底）

        # ---------------- 2. 结构筛查（拒绝清单，不静默降级） ----------------
        features: list[str] = []
        if any(child.tag == _W + "sectPr" for child in mat_children):
            features.append("section_break")
        if mat_body.findall(f".//{_W}pPr/{_W}sectPr"):
            features.append("section_break")
        if mat_body.findall(f".//{_W}footnoteReference") or mat_body.findall(f".//{_W}endnoteReference"):
            features.append("footnote_endnote")
        if mat_body.findall(f".//{_W}commentReference"):
            features.append("comments")
        if mat_body.findall(f".//{_W}ins") or mat_body.findall(f".//{_W}del"):
            features.append("tracked_changes")
        if mat_body.findall(f".//{_W}object") or mat_body.findall(f".//{_W}oleObject"):
            features.append("ole_object")
        if mat_body.findall(f".//{_W}sdt"):
            features.append("content_control")
        if features:
            raise UnsupportedMaterialStructureError(features)
        blocks = mat_children

        # ---------------- 3. 收集插入子树的依赖引用 ----------------
        stub = etree.Element(_W + "body")
        for block in blocks:
            stub.append(copy.deepcopy(block))
        used_rids: set[str] = set()
        for node in stub.iter():
            for attr in _RID_ATTRS:
                val = node.get(attr)
                if val:
                    used_rids.add(val)
        style_refs: set[str] = set()
        for tag in _STYLE_REF_TAGS:
            for node in stub.iter(tag):
                val = node.get(_W + "val")
                if val:
                    style_refs.add(val)
        num_refs = set()
        for node in stub.iter(_W + "numId"):
            parent = node.getparent()
            if parent is not None and parent.tag == _W + "numPr":
                val = node.get(_W + "val")
                if val and val != "0":
                    num_refs.add(val)

        replaced: dict[str, bytes] = {}
        added_parts: dict[str, bytes] = {}
        ct_defaults: list[tuple[str, str]] = []
        style_map: dict[str, str] = {}
        num_map: dict[str, str] = {}

        # ---------------- 4. relationship 重映射 + media 复制 ----------------
        ws_rels_name = "word/_rels/document.xml.rels"
        ws_rels = safe_parse_xml(ws_zip.read(ws_rels_name))
        mat_rels: dict[str, tuple[str, str, str]] = {}
        if "word/_rels/document.xml.rels" in ws_names | set(mat_zip.namelist()):
            for rel in safe_parse_xml(mat_zip.read("word/_rels/document.xml.rels")):
                mat_rels[rel.get("Id")] = (
                    rel.get("Type") or "", rel.get("Target") or "",
                    rel.get("TargetMode") or "Internal",
                )

        rid_map: dict[str, str] = {}

        used_material_rids = {_rid_numeric(r) for r in used_rids}

        def _next_rid() -> str:
            used = {_rid_numeric(rel.get("Id") or "") for rel in ws_rels}
            used |= {_rid_numeric(r) for r in rid_map.values()}
            used |= used_material_rids  # 新 id 同时避开双方已有 rId，杜绝恒等映射
            return f"rId{max(used | {0}) + 1}"

        for rid in sorted(used_rids):
            if rid not in mat_rels:
                features.append(f"unresolved_relationship:{rid}")
                continue
            rel_type, target, mode = mat_rels[rid]
            kind = rel_type.rsplit("/", 1)[-1]
            if kind == "hyperlink":
                if mode != "External":
                    features.append("internal_hyperlink")
                    continue
                rid_map[rid] = _next_rid()
                _add_rel(ws_rels, rid_map[rid], rel_type, target, "External")
            elif kind == "image":
                if mode == "External":
                    rid_map[rid] = _next_rid()
                    _add_rel(ws_rels, rid_map[rid], rel_type, target, "External")
                else:
                    part = _part_path(target)
                    if part not in mat_zip.namelist():
                        features.append(f"missing_media:{target}")
                        continue
                    media_name = _alloc_media_name(set(ws_names) | set(added_parts), part)
                    added_parts[media_name] = mat_zip.read(part)
                    ext = media_name.rsplit(".", 1)[-1].lower()
                    if ext in _MEDIA_CT:
                        ct_defaults.append((ext, _MEDIA_CT[ext]))
                    rid_map[rid] = _next_rid()
                    _add_rel(ws_rels, rid_map[rid], rel_type,
                             f"media/{media_name.rsplit('/', 1)[-1]}", "Internal")
            else:
                features.append(f"unsupported_relationship:{kind}")
        if features:
            raise UnsupportedMaterialStructureError(features)

        # ---------------- 5. styles：按「styleId+归一化定义」解析，冲突改名复制 ----------------
        if style_refs:
            if "word/styles.xml" not in ws_names:
                raise UnsupportedMaterialStructureError(["workspace_styles_missing"])
            if "word/styles.xml" not in mat_zip.namelist():
                raise UnsupportedMaterialStructureError(["material_styles_missing"])
            ws_styles = safe_parse_xml(ws_zip.read("word/styles.xml"))
            mat_styles = safe_parse_xml(mat_zip.read("word/styles.xml"))
            ws_defs = {s.get(_W + "styleId"): s for s in ws_styles if s.tag == _W + "style"}
            mat_defs = {s.get(_W + "styleId"): s for s in mat_styles if s.tag == _W + "style"}
            copied: dict[str, Any] = {}
            pending = list(style_refs)
            while pending:
                sid = pending.pop(0)
                if sid in style_map or sid not in mat_defs:
                    continue  # 已处理 / 素材未定义该样式（Word 回退默认，不算错误）
                if sid not in ws_defs:
                    copied[sid] = copy.deepcopy(mat_defs[sid])
                    style_map[sid] = sid
                elif _norm_def(ws_defs[sid]) != _norm_def(mat_defs[sid]):
                    digest = hashlib.sha256(str(_norm_def(mat_defs[sid])).encode()).hexdigest()[:8]
                    new_id = f"Material_{re.sub(r'[^A-Za-z0-9_]', '_', sid)}_{digest}"
                    el = copy.deepcopy(mat_defs[sid])
                    el.set(_W + "styleId", new_id)
                    name_el = el.find(_W + "name")
                    if name_el is not None:
                        name_el.set(_W + "val", new_id)
                    copied[sid] = el
                    style_map[sid] = new_id
                else:
                    style_map[sid] = sid  # 定义一致，直接复用
                dep = copied.get(sid) or mat_defs.get(sid)
                if dep is not None:
                    for ref_tag in (_W + "basedOn", _W + "link", _W + "next"):
                        ref = dep.find(ref_tag)
                        if ref is not None:
                            dep_id = ref.get(_W + "val")
                            if dep_id and dep_id not in ws_defs and dep_id in mat_defs:
                                pending.append(dep_id)
            for el in copied.values():
                _walk_styles(el, {k: v for k, v in style_map.items() if k != v})
                ws_styles.append(el)
            if copied:
                replaced["word/styles.xml"] = etree.tostring(
                    ws_styles, xml_declaration=True, encoding="UTF-8", standalone=True)

        # ---------------- 6. numbering：一律分配新 id ----------------
        numbering_new = False
        if num_refs:
            if "word/numbering.xml" not in mat_zip.namelist():
                raise UnsupportedMaterialStructureError(["material_numbering_missing"])
            mat_num = safe_parse_xml(mat_zip.read("word/numbering.xml"))
            mat_abstract = {a.get(_W + "abstractNumId"): a
                            for a in mat_num if a.tag == _W + "abstractNum"}
            mat_nums = {n.get(_W + "numId"): n for n in mat_num if n.tag == _W + "num"}
            if "word/numbering.xml" in ws_names:
                ws_num = safe_parse_xml(ws_zip.read("word/numbering.xml"))
            else:
                ws_num = etree.Element(_W + "numbering", nsmap={"w": _W[1:-1]})
                numbering_new = True
            used_abs = {int(a.get(_W + "abstractNumId")) for a in ws_num
                        if a.tag == _W + "abstractNum" and (a.get(_W + "abstractNumId") or "").isdigit()}
            used_num = {int(n.get(_W + "numId")) for n in ws_num
                        if n.tag == _W + "num" and (n.get(_W + "numId") or "").isdigit()}
            for nid in sorted(num_refs, key=lambda x: int(x) if x.isdigit() else 10 ** 9):
                num_el = mat_nums.get(nid)
                abs_ref = num_el.find(_W + "abstractNumId") if num_el is not None else None
                abstract = mat_abstract.get(abs_ref.get(_W + "val")) if abs_ref is not None else None
                if abstract is None:
                    continue
                new_abs = (max(used_abs) + 1) if used_abs else 0
                used_abs |= {new_abs}
                new_num = (max(used_num) + 1) if used_num else 1
                used_num |= {new_num}
                abstract_copy: Any = copy.deepcopy(abstract)
                abstract_copy.set(_W + "abstractNumId", str(new_abs))
                num_copy: Any = copy.deepcopy(num_el)
                num_copy.set(_W + "numId", str(new_num))
                abs_in_num = num_copy.find(_W + "abstractNumId")
                if abs_in_num is not None:
                    abs_in_num.set(_W + "val", str(new_abs))
                if numbering_new:
                    ws_num.append(abstract_copy)
                    ws_num.append(num_copy)
                else:
                    first_num = ws_num.find(_W + "num")
                    if first_num is not None:
                        first_num.addprevious(abstract_copy)  # schema 顺序：abstractNum 在 num 之前
                    else:
                        ws_num.append(abstract_copy)
                    ws_num.append(num_copy)
                num_map[nid] = str(new_num)
            if num_map:
                if numbering_new:
                    _add_rel(ws_rels, _next_rid(), _PKG_REL + "/numbering", "numbering.xml", "Internal")
                replaced["word/numbering.xml"] = etree.tostring(
                    ws_num, xml_declaration=True, encoding="UTF-8", standalone=True)

        # ---------------- 7. 应用重映射并插入 final sectPr 之前 ----------------
        for block in blocks:
            cp = copy.deepcopy(block)
            _walk_rids(cp, rid_map)
            _walk_styles(cp, style_map if style_refs else {})
            _walk_nums(cp, num_map)
            final_sectpr.addprevious(cp)

        replaced["word/document.xml"] = etree.tostring(
            ws_root, xml_declaration=True, encoding="UTF-8", standalone=True)
        replaced[ws_rels_name] = etree.tostring(
            ws_rels, xml_declaration=True, encoding="UTF-8", standalone=True)

        # ---------------- 8. Content Types + 组包 ----------------
        ct_new = _finalize_content_types(ws_zip.read("[Content_Types].xml"), ct_defaults, numbering_new)
        if ct_new != ws_zip.read("[Content_Types].xml"):
            replaced["[Content_Types].xml"] = ct_new
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
            for item in ws_zip.infolist():
                if item.filename in replaced or item.filename in added_parts:
                    continue
                zo.writestr(item, ws_zip.read(item.filename))
            for name, data in replaced.items():
                zo.writestr(name, data)
            for name, data in added_parts.items():
                zo.writestr(name, data)
        out_bytes: bytes = out.getvalue()

        # ---------------- 9. 包级验证 ----------------
        _validate_package(out_bytes, expect_total=len(ws_children) + len(blocks),
                          expect_grid=ws_grid_before + sum(1 for _ in _iters(blocks, _W + "gridSpan")),
                          expect_vmerge=ws_vmerge_before + sum(1 for _ in _iters(blocks, _W + "vMerge")))
        return out_bytes


def _iters(blocks: list[Any], tag: str) -> Any:
    for b in blocks:
        yield from b.iter(tag)


def _walk_rids(el: Any, mapping: dict[str, str]) -> None:
    for node in el.iter():
        for attr in _RID_ATTRS:
            val = node.get(attr)
            if val in mapping:
                node.set(attr, mapping[val])


def _walk_styles(el: Any, mapping: dict[str, str]) -> None:
    for node in el.iter():
        if node.tag in _STYLE_REF_TAGS:
            val = node.get(_W + "val")
            if val in mapping:
                node.set(_W + "val", mapping[val])


def _walk_nums(el: Any, mapping: dict[str, str]) -> None:
    for node in el.iter(_W + "numId"):
        parent = node.getparent()
        if parent is not None and parent.tag == _W + "numPr":
            val = node.get(_W + "val")
            if val in mapping:
                node.set(_W + "val", mapping[val])


def _finalize_content_types(data: bytes, media_defaults: list[tuple[str, str]],
                            numbering_new: bool) -> bytes:
    """按需补 [Content_Types].xml：media 扩展 Default、新 numbering part Override。"""
    root = safe_parse_xml(data)
    existing = {d.get("Extension", "").lower() for d in root if d.tag == _CT + "Default"}
    for ext, ct in media_defaults:
        if ext not in existing:
            el = etree.SubElement(root, _CT + "Default",
                                  nsmap={None: _CT[1:-1]})  # 沿用默认命名空间前缀
            el.set("Extension", ext)
            el.set("ContentType", ct)
    if numbering_new and not any(
            o.get("PartName") == "/word/numbering.xml" for o in root if o.tag == _CT + "Override"):
        ov = etree.SubElement(root, _CT + "Override", nsmap={None: _CT[1:-1]})
        ov.set("PartName", "/word/numbering.xml")
        ov.set("ContentType",
               "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml")
    out_ct: bytes = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    return out_ct


def _validate_package(new_bytes: bytes, *, expect_total: int, expect_grid: int,
                      expect_vmerge: int) -> None:
    """包级验证：ZIP / ContentTypes / document.xml / rels target / python-docx / 块与合并守恒。"""
    from docx import Document as Docx

    z = zipfile.ZipFile(io.BytesIO(new_bytes))  # 坏 ZIP 在此抛出
    z.namelist()
    safe_parse_xml(z.read("[Content_Types].xml"))
    root = safe_parse_xml(z.read("word/document.xml"))
    body = root.find(_W + "body")
    total = len(list(body))
    assert total == expect_total, f"块数不守恒：期望 {expect_total}，实际 {total}"
    rels = safe_parse_xml(z.read("word/_rels/document.xml.rels"))
    names = set(z.namelist())
    for rel in rels:
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target") or ""
        part = target if target.startswith("/") else _part_path(target)
        assert part in names, f"relationship target 缺失：{target}"
    Docx(io.BytesIO(new_bytes))  # python-docx 能打开
    assert sum(1 for _ in body.iter(_W + "gridSpan")) == expect_grid, "gridSpan 不守恒"
    assert sum(1 for _ in body.iter(_W + "vMerge")) == expect_vmerge, "vMerge 不守恒"


class AiTextMutator:
    """C-4b-1：确定性 AI 内容写入——提案 text 逐行转普通段落，追加到 final sectPr 之前。

    AI 内容是新生成文本（无格式继承需求），逐行建 w:p/w:r/w:t（空行→空段），
    包级重组复用与 C-3b 相同的「原包拷贝 + 仅改 document.xml」路径，不重画既有内容；
    写入后走同一 PackageValidator（块数守恒）。
    """

    def mutate_append(self, proposal: Any, workspace_bytes: bytes) -> bytes:
        import json as _json

        text = _json.loads(proposal.content_json)["text"]
        lines = text.replace("\r\n", "\n").split("\n")

        ws_zip = zipfile.ZipFile(io.BytesIO(workspace_bytes))
        root = safe_parse_xml(ws_zip.read("word/document.xml"))
        body = root.find(_W + "body")
        children = list(body)
        if not children or children[-1].tag != _W + "sectPr":
            raise UnsupportedMaterialStructureError(
                ["workspace_final_sectpr_missing"], "Workspace 缺少合法 final sectPr，拒绝写入")
        final_sectpr = children[-1]
        ws_grid = sum(1 for _ in body.iter(_W + "gridSpan"))
        ws_vmerge = sum(1 for _ in body.iter(_W + "vMerge"))

        for line in lines:
            p = etree.SubElement(body, _W + "p")
            r = etree.SubElement(p, _W + "r")
            t_el = etree.SubElement(r, _W + "t")
            t_el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            t_el.text = line
            final_sectpr.addprevious(p)  # 移动到 sectPr 之前，保持段落顺序

        new_doc = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
            for item in ws_zip.infolist():
                zo.writestr(item, new_doc if item.filename == "word/document.xml"
                            else ws_zip.read(item.filename))
        new_bytes: bytes = out.getvalue()
        _validate_package(new_bytes, expect_total=len(children) + len(lines),
                          expect_grid=ws_grid, expect_vmerge=ws_vmerge)
        return new_bytes
