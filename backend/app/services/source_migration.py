"""B-1c：source → OfficeDocument 迁移收口（幂等回填）。

- 每份 Document 建 OfficeDocument(type=source, title=filename, file_type)
- document_versions **按原行 id** 复制到 office_document_versions
  （表不同无冲突 → current_version_id / restored_from_version_id 零重映射，文件不复制）
- documents.office_document_id 回填；OfficeDocument.current_version_id 指原 current
- 幂等：已有 office_document_id 的文档跳过；启动时执行 + 可手动触发

迁移后语义（关单条件）：业务层只认 office_document_id；
documents.current_version_id / stored_name 为 deprecated 兼容字段（回填后与真源一致，
仅由兼容写路径维持，新代码禁止读取）。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from ..models import Document, DocumentVersion, OfficeDocument, OfficeDocumentVersion

logger = logging.getLogger(__name__)


def _file_size(files_dir: Any, file_path: str | None) -> int:
    if not file_path:
        return 0
    try:
        size: int = (files_dir / file_path).stat().st_size
        return size
    except OSError:
        return 0


def backfill_source_office_documents(session: Session) -> dict[str, int]:
    """幂等回填：返回 {created, skipped}。"""
    from ..config import get_settings

    created = skipped = 0
    docs = session.query(Document).all()
    for d in docs:
        if d.office_document_id:
            skipped += 1
            continue
        latest_dv = (
            session.query(DocumentVersion)
            .filter(DocumentVersion.document_id == d.id)
            .order_by(DocumentVersion.version.desc())
            .first()
        )
        current = d.current_version_id or (latest_dv.id if latest_dv else None)
        od = OfficeDocument(
            document_type="source",
            title=d.filename,
            file_type=d.file_type or "docx",
            current_version_id=current,
        )
        session.add(od)
        session.flush()
        versions = (
            session.query(DocumentVersion)
            .filter(DocumentVersion.document_id == d.id)
            .order_by(DocumentVersion.version)
            .all()
        )
        for dv in versions:
            session.add(OfficeDocumentVersion(
                id=dv.id,  # 原 id 直接沿用：restore 链/current 指针零重映射
                office_document_id=od.id,
                version=dv.version,
                file_path=dv.file_path,
                file_hash=dv.file_hash,
                file_size=_file_size(get_settings().files_dir, dv.file_path),
                source=dv.source,
                restored_from_version_id=dv.restored_from_version_id,
                editor_user=dv.editor_user,
                comment=dv.comment,
                created_at=dv.created_at,
            ))
        d.office_document_id = od.id
        created += 1
    session.commit()
    if created:
        logger.info("source office documents backfilled: created=%d skipped=%d", created, skipped)
    return {"created": created, "skipped": skipped}
