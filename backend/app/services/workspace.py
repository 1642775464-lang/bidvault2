"""C-1：Workspace OfficeDocument 底座。

- 每个 Project 最多一份 Workspace（OfficeDocument type=workspace）
- Workspace DOCX 是投标文件工作正文唯一真源；本模块只做：
  初始化（blank/template DOCX v1）与派生数据（WorkspaceDocumentProcessor）
- 版本/历史/恢复/callback/key/token 全部复用 OfficeDocumentService，不新建专用服务
"""

from __future__ import annotations

import io
import logging
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def blank_workspace_bytes(title: str) -> bytes:
    """初始化用 blank 工作稿（合法可编辑 DOCX；正式模板后续替换）。"""
    from docx import Document as Docx

    d = Docx()
    d.add_heading(title, level=0)
    d.add_paragraph("")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def ensure_workspace(session: Session, project_id: str) -> Any:
    """幂等：返回项目 Workspace 的 OfficeDocument（不存在则创建 v1=blank 工作稿）。

    禁止重复创建两个 Workspace（以 project.workspace_office_document_id 为准）。
    """
    from ..models import OfficeDocument, Project, WorkspaceDocument
    from .office_document_svc import OfficeDocumentService, OfficeDocumentTableAdapter

    project = session.get(Project, project_id)
    if project is None:
        raise ValueError("项目不存在")
    if project.workspace_office_document_id:
        od = session.get(OfficeDocument, project.workspace_office_document_id)
        if od is not None:
            return od
    title = f"{project.name} 投标工作稿"
    data = blank_workspace_bytes(title)
    od = OfficeDocument(document_type="workspace", title=title, file_type="docx")
    session.add(od)
    session.flush()
    svc = OfficeDocumentService(session=session, adapter=OfficeDocumentTableAdapter(session))
    stored = f"workspace_{od.id[:8]}.docx"
    svc.adapter.write_file(stored, data)
    import hashlib

    v = svc.adapter.insert_version(
        od.id, 1, file_path=stored, file_hash=hashlib.sha256(data).hexdigest(),
        file_size=len(data), source="init", editor_user=None, comment="工作稿初始化",
        restored_from_version_id=None,
    )
    svc.adapter.set_current_version(od.id, v.id)
    project.workspace_office_document_id = od.id
    session.add(WorkspaceDocument(
        project_id=project.id, office_document_id=od.id,
        derived_from_version_id=v.id,
    ))
    session.commit()
    logger.info("workspace created: project=%s od=%s", project.id, od.id)
    return od


def process_workspace_document(session: Any, od: Any, version: Any) -> None:
    """Workspace 派生数据：parse DOCX → derived_text（正文真源仍是 OfficeDocumentVersion）。"""
    from ..config import get_settings
    from ..models import WorkspaceDocument
    from .ingest.parser import parse_docx

    row = (
        session.query(WorkspaceDocument)
        .filter(WorkspaceDocument.office_document_id == od.id)
        .first()
    )
    if row is None:
        logger.warning("workspace row for office_document %s not found", od.id)
        return
    data = (get_settings().files_dir / version.file_path).read_bytes()
    parsed = parse_docx(data)
    lines: list[str] = []
    for tb in parsed.doc_tables:
        model = tb.model
        for r in (model["rows"] if model else []):
            cells = [c["text"].strip() for c in r["cells"] if not c.get("merged")]
            cells = [c for c in cells if c]
            if cells:
                lines.append(" | ".join(cells))
    for ln in parsed.text.split("\n"):
        s = ln.strip()
        if s:
            lines.append(s)
    row.derived_text = "\n".join(lines) or None
    row.derived_from_version_id = version.id
    session.commit()
    logger.info("workspace derived text updated: od=%s v=%s lines=%d", od.id, version.version, len(lines))


class WorkspaceDocumentProcessor:
    """post-save processor：document_type = workspace。"""

    document_type = "workspace"

    def process(self, od: Any, version: Any) -> None:
        from sqlalchemy.orm import sessionmaker

        from ..db import get_engine

        factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
        ses = factory()
        try:
            process_workspace_document(ses, od, version)
        finally:
            ses.close()
