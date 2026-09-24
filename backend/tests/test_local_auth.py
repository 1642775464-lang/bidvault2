"""D-1R4 第五节：本机鉴权与密钥收口测试。

- /api 仅环回可达（非环回 403）；
- 业务写接口无令牌 401，带令牌（头/Cookie）放行；
- /api/health 免鉴权；
- JWT/本地令牌按安装实例生成并持久化（凭据管理器不可用时落数据目录文件）。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def auth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("BIDVAULT_LOCAL_AUTH", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("BIDVAULT_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("ONLYOFFICE_JWT_SECRET", raising=False)
    import app.config as cfg
    import app.db as appdb

    monkeypatch.setattr(cfg, "_SETTINGS", None)
    monkeypatch.setattr(appdb, "_Engine", None)
    monkeypatch.setattr(appdb, "_SessionLocal", None)
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_write_without_token_rejected(auth_client):
    r = auth_client.post("/api/projects", json={"name": "无令牌写入"})
    assert r.status_code == 401
    assert "UNAUTHORIZED_LOCAL_TOKEN" in r.text


def test_write_with_header_token_allowed(auth_client):
    from app.config import get_settings

    tok = get_settings().local_access_token
    r = auth_client.post("/api/projects", json={"name": "带令牌写入"}, headers={"X-BidVault-Token": tok})
    assert r.status_code == 200, r.text


def test_cookie_flow_allows_subsequent_write(auth_client):
    # 读请求下发 Cookie 后，同会话写请求自动携带
    rg = auth_client.get("/api/projects")
    assert rg.status_code == 200
    assert "bv_token" in auth_client.cookies
    r = auth_client.post("/api/projects", json={"name": "Cookie 写入"})
    assert r.status_code == 200, r.text


def test_health_open(auth_client):
    r = auth_client.get("/api/health")
    assert r.status_code == 200


def test_instance_secrets_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data2"))
    monkeypatch.delenv("BIDVAULT_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("ONLYOFFICE_JWT_SECRET", raising=False)
    import app.config as cfg

    # 隔离机器级凭据管理器，确保走数据目录文件路径
    monkeypatch.setattr(cfg, "_wincred_read", lambda target: None)
    monkeypatch.setattr(cfg, "_wincred_write", lambda target, value: False)
    monkeypatch.setattr(cfg, "_SETTINGS", None)
    s1 = cfg.get_settings()
    f_jwt = tmp_path / "data2" / "config" / "jwt_secret"
    f_tok = tmp_path / "data2" / "config" / "local_token"
    assert f_jwt.is_file() and f_tok.is_file()
    assert s1.onlyoffice_jwt_secret == f_jwt.read_text(encoding="utf-8").strip()
    # 复用：再次解析得到同一密钥（不重复生成）
    monkeypatch.setattr(cfg, "_SETTINGS", None)
    s2 = cfg.get_settings()
    assert s2.onlyoffice_jwt_secret == s1.onlyoffice_jwt_secret
    assert s2.local_access_token == s1.local_access_token
