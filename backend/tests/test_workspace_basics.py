"""E-1A：工作区基础契约测试（E-0 契约 §1/§7/§10-E-1A 门禁）。

覆盖：创建（骨架/绑定清单/注册）、幂等修复、非法路径拒绝、单工作区约束、
监视忽略规则、增/改/删事件留痕、dismiss。
"""

from __future__ import annotations

import os

import pytest
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
        yield c
    app.dependency_overrides.clear()


def _db_session():
    import app.config as cfg
    from app.db import make_engine

    return sessionmaker(bind=make_engine(cfg.get_settings().db_path), expire_on_commit=False)()


def test_create_workspace_skeleton_and_registry(client, tmp_path):
    root = tmp_path / "标书工作区"
    r = client.post("/api/workspace", json={"path": str(root), "name": "我的工作区"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["active"] is True and body["skeleton_ok"] is True
    # 骨架目录
    for rel in ("项目", "素材库/企业资质", "素材库/人员简历", "素材库/项目案例",
                "素材库/技术方案", "中转"):
        assert (root / rel).is_dir(), rel
    # 绑定清单
    import json

    manifest = json.loads((root / ".bidvault" / "workspace.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["workspace_id"] == body["id"]
    assert manifest["name"] == "我的工作区"


def test_create_rejects_path_inside_data_dir(client, tmp_path):
    import app.config as cfg

    bad = cfg.get_settings().data_dir / "nested"
    r = client.post("/api/workspace", json={"path": str(bad)})
    assert r.status_code == 400
    assert "WORKSPACE_ERROR" in r.text


def test_create_rejects_drive_root(client, tmp_path):
    r = client.post("/api/workspace", json={"path": "C:\\"})
    assert r.status_code == 400


def test_single_active_workspace(client, tmp_path):
    r1 = client.post("/api/workspace", json={"path": str(tmp_path / "ws1")})
    assert r1.status_code == 201
    r2 = client.post("/api/workspace", json={"path": str(tmp_path / "ws2")})
    assert r2.status_code == 409
    assert "已存在活动工作区" in r2.text


def test_skeleton_repair_idempotent(client, tmp_path):
    root = tmp_path / "ws3"
    assert client.post("/api/workspace", json={"path": str(root)}).status_code == 201
    (root / "中转").rmdir()  # 模拟用户误删
    r = client.get("/api/workspace")
    assert r.status_code == 200
    body = r.json()
    assert body["skeleton_ok"] is True
    assert "中转" in body["repaired"]


def _bare_monitor(root, ws_id):
    """裸监视器：不启动真实 watchdog Observer，仅测事件管线（避免与测试线程并发互锁）。"""
    from pathlib import Path

    from app.services.workspace_monitor import WorkspaceMonitor

    mon = WorkspaceMonitor(_db_session, settle_seconds=0)
    mon._root = Path(root).resolve()
    mon._workspace_id = ws_id
    return mon


def test_monitor_emits_add_modify_delete(client, tmp_path, monkeypatch):
    root = tmp_path / "ws4"
    assert client.post("/api/workspace", json={"path": str(root)}).status_code == 201
    ws_id = client.get("/api/workspace").json()["id"]

    from app.services.workspace_fs import file_hash

    mon = _bare_monitor(root, ws_id)
    # added
    f = root / "项目" / "示例项目" / "招标文件" / "a.docx"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"hello")
    mon._schedule(str(f))
    evs = client.get("/api/workspace/events").json()["items"]
    assert [e["kind"] for e in evs] == ["added"]
    assert evs[0]["rel_path"].startswith("项目/")
    # modified（内容变化才产事件）
    f.write_bytes(b"hello world")
    mon._schedule(str(f))
    evs = client.get("/api/workspace/events").json()["items"]
    assert evs[0]["kind"] == "modified" and evs[0]["file_hash"] == file_hash(f)
    # 同内容重复触发 → 自写抑制，无新事件
    mon._schedule(str(f))
    evs2 = client.get("/api/workspace/events").json()["items"]
    assert len(evs2) == len(evs)
    # deleted
    f.unlink()
    mon._schedule(str(f), deleted=True)
    evs3 = client.get("/api/workspace/events").json()["items"]
    assert evs3[0]["kind"] == "deleted"
    # dismiss
    eid = evs3[0]["id"]
    assert client.post(f"/api/workspace/events/{eid}/dismiss").status_code == 200
    remaining = [e for e in client.get("/api/workspace/events").json()["items"] if e["id"] == eid]
    assert remaining == []


def test_monitor_ignores_temp_and_bidvault(client, tmp_path):
    root = tmp_path / "ws5"
    assert client.post("/api/workspace", json={"path": str(root)}).status_code == 201
    ws_id = client.get("/api/workspace").json()["id"]

    mon = _bare_monitor(root, ws_id)
    for rel in ("~$标书.docx", "草稿.tmp", ".bidvault/workspace.json"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        mon._schedule(str(p))
    assert client.get("/api/workspace/events").json()["items"] == []
    # 正常文件仍会产出（排除规则只对忽略项）
    ok = root / "项目" / "b.docx"
    ok.write_bytes(b"ok")
    mon._schedule(str(ok))
    items = client.get("/api/workspace/events").json()["items"]
    assert len(items) == 1 and items[0]["rel_path"] == "项目/b.docx"
