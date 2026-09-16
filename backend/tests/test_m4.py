"""M4 测试：废标检测规则引擎 / 已处理闭环 / 报告导出 / 缺件拦截。"""

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


TENDER_FULL = [
    "智慧园区数据中心项目招标文件",
    "投标人须知前附表",
    "递交截止时间：2026-09-17 09:30",
    "投标有效期：90 日历天",
    "投标保证金：20 万元（电汇或保函）",
    "正副本份数：正本 4 份 · 副本 2 份",
    "资质要求：机电工程施工总承包一级",
    "评标办法前附表",
    "评标办法：综合评估法",
    "技术分：60 分",
    "价格分：20 分",
]

BID_GOOD = [
    "智慧园区数据中心项目投标文件",
    "开标一览表",
    "投标报价：1,280 万元",
    "法定代表人身份证明",
    "法定代表人：张建国",
    "单位性质：有限责任公司",
    "授权委托书",
    "委托代理人：王志强",
    "投标有效期：90 日历天",
    "投标保证金：已按招标文件要求缴纳 20 万元",
    "基本情况表",
    "资质等级：机电工程施工总承包一级",
    "近年完成的类似项目情况表",
    "项目名称：银行机房项目",
    "项目团队成员及相关人员简历",
    "项目经理：王志强",
    "技术方案",
    "机房按 B 级标准建设",
]

