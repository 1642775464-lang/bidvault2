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
    page_no: int
    size: int
    context: str  # 所在页文本前 120 字（供分类与标题）


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
