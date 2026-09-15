"""M2 导入管线：queued→parsing→indexing→extracting→indexed，拆解结构项+素材自动入库。"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

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


def process_document(session: Session, doc: Document) -> IngestResult:
    """拆解管线：解析→索引→拆解抽取→素材入库。"""
    try:
        data = read_source(doc.source, doc.origin_path, doc.stored_name)

        doc.status = 1
        session.commit()
        parsed = parse_any(data, doc.file_type)
        doc.content_text = parsed.text[:800_000]

        n = _write_index(session, doc.id, parsed.pages)

        doc.status = 2
        session.commit()

        extracted, made = extract_and_harvest(session, doc)
        doc.status = 2
        session.commit()
        audit("doc_extracted", doc_id=doc.id, detail=f"结构项 {extracted} · 素材 {made}")
        return IngestResult(doc.id, 2, n, extracted, made)
    except Exception as exc:
        logger.exception("ingest failed: %s", doc.filename)
        doc.status = 0
        doc.error_msg = str(exc)[:1000]
        session.commit()
        audit("ingest_failed", doc_id=doc.id, detail=doc.error_msg)
        return IngestResult(doc_id=doc.id, status=0, chunks=0, extracted=0, materials=0, error=str(exc))


def extract_and_harvest(session: Session, doc: Document) -> tuple[int, int]:
    """拆解抽取：结构项+行落库 → 自动生成素材（doc+title 去重）。"""
    from .extractor import extract_structured

    text = doc.content_text or ""
    groups = extract_structured(text, doc.kind)

    existing = {m.title for m in session.query(Material).filter(
        Material.doc_name == doc.filename).all()}

    made = 0
    for gi, g in enumerate(groups):
        item = ExtractItem(
            doc_id=doc.id, group_name=g.group_name, name=g.name, sum=g.sum, seq=gi,
        )
        session.add(item)
        session.flush()
        for ri, (field_name, value) in enumerate(g.rows):
            session.add(ExtractRow(item_id=item.id, field=field_name, value=value, seq=ri))

        title = g.name if len(g.name) <= 24 else g.name[:22] + "…"
        if title not in existing:
            mat = Material(
                title=title,
                type="表格" if g.rows else "文本",
                cat=_guess_cat(g.name),
                src="文档库拆解",
                doc_name=doc.filename,
                project_id=doc.project_id,
                tags_json=json.dumps([g.group_name], ensure_ascii=False),
                rows_json=json.dumps(g.rows, ensure_ascii=False) if g.rows else None,
                text=g.sum if not g.rows else None,
            )
            session.add(mat)
            existing.add(title)
            made += 1
    session.commit()
    return len(groups), made


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
        conn.execute(
            "INSERT INTO chunks (chunk_id, document_id, chunk_index, content, page_no) "
            "VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, doc_id, c.chunk_index, c.content, c.page_no),
        )
    session.commit()
    return len(chunks)


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
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
    session.commit()
