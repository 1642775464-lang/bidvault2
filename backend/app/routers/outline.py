"""E-2A：DocumentOutline 只读路由——章节树查询（阅读/引用/风险三处消费）。

GET /api/office-documents/{oid}/outline            当前版本章节树（懒构建）
GET /api/office-documents/{oid}/outline/drift      与指定旧版本的漂移比对（可选）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import OfficeDocument, OfficeDocumentVersion
from ..services.document_outline import drift_report, get_or_build

router = APIRouter(prefix="/api/office-documents", tags=["章节树"])


def _current(session: Session, oid: str) -> tuple[Any, Any]:
    od = session.get(OfficeDocument, oid)
    if od is None:
        raise HTTPException(404, "OfficeDocument 不存在")
    cur = (session.query(OfficeDocumentVersion)
           .filter(OfficeDocumentVersion.id == od.current_version_id).first()) if od.current_version_id else None
    if cur is None:
        raise HTTPException(409, "该 Office 文件尚无正式版本")
    return od, cur


def _version_bytes(session: Session, version: OfficeDocumentVersion) -> bytes:
    from ..services.office_document_svc import OfficeDocumentService, OfficeDocumentTableAdapter

    svc = OfficeDocumentService(session=session, adapter=OfficeDocumentTableAdapter(session))
    return svc.adapter.read_file(version.file_path)


@router.get("/{oid}/outline")
def get_outline(oid: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    od, cur = _current(session, oid)
    tree = get_or_build(session, od.id, cur.id, _version_bytes(session, cur))
    return {"oid": od.id, "version_id": cur.id, "version": cur.version,
            "node_count": len(tree["nodes"]), "nodes": tree["nodes"]}


@router.get("/{oid}/outline/drift")
def outline_drift(oid: str, against_version_id: str,
                  session: Session = Depends(get_session)) -> dict[str, Any]:
    """当前版本章节树 vs 指定旧版本：完好/漂移/丢失（供引用漂移校验消费）。"""
    od, cur = _current(session, oid)
    old = session.query(OfficeDocumentVersion).filter(
        OfficeDocumentVersion.id == against_version_id,
        OfficeDocumentVersion.office_document_id == oid).first()
    if old is None:
        raise HTTPException(404, "对照版本不存在")
    old_tree = get_or_build(session, od.id, old.id, _version_bytes(session, old))
    new_tree = get_or_build(session, od.id, cur.id, _version_bytes(session, cur))
    report = drift_report(old_tree["nodes"], new_tree["nodes"])
    return {"oid": od.id, "old_version_id": old.id, "new_version_id": cur.id, **report}

