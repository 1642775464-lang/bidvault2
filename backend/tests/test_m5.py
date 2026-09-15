"""M5 测试：生产静态托管 / SPA fallback / 成品导出下载。"""

from __future__ import annotations

import io
import os
import time

import pytest
from docx import Document as DocxDocument
from fastapi.testclient import TestClient

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
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def _make():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    return _make


class TestSpa:
    def test_index_served(self, client):
        """生产模式：/ 返回前端入口（dist 已构建）。"""
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert 'id="app"' in r.text

    def test_spa_fallback(self, client):
        r = client.get("/some/deep/route")
        assert r.status_code == 200
        assert 'id="app"' in r.text

    def test_api_keeps_404(self, client):
        r = client.get("/api/nonexistent")
        assert r.status_code == 404

    def test_static_assets_served(self, client):
        idx = client.get("/").text
        # 从 index.html 提取真实 assets 文件名验证托管
        import re

        m = re.search(r'/assets/([\w.-]+\.js)', idx)
        assert m, "index 未引用 assets"
        r = client.get(f"/assets/{m.group(1)}")
        assert r.status_code == 200


class TestExport:
    def test_download_original(self, client):
        buf = io.BytesIO()
        d = DocxDocument()
        d.add_paragraph("成品导出测试文档")
        d.save(buf)
        r = client.post(
            "/api/documents/upload",
            files=[("files", ("成品导出.docx", buf.getvalue(), "application/octet-stream"))],
            data={"kind": "bid", "is_secret": "true"},
        )
        doc_id = r.json()["created"][0]["id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            docs = client.get("/api/documents").json()["items"]
            if docs and docs[0]["status"] == 2:
                break
            time.sleep(0.4)

        r2 = client.get(f"/api/documents/{doc_id}/download")
        assert r2.status_code == 200
        assert "wordprocessingml" in r2.headers["content-type"] or "octet-stream" in r2.headers["content-type"]
        assert "attachment" in r2.headers.get("content-disposition", "")

    def test_download_missing_404(self, client):
        assert client.get("/api/documents/nonexistent/download").status_code == 404
