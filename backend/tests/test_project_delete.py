"""项目级联删除：文档+拆解+工作稿+工作区项目文件夹删除；素材保留仅解除关联。

项目负责人 2026-10-01 指令：删除项目时一并删除相关招标文件及项目文件夹，素材库内容不删除。
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.db import get_session, make_engine
from app.main import app

_COUNT_SQL = {
    "projects": text("SELECT count(*) FROM projects"),
    "documents": text("SELECT count(*) FROM documents"),
    "office_documents": text("SELECT count(*) FROM office_documents"),
    "office_document_versions": text("SELECT count(*) FROM office_document_versions"),
    "extract_items": text("SELECT count(*) FROM extract_items"),
    "risk_items": text("SELECT count(*) FROM risk_items"),
    "materials": text("SELECT count(*) FROM materials"),
    "workspace_documents": text("SELECT count(*) FROM workspace_documents"),
}


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


def _docx_bytes(body: str) -> bytes:
    buf = io.BytesIO()
    d = Docx()
    d.add_paragraph(body)
    d.save(buf)
    return buf.getvalue()


def _mk_project_with_doc(client, name: str) -> str:
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    r = client.post(
        "/api/documents/upload",
        files=[("files", (name + ".docx", _docx_bytes("投标人须知前附表 示例内容"), "application/octet-stream"))],
        data={"kind": "tender", "project_id": pid, "is_secret": "false"},
    )
    assert r.status_code == 200, r.text
    return pid


def _counts():
    s = _db()
    try:
        return {k: s.execute(sql).scalar() for k, sql in _COUNT_SQL.items()}
    finally:
        s.close()


def test_delete_project_cascades_docs_and_keeps_materials(client):
    pid = _mk_project_with_doc(client, "待删项目A")
    import time

    deadline = time.time() + 40
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        if all(x["status"] == 2 for x in docs):
            break
        time.sleep(0.3)
    client.post("/api/materials", json={"title": "A项目素材", "cat": "其他", "type": "文本"})
    mats = client.get("/api/materials").json()["items"]
    mat_id = mats[0]["id"]
    client.post("/api/materials/" + mat_id + "/use", json={"project_id": pid})

    before = _counts()
    assert before["projects"] >= 1 and before["documents"] >= 1

    r = client.delete("/api/projects/" + pid)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["documents"] >= 1

    after = _counts()
    assert after["projects"] == before["projects"] - 1
    assert after["documents"] == before["documents"] - 1
    mats_after = client.get("/api/materials").json()["items"]
    assert any(m["id"] == mat_id for m in mats_after), "素材被误删"


def test_deleted_project_disappears_from_lists(client):
    pid = _mk_project_with_doc(client, "待删项目B")
    assert client.delete("/api/projects/" + pid).status_code == 200
    assert all(x["id"] != pid for x in client.get("/api/projects?mode=active").json()["items"])
    assert all(x["id"] != pid for x in client.get("/api/projects?mode=archived").json()["items"])
    assert client.delete("/api/projects/" + pid).status_code == 404  # 幂等保护


def test_workspace_folder_removed_with_project(client, tmp_path):
    """接入工作区的项目：删除时工作区项目文件夹一并移除。"""
    ws_root = tmp_path / "ws-root"
    r = client.post("/api/workspace", json={"path": str(ws_root), "name": "删除测试工作区"})
    assert r.status_code in (200, 201), r.text
    pid = _mk_project_with_doc(client, "工作区项目C")
    r2 = client.post(
        "/api/documents/upload",
        files=[("files", ("投标准备.docx", _docx_bytes("内容"), "application/octet-stream"))],
        data={"kind": "tender", "project_id": pid, "is_secret": "false"},
    )
    assert r2.status_code == 200
    proj = next(x for x in client.get("/api/projects?mode=active").json()["items"] if x["id"] == pid)
    rel = proj.get("workspace_rel")
    if rel:  # 已接入工作区
        folder = ws_root / rel
        assert folder.is_dir(), "项目文件夹未创建：" + str(folder)
    assert client.delete("/api/projects/" + pid).status_code == 200
    if rel:
        assert not (ws_root / rel).exists(), "项目文件夹未随项目删除"
