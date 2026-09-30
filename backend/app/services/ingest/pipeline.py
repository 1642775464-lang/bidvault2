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

from ...config import get_settings
from ...models import Document, ExtractItem, ExtractRow, Material
from ..audit import audit
from .parser import (
    OCR_UNAVAILABLE,
    OCR_UNAVAILABLE_MSG,
    OcrUnavailableError,
    parse_any,
    read_source,
)

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
    """队首待拆解文档。

    D-1R3：OCR_UNAVAILABLE 阻塞态文档不参与自动重试——否则队首文档每 2 秒
    失败一次，堵住其后全部文档（队头阻塞）。阻塞文档保持 status=0 + 提示文案，
    由用户安装 OCR 扩展后经「重新拆解」（清除 error_msg）显式重排。
    """
    from sqlalchemy import or_

    from .parser import OCR_UNAVAILABLE

    blocked = Document.error_msg.like(f"{OCR_UNAVAILABLE}%")
    return list(
        session.query(Document)
        .filter(Document.status == 0)
        .filter(or_(Document.error_msg.is_(None), ~blocked))
        .order_by(Document.created_at)
        .limit(limit)
        .all()
    )


def process_document(
    session: Session, doc: Document, *, version_id: str | None = None, trigger: str = "manual",
) -> IngestResult:
    """拆解管线：解析→索引→拆解抽取→素材入库。

    version_id 给定时（ONLYOFFICE 保存触发的重拆）：按该版本文件解析，
    且落库前校验 doc.current_version_id 仍指向该版本，旧版本结果绝不覆盖新版本。
    """
    try:
        if version_id:
            from ...models import OfficeDocumentVersion

            v = session.get(OfficeDocumentVersion, version_id)
            if v is None:
                raise ValueError(f"版本不存在：{version_id}")
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
                text("SELECT current_version_id FROM office_documents WHERE id = "
                     "(SELECT office_document_id FROM documents WHERE id = :i)"),
                {"i": doc.id},
            ).scalar()
            if fresh != version_id:
                logger.warning("stale version parse skipped: doc=%s parsed=%s current=%s", doc.id, version_id, fresh)
                audit("reextract_stale_skipped", doc_id=doc.id, detail=f"parsed v={version_id} current={fresh}")
                return IngestResult(doc.id, doc.status, 0, 0, 0, error="stale-version-skipped")

        purge_document_index(session, doc.id)
        n = _write_index(session, doc.id, parsed.pages)

        extracted, made = extract_and_harvest(session, doc, parsed, version_id=version_id,
                                              trigger=trigger, docx_data=data)
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
        # B-1c-4：原始上传即 OfficeDocument v1（唯一写真源=office 表；旧表进入 deprecated read-only）
        try:
            from ...services.source_migration import ensure_source_office_document

            ensure_source_office_document(session, doc)
        except Exception:
            logger.warning("ensure_source_office_document failed: %s", doc.filename, exc_info=True)
        audit("doc_extracted", doc_id=doc.id, detail=f"结构项 {extracted} · 素材 {made + img_made}")
        return IngestResult(doc.id, 2, n, extracted, made + img_made)
    except OcrUnavailableError:
        # D-1R3：OCR 扩展未安装——显式落状态供前端提示，禁止静默假成功/禁止转云端 OCR
        logger.warning("ocr unavailable: %s", doc.filename)
        doc.status = 0
        doc.error_msg = f"{OCR_UNAVAILABLE}::{OCR_UNAVAILABLE_MSG}"
        session.commit()
        audit("ocr_unavailable", doc_id=doc.id, detail="本地 OCR 扩展未安装")
        return IngestResult(doc_id=doc.id, status=0, chunks=0, extracted=0, materials=0, error=OCR_UNAVAILABLE)
    except Exception as exc:
        logger.exception("ingest failed: %s", doc.filename)
        doc.status = 0
        doc.error_msg = str(exc)[:1000]
        session.commit()
        audit("ingest_failed", doc_id=doc.id, detail=doc.error_msg)
        return IngestResult(doc_id=doc.id, status=0, chunks=0, extracted=0, materials=0, error=str(exc))


