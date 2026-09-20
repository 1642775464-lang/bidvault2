"""阶段三验收：ONLYOFFICE callback 保存生命周期 + 版本管理 + 防串写。

- status=6：只写工作快照，不产生正式版本、不换 current_version_id
- status=2：创建 DocumentVersion，file_hash 与保存内容一致，current_version_id 原子前移
- 内容未变化：不重复建版本
- 防串写：旧版本解析完成时不得覆盖新版本结果
- 连发两拍：最终 DOCX / current_version_id / 拆解结果同属最新版本
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


TENDER = (
    "投标人须知前附表\n"
    "递交截止时间：2026-10-01 09:00\n"
    "投标保证金：20万元\n"
)


def _docx_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    d = Docx()
    for line in text.split("\n"):
        d.add_paragraph(line)
    d.save(buf)
    return buf.getvalue()


def _upload(client) -> str:
    pid = client.post("/api/projects", json={"name": "版本管理项目"}).json()["id"]
    r = client.post(
        "/api/documents/upload",
        files=[("files", ("招标文件.docx", _docx_bytes(TENDER), "application/octet-stream"))],
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


def _wait_log(doc_id: str, keyword: str, timeout: float = 15) -> str:
    import app.routers.onlyoffice as oo

    deadline = time.time() + timeout
    while time.time() < deadline:
        state = oo._callback_log.get(doc_id, "")
        if keyword in state:
            return state
        time.sleep(0.2)
    return oo._callback_log.get(doc_id, "")


def _db_session():
    import app.config as cfg
    import app.db as appdb

    appdb._Engine = None
    appdb._SessionLocal = None
    factory = sessionmaker(bind=make_engine(cfg.get_settings().db_path), expire_on_commit=False)
    return factory()


def _current_version_id(doc_id: str):
    s = _db_session()
    try:
        from sqlalchemy import text

        return s.execute(text("SELECT current_version_id FROM documents WHERE id=:i"), {"i": doc_id}).scalar()
    finally:
        s.close()


def _version_rows(doc_id: str) -> list[tuple]:
    s = _db_session()
    try:
        from sqlalchemy import text

        return s.execute(
            text("SELECT version, file_hash, source FROM document_versions WHERE document_id=:i ORDER BY version"),
            {"i": doc_id},
        ).fetchall()
    finally:
        s.close()


def _patch_download(monkeypatch, content: bytes) -> None:
    import app.services.onlyoffice_svc as svc

    def fake_download(url: str, dest_path) -> None:  # noqa: ARG001
        with open(dest_path, "wb") as f:
            f.write(content)

    monkeypatch.setattr(svc, "download_to", fake_download)
    import app.routers.onlyoffice as oo

    monkeypatch.setattr(oo.onlyoffice_svc, "download_to", fake_download)


def test_status6_snapshot_no_official_version(client, monkeypatch):
    doc_id = _upload(client)
    # 初始化 v1
    client.get(f"/api/documents/{doc_id}/editor-config")
    v0 = _current_version_id(doc_id)
    assert v0, "v1 未初始化"
    rows0 = _version_rows(doc_id)

    content = b"working snapshot bytes"
    _patch_download(monkeypatch, content)
    client.post(f"/api/onlyoffice/callback/{doc_id}", json={"status": 6, "url": "http://ds/fake"})
    _wait_log(doc_id, "工作快照")

    assert _current_version_id(doc_id) == v0, "status=6 不得更换 current_version_id"
    assert _version_rows(doc_id) == rows0, "status=6 不得产生正式版本"
    from app.config import get_settings

    working = get_settings().files_dir / "working" / f"{doc_id}.docx"
    assert working.is_file() and working.read_bytes() == content, "工作快照未落盘"


def test_status2_creates_version_with_matching_hash(client, monkeypatch):
    doc_id = _upload(client)
    client.get(f"/api/documents/{doc_id}/editor-config")
    before = len(_version_rows(doc_id))

    content = _docx_bytes(TENDER + "\n新增条款：履约保证金 5%。")
    _patch_download(monkeypatch, content)
    client.post(f"/api/onlyoffice/callback/{doc_id}", json={"status": 2, "url": "http://ds/fake", "users": ["本地用户"]})
    _wait_log(doc_id, "v2")

    rows = _version_rows(doc_id)
    assert len(rows) == before + 1, rows
    latest = rows[-1]
    assert latest[0] == before + 1, "版本号应顺序递增"
    assert latest[1] == hashlib.sha256(content).hexdigest(), "版本哈希与保存内容不一致"
    assert latest[2] == "onlyoffice"
    assert _current_version_id(doc_id) is not None
    # 重拆完成后回调状态包含重拆
    _wait_log(doc_id, "已保存并重拆 v2")


def test_status2_same_content_no_new_version(client, monkeypatch):
    doc_id = _upload(client)
    client.get(f"/api/documents/{doc_id}/editor-config")
    content = _docx_bytes(TENDER + "\n第一次修改。")
    _patch_download(monkeypatch, content)
    client.post(f"/api/onlyoffice/callback/{doc_id}", json={"status": 2, "url": "http://ds/fake"})
    _wait_log(doc_id, "正式版本")
    n1 = len(_version_rows(doc_id))

    # 同内容再次 status=2 → 不重复建版本
    client.post(f"/api/onlyoffice/callback/{doc_id}", json={"status": 2, "url": "http://ds/fake"})
    time.sleep(2.0)
    assert len(_version_rows(doc_id)) == n1, "同内容不应重复建版本"


def test_stale_version_parse_skipped(client, monkeypatch):
    """防串写：解析目标版本落后于 current_version_id 时，结果必须被放弃。"""
    doc_id = _upload(client)
    client.get(f"/api/documents/{doc_id}/editor-config")  # v1
    content = _docx_bytes(TENDER + "\n第二次内容。")
    _patch_download(monkeypatch, content)
    client.post(f"/api/onlyoffice/callback/{doc_id}", json={"status": 2, "url": "http://ds/fake"})
    _wait_log(doc_id, "正式版本")
    v2_id = _current_version_id(doc_id)

    from sqlalchemy import text

    s = _db_session()
    try:
        old_v1 = s.execute(
            text("SELECT id FROM document_versions WHERE document_id=:i AND version=1"), {"i": doc_id}
        ).scalar()
        items_before = s.execute(
            text("SELECT count(*) FROM extract_items ei JOIN documents d ON d.id=ei.doc_id WHERE d.id=:i"),
            {"i": doc_id},
        ).scalar()
    finally:
        s.close()

    # 以 v1（旧版本）身份跑解析——current 已是 v2 → 必须跳过
    from app.services.ingest.pipeline import process_document

    s = _db_session()
    try:
        from app.models import Document

        doc = s.get(Document, doc_id)
        res = process_document(s, doc, version_id=old_v1)
        assert res.error == "stale-version-skipped"
    finally:
        s.close()

    s = _db_session()
    try:
        items_after = s.execute(
            text("SELECT count(*) FROM extract_items ei JOIN documents d ON d.id=ei.doc_id WHERE d.id=:i"),
            {"i": doc_id},
        ).scalar()
        assert items_after == items_before, "旧版本解析覆盖了新版本结果"
        cur = s.execute(text("SELECT current_version_id FROM documents WHERE id=:i"), {"i": doc_id}).scalar()
        assert cur == v2_id
    finally:
        s.close()


def test_rapid_double_save_final_consistency(client, monkeypatch):
    """连发两拍：最终 DOCX / current_version_id / 拆解结果同属最新版本。"""
    doc_id = _upload(client)
    client.get(f"/api/documents/{doc_id}/editor-config")

    content_b = _docx_bytes(TENDER + "\n快速保存 B。")
    _patch_download(monkeypatch, content_b)
    client.post(f"/api/onlyoffice/callback/{doc_id}", json={"status": 2, "url": "http://ds/fake"})
    _wait_log(doc_id, "v")

    content_c = _docx_bytes(TENDER + "\n快速保存 C。")
    _patch_download(monkeypatch, content_c)
    client.post(f"/api/onlyoffice/callback/{doc_id}", json={"status": 2, "url": "http://ds/fake"})

    # 等待最终版本与重拆完成
    deadline = time.time() + 30
    cur = None
    rows = []
    while time.time() < deadline:
        rows = _version_rows(doc_id)
        cur = _current_version_id(doc_id)
        s = _db_session()
        try:
            cur_hash = s.execute(
                text("SELECT file_hash FROM document_versions WHERE id=:i"), {"i": cur}
            ).scalar() if cur else None
        finally:
            s.close()
        if cur_hash == hashlib.sha256(content_c).hexdigest():
            # 再等重拆落地
            log = _wait_log(doc_id, "已保存并重拆 v", timeout=20)
            if "重拆" in log:
                break
        time.sleep(0.3)
    latest = rows[-1]
    assert latest[1] == hashlib.sha256(content_c).hexdigest(), f"最终版本不是 C：{rows}"
    cur_hash = None
    s = _db_session()
    try:
        cur_hash = s.execute(
            text("SELECT file_hash FROM document_versions WHERE id=:i"), {"i": cur}
        ).scalar()
        stored = s.execute(text("SELECT stored_name FROM documents WHERE id=:i"), {"i": doc_id}).scalar()
        import pathlib

        from app.config import get_settings

        final_bytes = (pathlib.Path(get_settings().files_dir) / stored).read_bytes()
    finally:
        s.close()
    assert cur_hash == latest[1], "current_version_id 与最新版本不一致"
    assert hashlib.sha256(final_bytes).hexdigest() == latest[1], "最终 DOCX 与版本记录不一致"
    s = _db_session()
    try:
        txt = s.execute(text("SELECT content_text FROM documents WHERE id=:i"), {"i": doc_id}).scalar()
    finally:
        s.close()
    assert "快速保存 C" in (txt or ""), "拆解结果不属于最新版本"
