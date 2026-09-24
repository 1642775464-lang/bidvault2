"""D-1R3：原生编辑器（DX）编辑会话契约测试。

会话协议：打开=取锁（并发 409）；保存=包校验+fence+新版本+Processor 追平+base 前移；
关闭/超时回收；会话期间 AI/Material/restore 写入 423（不绕过审批与 fencing）。
"""

from __future__ import annotations

import io
import os
import time

import pytest
from docx import Document as Docx
from sqlalchemy.orm import sessionmaker

from app.db import get_session, make_engine
from app.main import app
from app.models import OfficeDocument, OfficeDocumentVersion


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
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def _make():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = _make
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


from fastapi.testclient import TestClient  # noqa: E402  （fixture 定义后导入，风格与既有测试一致）


def _docx_bytes(content: str) -> bytes:
    buf = io.BytesIO()
    d = Docx()
    for line in content.split("\n"):
        d.add_paragraph(line)
    d.save(buf)
    return buf.getvalue()


def _db_session():
    import app.config as cfg

    return sessionmaker(bind=make_engine(cfg.get_settings().db_path), expire_on_commit=False)()


def _setup_ws(client) -> str:
    """项目 + 工作稿（带 current 版本）。返回 ws_oid。"""
    pid = client.post("/api/projects", json={"name": "D1R3 编辑会话"}).json()["id"]
    r = client.post(
        "/api/documents/upload",
        files=[("files", ("招标.docx", _docx_bytes("招标正文：服务期限 3 年。"),
                          "application/octet-stream"))],
        data={"kind": "tender", "project_id": pid, "is_secret": "false"},
    )
    assert r.status_code == 200, r.text
    doc_id = r.json()["created"][0]["id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        if next(x for x in docs if x["id"] == doc_id)["status"] == 2:
            break
        time.sleep(0.2)
    ws = client.post(f"/api/projects/{pid}/workspace").json()
    return ws["office_document_id"]


def _current(ws_oid: str) -> tuple[str, int]:
    s = _db_session()
    try:
        od = s.get(OfficeDocument, ws_oid)
        v = s.get(OfficeDocumentVersion, od.current_version_id)
        return v.id, v.version
    finally:
        s.close()


# ---------------- 会话端点契约 ----------------


def test_session_open_read_save_close(client):
    ws_oid = _setup_ws(client)
    vid0, v0 = _current(ws_oid)

    r = client.post(f"/api/office-documents/{ws_oid}/editor-session")
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]
    assert r.json()["base_version_id"] == vid0

    rf = client.get(f"/api/editor-sessions/{sid}/file")
    assert rf.status_code == 200 and rf.content[:2] == b"PK"

    edited = _docx_bytes("招标正文：服务期限 3 年。\n新增：质保期 24 个月。")
    rs = client.put(f"/api/editor-sessions/{sid}/file", content=edited)
    assert rs.status_code == 200, rs.text
    body = rs.json()
    assert body["source"] == "native_editor" and body["version"] == v0 + 1
    vid1, v1 = _current(ws_oid)
    assert vid1 == body["version_id"] and v1 == v0 + 1  # current 原子前移

    rc = client.post(f"/api/editor-sessions/{sid}/close")
    assert rc.status_code == 200 and rc.json()["closed"] is True


def test_session_concurrent_open_409(client):
    ws_oid = _setup_ws(client)
    r1 = client.post(f"/api/office-documents/{ws_oid}/editor-session")
    assert r1.status_code == 201
    r2 = client.post(f"/api/office-documents/{ws_oid}/editor-session")
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "EDITOR_SESSION_CONFLICT"
    # 关闭后可再开
    assert client.post(f"/api/editor-sessions/{r1.json()['session_id']}/close").status_code == 200
    r3 = client.post(f"/api/office-documents/{ws_oid}/editor-session")
    assert r3.status_code == 201


def test_session_save_fence_conflict_409(client):
    ws_oid = _setup_ws(client)
    sid = client.post(f"/api/office-documents/{ws_oid}/editor-session").json()["session_id"]
    # 会话打开期间 current 被外部推进（如 AI apply / restore）
    from app.services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )

    s = _db_session()
    try:
        od = s.get(OfficeDocument, ws_oid)
        svc = OfficeDocumentService(session=s, adapter=OfficeDocumentTableAdapter(s))
        svc.create_version(od, _docx_bytes("外部写入的新版本。"), source="ai_generate")
        s.commit()
    finally:
        s.close()
    rs = client.put(f"/api/editor-sessions/{sid}/file", content=_docx_bytes("编辑器内的旧基线修改。"))
    assert rs.status_code == 409
    assert rs.json()["detail"]["code"] == "EDITOR_SESSION_CONFLICT"
    assert "版本冲突" in rs.json()["detail"]["detail"]


