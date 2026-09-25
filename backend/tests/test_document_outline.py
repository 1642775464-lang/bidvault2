"""E-2A：DocumentOutline 统一章节树验收测试。

覆盖：构建（层级/锚点）、真实 114 页样本（≥100 节点、章节标题在树）、
懒构建持久化（二次查询不重建）、漂移比对（完好/漂移/丢失）、只读 API。
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path

import pytest
from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

SAMPLE = Path(__file__).resolve().parents[2] / "回归证据" / "D1R" / "dx-console" / "DxConsole" / "民机_test.docx"


def _docx_with_headings() -> bytes:
    d = Docx()
    d.add_heading("第一章 招标公告", level=1)
    d.add_paragraph("公告正文内容 ABC。")
    d.add_heading("第二章 投标人须知", level=1)
    d.add_heading("2.1 总则", level=2)
    d.add_paragraph("总则正文。")
    d.add_heading("第三章 评标办法", level=1)
    d.add_heading("3.1 评分标准", level=2)
    d.add_paragraph("评分细则。")
    buf = io.BytesIO()
    d.save(buf)
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


def test_build_outline_levels_and_anchors():
    from app.services.document_outline import build_outline

    tree = build_outline(_docx_with_headings())
    nodes = tree["nodes"]
    titles = [n["title"] for n in nodes]
    assert titles == ["第一章 招标公告", "第二章 投标人须知", "2.1 总则",
                      "第三章 评标办法", "3.1 评分标准"]
    by_title = {n["title"]: n for n in nodes}
    assert by_title["2.1 总则"]["parent_id"] == by_title["第二章 投标人须知"]["id"]
    assert by_title["3.1 评分标准"]["parent_id"] == by_title["第三章 评标办法"]["id"]
    assert all(len(n["anchor_hash"]) == 32 for n in nodes)
    # 锚点含后续正文：标题相同但正文不同 → 锚点不同
    assert by_title["2.1 总则"]["anchor_hash"] != by_title["3.1 评分标准"]["anchor_hash"]


def test_real_sample_114pages_outline():
    """真实 114 页民机 DOCX：章节树覆盖全部标题（与 D-1R4 DX 面板 134 项同量级）。"""
    if not SAMPLE.is_file():
        pytest.skip("民机样本不存在")
    from app.services.document_outline import build_outline

    tree = build_outline(SAMPLE.read_bytes())
    nodes = tree["nodes"]
    assert len(nodes) >= 100, f"标题数 {len(nodes)}"
    joined = " ".join(n["title"] for n in nodes)
    assert "第三章" in joined and "评标办法" in joined
    levels = {n["level"] for n in nodes}
    assert 2 in levels and 3 in levels


def test_lazy_build_persisted_once(client, tmp_path, monkeypatch):
    """懒构建：首次查询构建并持久化；二次查询直接读存储（不再重建）。"""
    oid = _setup_and_upload(client, tmp_path, content="#1 第一章 总则\n正文内容 ABC。\n#2 1.1 前提\n正文。\n")
    from app.models import DocumentOutline

    oid = client.get("/api/documents").json()["items"][0]["office_document_id"]

    def db_outline_row():
        s = _db_session()
        try:
            return s.query(DocumentOutline).filter(DocumentOutline.oid == oid).first()
        finally:
            s.close()

    r1 = client.get(f"/api/office-documents/{oid}/outline")
    assert r1.status_code == 200, r1.text
    assert r1.json()["node_count"] > 0
    built_at_1 = db_outline_row().built_at
    time.sleep(0.05)
    r2 = client.get(f"/api/office-documents/{oid}/outline")
    assert r2.status_code == 200
    assert db_outline_row().built_at == built_at_1  # 未重建


def test_outline_drift_report(client, tmp_path):
    """漂移语义：无修改=完好；标题删除=丢失；标题保留内容变=漂移。"""
    oid = _setup_and_upload(client, tmp_path, content="#1 第一章 总则\n正文内容 XYZ。\n#1 第二章 评标办法\n细则。\n")
    r1 = client.get(f"/api/office-documents/{oid}/outline")
    old_version = r1.json()["version_id"]

    from app.services.document_outline import build_outline, drift_report

    # 新版本：删掉"第二章"，保留"第一章"但正文变化 → 漂移 / 丢失
    d = Docx()
    d.add_heading("第一章 总则", level=1)
    d.add_paragraph("正文内容已修改 ABCD。")
    buf = io.BytesIO()
    d.save(buf)
    new_tree = build_outline(buf.getvalue())
    # 以旧版本存储树做比对（贴近 W-3 引用漂移用法）
    s = _db_session()
    try:
        from app.models import DocumentOutline

        row = s.query(DocumentOutline).filter(DocumentOutline.version_id == old_version).first()
        old_nodes = __import__("json").loads(row.nodes_json)["nodes"]
    finally:
        s.close()
    report = drift_report(old_nodes, new_tree["nodes"])
    by_title = {i["title"]: i["status"] for i in report["items"]}
    assert by_title["第一章 总则"] == "drifted"  # 标题在、正文锚点变
    assert by_title["第二章 评标办法"] == "lost"  # 标题删除
    assert report["counts"]["intact"] == 0


def _setup_and_upload(client, tmp_path, content: str = "招标正文：服务期限 3 年。评标办法 前附表") -> str:
    """content：'#1 标题' = Heading1，'#2 标题' = Heading2，其余为正文段落。"""
    buf = io.BytesIO()
    d = Docx()
    if "第一章" in content:
        for ln in content.split("\n"):
            ln = ln.strip()
            if not ln:
                continue
            if ln.startswith("#1 "):
                d.add_heading(ln[3:], level=1)
            elif ln.startswith("#2 "):
                d.add_heading(ln[3:], level=2)
            else:
                d.add_paragraph(ln)
    else:
        d.add_paragraph(content)
    d.save(buf)
    client.post("/api/workspace", json={"path": str(tmp_path / "ws")})
    pid = client.post("/api/projects", json={"name": "章节树项目"}).json()["id"]
    r = client.post("/api/documents/upload",
                    files=[("files", ("招标文件.docx", buf.getvalue(),
                                      "application/octet-stream"))],
                    data={"kind": "tender", "project_id": pid})
    assert r.status_code == 200, r.text
    doc_id = r.json()["created"][0]["id"]
    deadline = time.time() + 60
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        d = next((x for x in docs if x["id"] == doc_id), None)
        if d and d["status"] == 2 and d.get("office_document_id"):
            return d["office_document_id"]
        time.sleep(1)
    raise AssertionError("拆解未完成")


def _docx_bytes_of_uploaded(client):
    return b""


def _docx_of(oid: str) -> bytes:
    from app.db import make_engine
    from app.models import OfficeDocument, OfficeDocumentVersion
    from app.services.office_document_svc import OfficeDocumentService, OfficeDocumentTableAdapter

    s = _db_session()
    try:
        od = s.get(OfficeDocument, oid)
        svc = OfficeDocumentService(session=s, adapter=OfficeDocumentTableAdapter(s))
        cur = svc.get_current_version(od)
        return svc.adapter.read_file(cur.file_path)
    finally:
        s.close()
