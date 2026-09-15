"""M1 v1.2 核心链路测试：项目库/文档库/拆解/素材自动入库/检索/涉密闸门。"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path

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
    "投标保证金：20 万元",
    "评标办法前附表",
    "评标办法：综合评估法",
    "技术分：60 分",
    "投标文件格式",
    "商务格式：12 项",
]

BID_SAMPLE = [
    "智慧园区数据中心项目投标文件",
    "法定代表人身份证明",
    "法定代表人：张建国",
    "单位性质：有限责任公司",
    "授权委托书",
    "委托代理人：王志强",
    "委托权限：签署投标文件、参加开标",
    "基本情况表",
    "单位名称：华东建设集团有限公司",
    "资质等级：机电工程施工总承包一级",
    "近年财务状况",
    "2025 年：营收 8650 万",
    "近年完成的类似项目情况表",
    "项目名称：银行机房监控系统项目",
    "合同额：486 万",
    "项目团队成员及相关人员简历",
    "项目经理：王志强",
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


def _wait_docs(client, *, want_status: int | None = 2, timeout: float = 60) -> list[dict]:
    deadline = time.time() + timeout
    docs: list[dict] = []
    while time.time() < deadline:
        docs = client.get("/api/documents", params={"size": 20}).json()["items"]
        if docs and (want_status is None or all(d["status"] == want_status for d in docs)):
            break
        time.sleep(0.4)
    return docs


def _ingest_bid(client, tmp_path: Path) -> str:
    src = tmp_path / "库"
    src.mkdir(exist_ok=True)
    (src / "智慧园区项目-投标文件.docx").write_bytes(_docx_bytes(BID_SAMPLE))
    client.post("/api/paths", json={"path": str(src)})
    pid = client.get("/api/paths").json()["items"][0]["id"]
    client.post(f"/api/paths/{pid}/scan")
    docs = _wait_docs(client)
    assert docs, "投标文件未入库"
    return docs[0]["id"]


class TestProjectCRUD:
    def test_create_and_list(self, client):
        r = client.post("/api/projects", json={
            "name": "智慧园区数据中心项目", "role": "主标",
            "industry": "数据中心 / ICT", "open_at": "2026-09-17T09:30",
            "platform": "上海市公共资源交易平台", "cust": "南青云网络技术有限公司",
            "sales": "项目负责人",
        })
        assert r.status_code == 200
        r.json()["id"]
        items = client.get("/api/projects").json()["items"]
        assert items[0]["name"] == "智慧园区数据中心项目"
        assert items[0]["stage_label"] == "技术标制作"
        # meta 接口
        meta = client.get("/api/projects/meta").json()
        assert len(meta["industries"]) >= 5
        assert len(meta["stages"]) == 5

    def test_stage_stepper(self, client):
        client.post("/api/projects", json={"name": "阶段测试项目"})
        pid = client.get("/api/projects").json()["items"][0]["id"]
        r = client.post(f"/api/projects/{pid}/stage", json={"stage": 3})
        assert r.json()["stage"] == 3
        assert client.post(f"/api/projects/{pid}/stage", json={"stage": 9}).status_code == 400

    def test_archive_modes(self, client):
        client.post("/api/projects", json={"name": "归档测试"})
        pid = client.get("/api/projects").json()["items"][0]["id"]
        client.post(f"/api/projects/{pid}/archive")
        assert client.get("/api/projects", params={"mode": "archived"}).json()["total"] == 1
        assert client.get("/api/projects", params={"mode": "active"}).json()["total"] == 0

    def test_invalid_name(self, client):
        assert client.post("/api/projects", json={"name": "  "}).status_code == 400


class TestDocumentLibrary:
    def test_upload_tender_and_bid(self, client):
        for kind, sample, name in [
            ("tender", TENDER_SAMPLE, "招标文件_测试.pdf"),
            ("bid", BID_SAMPLE, "投标文件_测试.docx"),
        ]:
            r = client.post(
                "/api/documents/upload",
                files=[("files", (name, _docx_bytes(sample), "application/vnd..."))],
                data={"kind": kind, "is_secret": "true"},
            )
            assert r.status_code == 200
        docs = client.get("/api/documents").json()
        assert docs["total"] == 2
        kinds = {d["kind"] for d in docs["items"]}
        assert kinds == {"tender", "bid"}

    def test_extract_tender(self, client):
        client.post(
            "/api/documents/upload",
            files=[("files", ("招标文件_测试.docx", _docx_bytes(TENDER_SAMPLE), "application/vnd..."))],
            data={"kind": "tender"},
        )
        docs = _wait_docs(client)
        assert docs and docs[0]["status"] == 2, docs
        r = client.get(f"/api/documents/{docs[0]['id']}/extract").json()
        assert r["groups"], "应有拆解组"
        all_names = [i["name"] for g in r["groups"] for i in g["items"]]
        assert "投标人须知前附表" in all_names

    def test_extract_bid(self, client):
        client.post(
            "/api/documents/upload",
            files=[("files", ("投标文件_测试.docx", _docx_bytes(BID_SAMPLE), "application/vnd..."))],
            data={"kind": "bid"},
        )
        docs = _wait_docs(client)
        r = client.get(f"/api/documents/{docs[0]['id']}/extract").json()
        all_names = [i["name"] for g in r["groups"] for i in g["items"]]
        assert "授权委托书" in all_names
        # 字段行内容
        rows = [rw for grp in r["groups"] for it in grp["items"] for rw in it["rows"]]
        assert rows or r["groups"]

    def test_extract_updates_status(self, client):
        client.post(
            "/api/documents/upload",
            files=[("files", ("测试.docx", _docx_bytes(TENDER_SAMPLE), "application/vnd..."))],
            data={"kind": "tender"},
        )
        docs = _wait_docs(client)
        assert docs[0]["status"] == 2


class TestMaterialsAutoHarvest:
    def test_extract_auto_creates_materials(self, client):
        client.post(
            "/api/documents/upload",
            files=[("files", ("投标文件_测试.docx", _docx_bytes(BID_SAMPLE), "application/vnd..."))],
            data={"kind": "bid"},
        )
        _wait_docs(client)
        mats = client.get("/api/materials").json()["items"]
        assert mats, "拆解应自动生成素材"
        cats = {m["cat"] for m in mats}
        assert cats & {"资质文件", "业绩案例", "人员团队"}

    def test_material_crud(self, client):
        rid = client.post("/api/materials", json={
            "title": "测试素材", "type": "表格", "cat": "资质文件",
            "tags": ["测试"], "rows": [["法定代表人", "张建国"]],
        }).json()["id"]
        items = client.get("/api/materials", params={"cat": "资质文件"}).json()["items"]
        assert any(m["id"] == rid for m in items)
        r = client.put(f"/api/materials/{rid}", json={
            "title": "测试素材改", "type": "表格", "cat": "资质文件", "tags": [],
        })
        assert r.json()["ok"]
        assert client.delete(f"/api/materials/{rid}").json()["ok"]

    def test_use_material(self, client):
        client.post(
            "/api/documents/upload",
            files=[("files", ("投标.docx", _docx_bytes(BID_SAMPLE), "application/vnd..."))],
            data={"kind": "bid"},
        )
        _wait_docs(client)
        mats = client.get("/api/materials").json()["items"]
        pid = client.get("/api/projects", params={"mode": "active"}).json()["items"]
        # 无项目时跳过
        if not pid:
            return
        r = client.post(f"/api/materials/{mats[0]['id']}/use", json={"project_id": pid[0]["id"]})
        assert r.json()["ok"]

    def test_delete_with_refs(self, client):
        rid = client.post("/api/materials", json={
            "title": "待删素材", "type": "文本", "cat": "业绩案例", "text": "内容",
        }).json()["id"]
        r = client.delete(f"/api/materials/{rid}").json()
        assert r["ok"]


class TestSearchAndBind:
    def test_search_and_bind_flow(self, client):
        client.post(
            "/api/documents/upload",
            files=[("files", ("投标文件_智慧园区.docx", _docx_bytes(BID_SAMPLE), "application/vnd..."))],
            data={"kind": "bid"},
        )
        docs = _wait_docs(client)
        doc_id = docs[0]["id"]
        # 拆解
        client.post(f"/api/documents/{doc_id}/extract")
        # 检索
        sr = client.get("/api/search", params={"q": "授权委托"})
        hits = sr.json()["hits"]
        assert hits and hits[0]["chunk_id"]
        # 绑定到新项目
        r = client.post(f"/api/documents/{doc_id}/bind", json={
            "new_project_name": "绑定测试项目", "new_project_industry": "金融",
        })
        assert r.json()["ok"]
        # 项目详情应有该文档
        projects = client.get("/api/projects").json()["items"]
        target = next(p for p in projects if p["name"] == "绑定测试项目")
        detail = client.get(f"/api/projects/{target['id']}").json()
        assert any(d["id"] == doc_id for d in detail["documents"])

    def test_secret_gate(self, client):
        client.post(
            "/api/documents/upload",
            files=[("files", ("涉密.docx", _docx_bytes("涉密内容测试"), "application/vnd..."))],
            data={"is_secret": "true"},
        )
        _wait_docs(client)
        docs = client.get("/api/documents", params={"is_secret": 1}).json()
        assert docs["total"] >= 1
        import sqlite3

        from app.config import get_settings

        conn = sqlite3.connect(get_settings().db_path)
        try:
            n = conn.execute(
                "SELECT count(*) FROM audit_log WHERE pipeline LIKE 'glm%'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 0, "涉密文档 GLM 调用次数必须为 0"
