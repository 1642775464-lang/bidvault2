"""B-1c-4：Source 正式写链（OfficeDocumentVersion）闭环测试。

新语义（读=写=office 表）：
- status=6 仅工作快照（office 不建版本、current 不变）
- status=2 建 office_document_versions + current 原子前移 + SourceDocumentProcessor 更新派生
- 同内容去重；防串写（stale version skipped）；连发两拍最终一致
- 全部断言基于 office 表（documents.current_version_id/stored_name 为 deprecated，不再断言）
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
    os.environ["SOURCE_MAINTENANCE_MODE"] = "0"  # 本组测试验证写链闭环（正式态）
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


def _upload(client) -> tuple[str, str]:
    """上传 → 等拆解完成 → 返回 (doc_id, office_document_id)。"""
    pid = client.post("/api/projects", json={"name": "写链项目"}).json()["id"]
    r = client.post(
        "/api/documents/upload",
        files=[("files", ("写链.docx", _docx_bytes(BASE), "application/octet-stream"))],
        data={"kind": "tender", "project_id": pid, "is_secret": "false"},
    )
    assert r.status_code == 200
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
    assert od_id, "上传应自动建立 OfficeDocument"
    return doc_id, od_id


def _patch_download(monkeypatch, content: bytes) -> None:
    import app.routers.onlyoffice as oo
    import app.services.onlyoffice_svc as svc

    def fake_download(url: str, dest_path) -> None:  # noqa: ARG001
        Path(dest_path).write_bytes(content)

    monkeypatch.setattr(svc, "download_to", fake_download)
    monkeypatch.setattr(oo.onlyoffice_svc, "download_to", fake_download)


def _fire_callback(client, doc_id: str, status: int, content: bytes, monkeypatch) -> None:
    _patch_download(monkeypatch, content)
    client.post(f"/api/onlyoffice/callback/{doc_id}", json={"status": status, "url": "http://ds/x", "users": ["本地用户"]})


def _wait_versions(od_id: str, count: int, timeout: float = 30) -> list[tuple]:
    """轮询 office 版本表直到达到期望行数（消除线程时序依赖）。"""
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


def _office_current(od_id: str) -> str | None:
    s = _db()
    try:
        return s.execute(
            text("SELECT current_version_id FROM office_documents WHERE id=:o"), {"o": od_id}
        ).scalar()
    finally:
        s.close()


def test_status6_snapshot_only(client, monkeypatch):
    """status=6：仅工作快照——office 不建版本、current 不变。"""
    doc_id, od_id = _upload(client)
    cur0 = _office_current(od_id)
    n0 = len(_wait_versions(od_id, 1))
    _fire_callback(client, doc_id, 6, b"working snapshot", monkeypatch)
    time.sleep(3)
    assert _office_current(od_id) == cur0, "status=6 不得更换 current"
    assert len(_wait_versions(od_id, 1)) == n0, "status=6 不得产生正式版本"
    from app.config import get_settings

    working = get_settings().files_dir / "working" / f"{od_id}.docx"
    assert working.is_file() and working.read_bytes() == b"working snapshot", "工作快照未落盘（office 维度）"


def test_status2_creates_office_version_with_matching_hash(client, monkeypatch):
    """status=2：写 office_document_versions，hash 与落盘文件一致，current 前移，派生更新。"""
    doc_id, od_id = _upload(client)
    content = _docx_bytes(BASE + "\nB1C4_SOURCE_MARK 新增句子。")
    _fire_callback(client, doc_id, 2, content, monkeypatch)
    rows = _wait_versions(od_id, 2)
    assert len(rows) == 2, rows
    latest = rows[-1]
    assert latest[1] == hashlib.sha256(content).hexdigest(), "版本 hash 与保存内容不一致"
    assert latest[2] == "onlyoffice"
    # current 前移（office 真源）
    s = _db()
    try:
        cur_hash = s.execute(
            text("SELECT v.file_hash FROM office_documents o JOIN office_document_versions v "
                 "ON v.id=o.current_version_id WHERE o.id=:o"),
            {"o": od_id},
        ).scalar()
        txt = s.execute(text("SELECT content_text FROM documents WHERE id=:i"), {"i": doc_id}).scalar()
    finally:
        s.close()
    assert cur_hash == latest[1], "current 未指向新版本"
    # 派生数据（SourceDocumentProcessor）
    deadline = time.time() + 30
    while time.time() < deadline and "B1C4_SOURCE_MARK" not in (txt or ""):
        time.sleep(0.5)
        s = _db()
        try:
            txt = s.execute(text("SELECT content_text FROM documents WHERE id=:i"), {"i": doc_id}).scalar()
        finally:
            s.close()
    assert "B1C4_SOURCE_MARK" in (txt or ""), "派生 content_text 未更新"
    # 重开 key 切到 v2（office namespace）
    cfg2 = client.get(f"/api/documents/{doc_id}/editor-config").json()
    assert cfg2["config"]["document"]["key"].startswith(f"{od_id}-v2-")


def test_status2_same_content_no_new_version(client, monkeypatch):
    doc_id, od_id = _upload(client)
    content = _docx_bytes(BASE + "\n第一次修改。")
    _fire_callback(client, doc_id, 2, content, monkeypatch)
    rows1 = _wait_versions(od_id, 2)
    _fire_callback(client, doc_id, 2, content, monkeypatch)
    time.sleep(4)
    assert len(_wait_versions(od_id, 2)) == len(rows1), "同内容不应重复建版本"


def test_stale_version_parse_skipped(client, monkeypatch):
    """防串写：以旧版本 id 解析时必须跳过（current 已前移）。"""
    doc_id, od_id = _upload(client)
    content = _docx_bytes(BASE + "\n第二次内容。")
    _fire_callback(client, doc_id, 2, content, monkeypatch)
    _wait_versions(od_id, 2)
    # 等 v2 的 SourceDocumentProcessor 完成（派生落地）再取基线，避免与后台线程竞态
    deadline = time.time() + 60
    while time.time() < deadline:
        s = _db()
        try:
            t = s.execute(text("SELECT content_text FROM documents WHERE id=:i"), {"i": doc_id}).scalar() or ""
        finally:
            s.close()
        if "第二次内容" in t:
            break
        time.sleep(0.5)

    s = _db()
    try:
        v1_id = s.execute(
            text("SELECT id FROM office_document_versions WHERE office_document_id=:o AND version=1"),
            {"o": od_id},
        ).scalar()
        items_before = s.execute(
            text("SELECT count(*) FROM extract_items WHERE doc_id=:i"), {"i": doc_id}
        ).scalar()
    finally:
        s.close()
    assert v1_id, "v1 应存在"

    from app.models import Document
    from app.services.ingest.pipeline import process_document

    s = _db()
    try:
        doc = s.get(Document, doc_id)
        res = process_document(s, doc, version_id=v1_id)
        assert res.error == "stale-version-skipped", f"应跳过旧版本解析：{res}"
    finally:
        s.close()
    s = _db()
    try:
        items_after = s.execute(
            text("SELECT count(*) FROM extract_items WHERE doc_id=:i"), {"i": doc_id}
        ).scalar()
    finally:
        s.close()
    assert items_after == items_before, "旧版本解析覆盖了新版本结果"


def test_rapid_double_save_final_consistency(client, monkeypatch):
    """连发两拍：最终 DOCX / office current / 派生数据同属最新版本。"""
    doc_id, od_id = _upload(client)
    content_b = _docx_bytes(BASE + "\n快速保存 B。")
    _fire_callback(client, doc_id, 2, content_b, monkeypatch)
    _wait_versions(od_id, 2)
    content_c = _docx_bytes(BASE + "\n快速保存 C。")
    _fire_callback(client, doc_id, 2, content_c, monkeypatch)
    rows = _wait_versions(od_id, 3)

    latest = rows[-1]
    assert latest[1] == hashlib.sha256(content_c).hexdigest(), f"最终版本不是 C：{rows}"
    import pathlib

    from app.config import get_settings

    s = _db()
    try:
        fp = s.execute(
            text("SELECT v.file_path, v.file_hash FROM office_documents o "
                 "JOIN office_document_versions v ON v.id=o.current_version_id WHERE o.id=:o"),
            {"o": od_id},
        ).fetchone()
    finally:
        s.close()
    final_bytes = (pathlib.Path(get_settings().files_dir) / fp[0]).read_bytes()
    assert hashlib.sha256(final_bytes).hexdigest() == latest[1], "最终 DOCX 与版本记录不一致"
    assert fp[1] == latest[1], "current 与最新版本不一致"
    # 派生数据最终属于 C
    deadline = time.time() + 30
    txt = ""
    while time.time() < deadline and "快速保存 C" not in txt:
        time.sleep(0.5)
        s = _db()
        try:
            txt = s.execute(text("SELECT content_text FROM documents WHERE id=:i"), {"i": doc_id}).scalar() or ""
        finally:
            s.close()
    assert "快速保存 C" in txt, "派生数据不属于最新版本"