BID_BAD = [
    "智慧园区数据中心项目投标文件",
    "基本情况表",
    "资质等级：机电工程施工总承包二级",
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


def _wait_docs(client, *, timeout: float = 30) -> list[dict]:
    deadline = time.time() + timeout
    docs: list[dict] = []
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        if docs and all(d["status"] == 2 for d in docs):
            break
        time.sleep(0.4)
    return docs


def _setup_project(client, bid_paragraphs: list[str]) -> str:
    pid = client.post("/api/projects", json={"name": "废标检测项目"}).json()["id"]
    for name, data, kind in (
        ("招标文件.docx", _docx_bytes(TENDER_FULL), "tender"),
        ("投标文件.docx", _docx_bytes(bid_paragraphs), "bid"),
    ):
        r = client.post(
            "/api/documents/upload",
            files=[("files", (name, data, "application/octet-stream"))],
            data={"kind": kind, "project_id": pid, "is_secret": "true"},
        )
        assert r.status_code == 200
    docs = _wait_docs(client)
    assert docs and all(d["status"] == 2 for d in docs), docs
    return pid


class TestDetect:
    def test_good_bid_low_risk(self, client):
        pid = _setup_project(client, BID_GOOD)
        r = client.post(f"/api/detect/{pid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["risk_index"] >= 90, body["items"]
        # 正副本人工核对提示必然存在
        assert any(x["sev"] == "hint" for x in body["items"])

    def test_bad_bid_fatal_findings(self, client):
        pid = _setup_project(client, BID_BAD)
        r = client.post(f"/api/detect/{pid}")
        body = r.json()
        titles = " ".join(x["title"] for x in body["items"])
        assert "授权委托书缺失" in titles
        assert "身份证明缺失" in titles
        assert "保证金" in titles
        assert "有效期" in titles
        assert "资质等级" in titles or "资质" in titles
        # 指数低 = 高风险
        assert body["risk_index"] <= 76
        # 每项都有定位与依据
        for x in body["items"]:
            assert x["req"], x["title"]
            assert x["doc_id"], x["title"]

    def test_score_rows_linked(self, client):
        pid = _setup_project(client, BID_GOOD)
        body = client.post(f"/api/detect/{pid}").json()
        assert body["score_rows"], "评分对照为空"
        assert any(row[4] == "达标" for row in body["score_rows"])

    def test_detect_missing_tender_400(self, client):
        pid = client.post("/api/projects", json={"name": "空项目"}).json()["id"]
        r = client.post(f"/api/detect/{pid}")
        assert r.status_code == 400

    def test_done_closed_loop(self, client):
        pid = _setup_project(client, BID_BAD)
        body = client.post(f"/api/detect/{pid}").json()
        idx0 = body["risk_index"]
        risk_id = body["items"][0]["id"]
        r = client.patch(f"/api/detect/risks/{risk_id}/done", json={"done": True})
        assert r.json()["ok"] is True
        assert r.json()["risk_index"] > idx0  # 处理后指数回升

        risks = client.get(f"/api/detect/{pid}/risks").json()
        assert risks["has_result"] is True
        assert any(x["done"] for x in risks["items"])

    def test_rerank_replaces_old(self, client):
        pid = _setup_project(client, BID_BAD)
        client.post(f"/api/detect/{pid}")
        client.post(f"/api/detect/{pid}")
        risks = client.get(f"/api/detect/{pid}/risks").json()
        fatal_titles = [x["title"] for x in risks["items"] if x["sev"] == "fatal"]
        assert len(fatal_titles) == len(set(fatal_titles)), "重跑后风险项重复"

    def test_report_xlsx(self, client):
        pid = _setup_project(client, BID_BAD)
        client.post(f"/api/detect/{pid}")
        r = client.get(f"/api/detect/{pid}/report.xlsx")
        assert r.status_code == 200
        assert "spreadsheetml" in r.headers["content-type"]
        assert len(r.content) > 4000
        # 未检测的项目 → 404
        pid2 = client.post("/api/projects", json={"name": "无报告项目"}).json()["id"]
        assert client.get(f"/api/detect/{pid2}/report.xlsx").status_code == 404


class TestLlmExtract:
    def test_llm_merged_into_extract(self, client, monkeypatch):
        import app.services.llm_extract as le

        monkeypatch.setattr(le, "llm_available", lambda: True)

        def fake_llm(_text: str, kind: str) -> list[dict]:
            assert kind == "bid"
            return [
                {"group": "资格与身份文件", "name": "法定代表人身份证明", "rows": [["大模型补充字段", "LLM 识别值"]]},
                {"group": "资格与身份文件", "name": "大模型新发现结构项", "rows": [["字段甲", "值乙"]]},
            ]

        monkeypatch.setattr(le, "llm_extract", fake_llm)
        _setup_project(client, BID_GOOD)
        docs = client.get("/api/documents").json()["items"]
        bid_doc = next(d for d in docs if d["kind"] == "bid")
        r = client.post(f"/api/documents/{bid_doc['id']}/extract")
        assert r.status_code == 200

        ex = client.get(f"/api/documents/{bid_doc['id']}/extract").json()
        all_rows = [row for g in ex["groups"] for it in g["items"] for row in it["rows"]]
        names = [it["name"] for g in ex["groups"] for it in g["items"]]
        assert ["大模型补充字段", "LLM 识别值"] in all_rows, "LLM 字段未合并"
        assert "大模型新发现结构项" in names, "LLM 新结构项未追加"

    def test_llm_failure_falls_back(self, client, monkeypatch):
        import app.services.llm_extract as le

        monkeypatch.setattr(le, "llm_available", lambda: True)
        monkeypatch.setattr(le, "llm_extract", lambda _t, _k: [])  # 真实失败路径：静默回空列表
        pid = _setup_project(client, BID_GOOD)
        r = client.post(f"/api/detect/{pid}")
        assert r.status_code == 200 and r.json()["items"] is not None


class TestExtractEditing:
    def test_put_rows_updates_item(self, client):
        _setup_project(client, BID_GOOD)
        docs = client.get("/api/documents").json()["items"]
        bid_doc = next(d for d in docs if d["kind"] == "bid")
        ex = client.get(f"/api/documents/{bid_doc['id']}/extract").json()
        item = next(it for g in ex["groups"] for it in g["items"] if it["rows"])
        r = client.put(
            f"/api/documents/{bid_doc['id']}/extract/{item['id']}",
            json={"rows": [["页内编辑字段", "页内编辑值"], ["新增字段", "新增值"]]},
        )
        assert r.status_code == 200 and r.json()["ok"]
        ex2 = client.get(f"/api/documents/{bid_doc['id']}/extract").json()
        item2 = next(it for g in ex2["groups"] for it in g["items"] if it["id"] == item["id"])
        assert ["页内编辑字段", "页内编辑值"] in item2["rows"]
        assert ["新增字段", "新增值"] in item2["rows"]
        # 覆盖语义：旧行被替换
        assert len(item2["rows"]) == 2

    def test_chapter_toc_only_chapters(self, client):
        """章节目录只收章级条目，不收 8.3.1 之类细目。"""
        tender = [
            "测试项目招标文件",
            "目录",
            "第一章 招标公告",
            "第二章 投标人须知",
            "第三章 评标办法",
            "投标人须知前附表",
            "递交截止时间：2026-10-01 09:00",
            "1. 招标条件",
            "8.3.1 规范性审查功能 应能够按照规则进行审查",
            "8.3.2 工艺设备招标文件规范性审查功能",
            "招标编号：TEST-2026-001",
            "评标办法前附表",
            "评标办法：综合评估法",
        ]
        pid = client.post("/api/projects", json={"name": "目录项目"}).json()["id"]
        r = client.post(
            "/api/documents/upload",
            files=[("files", ("目录招标.docx", _docx_bytes(tender), "application/octet-stream"))],
            data={"kind": "tender", "project_id": pid},
        )
        doc_id = r.json()["created"][0]["id"]
        deadline = time.time() + 60
        while time.time() < deadline:
            docs = client.get("/api/documents").json()["items"]
            if docs and all(d["status"] == 2 for d in docs):
                break
            time.sleep(0.4)
        ex = client.get(f"/api/documents/{doc_id}/extract").json()
        toc = next((it for g in ex["groups"] for it in g["items"] if "章节目录" in it["name"]), None)
        assert toc is not None
        fields = [row[0] for row in toc["rows"]]
        assert "第一章" in fields and "第二章" in fields and "第三章" in fields
        assert not any(f and f[0].isdigit() for f in fields), f"目录混入数字细目：{fields}"
        assert not any("招标编号" in f for f in fields), f"目录混入信息行：{fields}"
