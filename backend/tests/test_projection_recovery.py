"""D-2B：投影写回 DB 先行协议验收（崩溃窗口收口 + 外部修改保护）。

协议（D-2A §6 v1.1 冻结）：①事务A projection_state(pending)+版本记录 → ②os.replace
→ ③标 synced。崩溃窗口：①后②前=pending+投影旧→重投影；②后③前=pending+投影新→补标。
外部修改保护：投影 hash ≠ DB 版本 hash 且无意图 → 保留原件落事件候选，不覆盖。
"""

from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path

import pytest
from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


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
    from app.db import get_session, make_engine
    from app.main import app

    monkeypatch.setattr(cfg, "_SETTINGS", None)
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
        c.post("/api/workspace", json={"path": str(tmp_path / "ws")})
        yield c
    app.dependency_overrides.clear()


def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    d = Docx()
    d.add_paragraph(text)
    d.save(buf)
    return buf.getvalue()


def _seed_project_doc(client, tmp_path, name: str) -> tuple[str, str, Path]:
    """项目 + 招标文档（自动拆解）→ (pid, doc_id, 投影文件路径)。"""
    r = client.post("/api/projects", json={"name": name}).json()
    pid = r["id"]
    rel = r["workspace_folder"]
    r = client.post("/api/documents/upload",
                    files=[("files", (f"{name}.docx", _docx(f"{name} 正文。"),
                                      "application/octet-stream"))],
                    data={"kind": "tender", "project_id": pid, "decompose": "true"})
    doc_id = r.json()["created"][0]["id"]
    for _ in range(60):
        docs = client.get("/api/documents").json()["items"]
        d = next((x for x in docs if x["id"] == doc_id), None)
        if d and d["status"] == 2 and d.get("office_document_id"):
            break
        time.sleep(1)
    else:
        raise AssertionError("拆解超时")
    docs = client.get("/api/documents").json()["items"]
    d = next(x for x in docs if x["id"] == doc_id)
    proj_file = tmp_path / "ws" / rel / "投标文件" / f"{name}.docx"
    return pid, doc_id, proj_file


def test_crash_window_before_replace_recovers(client, tmp_path):
    """崩溃窗口①：投影丢失（意图记录在 DB）→ 启动恢复以 DB 版本重投影。"""
    pid, doc_id, proj_file = _seed_project_doc(client, tmp_path, "恢复窗口项目")
    proj_file.unlink()
    import app.config as cfg
    from app.db import make_engine
    from app.services.projection_recovery import reconcile_projections

    s = sessionmaker(bind=make_engine(cfg.get_settings().db_path), expire_on_commit=False)()
    try:
        rec = reconcile_projections(s, pid, tmp_path / "ws", cfg.get_settings().files_dir)
        assert rec["resynced"] >= 1
    finally:
        s.close()
    assert proj_file.is_file() and proj_file.read_bytes()[:2] == b"PK"


def test_external_modification_protected(client, tmp_path):
    """外部修改保护：投影被外部改动 → 对账保留原件，不覆盖。"""
    pid, doc_id, proj_file = _seed_project_doc(client, tmp_path, "外部修改项目")
    proj_file.write_bytes(_docx("用户在 Word 里手工改过的内容。"))
    import app.config as cfg
    from app.db import make_engine
    from app.services.projection_recovery import reconcile_projections

    s = sessionmaker(bind=make_engine(cfg.get_settings().db_path), expire_on_commit=False)()
    try:
        rec = reconcile_projections(s, pid, tmp_path / "ws", cfg.get_settings().files_dir)
    finally:
        s.close()
    import zipfile as zf

    with zf.ZipFile(proj_file) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "用户在 Word 里手工改过的内容。" in xml, "外部修改应保留"
    # 原件保留（用户改动不被覆盖）
    assert "用户在 Word 里手工改过的内容。" in proj_file.read_text(encoding="utf-8", errors="ignore")
