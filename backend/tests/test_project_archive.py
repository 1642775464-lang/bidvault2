"""项目归档：带登记结果的归档请求体契约（防「缺 Content-Type → 422」回归）。

前端 archiveOp 必须发 application/json；本测试从服务端契约角度钉住：
带 JSON 体的归档可登记结果并进入案例库；缺体/缺头可归档但不登记。
"""

from __future__ import annotations

import io
import os

import pytest
from docx import Document as Docx
from fastapi.testclient import TestClient
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


def _mk_project(client, name: str = "归档契约项目") -> str:
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_archive_with_json_body_registers_result(client):
    """JSON 体 + application/json（前端 archiveOp 形态）：登记结果并归档。"""
    pid = _mk_project(client)
    r = client.post(f"/api/projects/{pid}/archive",
                    json={"result": "未中标", "result_note": "技术分偏低", "result_amount": ""})
    assert r.status_code == 200, f"归档失败：{r.status_code} {r.text}"
    assert r.json()["ok"] is True

    got = next(x for x in client.get("/api/projects?mode=archived").json()["items"] if x["id"] == pid)
    assert got["is_archived"] is True
    assert got["result"] == "未中标"
    assert got["result_note"] == "技术分偏低"
    assert got["result_amount"] is None


def test_archive_without_body(client):
    """无体归档（纯归档动作）：仍应成功，结果留空。"""
    pid = _mk_project(client, "纯归档项目")
    r = client.post(f"/api/projects/{pid}/archive")
    assert r.status_code == 200, r.text
    got = next(x for x in client.get("/api/projects?mode=archived").json()["items"] if x["id"] == pid)
    assert got["is_archived"] is True and got["result"] is None


def test_archive_invalid_result_rejected(client):
    pid = _mk_project(client, "非法结果项目")
    r = client.post(f"/api/projects/{pid}/archive", json={"result": "流标"})
    assert r.status_code == 400


def test_won_project_enters_cases(client):
    """归档且中标 → 进入案例库候选（客户历史合作路径）。"""
    pid = _mk_project(client, "中标登记项目")
    client.post(f"/api/projects/{pid}/archive", json={"result": "中标", "result_amount": "1280万元"})
    r = client.get("/api/projects/cases?q=中标登记")
    assert r.status_code == 200
    items = r.json().get("items", [])
    assert any(x.get("id") == pid or x.get("name") == "中标登记项目" for x in items), items


def test_unarchive_restores_active(client):
    pid = _mk_project(client, "撤销归档项目")
    client.post(f"/api/projects/{pid}/archive", json={"result": "未中标"})
    r = client.post(f"/api/projects/{pid}/unarchive")
    assert r.status_code == 200
    active = client.get("/api/projects?mode=active").json()["items"]
    assert any(x["id"] == pid for x in active)


_ = io, Docx  # 保留导入一致性（后续如需构造文档夹具）
