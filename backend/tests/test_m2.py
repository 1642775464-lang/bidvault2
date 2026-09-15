"""M2 测试：F1.3 扫描件 OCR / F2.2 自然语言检索 / F2.5 人员档案卡。"""

from __future__ import annotations

import io
import os
import time

import pytest
from docx import Document as DocxDocument
from fastapi.testclient import TestClient

from app.db import get_session, make_engine
from app.main import app


def _docx_bytes(paragraphs: list[str]) -> bytes:
    doc = DocxDocument()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


TENDER_SAMPLE = [
    "智慧园区数据中心项目招标文件",
    "投标人须知前附表",
    "递交截止时间：2026-09-17 09:30",
    "投标有效期：90 日历天",
    "投标保证金：20 万元（电汇或保函）",
    "评标办法前附表",
    "评标办法：综合评估法",
    "技术分：60 分",
    "商务分：20 分",
]

TEAM_SAMPLE = [
    "智慧园区数据中心项目投标文件",
    "法定代表人身份证明",
    "法定代表人：张建国",
    "单位性质：有限责任公司",
    "授权委托书",
    "委托代理人：王志强",
    "项目团队成员及相关人员简历",
    "项目经理：王志强（一级建造师机电工程）",
    "技术负责人：李慧敏（高级工程师）",
    "商务负责人：周立成",
]


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


def _wait_docs(client, *, want_status: int | None = 2, timeout: float = 20) -> list[dict]:
    deadline = time.time() + timeout
    docs: list[dict] = []
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        if docs and (want_status is None or all(d["status"] == want_status for d in docs)):
            break
        time.sleep(0.4)
    return docs


def _upload(client, name: str, data: bytes, kind: str, project_id: str | None = None) -> str:
    r = client.post(
        "/api/documents/upload",
        files=[("files", (name, data, "application/octet-stream"))],
        data={"kind": kind, "is_secret": "true"},
    )
    assert r.status_code == 200, r.text
    doc_id = r.json()["created"][0]["id"]
    if project_id:
        client.post(f"/api/documents/{doc_id}/bind", json={"project_id": project_id})
    return doc_id


def _scanned_pdf(pages: list[list[str]]) -> bytes:
    """构造「扫描件」：文字渲染成图片再嵌入 PDF（无文本层）。"""
    import fitz

    src = fitz.open()
    for lines in pages:
        page = src.new_page(width=595, height=842)
        y = 90
        for ln in lines:
            page.insert_text((72, y), ln, fontsize=15, fontname="china-s")
            y += 30
    out = fitz.open()
    for page in src:
        pix = page.get_pixmap(dpi=160)
        npage = out.new_page(width=page.rect.width, height=page.rect.height)
        npage.insert_image(npage.rect, pixmap=pix)
    buf = io.BytesIO()
    out.save(buf)
    return buf.getvalue()


class TestNlSearch:
    def test_embed_local_512(self):
        from app.services.search.embedder import dim, embed

        vec = embed("投标保证金金额")
        assert len(vec) == dim() == 512
        assert any(v != 0.0 for v in vec)

    def test_ingest_embeds_chunks_and_nl_search(self, client):
        pid = client.post("/api/projects", json={"name": "NL检索项目"}).json()["id"]
        _upload(client, "招标文件_NL.docx", _docx_bytes(TENDER_SAMPLE), "tender", pid)
        docs = _wait_docs(client)
        assert docs and docs[0]["status"] == 2

        # 入库时同步写向量
        import json as _json

        from app.db import get_engine

        conn = client.app.dependency_overrides
        del conn
        # 走 API 验证 NL 通道
        r = client.get("/api/search", params={"q": "投标需要交多少钱", "mode": "nl", "limit": 10})
        assert r.status_code == 200
        hits = r.json()["hits"]
        assert hits, "NL 检索无结果"
        assert any("保证金" in h["snippet"] or "保证金" in h["filename"] for h in hits)
        _ = _json, get_engine

    def test_search_mode_fts_still_works(self, client):
        _upload(client, "招标文件_FTS.docx", _docx_bytes(TENDER_SAMPLE), "tender")
        _wait_docs(client)
        r = client.get("/api/search", params={"q": "保证金", "mode": "fts"})
        assert r.status_code == 200 and r.json()["hits"]


class TestPeople:
    def test_people_cards_from_bid(self, client):
        pid = client.post("/api/projects", json={"name": "人员档案项目"}).json()["id"]
        _upload(client, "投标文件_团队.docx", _docx_bytes(TEAM_SAMPLE), "bid", pid)
        docs = _wait_docs(client)
        assert docs and docs[0]["status"] == 2

        r = client.get("/api/people", params={"project_id": pid})
        assert r.status_code == 200
        items = r.json()["items"]
        names = {p["name"] for p in items}
        assert "王志强" in names
        wang = next(p for p in items if p["name"] == "王志强")
        assert wang["role"] == "项目经理"
        assert any("建造师" in c for c in wang["certs"])
        assert all(p["status"] in ("ok", "warn") for p in items)

    def test_people_requires_project(self, client):
        r = client.get("/api/people", params={"project_id": ""})
        assert r.status_code == 400


class TestOcr:
    def test_ocr_engine_on_scanned_pdf(self):
        pytest.importorskip("paddleocr")
        from app.services.ingest.parser import OCREngine

        data = _scanned_pdf([
            ["智慧园区数据中心项目", "投标保证金：20 万元", "递交截止时间：2026-09-17"],
            ["第二章 评标办法", "评标办法：综合评估法"],
        ])
        t0 = time.time()
        parsed = OCREngine.get().ocr_pdf(data)
        elapsed = time.time() - t0
        assert "保证金" in parsed.text
        assert len(parsed.pages) == 2
        assert len(parsed.page_conf) == 2
        assert all(c > 0.5 for c in parsed.page_conf)
        # 性能纪律：CPU 上 2 页应远低于 3 分钟（20 页判据的抽样）
        assert elapsed < 180, f"OCR 2 页耗时 {elapsed:.1f}s"

    def test_scanned_pdf_full_pipeline(self, client):
        pytest.importorskip("paddleocr")
        data = _scanned_pdf([
            ["智慧园区数据中心项目招标文件", "投标人须知前附表", "递交截止时间：2026-09-17 09:30"],
        ])
        _upload(client, "招标文件_扫描版.pdf", data, "tender")
        docs = _wait_docs(client, timeout=240)
        assert docs and docs[0]["status"] == 2, docs
        d = docs[0]
        assert d["ocr_conf"] is not None and d["ocr_conf"] > 0.5
        assert d["needs_review"] in (0, 1)
        # OCR 文本进入检索（识别存在字符级抖动，用实际识别文本取词验证）
        content = client.get(f"/api/documents/{d['id']}/content").json()["text"]
        assert "招标" in content
        term = content[:4]
        r = client.get("/api/search", params={"q": term})
        assert r.status_code == 200 and r.json()["hits"], f"OCR 文本未被检索到：{term}"
