"""E-1D：项目创建与新招标流程接入验收测试。

覆盖：项目目录生成（招标/投标/输出文件）、招标上传自动投影+拆解、
投标询问式（暂不拆解=仅归档，确认后建档拆解）、同名项目 -2 目录、
无工作区时行为不回归。
"""

from __future__ import annotations

import io
import os
import time

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
        yield c
    app.dependency_overrides.clear()


def _docx(content: str) -> bytes:
    buf = io.BytesIO()
    d = Docx()
    d.add_paragraph(content)
    d.save(buf)
    return buf.getvalue()


def _setup_ws(client, tmp_path) -> str:
    r = client.post("/api/workspace", json={"path": str(tmp_path / "ws")})
    assert r.status_code == 201, r.text
    return str(tmp_path / "ws")


def _wait_status(client, filename: str, want: int, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        d = next((x for x in docs if x["filename"] == filename), None)
        if d and d["status"] == want:
            return True
        time.sleep(1)
    return False


def test_project_creates_workspace_folders(client, tmp_path):
    _setup_ws(client, tmp_path)
    r = client.post("/api/projects", json={"name": "XX医院采购项目"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workspace_folder"] == "项目/XX医院采购项目"
    for sub in ("招标文件", "投标文件", "输出文件"):
        assert (tmp_path / "ws" / "项目" / "XX医院采购项目" / sub).is_dir()


def test_project_name_conflict_gets_suffix_folder(client, tmp_path):
    _setup_ws(client, tmp_path)
    r1 = client.post("/api/projects", json={"name": "同名校验项目"})
    r2 = client.post("/api/projects", json={"name": "同名校验项目"})
    assert r1.json()["workspace_folder"] == "项目/同名校验项目"
    assert r2.json()["workspace_folder"] == "项目/同名校验项目-2"
    assert (tmp_path / "ws" / "项目" / "同名校验项目-2" / "招标文件").is_dir()


def test_tender_upload_projected_and_decomposed(client, tmp_path):
    """E-1D 主流程：新建项目 → 上传招标文件 → 自动进招标文件夹 → 自动拆解。"""
    _setup_ws(client, tmp_path)
    pid = client.post("/api/projects", json={"name": "投影项目"}).json()["id"]
    r = client.post("/api/documents/upload",
                    files=[("files", ("招标文件.docx", _docx("招标正文：服务期限 3 年。评标办法 前附表"),
                                      "application/octet-stream"))],
                    data={"kind": "tender", "project_id": pid, "is_secret": "false"})
    assert r.status_code == 200, r.text
    doc_id = r.json()["created"][0]["id"]
    # 投影落位
    assert (tmp_path / "ws" / "项目" / "投影项目" / "招标文件" / "招标文件.docx").is_file()
    # 自动拆解
    assert _wait_status(client, "招标文件.docx", 2), "招标文件应自动拆解完成"


def test_bid_upload_ask_mode_then_decompose(client, tmp_path):
    """E-1D：上传投标文件 → 用户确认是否拆解。decompose=false 仅归档；确认后建档拆解。"""
    _setup_ws(client, tmp_path)
    pid = client.post("/api/projects", json={"name": "询问式项目"}).json()["id"]

    # ① 暂不拆解：仅归档投影，不建档
    r = client.post("/api/documents/upload",
                    files=[("files", ("投标文件.docx", _docx("投标文件 投标函 授权委托书"),
                                      "application/octet-stream"))],
                    data={"kind": "bid", "project_id": pid, "decompose": "false"})
    assert r.status_code == 200, r.text
    item = r.json()["created"][0]
    assert item.get("archived_only") is True and "id" not in item
    assert (tmp_path / "ws" / "项目" / "询问式项目" / "投标文件" / "投标文件.docx").is_file()
    docs = client.get("/api/documents").json()["items"]
    assert not [d for d in docs if d["filename"] == "投标文件.docx"], "未确认拆解不建档"

    # ② 用户确认拆解 → 建档 + 入队
    rel = item["rel_path"]
    r = client.post("/api/workspace/decompose", json={"rel_path": rel, "project_id": pid})
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "bid"
    assert _wait_status(client, "投标文件.docx", 2), "确认拆解后应完成拆解"


def test_bid_upload_decompose_true_registers(client, tmp_path):
    _setup_ws(client, tmp_path)
    pid = client.post("/api/projects", json={"name": "直接拆解项目"}).json()["id"]
    r = client.post("/api/documents/upload",
                    files=[("files", ("投标文件.docx", _docx("投标文件 投标函"),
                                      "application/octet-stream"))],
                    data={"kind": "bid", "project_id": pid, "decompose": "true"})
    assert r.status_code == 200
    assert "id" in r.json()["created"][0]
    assert (tmp_path / "ws" / "项目" / "直接拆解项目" / "投标文件" / "投标文件.docx").is_file()
    assert _wait_status(client, "投标文件.docx", 2)


def test_upload_without_workspace_unchanged(client, tmp_path):
    """无活动工作区：上传行为与既有完全一致（建档入队，无投影）。"""
    pid = client.post("/api/projects", json={"name": "无工作区项目"}).json()["id"]
    r = client.post("/api/documents/upload",
                    files=[("files", ("招标文件.docx", _docx("招标正文 评标办法"),
                                      "application/octet-stream"))],
                    data={"kind": "tender", "project_id": pid})
    assert r.status_code == 200
    body = r.json()
    assert "id" in body["created"][0]
    assert "rel_path" not in body["created"][0]
    assert _wait_status(client, "招标文件.docx", 2)
