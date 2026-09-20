"""M2 导入管线：queued→parsing→indexing→extracting→indexed，拆解结构项+素材自动入库。"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models import Document, ExtractItem, ExtractRow, Material
from ..audit import audit
from .parser import parse_any, read_source

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestResult:
    doc_id: str
    status: int
    chunks: int
    extracted: int
    materials: int
    error: str | None = None


def load_pending(session: Session, limit: int = 1) -> list[Document]:
    return list(
        session.query(Document)
        .filter(Document.status == 0)
        .order_by(Document.created_at)
        .limit(limit)
        .all()
    )


def process_document(session: Session, doc: Document, *, version_id: str | None = None) -> IngestResult:
    """拆解管线：解析→索引→拆解抽取→素材入库。

    version_id 给定时（ONLYOFFICE 保存触发的重拆）：按该版本文件解析，
    且落库前校验 doc.current_version_id 仍指向该版本，旧版本结果绝不覆盖新版本。
    """
    try:
        if version_id:
            from ...models import DocumentVersion

            v = session.get(DocumentVersion, version_id)
            if v is None:
                raise ValueError(f"版本不存在：{version_id}")
            from ...config import get_settings

            data = (get_settings().files_dir / v.file_path).read_bytes()
        else:
            data = read_source(doc.source, doc.origin_path, doc.stored_name)

        doc.status = 1
        session.commit()
        parsed = parse_any(data, doc.file_type)
        doc.content_text = parsed.text[:800_000]
        if parsed.page_conf:
            doc.ocr_conf = round(sum(parsed.page_conf) / len(parsed.page_conf), 4)
            doc.needs_review = 1 if any(c < 0.85 for c in parsed.page_conf) else 0
            audit("ocr_done", doc_id=doc.id,
                  detail=f"pages {len(parsed.page_conf)} · conf {doc.ocr_conf} · review {doc.needs_review}")

        # 防串写：清索引/落库前，当前版本必须仍指向本次解析的版本
        if version_id:
            fresh = session.execute(
                text("SELECT current_version_id FROM documents WHERE id = :i"),
                {"i": doc.id},
            ).scalar()
            if fresh != version_id:
                logger.warning("stale version parse skipped: doc=%s parsed=%s current=%s", doc.id, version_id, fresh)
                audit("reextract_stale_skipped", doc_id=doc.id, detail=f"parsed v={version_id} current={fresh}")
                return IngestResult(doc.id, doc.status, 0, 0, 0, error="stale-version-skipped")

        purge_document_index(session, doc.id)
        n = _write_index(session, doc.id, parsed.pages)

        extracted, made = extract_and_harvest(session, doc, parsed)
        img_made = 0
        if doc.file_type == "pdf":
            try:
                from ..images import extract_images, harvest_image_materials

                imgs = extract_images(data, doc.id)
                img_made = harvest_image_materials(session, doc.id, doc.filename, doc.project_id, imgs)
            except Exception:
                logger.warning("image extraction failed: %s", doc.filename, exc_info=True)
        doc.status = 2
        session.commit()
        audit("doc_extracted", doc_id=doc.id, detail=f"结构项 {extracted} · 素材 {made + img_made}")
        return IngestResult(doc.id, 2, n, extracted, made + img_made)
    except Exception as exc:
        logger.exception("ingest failed: %s", doc.filename)
        doc.status = 0
        doc.error_msg = str(exc)[:1000]
        session.commit()
        audit("ingest_failed", doc_id=doc.id, detail=doc.error_msg)
        return IngestResult(doc_id=doc.id, status=0, chunks=0, extracted=0, materials=0, error=str(exc))


def extract_and_harvest(session: Session, doc: Document, parsed: Any = None) -> tuple[int, int]:
    """拆解抽取：规则锚点区间原样复制 → 结构项+行落库 → 素材入库（去重）。"""
    from .extractor import extract_structured

    text = doc.content_text or ""
    groups = extract_structured(text, doc.kind, parsed.doc_tables if parsed else None)
    # 过滤全空段落行；V2 表格行（dict）一律保留——表格内空行/合并结构不得删除
    def _row_keep(r: Any) -> bool:
        if isinstance(r, dict):
            return True
        return any(str(c).strip() for c in r)

    for g in groups:
        g.rows = [r for r in g.rows if _row_keep(r)]
    groups = [g for g in groups if g.rows]

    # 重新拆解：先清空旧结构项与行（素材由标题去重自行保护）
    old_items = session.query(ExtractItem).filter(ExtractItem.doc_id == doc.id).all()
    for it in old_items:
        session.query(ExtractRow).filter(ExtractRow.item_id == it.id).delete()
    session.query(ExtractItem).filter(ExtractItem.doc_id == doc.id).delete()
    session.commit()

    # ============================================================
    # IMPORTANT
    #
    # 原始 Word 拆解必须严格保持源文档结构（原样复制制，见 PRD v1.3）。
    #
    # LLM 不允许添加、删除、合并、重排 g.rows——此前在此处合并 LLM 抽取行，
    # 会把「招标人：XXX」改写成两列行，导致正文段落被渲染成表格，已禁用。
    #
    # 如果未来需要 LLM 做语义分类，应写入独立字段，不能修改原始 rows。
    # ============================================================

    existing = {m.title: m for m in session.query(Material).filter(
        Material.doc_name == doc.filename).all()}

    made = 0
    for gi, g in enumerate(groups):
        item = ExtractItem(
            doc_id=doc.id, group_name=g.group_name, name=g.name, sum=g.sum, seq=gi,
        )
        session.add(item)
        session.flush()
        # cells_json 存整行原始全列（含空单元格，页面按列数还原段落/表格）；
        # field/value 存合成值，仅服务于历史检索与检测兼容，不影响正文是否入库。
        for ri, r in enumerate(g.rows):
            if isinstance(r, dict):
                # V2 表格行：空行/合并结构原样保留；field/value 仅作检索检测兼容
                texts = [
                    str(c.get("text", "")).strip()
                    for c in r.get("cells", []) if not c.get("merged")
                ]
                texts = [t for t in texts if t]
                fname = " ".join(texts[:-1]) if len(texts) > 1 else (texts[0] if texts else "表格内容")
                fval = texts[-1] if texts else ""
                session.add(ExtractRow(
                    item_id=item.id, field=fname[:120] or "表格内容", value=fval[:2000], seq=ri,
                    cells_json=json.dumps(r, ensure_ascii=False),
                ))
                continue
            if not isinstance(r, (list, tuple)):
                continue
            original_cells = [str(c).strip() for c in r]
            if not any(original_cells):
                continue
            effective_cells = [c for c in original_cells if c]
            if not effective_cells:
                continue
            if len(original_cells) == 1:
                # Word 正文 paragraph：field/value 仅作检索兼容，正文本身必须入库
                text_value = original_cells[0]
                m = re.match(r"^([^：:]{2,40})[:：]\s*(.+)$", text_value)
                if m:
                    fname = m.group(1).strip()
                    fval = m.group(2).strip()
                else:
                    fname = "正文"
                    fval = text_value
            else:
                # Word 表格 row
                non_empty_cells = [c for c in original_cells if c]
                if len(non_empty_cells) == 1:
                    fname = "表格内容"
                    fval = non_empty_cells[0]
                else:
                    fname = " ".join(non_empty_cells[:-1])
                    fval = non_empty_cells[-1]
            session.add(ExtractRow(
                item_id=item.id, field=fname[:120], value=fval[:2000], seq=ri,
                cells_json=json.dumps(original_cells, ensure_ascii=False),
            ))

        title = g.name if len(g.name) <= 24 else g.name[:22] + "…"
        new_rows = json.dumps(g.rows, ensure_ascii=False) if g.rows else None
        if title in existing:
            # 重新拆解：刷新同名素材内容（保留引用计数与分类）
            mat = existing[title]
            mat.rows_json = new_rows or mat.rows_json
            mat.text = (g.sum or mat.text) if not g.rows else None
            if g.rows:
                mat.type = _detect_rows_material_type(g.rows)
        else:
            mat = Material(
                title=title,
                type=_detect_rows_material_type(g.rows),
                cat=_guess_cat(g.name),
                src="文档库拆解",
                doc_name=doc.filename,
                project_id=doc.project_id,
                tags_json=json.dumps([g.group_name], ensure_ascii=False),
                rows_json=new_rows,
                text=g.sum if not g.rows else None,
            )
            session.add(mat)
            existing[title] = mat
            made += 1
    session.commit()
    return len(groups), made


def _detect_rows_material_type(rows: list[Any]) -> str:
    """素材类型最小修正：存在 V2 表格行或多列行才算「表格」，纯单列行是「文本」。"""
    has_table = any(
        (isinstance(r, dict) and r.get("type") == "table")
        or (isinstance(r, (list, tuple)) and len(r) > 1)
        for r in rows
    )
    return "表格" if has_table else "文本"


def _guess_cat(name: str) -> str:
    if "身份证明" in name or "授权" in name or "基本情况" in name:
        return "资质文件"
    if "财务" in name:
        return "财务状况"
    if "业绩" in name or "供货" in name or "承接" in name:
        return "业绩案例"
    if "团队" in name or "简历" in name:
        return "人员团队"
    return "投标格式"


def _write_index(session: Session, doc_id: str, pages: list[str]) -> int:
    from .indexer import chunk_text

    chunks = chunk_text(pages)
    conn = session.connection().connection
    for c in chunks:
        cid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO chunks (chunk_id, document_id, chunk_index, content, page_no) "
            "VALUES (?, ?, ?, ?, ?)",
            (cid, doc_id, c.chunk_index, c.content, c.page_no),
        )
        try:
            from ..search.embedder import embed

            conn.execute(
                "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                (cid, json.dumps(embed(c.content))),
            )
        except Exception:
            pass
    session.commit()
    return len(chunks)


def backfill_embeddings(session: Session, *, limit: int = 500) -> int:
    """为缺向量的存量切片补 embedding（启动时调用，幂等）。"""
    conn = session.connection().connection
    try:
        rows = conn.execute(
            "SELECT chunk_id, content FROM chunks "
            "WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks_vec) LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception:
        return 0
    if not rows:
        return 0
    from ..search.embedder import embed

    n = 0
    for chunk_id, content in rows:
        try:
            conn.execute(
                "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, json.dumps(embed(content))),
            )
            n += 1
        except Exception:
            break
    session.commit()
    return n


def requeue_failed(session: Session, doc_id: str) -> bool:
    doc = session.get(Document, doc_id)
    if doc is None or doc.status != 0:
        return False
    doc.status = 0
    doc.error_msg = None
    session.commit()
    return True


def purge_document_index(session: Session, doc_id: str) -> None:
    conn = session.connection().connection
    conn.execute(
        "DELETE FROM chunks_vec WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE document_id = ?)",
        (doc_id,),
    )
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
    session.commit()
