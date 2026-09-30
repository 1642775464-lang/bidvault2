"""M3 图片证据链：PDF 内嵌图提取 → 图片素材自动入库 → GLM-4V-Flash 字段识别。

红线：is_secret=1 的文档不调 GLM-4V（涉密零出域），图片仅本地存储待人工补录。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Material
from .audit import audit

logger = logging.getLogger(__name__)

# 小于该尺寸的视为图标/装饰线，不入库
MIN_SIDE = 200
MIN_BYTES = 20 * 1024

_PAGE_CONTEXT_WORDS = [
    ("营业执照", "资质文件"), ("许可证", "资质文件"), ("证书", "资质文件"), ("资质", "资质文件"),
    ("财务", "财务状况"), ("审计", "财务状况"),
    ("业绩", "业绩案例"), ("合同", "业绩案例"), ("验收", "业绩案例"),
    ("团队", "人员团队"), ("简历", "人员团队"), ("身份证", "人员团队"),
    ("技术", "技术方案"), ("方案", "技术方案"),
]


@dataclass(frozen=True)
class ExtractedImage:
    rel_path: str  # 相对 files_dir 的路径（img/{doc_id}/{name}）
    page_no: int | None  # PDF 页号；docx 无页概念为 None
    size: int
    context: str  # 所在页/最近前置标题文本前 120 字（供分类与标题）
    line_no: int | None = None  # docx：图片所在行槽（1-based）；PDF 为 None


def guess_cat(context: str) -> str:
    for w, cat in _PAGE_CONTEXT_WORDS:
        if w in context:
            return cat
    return "资质文件"


def guess_title(context: str, page_no: int) -> str:
    for w, _ in _PAGE_CONTEXT_WORDS:
        if w in context:
            return f"{w}（图片证据 P{page_no}）"
    return f"图片证据（P{page_no}）"


def extract_images(pdf_data: bytes, doc_id: str) -> list[ExtractedImage]:
    """提取 PDF 内嵌大图，落盘 files/img/{doc_id}/，返回清单。"""
    import fitz

    from ..config import get_settings

    out_dir = get_settings().files_dir / "img" / doc_id
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[ExtractedImage] = []
    seen_xrefs: set[int] = set()
    with fitz.open(stream=pdf_data, filetype="pdf") as pdf:
        for pno, page in enumerate(pdf, start=1):
            try:
                imgs = page.get_images(full=True)
            except Exception:
                continue
            context = (page.get_text("text") or "")[:120]
            for img in imgs:
                xref = img[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    info = pdf.extract_image(xref)
                except Exception:
                    continue
                if info["width"] < MIN_SIDE or info["height"] < MIN_SIDE or len(info["image"]) < MIN_BYTES:
                    continue
                ext = info["ext"] if info["ext"] in ("png", "jpg", "jpeg") else "png"
                name = f"p{pno:03d}_x{xref}.{ext}"
                (out_dir / name).write_bytes(info["image"])
                results.append(ExtractedImage(
                    rel_path=f"img/{doc_id}/{name}", page_no=pno,
                    size=len(info["image"]), context=context,
                ))
    return results


def harvest_image_materials(session: Session, doc_id: str, filename: str,
                            project_id: str | None, images: list[ExtractedImage]) -> int:
    """图片证据 → 素材（doc+file_name 去重）。返回新增数。"""
    existing = {
        m.file_name
        for m in session.query(Material).filter(
            Material.doc_name == filename, Material.type == "图片").all()
    }
    made = 0
    for im in images:
        if im.rel_path in existing:
            continue
        session.add(Material(
            title=guess_title(im.context, im.page_no),
            type="图片", cat=guess_cat(im.context), src="文档库拆解",
            doc_name=filename, project_id=project_id,
            tags_json=json.dumps(["图片证据", f"P{im.page_no}"], ensure_ascii=False),
            file_name=im.rel_path, file_size=f"{im.size / 1024:.0f} KB",
            source_page=im.page_no,
        ))
        existing.add(im.rel_path)
        made += 1
    session.commit()
    if made:
        audit("image_materials_harvested", doc_id=doc_id, detail=f"{made} 张图片证据入库")
    return made


def img_file_path(rel_path: str) -> Path | None:
    """相对 files_dir 的路径 → 实际文件；防路径穿越，越界返回 None。"""
    from ..config import get_settings

    base = get_settings().files_dir.resolve()
    target: Path = (base / rel_path).resolve()
    if target != base and base not in target.parents:
        return None
    if not target.is_file():
        return None
    return target


def safe_img_rel(doc_id: str, name: str) -> str | None:
    """拼接前校验，杜绝 .. 穿越。"""
    if "/" in name or "\\" in name or ".." in name:
        return None
    return f"img/{doc_id}/{name}"


def _image_size(blob: bytes) -> tuple[int, int] | None:
    """从图片字节头部读取 (宽, 高)——纯 Python，不依赖 cv2/PIL（安装态 runtime 无 opencv）。

    支持 PNG / JPEG（docx media 的绝对主流形态）；无法识别返回 None（该图不入库）。
    """
    import struct

    if len(blob) >= 24 and blob[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            w, h = struct.unpack(">II", blob[16:24])
            return int(w), int(h)
        except struct.error:
            return None
    if len(blob) >= 4 and blob[:2] == b"\xff\xd8":  # JPEG
        i = 2
        n = len(blob)
        while i + 9 < n:
            if blob[i] != 0xFF:
                i += 1
                continue
            marker = blob[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker == 0xD9 or marker == 0xDA:  # EOI / SOS：之后无可读尺寸段
                return None
            seg_len = struct.unpack(">H", blob[i + 2:i + 4])[0]
            if seg_len < 2:
                return None
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):  # SOF：含尺寸
                try:
                    h, w = struct.unpack(">HH", blob[i + 5:i + 9])
                    return int(w), int(h)
                except struct.error:
                    return None
            i += 2 + seg_len
        return None
    return None


def extract_docx_images(docx_data: bytes, doc_id: str,
                        image_refs: tuple[tuple[int, str], ...],
                        lines: list[str]) -> list[ExtractedImage]:
    """提取 DOCX 内嵌照片级图片（契约 v1.3）：落盘 files/img/{doc_id}/，返回带行号的清单。

    rId → media 解析走 word/_rels/document.xml.rels；阈值同 M3（≥20KB 且最短边 ≥200px）；
    同文档内容 sha256 去重（同一证书图多处引用只收一次）；context 取最近前置非空行
    （通常是所属标题，如「身份证」），供分类与素材内标题。
    """
    import io as _io
    import re as _re
    import zipfile as _zipfile
    import hashlib as _hashlib

    from ..config import get_settings

    out_dir = get_settings().files_dir / "img" / doc_id
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[ExtractedImage] = []
    seen_hash: set[str] = set()
    try:
        zf = _zipfile.ZipFile(_io.BytesIO(docx_data))
    except Exception:
        return results

    # rId → media 路径（document.xml.rels）。rels 来自上传文档，属不可信 XML：
    # 不做 XML 解析（防实体扩展），用严格白名单正则提取扁平的 Relationship 属性对，
    # Target 限定 media/ 下且字符集白名单（拒绝实体/路径穿越），并限制输入大小。
    rid_target: dict[str, str] = {}
    try:
        rels_text = zf.read("word/_rels/document.xml.rels").decode("utf-8", errors="replace")
    except Exception:
        return results
    if len(rels_text) <= 262_144 and "<!DOCTYPE" not in rels_text.upper() and "<!ENTITY" not in rels_text.upper():
        _VAL = r'"([^"&<>]*)"'
        for m in _re.finditer(r"<Relationship\b[^>]*>", rels_text):
            tag = m.group(0)
            mid = _re.search(r"\bId=" + _VAL, tag)
            mtg = _re.search(r"\bTarget=" + _VAL, tag)
            if not mid or not mtg:
                continue
            rid, target = mid.group(1), mtg.group(1).strip()
            if rid and target.startswith("media/") and _re.fullmatch(
                    r"media/[A-Za-z0-9._\- ]+", target) and ".." not in target:
                rid_target[rid] = "word/" + target

    def _context_for(slot: int) -> str:
        for back in range(0, 4):
            i = slot - back
            if 0 <= i < len(lines) and lines[i].strip():
                return lines[i].strip()[:120]
        return ""

    names = set(zf.namelist())
    for slot, rid in image_refs:
        target = rid_target.get(rid)
        if not target or target not in names:
            continue
        try:
            blob = zf.read(target)
        except Exception:
            continue
        if len(blob) < MIN_BYTES:
            continue
        digest = _hashlib.sha256(blob).hexdigest()
        if digest in seen_hash:
            continue
        dims = _image_size(blob)
        if dims is None or min(dims) < MIN_SIDE:
            continue
        seen_hash.add(digest)
        ext = "png" if target.lower().endswith(".png") else "jpg"
        name = f"L{slot + 1:04d}_{digest[:10]}.{ext}"
        (out_dir / name).write_bytes(blob)
        results.append(ExtractedImage(
            rel_path=f"img/{doc_id}/{name}", page_no=None,
            size=len(blob), context=_context_for(slot), line_no=slot + 1,
        ))
    return results
