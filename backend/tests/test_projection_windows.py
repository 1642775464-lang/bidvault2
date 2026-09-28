"""D-2B-R ③：DB 先行投影写回——四个崩溃窗口异常注入测试。

协议（D-2A §6 v1.1 冻结）：①事务A(pending 意图) → ②os.replace → ③事务B(synced)。
窗口①：①后②前（replace 前崩溃/异常）→ pending+投影旧 → 启动恢复重投影。
窗口②：②后③前（synced 前崩溃）→ pending+投影新(hash==target) → 补标 synced。
窗口③：无意图 + 投影被外部修改 → 保留原件落事件候选（不覆盖）。
窗口④：replace 抛 PermissionError → 意图撤销 + 候选落中转。
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


def _docx(text):
    buf = io.BytesIO()
    d = Docx()
    d.add_paragraph(text)
    d.save(buf)
    return buf.getvalue()


def _seed(client, tmp_path, name):
    r = client.post("/api/projects", json={"name": name}).json()
    pid, rel = r["id"], r["workspace_folder"]
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
    docs = client.get("/api/documents").json()["items"]
    d = next(x for x in docs if x["id"] == doc_id)
    oid = d["office_document_id"]
    proj_file = tmp_path / "ws" / rel / "投标文件" / f"{name}.docx"
    return pid, oid, proj_file


def _engine_session():
    import app.config as cfg
    from app.db import make_engine

    return sessionmaker(bind=make_engine(cfg.get_settings().db_path), expire_on_commit=False)()


def _current_bytes(oid):
    from app.models import OfficeDocument, OfficeDocumentVersion
    from app.services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )

    s = _engine_session()
    try:
        od = s.get(OfficeDocument, oid)
        svc = OfficeDocumentService(session=s, adapter=OfficeDocumentTableAdapter(s))
        cur = svc.get_current_version(od)
        return svc.adapter.read_file(cur.file_path)
    finally:
        s.close()


def _inject_pending(oid, rel_path, target_hash, pid):
    from app.models import ProjectionState

    s = _engine_session()
    try:
        s.add(ProjectionState(workspace_id=pid, rel_path=rel_path, oid=oid,
                              target_hash=target_hash, state="pending"))
        s.commit()
    finally:
        s.close()


def test_window_1_crash_before_replace(client, tmp_path):
    """窗口①：意图已提交、替换前崩溃 → 启动恢复以 DB 版本重投影。"""
    import hashlib

    from app.services.projection_recovery import reconcile_projections

    pid, oid, proj_file = _seed(client, tmp_path, "窗口一项目")
    proj_file.unlink(missing_ok=True)  # 替换未发生即崩溃（投影丢失）
    data = _current_bytes(oid)
    _inject_pending(oid, str(proj_file.relative_to(tmp_path / "ws")), hashlib.sha256(data).hexdigest(), pid)
    ws_root = tmp_path / "ws"
    s = _engine_session()
    try:
        rec = reconcile_projections(s, pid, ws_root, _files_dir_of())
        assert rec["resynced"] >= 1
    finally:
        s.close()
    assert proj_file.is_file() and proj_file.read_bytes()[:2] == b"PK"


def test_window_2_crash_after_replace(client, tmp_path):
    """窗口②：替换成功、synced 标记前崩溃 → pending+投影新(hash==target) → 补标 synced。"""
    import hashlib

    from app.models import ProjectionState
    from app.services.projection_recovery import reconcile_projections

    pid, oid, proj_file = _seed(client, tmp_path, "窗口二项目")
    data = _current_bytes(oid)
    proj_file.write_bytes(data)  # 替换已完成
    h = hashlib.sha256(data).hexdigest()
    _inject_pending(oid, str(proj_file.relative_to(tmp_path / "ws")), h, pid)
    ws_root = tmp_path / "ws"
    s = _engine_session()
    try:
        rec = reconcile_projections(s, pid, ws_root, _files_dir_of())
        assert rec["marked_synced"] >= 1
    finally:
        s.close()
    s2 = _engine_session()
    try:
        from app.models import ProjectionState

        row = s2.query(ProjectionState).filter(ProjectionState.oid == oid).first()
        assert row.state == "synced"
    finally:
        s2.close()


def test_window_3_external_modification_protected(client, tmp_path):
    """窗口③：投影被外部修改（hash≠target、无 pending 意图）→ 保留原件不覆盖。"""
    from app.services.projection_recovery import reconcile_projections

    pid, oid, proj_file = _seed(client, tmp_path, "窗口三项目")
    proj_file.write_bytes(_docx("用户在 Word 里手工改过的内容。"))
    ws_root = tmp_path / "ws"
    s = _engine_session()
    try:
        rec = reconcile_projections(s, pid, ws_root, _files_dir_of())
    finally:
        s.close()
    # 原件保留（用户修改不被 DB 覆盖）
    with zipfile.ZipFile(proj_file) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "用户在 Word 里手工改过的内容。" in xml, "外部修改应保留不被覆盖"


def test_window_4_permission_error_mid_replace(client, tmp_path, monkeypatch):
    """窗口④：replace 抛 PermissionError → 意图撤销+候选落中转（无 pending 残留）。"""
    import os as _os

    pid, oid, proj_file = _seed(client, tmp_path, "窗口四项目")
    material_id = _import_material(client, "素材窗口四")
    node = client.get(f"/api/office-documents/{oid}/outline").json()["nodes"]
    node = node[0] if node else None
    if node is None:
        pytest.skip("无章节节点")
    real_replace = _os.replace

    def _locked(src, dst, *a, **k):
        if str(dst).endswith(".docx") and "投标文件" in str(dst):
            raise PermissionError(32, "占用")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(_os, "replace", _locked)
    r = client.post("/api/workspace/references/insert", json={
        "oid": oid, "node_id": node["id"], "material_id": material_id,
        "purpose": "项目负责人", "insert_mode": "chapter"})
    monkeypatch.setattr(_os, "replace", real_replace)
    assert r["status"] == 201 and r["writeback"] == "pending_writeback"
    # 无 pending 残留（意图撤销）
    s = _engine_session()
    try:
        from app.models import ProjectionState

        assert s.query(ProjectionState).filter(ProjectionState.state == "pending").count() == 0
    finally:
        s.close()


def _import_material(client, name):
    import tempfile

    zp = Path(tempfile.mkdtemp()) / f"素材-{name}.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(f"人员简历-{name}.docx", _docx([f"{name} 素材内容"]))
    batch = client.post("/api/workspace/imports", json={"paths": [str(zp)]}).json()["batch_id"]
    items = client.get(f"/api/workspace/imports/{batch}").json()["items"]
    client.post(f"/api/workspace/imports/{batch}/confirm", json={"decisions": [
        {"item_id": i["id"], "kind": "material", "material_cat": "人员简历"} for i in items]})
    return client.get("/api/materials").json()["items"][0]["id"]


def _files_dir_of():
    import app.config as cfg

    return cfg.get_settings().files_dir
