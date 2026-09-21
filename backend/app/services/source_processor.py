"""Source 文档处理器（阶段六 B-1c）：source DOCX 保存后触发版本化拆解链。

职责（唯一）：调 process_document(按 OfficeDocumentVersion 文件解析)，
更新 source 派生数据（content_text/ExtractItem/chunks/素材/索引）。
防串写：process_document 内部校验 OfficeDocument.current_version_id == 解析版本。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SourceDocumentProcessor:
    """post-save processor：document_type = source。"""

    document_type = "source"

    def process(self, od: Any, version: Any) -> None:
        from sqlalchemy.orm import sessionmaker

        from ..db import get_engine
        from ..models import Document
        from ..services.ingest.pipeline import process_document

        factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
        ses = factory()
        try:
            doc = (
                ses.query(Document)
                .filter(Document.office_document_id == od.id)
                .first()
            )
            if doc is None:
                logger.warning("source document for office_document %s not found", od.id)
                return
            process_document(ses, doc, version_id=version.id)
        finally:
            ses.close()
