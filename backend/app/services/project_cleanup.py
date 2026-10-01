"""项目删除（级联）：文档+拆解+工作稿+工作区项目文件夹一并删除；素材保留（仅解除关联）。

语义（项目负责人 2026-10-01 指令）：
- 删除：项目本体、项目全部文档（含拆解结果/索引/风险项/工作稿及其版本文件）、
  工作区根下「项目/<名>/」文件夹（招标文件/投标文件/输出文件）
- 保留：素材库内容（materials 本体与图片证据），仅解除与项目的关联（project_id 置空、引用台账删除）
- 审计日志不删（留痕）
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _collect_office_file_paths(session: Session, od_ids: list[str]) -> list[str]:
    """收集 office 版本链的全部磁盘文件相对路径（去重保序）。"""
    if not od_ids:
        return []
    from ..models import OfficeDocumentVersion

    rows = (session.query(OfficeDocumentVersion.file_path)
            .filter(OfficeDocumentVersion.office_document_id.in_(od_ids)).all())
    seen: list[str] = []
    for (fp,) in rows:
        if fp and fp not in seen:
            seen.append(fp)
    return seen


def _delete_office_chain(session: Session, od_ids: list[str]) -> None:
    """删除 office 文档链及其派生数据（磁盘文件路径由调用方先行收集）。"""
    from ..models import (
        AiProposal,
        DocumentOutline,
        MaterialReference,
        OfficeDocument,
        OfficeDocumentVersion,
        ProjectionState,
    )

    if not od_ids:
        return
    from ..models import OfficeDocumentVersion as _ODV

    version_ids = [v for (v,) in session.query(_ODV.id)
                   .filter(_ODV.office_document_id.in_(od_ids)).all()]
    if version_ids:
        session.query(DocumentOutline).filter(DocumentOutline.version_id.in_(version_ids)).delete(synchronize_session=False)
    session.query(ProjectionState).filter(ProjectionState.oid.in_(od_ids)).delete(synchronize_session=False)
    session.query(MaterialReference).filter(MaterialReference.target_document_id.in_(od_ids)).delete(synchronize_session=False)
    session.query(AiProposal).filter(AiProposal.workspace_office_document_id.in_(od_ids)).delete(synchronize_session=False)
    session.query(OfficeDocumentVersion).filter(OfficeDocumentVersion.office_document_id.in_(od_ids)).delete(synchronize_session=False)
    session.query(OfficeDocument).filter(OfficeDocument.id.in_(od_ids)).delete(synchronize_session=False)


def _delete_documents_of(session: Session, project_id: str) -> tuple[int, list[str]]:
    """删除项目全部文档及其从属数据。返回 (文档数, 磁盘相对路径清单)。"""
    from ..models import (
        Chunk,
        Document,
        DocumentVersion,
        ExtractExecution,
        ExtractItem,
        ExtractRow,
        RiskItem,
    )
    from .ingest.pipeline import purge_document_index

    docs = session.query(Document).filter(Document.project_id == project_id).all()
    disk_files: list[str] = []
    for doc in docs:
        if doc.office_document_id:
            disk_files.extend(_collect_office_file_paths(session, [doc.office_document_id]))
            _delete_office_chain(session, [doc.office_document_id])
        if doc.stored_name:
            disk_files.append(doc.stored_name)
        session.query(DocumentVersion).filter(DocumentVersion.document_id == doc.id).delete(synchronize_session=False)
        items = session.query(ExtractItem).filter(ExtractItem.doc_id == doc.id).all()
        for it in items:
            session.query(ExtractRow).filter(ExtractRow.item_id == it.id).delete(synchronize_session=False)
        session.query(ExtractItem).filter(ExtractItem.doc_id == doc.id).delete(synchronize_session=False)
        session.query(ExtractExecution).filter(ExtractExecution.document_id == doc.id).delete(synchronize_session=False)
        session.query(RiskItem).filter(RiskItem.doc_id == doc.id).delete(synchronize_session=False)
        purge_document_index(session, doc.id)
        session.delete(doc)
    return len(docs), disk_files


def _delete_workspace_doc(session: Session, project_id: str, oid: str | None) -> list[str]:
    """删除项目工作稿（OfficeDocument type=workspace）及派生缓存。"""
    from ..models import WorkspaceDocument

    disk_files: list[str] = []
    if oid:
        disk_files.extend(_collect_office_file_paths(session, [oid]))
        _delete_office_chain(session, [oid])
    session.query(WorkspaceDocument).filter(WorkspaceDocument.project_id == project_id).delete(synchronize_session=False)
    return disk_files


def _remove_project_folder(ws_root: Path | None, workspace_rel: str | None) -> str | None:
    """删除工作区根下的项目文件夹；工作区未接入/路径不安全则跳过。"""
    if not ws_root or not workspace_rel:
        return None
    try:
        root = ws_root.resolve()
        target = (root / workspace_rel).resolve()
        if root not in target.parents or not target.is_dir():
            return None  # 防穿越/不存在
        shutil.rmtree(target, ignore_errors=True)
        return str(target)
    except Exception:  # noqa: BLE001  文件夹删除失败不阻塞 DB 删除
        logger.exception("project folder removal failed: %s", workspace_rel)
        return None


def _remove_files(files_dir: Path, rel_paths: list[str]) -> int:
    """删除 files_dir 下的受控文件（仅顶层相对路径，拒绝逃逸；不存在忽略）。"""
    base = files_dir.resolve()
    removed = 0
    for rel in rel_paths:
        try:
            target = (base / rel).resolve()
            if base != target and base in target.parents and target.is_file():
                target.unlink()
                removed += 1
        except OSError:
            logger.warning("project cleanup: cannot remove %s", rel)
    return removed


def delete_project_cascade(session: Session, project: Any) -> dict[str, Any]:
    """级联删除项目；素材保留仅解除关联。返回统计供审计与前端提示。

    注意：素材的 images_json 图片文件属于素材（不删）；仅删除文档原件与工作稿版本文件。
    """
    from ..models import Material, MaterialRef

    # 0) 先捕获删除后仍需要的信息（commit 后 project 实例不可用）
    workspace_rel = project.workspace_rel
    ws_root: Path | None = None
    try:
        from .workspace_fs import get_active

        ws = get_active(session)
        if ws is not None:
            ws_root = Path(ws.root_path)
    except Exception:  # noqa: BLE001  工作区未接入时跳过文件夹清理
        ws_root = None

    # 1) 文档与工作稿
    doc_count, disk_files = _delete_documents_of(session, project.id)
    disk_files += _delete_workspace_doc(session, project.id, project.workspace_office_document_id)

    # 2) 素材保留：解除关联（引用台账删、project_id 置空）
    ref_count = session.query(MaterialRef).filter(MaterialRef.project_id == project.id).count()
    session.query(MaterialRef).filter(MaterialRef.project_id == project.id).delete(synchronize_session=False)
    session.query(Material).filter(Material.project_id == project.id).update(
        {Material.project_id: None}, synchronize_session=False)

    # 3) 删项目本体
    name = project.name
    session.delete(project)
    session.commit()

    # 4) 磁盘清理（DB 成功后执行；失败仅日志不回滚）
    from ..config import get_settings

    removed_files = _remove_files(get_settings().files_dir, disk_files)
    folder_removed = _remove_project_folder(ws_root, workspace_rel)

    return {
        "name": name,
        "documents": doc_count,
        "files_removed": removed_files,
        "folder_removed": folder_removed,
        "material_refs_cleared": ref_count,
    }
