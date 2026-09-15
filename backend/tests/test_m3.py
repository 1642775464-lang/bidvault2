"""M3 测试：图片证据自动入库 / 页面预览 / 路径穿越防护 / GLM-4V 识别回写。"""

from __future__ import annotations

import io
import os
import struct
import time
import zlib

import pytest
from fastapi.testclient import TestClient

from app.db import get_session, make_engine
from app.main import app


def _random_png(w: int = 400, h: int = 300) -> bytes:
    """纯 stdlib PNG：随机像素（体积足够触发证据图入库阈值）。"""
    raw = b"".join(b"\x00" + bytes(zlib.crc32(b"") for _ in range(0)) for _ in range(0))  # 占位
    rows = b"".join(b"\x00" + os.urandom(w * 3) for _ in range(h))
    raw = rows

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _pdf_with_image(page_text: str = "营业执照复印件") -> bytes:
    """含内嵌大图的 PDF（M3 图片证据的输入形态）。"""
    import fitz

    png = _random_png()
    pdf = fitz.open()
    page = pdf.new_page(width=595, height=842)
    page.insert_text((72, 90), page_text, fontsize=15, fontname="china-s")
    page.insert_image(fitz.Rect(72, 120, 400, 350), stream=png)
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


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


def _wait_docs(client, *, timeout: float = 30) -> list[dict]:
    deadline = time.time() + timeout
    docs: list[dict] = []
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        if docs and all(d["status"] == 2 for d in docs):
            break
        time.sleep(0.4)
    return docs


def _upload_image_pdf(client, is_secret: str = "true") -> tuple[str, str]:
    r = client.post(
        "/api/documents/upload",
        files=[("files", ("资质文件_扫描.pdf", _pdf_with_image(), "application/pdf"))],
        data={"kind": "bid", "is_secret": is_secret},
    )
    assert r.status_code == 200, r.text
    return r.json()["created"][0]["id"], "资质文件_扫描.pdf"


class TestImageHarvest:
    def test_image_material_auto_ingest(self, client):
        doc_id, filename = _upload_image_pdf(client)
        docs = _wait_docs(client)
        assert docs and docs[0]["status"] == 2

        mats = client.get("/api/materials").json()["items"]
        imgs = [m for m in mats if m["type"] == "图片" and m["doc_name"] == filename]
        assert len(imgs) == 1, f"图片素材未入库：{[m['title'] for m in mats]}"
        m = imgs[0]
        assert m["source_page"] == 1
        assert m["file_name"] and m["file_name"].startswith(f"img/{doc_id}/")
        assert "KB" in (m["file_size"] or "")
        # 上下文分类：页文本含「营业执照」→ 资质文件
        assert m["cat"] == "资质文件"
        assert "图片证据" in m["title"] or "营业执照" in m["title"]

        # 重复拆解不重复入库
        client.post(f"/api/documents/{doc_id}/extract")
        mats2 = client.get("/api/materials").json()["items"]
        imgs2 = [m for m in mats2 if m["type"] == "图片" and m["doc_name"] == filename]
        assert len(imgs2) == 1

    def test_image_file_accessible(self, client):
        doc_id, _ = _upload_image_pdf(client)
        _wait_docs(client)
        m = next(m for m in client.get("/api/materials").json()["items"] if m["type"] == "图片")
        name = m["file_name"].split("/")[-1]
        r = client.get(f"/api/files/img/{doc_id}/{name}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/")

    def test_image_file_traversal_blocked(self, client):
        r = client.get("/api/files/img/somedoc/..%5C..%5Fsecret.png")
        assert r.status_code in (404, 400)
        r2 = client.get("/api/files/img/somedoc/nope.png")
        assert r2.status_code == 404


class TestPagePreview:
    def test_page_count_and_render(self, client):
        doc_id, _ = _upload_image_pdf(client)
        _wait_docs(client)
        pc = client.get(f"/api/documents/{doc_id}/page_count").json()
        assert pc == {"pages": 1, "previewable": True}

        r = client.get(f"/api/documents/{doc_id}/pages", params={"page_no": 1})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert len(r.content) > 1000

        r404 = client.get(f"/api/documents/{doc_id}/pages", params={"page_no": 2})
        assert r404.status_code == 404

    def test_preview_not_for_docx(self, client):
        from docx import Document as DocxDocument

        buf = io.BytesIO()
        d = DocxDocument()
        d.add_paragraph("普通文档")
        d.save(buf)
        r = client.post(
            "/api/documents/upload",
            files=[("files", ("普通.docx", buf.getvalue(), "application/octet-stream"))],
            data={"kind": "tender", "is_secret": "true"},
        )
        doc_id = r.json()["created"][0]["id"]
        _wait_docs(client)
        assert client.get(f"/api/documents/{doc_id}/page_count").json()["previewable"] is False
        assert client.get(f"/api/documents/{doc_id}/pages").status_code == 400


class TestGlmVision:
    def test_analyze_updates_material(self, client, monkeypatch):
        doc_id, _ = _upload_image_pdf(client)
        _wait_docs(client)

        import app.services.glm_vision as gv

        def fake_analyze(img: bytes, _ext: str) -> dict:
            assert len(img) > 1000
            return {"type": "营业执照", "fields": {"单位名称": "华东建设集团有限公司"}, "summary": "营业执照副本"}

        monkeypatch.setattr(gv, "analyze_image", fake_analyze)
        r = client.post(f"/api/documents/{doc_id}/analyze_images")
        assert r.status_code == 200, r.text
        assert r.json()["analyzed"] == 1

        m = next(m for m in client.get("/api/materials").json()["items"] if m["type"] == "图片")
        assert m["rows"] and m["rows"][0][0] == "单位名称"
        assert "营业执照" in m["tags"]
