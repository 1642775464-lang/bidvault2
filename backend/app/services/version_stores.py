"""B-1c-2：双读对账 Store——Legacy（documents/document_versions）vs Office（通用表）。

原则：
- 两个 Store 均为**只读**，互不 fallback；old read + new read → explicit compare；
- 任一差异输出 mismatch，不得用其中一边掩盖另一边；
- 本模块不改运行链路（B-1c-3 才切读取链，B-1c-4 才切写链）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VersionRecord:
    """版本记录的规范形态（old/new 双读共用）。"""

    version_id: str
    version: int
    file_path: str | None
    file_hash: str | None
    source: str | None
    editor_user: str | None
    comment: str | None
    created_at: str | None
    restored_from_version_id: str | None


@dataclass(frozen=True)
class DocumentSnapshot:
    """一个 Office 文件对象的版本集合快照。"""

    doc_id: str
    title: str | None
    current_version_id: str | None
    versions: dict[str, VersionRecord] = field(default_factory=dict)  # key = version_id


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


class LegacySourceVersionStore:
    """旧链只读：documents + document_versions。"""

    name = "legacy"

    def load(self, session: Any, doc_id: str) -> DocumentSnapshot | None:
        from ..models import Document, DocumentVersion

        doc = session.get(Document, doc_id)
        if doc is None:
            return None
        versions: dict[str, VersionRecord] = {}
        for dv in (
            session.query(DocumentVersion)
            .filter(DocumentVersion.document_id == doc_id)
            .all()
        ):
            versions[dv.id] = VersionRecord(
                version_id=dv.id, version=dv.version, file_path=dv.file_path,
                file_hash=dv.file_hash, source=dv.source, editor_user=dv.editor_user,
                comment=dv.comment, created_at=_iso(dv.created_at),
                restored_from_version_id=dv.restored_from_version_id,
            )
        return DocumentSnapshot(
            doc_id=doc.id, title=doc.filename,
            current_version_id=doc.current_version_id, versions=versions,
        )

    def load_all(self, session: Any) -> list[DocumentSnapshot | None]:
        from ..models import Document

        snapshots: list[DocumentSnapshot | None] = [
            self.load(session, d.id) for d in session.query(Document).all()
        ]
        return snapshots  # 未建档返回 None（对账时按 mismatch 处理）


class OfficeVersionStore:
    """新链只读：office_documents + office_document_versions（经 documents.office_document_id 映射）。"""

    name = "office"

    def load(self, session: Any, doc_id: str) -> DocumentSnapshot | None:

        from ..models import Document, OfficeDocument, OfficeDocumentVersion

        doc = session.get(Document, doc_id)
        if doc is None or not doc.office_document_id:
            return None
        od = session.get(OfficeDocument, doc.office_document_id)
        if od is None:
            return None
        versions: dict[str, VersionRecord] = {}
        for v in (
            session.query(OfficeDocumentVersion)
            .filter(OfficeDocumentVersion.office_document_id == od.id)
            .all()
        ):
            versions[v.id] = VersionRecord(
                version_id=v.id, version=v.version, file_path=v.file_path,
                file_hash=v.file_hash, source=v.source, editor_user=v.editor_user,
                comment=v.comment, created_at=_iso(v.created_at),
                restored_from_version_id=v.restored_from_version_id,
            )
        return DocumentSnapshot(
            doc_id=doc.id, title=od.title,
            current_version_id=od.current_version_id, versions=versions,
        )

    def load_all(self, session: Any) -> list[DocumentSnapshot | None]:
        from ..models import Document

        snapshots: list[DocumentSnapshot | None] = [
            self.load(session, d.id) for d in session.query(Document).all()
        ]
        return snapshots  # None 表示未建档（对账时按 mismatched 处理）


def reconcile_source_documents(session: Any) -> dict[str, Any]:
    """全量 source 文档双读对账：old vs new 逐字段 explicit compare。"""
    legacy = LegacySourceVersionStore()
    office = OfficeVersionStore()

    summary: dict[str, int] = {
        "documents_total": 0,
        "matched_documents": 0,
        "mismatched_documents": 0,
        "old_versions": 0,
        "new_versions": 0,
        "missing_versions": 0,      # old 有 new 无
        "extra_versions": 0,        # new 有 old 无
        "hash_mismatch": 0,
        "path_mismatch": 0,
        "current_mismatch": 0,
        "restore_chain_mismatch": 0,
        "field_mismatch": 0,        # version number/source/editor_user/comment/created_at
        "inferred_current": 0,      # 旧 current 为空、按最新 version 推断且与 new 一致
    }
    mismatches: list[dict[str, Any]] = []

    from ..models import Document

    docs = session.query(Document).all()
    summary["documents_total"] = len(docs)
    for d in docs:
        old = legacy.load(session, d.id)
        new = office.load(session, d.id)
        if old is None:
            continue
        if new is None:
            summary["mismatched_documents"] += 1
            mismatches.append({"doc_id": d.id, "kind": "missing_office_document"})
            continue
        doc_mismatches: list[str] = []

        # 1) version 集合
        old_ids, new_ids = set(old.versions), set(new.versions)
        summary["old_versions"] += len(old_ids)
        summary["new_versions"] += len(new_ids)
        for vid in sorted(old_ids - new_ids):
            summary["missing_versions"] += 1
            doc_mismatches.append(f"missing version {vid}")
        for vid in sorted(new_ids - old_ids):
            summary["extra_versions"] += 1
            doc_mismatches.append(f"extra version {vid}")

        # 2) 交集逐字段
        for vid in sorted(old_ids & new_ids):
            o, n = old.versions[vid], new.versions[vid]
            if o.file_hash != n.file_hash:
                summary["hash_mismatch"] += 1
                doc_mismatches.append(f"{vid} hash")
            if o.file_path != n.file_path:
                summary["path_mismatch"] += 1
                doc_mismatches.append(f"{vid} path")
            if o.restored_from_version_id != n.restored_from_version_id:
                summary["restore_chain_mismatch"] += 1
                doc_mismatches.append(f"{vid} restored_from")
            if (o.version, o.source, o.editor_user, o.comment, o.created_at) != (
                n.version, n.source, n.editor_user, n.comment, n.created_at
            ):
                summary["field_mismatch"] += 1
                doc_mismatches.append(f"{vid} fields")

        # 3) current：旧空 → 推断=最新 version id 并标记 inferred；否则严格相等
        old_cur, new_cur = old.current_version_id, new.current_version_id
        if old_cur is None:
            if old.versions:
                latest = max(old.versions.values(), key=lambda v: v.version)
                if new_cur == latest.version_id:
                    summary["inferred_current"] += 1
                else:
                    summary["current_mismatch"] += 1
                    doc_mismatches.append("current inferred mismatch")
            elif new_cur is not None:
                summary["current_mismatch"] += 1
                doc_mismatches.append("current unexpected")
        elif old_cur != new_cur:
            summary["current_mismatch"] += 1
            doc_mismatches.append("current id mismatch")

        if doc_mismatches:
            summary["mismatched_documents"] += 1
            mismatches.append({"doc_id": d.id, "kind": "source", "issues": doc_mismatches})
        else:
            summary["matched_documents"] += 1

    return {"summary": summary, "mismatches": mismatches}
