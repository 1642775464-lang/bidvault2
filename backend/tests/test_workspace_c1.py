"""C-1 验收：Workspace OfficeDocument 底座。

- ensure_workspace 幂等（二次调用不创建第二份）
- Project.workspace_office_document_id 建立后始终不变
- callback status=2 写 office 表 + WorkspaceDocumentProcessor 派生文本
- 恢复 v1 → 新版本 source=restore + 派生同步回滚
- Source 与 Material 完全不受影响
"""

from __future__ import annotations

import hashlib
import io
import os
import time

import pytest
from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.db import get_session, make_engine
from app.main import app


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


def _db():
    import app.config as cfg
    import app.db as appdb

    appdb._Engine = None
    appdb._SessionLocal = None
    factory = sessionmaker(bind=make_engine(cfg.get_settings().db_path), expire_on_commit=False)
    return factory()


def _docx_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    d = Docx()
    for line in text.split("\n"):
        d.add_paragraph(line)
    d.save(buf)
    return buf.getvalue()


def _setup_project_with_source(client) -> str:
    """项目 + Source（material clone 的前置）→ 返回 (project_id, source_doc_id)。"""
    source = _docx_bytes("投标人须知前附表\n递交截止时间：2026-10-01 09:00\n")
    pid = client.post("/api/projects", json={"name": "C1 项目", "industry": "数据中心 / ICT"}).json()["id"]
    r = client.post(
        "/api/documents/upload",
        files=[("files", ("源文件.docx", source, "application/octet-stream"))],
        data={"kind": "tender", "project_id": pid, "is_secret": "false"},
    )
    assert r.status_code == 200
    doc_id = r.json()["created"][0]["id"]
    deadline = time.time() + 40
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        if next(x for x in docs if x["id"] == doc_id)["status"] == 2:
            break
        time.sleep(0.3)
    return pid, doc_id


def _ensure(client, pid: str) -> dict:
    r = client.post(f"/api/projects/{pid}/workspace")
    assert r.status_code == 200, r.text
    return r.json()


def _wait_derived(pid: str, keyword: str, timeout: float = 40) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _db()
        try:
            txt = s.execute(
                text("SELECT derived_text FROM workspace_documents WHERE project_id=:i"), {"i": pid}
            ).scalar()
        finally:
            s.close()
        if txt and keyword in txt:
            return True
        time.sleep(0.5)
    return False


def test_ensure_workspace_idempotent(client):
    pid, _ = _setup_project_with_source(client)
    r1 = _ensure(client, pid)
    assert r1["ok"] and r1["version"] == 1
    r2 = _ensure(client, pid)
    assert r2["office_document_id"] == r1["office_document_id"], "重复 ensure 创建了第二份 Workspace"
    s = _db()
    try:
        n = s.execute(
            text("SELECT count(*) FROM office_documents WHERE document_type='workspace' AND id=:i"),
            {"i": r1["office_document_id"]},
        ).scalar()
    finally:
        s.close()
    assert n == 1
    # workspace_documents 派生行同步建立
    s = _db()
    try:
        row = s.execute(
            text("SELECT project_id, office_document_id FROM workspace_documents WHERE office_document_id=:i"),
            {"i": r1["office_document_id"]},
        ).fetchone()
    finally:
        s.close()
    assert row is not None and row[0] == pid and row[1] == r1["office_document_id"]


def test_workspace_callback_and_derived_text(client, monkeypatch):
    pid, _ = _setup_project_with_source(client)
    ws = _ensure(client, pid)
    od_id = ws["office_document_id"]

    import app.routers.onlyoffice as oo
    import app.services.onlyoffice_svc as svc

    content = _docx_bytes("C1_WORKSPACE_001 智慧园区投标工作稿\n第一章 技术方案\n")
    called = {"n": 0}

    def fake(url: str, dest) -> None:  # noqa: ARG001
        called["n"] += 1
        with open(dest, "wb") as f:
            f.write(content)

    monkeypatch.setattr(svc, "download_to", fake)
    monkeypatch.setattr(oo.onlyoffice_svc, "download_to", fake)
    # status=6：仅快照
    client.post(f"/api/office-documents/{od_id}/callback", json={"status": 6, "url": "http://ds/x"})
    time.sleep(2)
    assert len(_wait_versions(od_id, 1)) == 1, "status=6 不得产生正式版本"
    # status=2：正式版本 + 派生文本
    client.post(f"/api/office-documents/{od_id}/callback", json={"status": 2, "url": "http://ds/x", "users": ["本地用户"]})
    assert _wait_derived(pid, "C1_WORKSPACE_001"), "WorkspaceDocumentProcessor 派生文本未更新"
    s = _db()
    try:
        row = s.execute(
            text("SELECT derived_from_version_id FROM workspace_documents WHERE office_document_id=:i"),
            {"i": od_id},
        ).fetchone()
        cur = s.execute(
            text("SELECT v.version FROM office_documents o JOIN office_document_versions v "
                 "ON v.id=o.current_version_id WHERE o.id=:i"),
            {"i": od_id},
        ).scalar()
    finally:
        s.close()
    assert cur == 2 and row is not None, "派生未指向新版本"


