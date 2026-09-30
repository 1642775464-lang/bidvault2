"""Phase 4+：AI 网关默认层级（契约外增强，项目负责人「需要」指令）。

层级（高→低）：已保存配置 > BIDVAULT_AI_* 环境变量 > 智谱 key 自动派生 > 未配置。
数据访问纪律：全部走 ORM 会话对象（参数化）。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
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


def _make_project_with_workspace(client) -> tuple[str, str]:
    r = client.post("/api/projects", json={"name": "网关默认层级验证", "industry": "信息化"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    w = client.post(f"/api/projects/{pid}/workspace")
    assert w.status_code == 200, w.text
    return pid, w.json()["office_document_id"]


def test_zhipu_key_autoseeds_gateway_default(client, monkeypatch):
    """无保存配置 + 有 ZHIPU_API_KEY → 网关自动派生智谱默认（开箱即用）。"""
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-test-autoseed")
    monkeypatch.delenv("BIDVAULT_AI_PROVIDER", raising=False)
    _pid, oid = _make_project_with_workspace(client)

    cfg = client.get(f"/api/office-documents/{oid}/ai-gateway/config").json()
    assert cfg["provider"] == "openai_compatible"
    assert cfg["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert cfg["model"] == "glm-4-flash"
    assert cfg["api_key_ref"] == "ZHIPU_API_KEY"
    assert cfg["has_key"] is True


def test_bidvault_ai_env_overrides_zhipu_default(client, monkeypatch):
    """BIDVAULT_AI_* 环境变量优先于智谱自动派生。"""
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-test-autoseed")
    monkeypatch.setenv("BIDVAULT_AI_PROVIDER", "openai_compatible")
    monkeypatch.setenv("BIDVAULT_AI_BASE_URL", "https://ai.example.com/v1")
    monkeypatch.setenv("BIDVAULT_AI_MODEL", "custom-model")
    monkeypatch.setenv("BIDVAULT_AI_KEY_REF", "BIDVAULT_AI_KEY")
    monkeypatch.setenv("BIDVAULT_AI_KEY", "sk-custom")
    _pid, oid = _make_project_with_workspace(client)

    cfg = client.get(f"/api/office-documents/{oid}/ai-gateway/config").json()
    assert cfg["provider"] == "openai_compatible"
    assert cfg["base_url"] == "https://ai.example.com/v1"
    assert cfg["model"] == "custom-model"
    assert cfg["api_key_ref"] == "BIDVAULT_AI_KEY"
    assert cfg["has_key"] is True


def test_no_key_no_env_stays_unconfigured(client, monkeypatch):
    """无智谱 key 且无 BIDVAULT_AI_* → 保持未配置（不虚报）。"""
    for k in ("ZHIPU_API_KEY", "BIDVAULT_AI_PROVIDER", "BIDVAULT_AI_BASE_URL",
              "BIDVAULT_AI_MODEL", "BIDVAULT_AI_KEY_REF"):
        monkeypatch.delenv(k, raising=False)
    _pid, oid = _make_project_with_workspace(client)

    cfg = client.get(f"/api/office-documents/{oid}/ai-gateway/config").json()
    assert cfg["provider"] == ""
    assert cfg["has_key"] is False


def test_saved_config_beats_all_defaults(client, monkeypatch):
    """已保存配置 > 一切默认（用户显式配置不可被出厂值覆盖）。"""
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-test-autoseed")
    _pid, oid = _make_project_with_workspace(client)

    r = client.put(f"/api/office-documents/{oid}/ai-gateway/config", json={
        "provider": "openai_compatible",
        "base_url": "https://saved.example.com/v1",
        "model": "saved-model",
        "api_key_ref": "ZHIPU_API_KEY",
    })
    assert r.status_code == 200, r.text

    cfg = client.get(f"/api/office-documents/{oid}/ai-gateway/config").json()
    assert cfg["base_url"] == "https://saved.example.com/v1"
    assert cfg["model"] == "saved-model"


def test_zhipu_default_respects_base_url_override(client, monkeypatch):
    """ZHIPU_BASE_URL 覆盖默认 endpoint（私有化网关场景）。"""
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-test-autoseed")
    monkeypatch.setenv("ZHIPU_BASE_URL", "https://gateway.corp.internal/v4")
    _pid, oid = _make_project_with_workspace(client)

    cfg = client.get(f"/api/office-documents/{oid}/ai-gateway/config").json()
    assert cfg["base_url"] == "https://gateway.corp.internal/v4"