def test_session_blocks_ai_material_restore(client):
    """会话期间：AI 提案 claim / Material 插入 / restore 一律 423。"""
    ws_oid = _setup_ws(client)
    base_vid, _ = _current(ws_oid)
    sid = client.post(f"/api/office-documents/{ws_oid}/editor-session").json()["session_id"]

    # AI 提案 claim（已审批路径）→ 423
    rp = client.post(f"/api/office-documents/{ws_oid}/ai-proposals", json={
        "base_workspace_version_id": base_vid, "text": "AI 补充段落。", "model": "glm-4-flash",
    })
    assert rp.status_code == 200, rp.text
    pid = rp.json()["id"]
    client.post(f"/api/ai-proposals/{pid}/approve")
    ra = client.post(f"/api/ai-proposals/{pid}/apply")
    assert ra.status_code == 423, ra.text

    # Material 插入 → 423
    rm = client.post(f"/api/office-documents/{ws_oid}/material-insert", json={
        "base_workspace_version_id": base_vid,
        "material_id": "nonexistent",
        "material_office_document_id": "nonexistent",
        "material_version_id": "nonexistent",
        "insert_mode": "append_end",
    })
    assert rm.status_code == 423, rm.text

    # restore → 423
    rr = client.post(f"/api/office-documents/{ws_oid}/versions/{base_vid}/restore", json={})
    assert rr.status_code == 423, rr.text

    # 关闭会话后 restore 不再受阻（423 消失）
    client.post(f"/api/editor-sessions/{sid}/close")
    rr2 = client.post(f"/api/office-documents/{ws_oid}/versions/{base_vid}/restore", json={})
    assert rr2.status_code != 423


def test_session_expiry_reclaimed(client, monkeypatch):
    """异常退出：last_seen 超时的会话被懒回收，锁可重新获取。"""
    from app.models import EditorSession
    from app.services.editor_session import SESSION_TTL

    ws_oid = _setup_ws(client)
    sid = client.post(f"/api/office-documents/{ws_oid}/editor-session").json()["session_id"]
    # 模拟异常退出：last_seen 回拨超过 TTL
    s = _db_session()
    try:
        row = s.get(EditorSession, sid)
        row.last_seen = row.last_seen.replace(tzinfo=None) - SESSION_TTL - __import__("datetime").timedelta(minutes=1)
        s.commit()
    finally:
        s.close()
    r = client.post(f"/api/office-documents/{ws_oid}/editor-session")
    assert r.status_code == 201  # 旧会话已被回收，新会话成功
    # 旧会话保存 → 已关闭
    rs = client.put(f"/api/editor-sessions/{sid}/file", content=_docx_bytes("迟到的保存。"))
    assert rs.status_code == 409


def test_session_rejects_invalid_package(client):
    ws_oid = _setup_ws(client)
    sid = client.post(f"/api/office-documents/{ws_oid}/editor-session").json()["session_id"]
    rs = client.put(f"/api/editor-sessions/{sid}/file", content=b"not a zip")
    assert rs.status_code == 409
    assert "DOCX" in rs.json()["detail"]["detail"]


def test_session_rejects_trial_contaminated_package(client):
    """D-1R4：试用构建写入正文的产物不得落为业务版本（验收阻断在保存路径生效）。"""
    import io as _io
    import zipfile as _zf

    from app.services.editor_session import detect_trial_content, validate_docx_package
    from app.services.editor_session import EditorSessionError as ESE
    from app.services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )

    ws_oid = _setup_ws(client)
    sid = client.post(f"/api/office-documents/{ws_oid}/editor-session").json()["session_id"]

    s = _db_session()
    try:
        from app.models import OfficeDocument

        od = s.get(OfficeDocument, ws_oid)
        svc = OfficeDocumentService(session=s, adapter=OfficeDocumentTableAdapter(s))
        cur = svc.get_current_version(od)
        base_bytes = svc.adapter.read_file(cur.file_path)
        cur_id = cur.id
    finally:
        s.close()

    # 构造含试用标记的包（模拟试用构建保存产物）
    zin = _zf.ZipFile(_io.BytesIO(base_bytes))
    buf = _io.BytesIO()
    with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as zout:
        for n in zin.namelist():
            blob = zin.read(n)
            if n == "word/document.xml":
                blob = blob.replace(
                    b"</w:body>",
                    b"<w:p><w:r><w:t>For evaluation purposes only. "
                    b"Please register an existing license (devexpress.com/DX1000)</w:t></w:r></w:p></w:body>",
                )
            zout.writestr(n, blob)
    polluted = buf.getvalue()
    assert detect_trial_content(polluted), "注入的试用标记应被检出"

    rs = client.put(f"/api/editor-sessions/{sid}/file", content=polluted)
    assert rs.status_code == 409
    assert rs.json()["detail"]["detail"].startswith("TRIAL_CONTENT_REJECTED")

    # 版本未被污染推进
    s = _db_session()
    try:
        from app.models import OfficeDocument

        od = s.get(OfficeDocument, ws_oid)
        cur2 = OfficeDocumentService(session=s, adapter=OfficeDocumentTableAdapter(s)).get_current_version(od)
        assert cur2.id == cur_id
    finally:
        s.close()
    with pytest.raises(ESE):
        validate_docx_package(polluted)
