"""阶段六 B-2 验收：Material 在线编辑与版本闭环。

- processor_runs 持久幂等：UNIQUE 抢占 / failed → retry / 重启（新进程内存）不丢
- 通用 callback（/api/office-documents/{oid}/callback）：
  status=6 仅快照不建版本；status=2 建正式版本 + MaterialDocumentProcessor 更新派生数据
- 恢复：vN 恢复旧版 → 新版本 source=restore + 派生数据同步回滚
- Source 链零改动
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
    os.environ["SOURCE_MAINTENANCE_MODE"] = "0"  # 素材编辑闭环测试不受 Source 维护锁影响
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


def _patch_download(monkeypatch, content: bytes) -> None:
    import app.routers.onlyoffice as oo
    import app.services.onlyoffice_svc as svc

    def fake_download(url: str, dest_path) -> None:  # noqa: ARG001
        with open(dest_path, "wb") as f:
            f.write(content)

    monkeypatch.setattr(svc, "download_to", fake_download)
    monkeypatch.setattr(oo.onlyoffice_svc, "download_to", fake_download)
    monkeypatch.setattr(oo, "onlyoffice_svc", oo.onlyoffice_svc)


def _setup_word_material(client) -> tuple[str, str, bytes]:
    """上传 source → v1 → 建素材 → clone 生成 Material OfficeDocument。返回 (material_id, od_id, source_bytes)。"""
    source = _docx_bytes("投标人须知前附表\n递交截止时间：2026-10-01 09:00\n")
    pid = client.post("/api/projects", json={"name": "B2 项目"}).json()["id"]
    r = client.post(
        "/api/documents/upload",
        files=[("files", ("源文件.docx", source, "application/octet-stream"))],
        data={"kind": "tender", "project_id": pid, "is_secret": "false"},
    )
    doc_id = r.json()["created"][0]["id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        if next(x for x in docs if x["id"] == doc_id)["status"] == 2:
            break
        time.sleep(0.2)
    client.get(f"/api/documents/{doc_id}/editor-config")  # v1
    m = client.post("/api/materials", json={"title": "B2 素材", "cat": "投标格式", "type": "文本"}).json()
    r2 = client.post(
        f"/api/materials/{m['id']}/office-document",
        json={"source_document_id": doc_id, "child_range": [0, 1], "comment": "提取"},
    )
    assert r2.status_code == 200, r2.text
    return m["id"], r2.json()["office_document_id"], source


def _wait_cb_log(od_id: str, keyword: str, timeout: float = 20) -> str:
    import app.routers.onlyoffice as oo

    deadline = time.time() + timeout
    while time.time() < deadline:
        state = oo._office_cb_log.get(od_id, "")
        if keyword in state:
            return state
        time.sleep(0.2)
    return oo._office_cb_log.get(od_id, "")


def test_processor_run_persistent_idempotency(tmp_path, monkeypatch):
    """持久幂等：同 (version, type) 二次 try_claim 失败；failed 后可 retry。"""
    import app.config as cfg
    import app.db as appdb
    from app.services.office_document_svc import ProcessorRunTableAdapter

    os.environ["DATA_DIR"] = str(tmp_path / "data")
    monkeypatch.setattr(cfg, "_SETTINGS", None)
    monkeypatch.setattr(appdb, "_Engine", None)
    monkeypatch.setattr(appdb, "_SessionLocal", None)
    engine = make_engine(cfg.get_settings().db_path)
    reg = ProcessorRunTableAdapter(engine)
    assert reg.try_claim("v-x", "material") is True
    assert reg.try_claim("v-x", "material") is False, "UNIQUE 抢占未生效"
    reg.mark_failed("v-x", "material", "boom")
    # failed 仍占用 → 重试接口删除后可重新抢占
    assert reg.try_claim("v-x", "material") is False
    s = _db()
    try:
        s.execute(text("DELETE FROM processor_runs WHERE office_document_version_id='v-x'"))
        s.commit()
    finally:
        s.close()
    assert reg.try_claim("v-x", "material") is True, "failed→retry 后应可重新抢占"
    reg.mark_done("v-x", "material")
    s = _db()
    try:
        row = s.execute(
            text("SELECT status FROM processor_runs WHERE office_document_version_id='v-x'"),
        ).scalar()
        assert row == "done"
    finally:
        s.close()


def test_material_callback_status6_no_version(client, monkeypatch):
    _mid, od_id, _ = _setup_word_material(client)
    before = client.get(f"/api/office-documents/{od_id}/editor-config").json()["version"]
    _patch_download(monkeypatch, b"working snapshot")
    client.post(f"/api/office-documents/{od_id}/callback", json={"status": 6, "url": "http://ds/x"})
    assert "工作快照" in _wait_cb_log(od_id, "工作快照")
    assert client.get(f"/api/office-documents/{od_id}/editor-config").json()["version"] == before, "status=6 不得产生新版本"


def test_material_callback_status2_updates_derived(client, monkeypatch):
    mid, od_id, _ = _setup_word_material(client)
    content = _docx_bytes("投标人须知前附表\n递交截止时间：2026-11-11 10:00\n五、技术要求：供货周期 60 天。\n")
    _patch_download(monkeypatch, content)
    client.post(f"/api/office-documents/{od_id}/callback", json={"status": 2, "url": "http://ds/x", "users": ["本地用户"]})
    _wait_cb_log(od_id, "v2")

    assert client.get(f"/api/office-documents/{od_id}/editor-config").json()["version"] == 2
    mats = {m["id"]: m for m in client.get("/api/materials").json()["items"]}
    m = mats[mid]
    assert "2026-11-11" in (m["text"] or ""), "派生 text 未更新"
    assert m["rows"] is None, "纯段落素材 rows 应为 None（派生镜像当前 DOCX）"
    # sha256 与版本记录一致
    import pathlib

    import app.config as cfg

    s = _db()
    try:
        fp = s.execute(
            text("SELECT v.file_path, v.file_hash FROM office_documents o "
                 "JOIN office_document_versions v ON v.id=o.current_version_id WHERE o.id=:i"),
            {"i": od_id},
        ).fetchone()
    finally:
        s.close()
    data = (pathlib.Path(cfg.get_settings().files_dir) / fp[0]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == fp[1]
    # 重开 key 切到 v2
    cfg2 = client.get(f"/api/office-documents/{od_id}/editor-config").json()
    assert cfg2["config"]["document"]["key"].startswith(f"{od_id}-v2-")


def test_material_restore_rolls_back_derived(client, monkeypatch):
    mid, od_id, _ = _setup_word_material(client)
    v2_content = _docx_bytes("投标人须知前附表\n递交截止时间：2026-11-11 10:00\n新句子只在 v2。\n")
    _patch_download(monkeypatch, v2_content)
    client.post(f"/api/office-documents/{od_id}/callback", json={"status": 2, "url": "http://ds/x"})
    _wait_cb_log(od_id, "v2")

    versions = client.get(f"/api/office-documents/{od_id}/versions").json()["items"]
    v1 = next(v for v in versions if v["version"] == 1)
    r = client.post(f"/api/office-documents/{od_id}/versions/{v1['id']}/restore", json={"comment": "回滚素材"})
    assert r.status_code == 200 and r.json()["version"] == 3

    # 派生数据同步回滚（等 processor）
    deadline = time.time() + 20
    while time.time() < deadline:
        m = next(m for m in client.get("/api/materials").json()["items"] if m["id"] == mid)
        if "新句子只在 v2" not in (m["text"] or ""):
            break
        time.sleep(0.3)
    assert "新句子只在 v2" not in (m["text"] or ""), "恢复后派生数据未回滚"
    assert client.get(f"/api/office-documents/{od_id}/editor-config").json()["version"] == 3
    # Source 链零改动：material 的 source_document_version 指向的版本文件未被改写
    import pathlib

    import app.config as cfg

    s = _db()
    try:
        row = s.execute(
            text("SELECT m.source_document_version_id, v.file_path, v.file_hash FROM materials m "
                 "JOIN office_document_versions v ON v.id=m.source_document_version_id WHERE m.id=:i"),
            {"i": mid},
        ).fetchone()
    finally:
        s.close()
    src_bytes = (pathlib.Path(cfg.get_settings().files_dir) / row[1]).read_bytes()
    assert hashlib.sha256(src_bytes).hexdigest() == row[2], "Source 版本文件被改动"

