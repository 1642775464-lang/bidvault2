"""B-1c-4：Source 恢复（通用 Office 版本恢复）闭环测试。

新语义：恢复=创建新版本（source=restore, restored_from=目标版本），current 指向新版本，
SourceDocumentProcessor 同步回滚派生数据；全部基于 office 表。
"""

from __future__ import annotations

import hashlib
import io
import os
import time
from pathlib import Path

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


BASE = "投标人须知前附表\n递交截止时间：2026-10-01 09:00\n"


def _setup(client, monkeypatch) -> tuple[str, str]:
    pid = client.post("/api/projects", json={"name": "恢复项目"}).json()["id"]
    r = client.post(
        "/api/documents/upload",
        files=[("files", ("恢复.docx", _docx_bytes(BASE), "application/octet-stream"))],
        data={"kind": "tender", "project_id": pid, "is_secret": "false"},
    )
    doc_id = r.json()["created"][0]["id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        d = next(x for x in docs if x["id"] == doc_id)
        if d["status"] == 2:
            break
        time.sleep(0.3)
    od_id = next(
        x["office_document_id"] for x in client.get("/api/documents").json()["items"] if x["id"] == doc_id
    )
    # 建 v2（编辑保存）
    import app.routers.onlyoffice as oo
    import app.services.onlyoffice_svc as svc

    content = _docx_bytes(BASE + "\n只应出现在 v2 的句子。")

    def fake(url: str, dest) -> None:  # noqa: ARG001
        Path(dest).write_bytes(content)

    monkeypatch.setattr(svc, "download_to", fake)
    monkeypatch.setattr(oo.onlyoffice_svc, "download_to", fake)
    client.post(f"/api/onlyoffice/callback/{doc_id}", json={"status": 2, "url": "http://ds/x", "users": ["本地用户"]})
    deadline = time.time() + 30
    while time.time() < deadline:
        s = _db()
        try:
            n = s.execute(
                text("SELECT count(*) FROM office_document_versions WHERE office_document_id=:o"),
                {"o": od_id},
            ).scalar()
        finally:
            s.close()
        if n >= 2:
            break
        time.sleep(0.3)
    return doc_id, od_id


def _versions(od_id: str) -> list[dict]:
    s = _db()
    try:
        rows = s.execute(
            text("SELECT v.id, v.version, v.file_hash, v.source, v.restored_from_version_id, o.current_version_id "
                 "FROM office_documents o JOIN office_document_versions v ON v.office_document_id=o.id "
                 "WHERE o.id=:o ORDER BY v.version"),
            {"o": od_id},
        ).fetchall()
    finally:
        s.close()
    return [
        {"id": r[0], "version": r[1], "file_hash": r[2], "source": r[3],
         "restored_from_version_id": r[4], "is_current": r[0] == r[5]}
        for r in rows
    ]


def test_restore_creates_new_office_version(client, monkeypatch):
    """恢复 v1 → 创建 v3（source=restore）+ current=v3 + 派生数据回滚。"""
    doc_id, od_id = _setup(client, monkeypatch)
    vs = _versions(od_id)
    v1 = next(v for v in vs if v["version"] == 1)
    r = client.post(f"/api/onlyoffice/versions/{doc_id}/{v1['id']}/restore", json={"comment": "回滚验收"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 3 and body["restored_from"] == 1

    deadline = time.time() + 30
    vs2: list[dict] = []
    while time.time() < deadline:
        vs2 = _versions(od_id)
        if len(vs2) >= 3:
            break
        time.sleep(0.3)
    v3 = next(v for v in vs2 if v["version"] == 3)
    assert v3["source"] == "restore" and v3["restored_from_version_id"] == v1["id"]
    assert v3["file_hash"] == v1["file_hash"], "恢复版本 hash 应等于来源版本"
    assert v3["is_current"] and not next(v for v in vs2 if v["version"] == 2)["is_current"], "current 应指向 v3"
    # 派生数据回滚（SourceDocumentProcessor）
    deadline = time.time() + 30
    txt = ""
    while (time.time() < deadline and "只应出现在 v2 的句子" in txt) or txt == "":
        time.sleep(0.5)
        s = _db()
        try:
            txt = s.execute(text("SELECT content_text FROM documents WHERE id=:i"), {"i": doc_id}).scalar() or ""
        finally:
            s.close()
        if "只应出现在 v2 的句子" not in txt and txt:
            break
    assert "只应出现在 v2 的句子" not in txt, "恢复后派生数据未回滚"
    # 历史版本不可变：v1 文件字节不变
    import pathlib

    from app.config import get_settings

    s = _db()
    try:
        v1_fp = s.execute(
            text("SELECT file_path FROM office_document_versions WHERE id=:i"), {"i": v1["id"]}
        ).scalar()
    finally:
        s.close()
    data = (pathlib.Path(get_settings().files_dir) / v1_fp).read_bytes()
    assert hashlib.sha256(data).hexdigest() == v1["file_hash"], "历史版本文件被改动"


def test_restore_current_version_rejected(client, monkeypatch):
    doc_id, od_id = _setup(client, monkeypatch)
    vs = _versions(od_id)
    cur = next(v for v in vs if v["is_current"])
    r = client.post(f"/api/onlyoffice/versions/{doc_id}/{cur['id']}/restore", json={})
    assert r.status_code == 409, r.text
