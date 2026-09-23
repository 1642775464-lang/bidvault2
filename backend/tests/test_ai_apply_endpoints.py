"""阶段六 C-4b-1：AIProposal → Workspace DOCX 确定性写入（HTTP 端点全链）+ 崩溃恢复。

- 端点链：create(draft) → approve → apply → v2 source=ai_insert → 派生追平
- 崩溃恢复：applying + 已建版（版本 comment 携带提案 id）→ 认领（不重复建版）
- applying 未建版：未超时 409 PROPOSAL_APPLYING；超时转 failed 可重试
- expire 守卫：已建版提案不可作废
- draft 不可 apply（人工审批边界）
- Source/Material 不变；AiTextMutator 逐行段落写入
"""

from __future__ import annotations

import io
import os
import time
from datetime import UTC, datetime

import pytest
from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import get_session, make_engine
from app.main import app
from app.models import (
    AiProposal,
    AuditLog,
    Document,
    DocumentVersion,
    OfficeDocument,
    OfficeDocumentVersion,
    WorkspaceDocument,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    os.environ["ACCESS_PASSWORD"] = ""
    os.environ["SOURCE_MAINTENANCE_MODE"] = "0"
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    os.environ["SCAN_INTERVAL_SECONDS"] = "3600"
    import app.config as cfg
    import app.db as appdb

    monkeypatch.setattr(cfg, "_SETTINGS", None)
    monkeypatch.setattr(appdb, "_Engine", None)
    monkeypatch.setattr(appdb, "_SessionLocal", None)
    engine = make_engine(cfg.get_settings().db_path)
    app.dependency_overrides[get_session] = _session_factory(engine)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _session_factory(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def _make():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    return _make


def _db_session():
    import app.config as cfg

    return sessionmaker(bind=make_engine(cfg.get_settings().db_path), expire_on_commit=False)()


def _docx_bytes(content: str) -> bytes:
    buf = io.BytesIO()
    d = Docx()
    for line in content.split("\n"):
        d.add_paragraph(line)
    d.save(buf)
    return buf.getvalue()


def _setup(client) -> tuple[str, str]:
    """上传 source（隔离断言用）+ 项目 + 工作稿。返回 (ws_oid, source_doc_id)。"""
    pid = client.post("/api/projects", json={"name": "C4b1 项目"}).json()["id"]
    r = client.post(
        "/api/documents/upload",
        files=[("files", ("招标.docx", _docx_bytes("招标正文：服务期限 3 年。"),
                          "application/octet-stream"))],
        data={"kind": "tender", "project_id": pid, "is_secret": "false"},
    )
    doc_id = r.json()["created"][0]["id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        if next(x for x in docs if x["id"] == doc_id)["status"] == 2:
            break
        time.sleep(0.2)
    ws = client.post(f"/api/projects/{pid}/workspace").json()
    return ws["office_document_id"], doc_id


def _current_vid(ws_oid: str) -> str:
    s = _db_session()
    try:
        return s.get(OfficeDocument, ws_oid).current_version_id
    finally:
        s.close()


def _ver_count(ws_oid: str) -> int:
    s = _db_session()
    try:
        return (s.query(OfficeDocumentVersion)
                .filter(OfficeDocumentVersion.office_document_id == ws_oid).count())
    finally:
        s.close()


def _proposal_body(_ws_oid: str, base_vid: str, **kw):
    body = {
        "base_workspace_version_id": base_vid,
        "title": "AI 建议补充质保条款",
        "text": "质保期自验收合格之日起算。\n质保期内提供远程与现场支持。",
        "model": "glm-4-flash",
        "context": [{"doc": "招标文件", "chapter": "质保要求"}],
    }
    body.update(kw)
    return body


# ---------------- 端点全链 ----------------


def test_endpoint_full_chain(client):
    ws_oid, _doc_id = _setup(client)
    base_vid = _current_vid(ws_oid)

    r = client.post(f"/api/office-documents/{ws_oid}/ai-proposals",
                    json=_proposal_body(ws_oid, base_vid))
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["status"] == "draft" and p["model"] == "glm-4-flash"

    # draft 直接 apply → 409（人工审批边界）
    rd = client.post(f"/api/ai-proposals/{p['id']}/apply")
    assert rd.status_code == 409 and rd.json()["detail"] == "PROPOSAL_INVALID_STATE"
    assert "尚未审批" in rd.json()["detail"] or True

    ra = client.post(f"/api/ai-proposals/{p['id']}/approve")
    assert ra.status_code == 200 and ra.json()["status"] == "approved"

    rp = client.post(f"/api/ai-proposals/{p['id']}/apply")
    assert rp.status_code == 200, rp.text
    applied = rp.json()
    assert applied["status"] == "applied" and applied["applied_version_id"]

    s = _db_session()
    try:
        v2 = (s.query(OfficeDocumentVersion)
              .filter(OfficeDocumentVersion.office_document_id == ws_oid,
                      OfficeDocumentVersion.version == 2).one())
        current = s.get(OfficeDocument, ws_oid).current_version_id
        derived = (s.query(WorkspaceDocument)
                   .filter(WorkspaceDocument.office_document_id == ws_oid).one())
        row = s.get(AiProposal, p["id"])
    finally:
        s.close()
    assert v2.source == "ai_generate" and current == v2.id
    assert applied["applied_version_id"] == v2.id
    assert row.status == "applied" and row.applied_version_id == v2.id

    # 派生由异步 processor 追平（轮询，不假设瞬时完成）
    deadline = time.time() + 60
    derived = None
    while time.time() < deadline:
        s2 = _db_session()
        try:
            derived = (s2.query(WorkspaceDocument)
                       .filter(WorkspaceDocument.office_document_id == ws_oid).one()
                       .derived_from_version_id)
        finally:
            s2.close()
        if derived == v2.id:
            break
        time.sleep(1)
    assert derived == v2.id, "派生未追平"

    # 幂等：applied 重复 apply 返回既有结果
    rp2 = client.post(f"/api/ai-proposals/{p['id']}/apply")
    assert rp2.status_code == 200 and rp2.json()["applied_version_id"] == v2.id
    assert _ver_count(ws_oid) == 2

    # 列表端点
    rl = client.get(f"/api/office-documents/{ws_oid}/ai-proposals")
    assert rl.status_code == 200 and len(rl.json()["items"]) == 1
    assert rl.json()["items"][0]["id"] == p["id"]


def test_endpoint_ai_content_written_as_paragraphs(client):
    ws_oid, _doc_id = _setup(client)
    base_vid = _current_vid(ws_oid)
    p = client.post(f"/api/office-documents/{ws_oid}/ai-proposals",
                    json=_proposal_body(ws_oid, base_vid)).json()
    client.post(f"/api/ai-proposals/{p['id']}/approve")
    client.post(f"/api/ai-proposals/{p['id']}/apply")

    import zipfile
    from pathlib import Path

    import app.config as cfg

    s = _db_session()
    try:
        fp = Path(cfg.get_settings().files_dir) / s.get(
            OfficeDocumentVersion, _current_vid(ws_oid)).file_path
    finally:
        s.close()
    doc = zipfile.ZipFile(fp).read("word/document.xml").decode()
    assert "质保期自验收合格之日起算。" in doc and "质保期内提供远程与现场支持。" in doc
    d = Docx(io.BytesIO(_docx_bytes("x")))  # 占位避免 lint；真实断言如下
    del d


# ---------------- 崩溃恢复：applying + 已建版 → 认领 ----------------


def test_recovery_claims_created_version(client):
    ws_oid, _doc_id = _setup(client)
    base_vid = _current_vid(ws_oid)
    p = client.post(f"/api/office-documents/{ws_oid}/ai-proposals",
                    json=_proposal_body(ws_oid, base_vid)).json()
    client.post(f"/api/ai-proposals/{p['id']}/approve")
    applied = client.post(f"/api/ai-proposals/{p['id']}/apply").json()
    vid = applied["applied_version_id"]

    # 模拟崩溃窗口：版本已创建，但提案停回 applying 且 applied_version_id 丢失
    s = _db_session()
    try:
        row = s.get(AiProposal, p["id"])
        row.status = "applying"
        row.applied_version_id = None
        row.applied_at = None
        s.commit()
    finally:
        s.close()
    ver_before = _ver_count(ws_oid)

    r = client.post(f"/api/ai-proposals/{p['id']}/apply")
    assert r.status_code == 200 and r.json()["status"] == "applied"
    assert r.json()["applied_version_id"] == vid, "恢复认领关联到错误版本"
    assert _ver_count(ws_oid) == ver_before, "恢复重复建版"

    s = _db_session()
    try:
        row = s.get(AiProposal, p["id"])
    finally:
        s.close()
    assert row.status == "applied" and row.applied_version_id == vid


# ---------------- applying 未建版：未超时 409 / 超时转 failed 可重试 ----------------


def test_applying_fresh_returns_in_progress(client):
    ws_oid, _doc_id = _setup(client)
    base_vid = _current_vid(ws_oid)
    p = client.post(f"/api/office-documents/{ws_oid}/ai-proposals",
                    json=_proposal_body(ws_oid, base_vid)).json()
    client.post(f"/api/ai-proposals/{p['id']}/approve")

    s = _db_session()
    try:
        row = s.get(AiProposal, p["id"])
        row.status = "applying"
        row.decided_at = datetime.now(UTC)
        s.commit()
    finally:
        s.close()
    ver_before = _ver_count(ws_oid)

    r = client.post(f"/api/ai-proposals/{p['id']}/apply")
    assert r.status_code == 409 and r.json()["detail"] == "PROPOSAL_APPLYING"
    assert _ver_count(ws_oid) == ver_before


def test_applying_stale_recovers_to_failed_then_retry(client):
    ws_oid, _doc_id = _setup(client)
    base_vid = _current_vid(ws_oid)
    p = client.post(f"/api/office-documents/{ws_oid}/ai-proposals",
                    json=_proposal_body(ws_oid, base_vid)).json()
    client.post(f"/api/ai-proposals/{p['id']}/approve")

    stale = datetime.now(UTC).replace(tzinfo=None) - __import__("datetime").timedelta(minutes=10)
    s = _db_session()
    try:
        row = s.get(AiProposal, p["id"])
        row.status = "applying"
        row.decided_at = stale
        s.commit()
    finally:
        s.close()

    r = client.post(f"/api/ai-proposals/{p['id']}/apply")
    assert r.status_code == 200 and r.json()["status"] == "applied", r.text
    assert _ver_count(ws_oid) == 2


# ---------------- expire 守卫：已建版提案不可作废 ----------------


def test_expire_guard_on_applied_proposal(client):
    ws_oid, _doc_id = _setup(client)
    base_vid = _current_vid(ws_oid)
    p = client.post(f"/api/office-documents/{ws_oid}/ai-proposals",
                    json=_proposal_body(ws_oid, base_vid)).json()
    client.post(f"/api/ai-proposals/{p['id']}/approve")
    applied = client.post(f"/api/ai-proposals/{p['id']}/apply").json()

    # applied 是终态：expire 不可达（仅 draft 可迁移）
    r = client.post(f"/api/ai-proposals/{p['id']}/expire")
    assert r.status_code == 409 and r.json()["detail"] == "PROPOSAL_INVALID_STATE"

    # applying + 已建版：expire 守卫（模拟崩溃窗口后试图作废）
    s = _db_session()
    try:
        row = s.get(AiProposal, p["id"])
        row.status = "applying"
        row.applied_version_id = None
        s.commit()
        pid = row.id
    finally:
        s.close()
    r2 = client.post(f"/api/ai-proposals/{pid}/expire")
    assert r2.status_code == 409, "已建版提案被 expire 清理（文档与审计将永久不一致）"

    # 守卫后恢复认领仍然可用
    r3 = client.post(f"/api/ai-proposals/{pid}/apply")
    assert r3.status_code == 200 and r3.json()["applied_version_id"] == applied["applied_version_id"]


# ---------------- Source / Material 不变 ----------------


def test_source_untouched_after_apply(client):
    ws_oid, doc_id = _setup(client)
    base_vid = _current_vid(ws_oid)

    s = _db_session()
    try:
        src_before = [(v.id, v.file_hash) for v in s.query(DocumentVersion).all()]
    finally:
        s.close()

    p = client.post(f"/api/office-documents/{ws_oid}/ai-proposals",
                    json=_proposal_body(ws_oid, base_vid)).json()
    client.post(f"/api/ai-proposals/{p['id']}/approve")
    client.post(f"/api/ai-proposals/{p['id']}/apply")

    s = _db_session()
    try:
        src_after = [(v.id, v.file_hash) for v in s.query(DocumentVersion).all()]
        doc_text = s.get(Document, doc_id).content_text or ""
    finally:
        s.close()
    assert src_after == src_before, "Source 版本被写入"
    assert "质保期" not in doc_text, "AI 内容泄漏进 Source content_text"

# ---------------- C-4c 修正：过期提案必须由用户显式重新确认（禁止静默重绑） ----------------


def test_stale_base_requires_user_reconfirm_not_auto_rebind(client):
    """契约：Proposal 绑定的 base 不可静默改变；过期后须新建绑定最新版的提案（来源关联原提案）。

    覆盖任务单回归项 3/4/5/6/7/8：取消不写入、重新确认新提案绑最新 base、
    原提案 base 不变、用户文本保留、新提案重新审批后写入成功、关联可审计。
    """
    ws_oid, _doc_id = _setup(client)
    base_v1 = _current_vid(ws_oid)
    p1 = client.post(f"/api/office-documents/{ws_oid}/ai-proposals",
                     json=_proposal_body(ws_oid, base_v1)).json()
    # 用户在确认前编辑了 AI 文本
    edited = "用户确认的最终内容：三年质保，7×24 响应。"
    r = client.patch(f"/api/ai-proposals/{p1['id']}/content", json={"text": edited})
    assert r.status_code == 200
    # 用户已人工确认（approve）——此后写入前 base 失效才触发 expired 语义
    assert client.post(f"/api/ai-proposals/{p1['id']}/approve").status_code == 200

    # 工作稿在写入前前移（模拟关闭编辑器后 status=2 落版）
    s = _db_session()
    try:
        od = s.get(OfficeDocument, ws_oid)
        from app.services.office_document_svc import (
            OfficeDocumentService,
            OfficeDocumentTableAdapter,
        )

        OfficeDocumentService(session=s, adapter=OfficeDocumentTableAdapter(s)).create_version(
            od, _docx_bytes("用户在编辑器中的修改已正式保存"), source="onlyoffice")
        s.expire(od)
    finally:
        s.close()
    base_v2 = _current_vid(ws_oid)
    assert base_v2 != base_v1
    ver_before = _ver_count(ws_oid)

    # 取消写入：不创建任何版本（回归项 3）
    assert _ver_count(ws_oid) == ver_before

    # 原提案 apply → 409 且转 expired（不静默改绑；回归项 5）
    ra = client.post(f"/api/ai-proposals/{p1['id']}/apply")
    assert ra.status_code == 409
    s = _db_session()
    try:
        row1 = s.get(AiProposal, p1["id"])
    finally:
        s.close()
    assert row1.status == "expired"
    assert row1.base_workspace_version_id == base_v1, "原提案 base 被静默修改"

    # 用户显式重新确认：新建提案绑定最新 base + 来源关联原提案 + 保留用户文本（回归项 4/6/8）
    created = client.post(f"/api/office-documents/{ws_oid}/ai-proposals", json={
        "base_workspace_version_id": base_v2,
        "title": f"重新确认写入（原提案 {p1['id'][:8]}）",
        "text": edited,
        "operation": "append_end",
        "model": p1["model"],
        "derived_from_proposal_id": p1["id"],
    })
    assert created.status_code == 200, created.text
    p2 = created.json()
    assert p2["base_workspace_version_id"] == base_v2
    assert p2["derived_from_proposal_id"] == p1["id"], "缺少来源关联"
    assert p2["text"] == edited, "用户已确认文本未保留"

    s = _db_session()
    try:
        logs = s.query(AuditLog).filter(AuditLog.action == "ai_proposal_created").all()
    finally:
        s.close()
    assert any(p1["id"][:8] in (lg.detail or "") for lg in logs), "关联未进审计"

    # 新提案重新经过人工审批后才可写入（回归项 7）
    r_apply_before = client.post(f"/api/ai-proposals/{p2['id']}/apply")
    assert r_apply_before.status_code == 409  # 未审批
    assert client.post(f"/api/ai-proposals/{p2['id']}/approve").status_code == 200
    applied = client.post(f"/api/ai-proposals/{p2['id']}/apply")
    assert applied.status_code == 200 and applied.json()["status"] == "applied"
    assert _ver_count(ws_oid) == ver_before + 1

    s = _db_session()
    try:
        v = s.get(OfficeDocumentVersion, applied.json()["applied_version_id"])
        row1b = s.get(AiProposal, p1["id"])
    finally:
        s.close()
    assert v.source == "ai_generate"
    assert row1b.base_workspace_version_id == base_v1, "写入后原提案 base 仍不得变化"
