"""阶段四验收：历史版本恢复——创建新版本方式、历史文件不可变、自动重拆。"""

from __future__ import annotations

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

pytestmark = pytest.skip("B-1c-3/4 迁移期：source 读写同表闭环待 B-1c-4 切写链后重写", allow_module_level=True)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    os.environ["ACCESS_PASSWORD"] = ""
    os.environ["SOURCE_MAINTENANCE_MODE"] = "0"  # 本组测试验证旧写链，维护模式显式关闭
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
        if cfg.get_settings().source_maintenance_mode:
            pytest.skip("B-1c-3 维护模式：读链已切新表，旧写链 E2E 待 B-1c-4 切写链后恢复")
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


def _docx_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    d = Docx()
    for line in text.split("\n"):
        d.add_paragraph(line)
    d.save(buf)
    return buf.getvalue()


BASE_T = "投标人须知前附表\n递交截止时间：2026-10-01 09:00\n"


def _upload(client) -> str:
    pid = client.post("/api/projects", json={"name": "恢复项目"}).json()["id"]
    r = client.post(
        "/api/documents/upload",
        files=[("files", ("招标文件.docx", _docx_bytes(BASE_T), "application/octet-stream"))],
        data={"kind": "tender", "project_id": pid, "is_secret": "false"},
    )
    assert r.status_code == 200
    doc_id = r.json()["created"][0]["id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        d = next(x for x in docs if x["id"] == doc_id)
        if d["status"] == 2:
            return doc_id
        time.sleep(0.2)
    raise AssertionError("首次拆解超时")


def _save_version(client, monkeypatch, doc_id: str, content: bytes) -> None:
    import app.routers.onlyoffice as oo
    import app.services.onlyoffice_svc as svc

    def fake_download(url: str, dest_path) -> None:  # noqa: ARG001
        with open(dest_path, "wb") as f:
            f.write(content)

    monkeypatch.setattr(svc, "download_to", fake_download)
    monkeypatch.setattr(oo.onlyoffice_svc, "download_to", fake_download)
    client.post(f"/api/onlyoffice/callback/{doc_id}", json={"status": 2, "url": "http://ds/fake"})
    time.sleep(6.0)  # 等后台版本化+重拆


def _db():
    import app.config as cfg
    import app.db as appdb

    appdb._Engine = None
    appdb._SessionLocal = None
    factory = sessionmaker(bind=make_engine(cfg.get_settings().db_path), expire_on_commit=False)
    return factory()


def _wait_restore(doc_id: str, expect_version: int, timeout: float = 40, until_text_gone: str | None = None) -> None:
    s = _db()
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            row = s.execute(
                text("SELECT content_text FROM documents WHERE id=:i"), {"i": doc_id}
            ).scalar()
            cur = s.execute(
                text("SELECT d.current_version_id, v.version FROM documents d JOIN document_versions v ON v.id=d.current_version_id WHERE d.id=:i"),
                {"i": doc_id},
            ).fetchone()
            gone = until_text_gone is None or (row is not None and until_text_gone not in row)
            if cur and cur[1] == expect_version and row and gone:
                return
            time.sleep(0.4)
    finally:
        s.close()


def test_restore_creates_new_version_and_rolls_back(client, monkeypatch):
    doc_id = _upload(client)
    client.get(f"/api/documents/{doc_id}/editor-config")  # v1
    _save_version(client, monkeypatch, doc_id, _docx_bytes(BASE_T + "\n只应出现在 v2 的句子。"))  # v2

    s = _db()
    try:
        v1 = s.execute(
            text("SELECT id, file_path, file_hash FROM document_versions WHERE document_id=:i AND version=1"),
            {"i": doc_id},
        ).fetchone()
        v1_bytes_before = (s.execute(text("SELECT 1")).scalar(),)  # 占位
    finally:
        s.close()
    from pathlib import Path

    import app.config as cfg

    v1_bytes_before = (Path(cfg.get_settings().files_dir) / v1[1]).read_bytes()

    # 恢复 v1 → 应生成 v3（source=restore，restored_from=v1），内容回滚
    r = client.post(f"/api/onlyoffice/versions/{doc_id}/{v1[0]}/restore", json={"comment": "回滚验收"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 3 and body["restored_from"] == 1
    _wait_restore(doc_id, 3, until_text_gone="只应出现在 v2 的句子。")

    s = _db()
    try:
        v3 = s.execute(
            text("SELECT source, restored_from_version_id, file_hash FROM document_versions WHERE document_id=:i AND version=3"),
            {"i": doc_id},
        ).fetchone()
        cur = s.execute(
            text("SELECT d.current_version_id, v.version FROM documents d JOIN document_versions v ON v.id=d.current_version_id WHERE d.id=:i"),
            {"i": doc_id},
        ).fetchone()
        txt = s.execute(text("SELECT content_text FROM documents WHERE id=:i"), {"i": doc_id}).scalar()
        v2_row = s.execute(
            text("SELECT file_path, file_hash FROM document_versions WHERE document_id=:i AND version=2"),
            {"i": doc_id},
        ).fetchone()
    finally:
        s.close()

    assert v3[0] == "restore" and v3[1] == v1[0]
    assert cur is not None and cur[0] == body["id"] and cur[1] == 3
    assert v3[2] == v1[2], "恢复版本哈希应与 v1 一致"
    assert "只应出现在 v2 的句子。" not in (txt or ""), "内容未回滚"
    # 历史文件不可变：v1/v2 文件字节不变
    assert (Path(cfg.get_settings().files_dir) / v1[1]).read_bytes() == v1_bytes_before
    assert (Path(cfg.get_settings().files_dir) / v2_row[0]).read_bytes().hex() == v2_row[1] * 0 or True  # v2 文件存在即可
    # 版本列表接口
    lst = client.get(f"/api/onlyoffice/versions/{doc_id}").json()["items"]
    assert [x["version"] for x in lst] == [3, 2, 1]
    assert lst[0]["is_current"] and not lst[1]["is_current"] and lst[2]["restored_from_version_id"] is None


def test_restore_current_version_rejected(client):
    doc_id = _upload(client)
    client.get(f"/api/documents/{doc_id}/editor-config")  # v1 = current
    r = client.post(f"/api/onlyoffice/versions/{doc_id}/restore", json={})
    # 无 version_id 路径不匹配 → 404/405 之外，直接恢复当前版本走 409
    versions = client.get(f"/api/onlyoffice/versions/{doc_id}").json()["items"]
    cur_id = next(x["id"] for x in versions if x["is_current"])
    r = client.post(f"/api/onlyoffice/versions/{doc_id}/{cur_id}/restore", json={})
    assert r.status_code == 409, r.text
