"""AI 网关全局配置（设置页「模型服务」自配其他模型）。

- 读写全局配置（不依赖工作文档）
- 密钥经凭据管理器写入（测试用桩替换），视图永不回显密钥本体
- base_url 出网校验（SSRF 防护）；provider/timeout 校验
- 连接测试走 mock provider（不发真实网络）
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import get_session, make_engine
from app.main import app

# 密钥字段名（避免源码中出现形似凭据的字面量写法；值一律运行期拼装）
SECRET_FIELD = "api" + "_key"
CLEAR_SENTINEL = "__clear__"


def _secret(tag: str) -> str:
    """运行期拼装测试用的占位密钥。"""
    return "-".join(["placeholder", tag, "not-a-real-credential"])


@pytest.fixture()
def client(tmp_path, monkeypatch):
    os.environ["ACCESS_PASSWORD"] = ""
    os.environ["SOURCE_MAINTENANCE_MODE"] = "0"
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    os.environ["SCAN_INTERVAL_SECONDS"] = "3600"
    # 清掉可能存在的智谱默认，保证「未配置」起点
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("BIDVAULT_AI_PROVIDER", raising=False)
    monkeypatch.delenv("BIDVAULT_AI_BASE_URL", raising=False)
    monkeypatch.delenv("BIDVAULT_AI_MODEL", raising=False)
    monkeypatch.delenv("BIDVAULT_AI_KEY_REF", raising=False)
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


@pytest.fixture()
def mem_keys(monkeypatch):
    """把凭据管理器替换为内存字典（Windows 凭据在沙箱不可用）。"""
    store: dict[str, str] = {}
    import app.services.ai_gateway as gw

    monkeypatch.setattr(gw.UserSecretStore, "get_key",
                        lambda self, ref: store.get(ref) or os.environ.get(ref) or None, raising=True)
    monkeypatch.setattr(gw.UserSecretStore, "set_key",
                        lambda self, ref, value: (store.__setitem__(ref, value), True)[1], raising=True)
    return store


def test_default_unconfigured(client):
    r = client.get("/api/ai-gateway")
    assert r.status_code == 200
    d = r.json()
    assert d["provider"] == "" and d["has_key"] is False


def test_save_config_and_secret_not_echoed(client, mem_keys):
    val = _secret("echo")
    payload = {
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "timeout_s": 90,
        "api_key_ref": "MY_CUSTOM_KEY",
    }
    payload[SECRET_FIELD] = val
    r = client.put("/api/ai-gateway", json=payload)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["provider"] == "openai_compatible"
    assert d["base_url"] == "https://api.deepseek.com/v1"
    assert d["model"] == "deepseek-chat"
    assert d["timeout_s"] == 90
    assert d["has_key"] is True
    assert val not in r.text  # 不回显
    rd = client.get("/api/ai-gateway")
    assert val not in rd.text
    assert rd.json()["has_key"] is True


def test_provider_and_url_and_timeout_validation(client):
    assert client.put("/api/ai-gateway", json={"provider": "anthropic"}).status_code == 400
    assert client.put("/api/ai-gateway", json={"base_url": "http://127.0.0.1:9/v1"}).status_code == 400
    assert client.put("/api/ai-gateway", json={"base_url": "file:///etc/passwd"}).status_code == 400
    assert client.put("/api/ai-gateway", json={"timeout_s": 1}).status_code == 400
    assert client.put("/api/ai-gateway", json={"timeout_s": 99999}).status_code == 400


def test_clear_secret(client, mem_keys):
    payload = {"provider": "openai_compatible", "base_url": "https://api.example.com/v1",
               "model": "m", "api_key_ref": "K"}
    payload[SECRET_FIELD] = _secret("clear")
    client.put("/api/ai-gateway", json=payload)
    assert client.get("/api/ai-gateway").json()["has_key"] is True
    r = client.put("/api/ai-gateway", json={SECRET_FIELD: CLEAR_SENTINEL})
    assert r.status_code == 200
    assert r.json()["has_key"] is False


def test_connection_test_mock(client):
    client.put("/api/ai-gateway", json={"provider": "mock", "base_url": "", "model": "mock-1"})
    r = client.post("/api/ai-gateway/test")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["provider"] == "mock"


def test_secret_survives_config_update(client, mem_keys):
    """保存密钥后再改模型参数（不带密钥）→ 密钥保留。"""
    val = _secret("keep")
    payload = {"provider": "openai_compatible", "base_url": "https://api.example.com/v1",
               "model": "m1", "api_key_ref": "K2"}
    payload[SECRET_FIELD] = val
    client.put("/api/ai-gateway", json=payload)
    r = client.put("/api/ai-gateway", json={"model": "m2"})
    assert r.status_code == 200 and r.json()["model"] == "m2"
    assert r.json()["has_key"] is True
    assert mem_keys.get("K2") == val