def extract_and_harvest(
    session: Session, doc: Document, parsed: Any = None, *,
    version_id: str | None = None, trigger: str = "manual",
    docx_data: bytes | None = None,
) -> tuple[int, int]:
    """拆解抽取：规则锚点区间原样复制 → 结构项+行落库 → 素材入库（去重）。

    契约 §四（条款 B）：规则集快照在**执行开始前**解析并冻结——先落
    ExtractExecution(status=running) 并写入快照，再执行抽取；执行期间规则被修改
    不影响本次结果。成功后再写文档 provenance（§6.5）。
    """
    import json as _json
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from ...models import ExtractExecution
    from ..extract_rules import current_version, ensure_rules, rules_hash, resolve_rules
    from .extractor import extract_structured_with_regions

    ensure_rules(session)  # 内置规则在库（幂等），保证首次拆解即有规则可用
    applied = resolve_rules(session, doc.kind)  # 该文档类型的启用规则（seq 升序）
    ruleset_version = current_version(session)
    digest = rules_hash(applied)
    execution = ExtractExecution(
        document_id=doc.id,
        document_version_id=version_id,
        ruleset_version=ruleset_version,
        rules_hash=digest,
        rule_snapshot_json=_json.dumps(applied, ensure_ascii=False),
        trigger=trigger,
        status="running",
    )
    session.add(execution)
    session.commit()  # 快照此刻冻结（此后规则改动不影响本次执行）

    try:
        # 抽取与图片行槽必须同源：优先 parsed.text（本次解析账本，未截断）
        text = parsed.text if parsed is not None and parsed.text else (doc.content_text or "")
        regions = extract_structured_with_regions(
            text, doc.kind, parsed.doc_tables if parsed else None, rules=applied)
        groups = [reg.group for reg in regions]
    except Exception as exc:
        execution.status = "failed"
        execution.error_msg = str(exc)[:1000]
        execution.finished_at = _dt.now(_UTC)
        session.commit()
        raise

    # DOCX 图片证据（契约 v1.3）：照片级内嵌图提取（阈值/hash 去重），随后按区间归属挂素材。
    # 注意：行号码必须与区间判定同源——content_text 可能被截断（80 万字符缓存），
    # 故优先使用本次解析的 parsed.text（与 parsed.doc_image_refs 同一账本）。
    docx_images: list[Any] = []
    if docx_data is not None and parsed is not None and parsed.doc_image_refs:
        try:
            from ..images import extract_docx_images

            docx_images = extract_docx_images(
                docx_data, doc.id, parsed.doc_image_refs, (parsed.text or text).split("\n"))
        except Exception:
            logger.warning("docx image extraction failed: %s", doc.filename, exc_info=True)
    # 过滤全空段落行；V2 表格行（dict）一律保留——表格内空行/合并结构不得删除
    def _row_keep(r: Any) -> bool:
        if isinstance(r, dict):
            return True
        return any(str(c).strip() for c in r)

    pairs: list[tuple[Any, Any]] = []
    for reg in regions:
        reg.group.rows = [r for r in reg.group.rows if _row_keep(r)]
        if reg.group.rows:
            pairs.append((reg.group, reg))
    groups = [g for g, _reg in pairs]

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
    for gi, (g, reg) in enumerate(pairs):
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
        # 契约 v1.3：区间内照片级图片 → 素材 images_json（≤100 张；docx 无页概念用行号）
        region_images_json: str | None = None
        if docx_images and reg.anchor_line is not None:
            lo1, hi1 = reg.anchor_line + 1, reg.end_line or (reg.anchor_line + 1)
            in_region = [im for im in docx_images if im.line_no and lo1 <= im.line_no <= hi1]
            if in_region:
                region_images_json = json.dumps([
                    {"file_name": im.rel_path, "line_no": im.line_no, "size": im.size}
                    for im in in_region[:100]
                ], ensure_ascii=False)
        if title in existing:
            # 重新拆解：刷新同名素材内容（保留引用计数与分类）
            mat = existing[title]
            mat.rows_json = new_rows or mat.rows_json
            mat.text = (g.sum or mat.text) if not g.rows else None
            mat.images_json = region_images_json
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
                images_json=region_images_json,
            )
            session.add(mat)
            existing[title] = mat
            made += 1
    session.commit()

    # 契约 §四 / §6.5：成功收尾——执行记录置 success 并写文档 provenance
    execution.status = "success"
    execution.items_count = len(groups)
    execution.rows_count = sum(len(g.rows) for g in groups)
    execution.finished_at = _dt.now(_UTC)
    doc.extract_ruleset_version = ruleset_version
    doc.extract_rules_hash = digest
    session.commit()
    audit("extract_executed", doc_id=doc.id,
          detail=f"ruleset v{ruleset_version} · 规则 {len(applied)} 条 · 结构项 {len(groups)} · 行 {execution.rows_count}")
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