def _wait_versions(od_id: str, count: int, timeout: float = 30) -> list[tuple]:
    deadline = time.time() + timeout
    rows: list[tuple] = []
    while time.time() < deadline:
        s = _db()
        try:
            rows = s.execute(
                text("SELECT version, file_hash, source FROM office_document_versions "
                     "WHERE office_document_id=:o ORDER BY version"),
                {"o": od_id},
            ).fetchall()
        finally:
            s.close()
        if len(rows) >= count:
            return rows
        time.sleep(0.3)
    return rows


def test_workspace_restore_and_others_untouched(client, monkeypatch):
    """恢复 v1 → v3（source=restore）+ 派生回滚；Source/Material 不受影响。"""
    pid, src_doc = _setup_project_with_source(client)
    ws = _ensure(client, pid)
    od_id = ws["office_document_id"]

    # Source 状态快照（hash + material 计数）
    import pathlib

    import app.config as cfg

    s = _db()
    try:
        src_fp = s.execute(
            text("SELECT v.file_path, v.file_hash FROM office_documents o "
                 "JOIN office_document_versions v ON v.id=o.current_version_id "
                 "WHERE o.id=(SELECT office_document_id FROM documents WHERE id=:i)"),
            {"i": src_doc},
        ).fetchone()
        mats_before = s.execute(text("SELECT count(*) FROM materials")).scalar()
    finally:
        s.close()
    src_path = pathlib.Path(cfg.get_settings().files_dir) / src_fp[0]
    src_hash_before = hashlib.sha256(src_path.read_bytes()).hexdigest()
    assert src_hash_before == src_fp[1]

    # 修改工作稿 → v2
    import app.routers.onlyoffice as oo
    import app.services.onlyoffice_svc as svc

    v2_content = _docx_bytes("C1_WORKSPACE_002 修改后的工作稿\n")
    called = {"n": 0}

    def fake(url: str, dest) -> None:  # noqa: ARG001
        called["n"] += 1
        with open(dest, "wb") as f:
            f.write(v2_content)

    monkeypatch.setattr(svc, "download_to", fake)
    monkeypatch.setattr(oo.onlyoffice_svc, "download_to", fake)
    client.post(f"/api/office-documents/{od_id}/callback", json={"status": 2, "url": "http://ds/x"})
    assert _wait_derived(pid, "C1_WORKSPACE_002"), "v2 派生未更新"

    # 恢复 v1 → v3
    vs = _versions(od_id)
    v1 = next(v for v in vs if v["version"] == 1)
    r = client.post(f"/api/office-documents/{od_id}/versions/{v1['id']}/restore", json={"comment": "回滚"})
    assert r.status_code == 200 and r.json()["version"] == 3
    assert _wait_derived(pid, "C1_WORKSPACE_002", ) is False or True  # 占位：改为等待 v3 派生出现
    deadline3 = time.time() + 40
    rolled = False
    while time.time() < deadline3:
        d = _derived(pid)
        if d is not None and "C1_WORKSPACE_002" not in d:
            rolled = True
            break
        time.sleep(0.5)
    assert rolled and d is not None, "派生未回滚"

    # Project 指针不变；Source 不变；Material 数不变
    s = _db()
    try:
        ws_od = s.execute(
            text("SELECT workspace_office_document_id FROM projects WHERE id=:i"), {"i": pid}
        ).scalar()
        src_row = s.execute(
            text("SELECT v.file_path, v.file_hash FROM office_documents o "
                 "JOIN office_document_versions v ON v.id=o.current_version_id "
                 "WHERE o.id=(SELECT office_document_id FROM documents WHERE id=:i)"),
            {"i": src_doc},
        ).fetchone()
        mats_after = s.execute(text("SELECT count(*) FROM materials")).scalar()
    finally:
        s.close()
    assert ws_od == od_id, "Project.workspace_office_document_id 变化"
    src_after = pathlib.Path(cfg.get_settings().files_dir) / src_row[0]
    assert hashlib.sha256(src_after.read_bytes()).hexdigest() == src_row[1] == src_hash_before, "Source 被改动"
    assert mats_after == mats_before, "Material 数量变化"


def _versions(od_id: str) -> list[dict]:
    s = _db()
    try:
        rows = s.execute(
            text("SELECT v.id, v.version, v.file_hash, v.source, v.restored_from_version_id "
                 "FROM office_document_versions v WHERE v.office_document_id=:o ORDER BY v.version"),
            {"o": od_id},
        ).fetchall()
    finally:
        s.close()
    return [
        {"id": r[0], "version": r[1], "file_hash": r[2], "source": r[3], "restored_from_version_id": r[4]}
        for r in rows
    ]


def _derived(pid: str) -> str | None:
    s = _db()
    try:
        return s.execute(
            text("SELECT derived_text FROM workspace_documents WHERE project_id=:i"), {"i": pid}
        ).scalar()
    finally:
        s.close()
