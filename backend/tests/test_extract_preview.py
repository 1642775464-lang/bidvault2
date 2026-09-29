"""Phase 4：规则试运行 preview（契约《标库-拆解规则契约》§十一 v1.1）。

验收标准（§11.6 冻结项）：
1 不产生版本 / 2 不改变文档状态 / 3 不写业务表 / 4 指定规则版本一致否则 409 /
5 返回命中证据（锚点·行号·页号·行数·预览·未命中原因）/ 6 同输入结果确定性一致。
另覆盖：Guard（≤100MB/≤300 页）、与正式拆解一致性（§11.4）、幂等（项目负责人补充）。

数据访问纪律：全部走 ORM 会话对象（参数化），不使用原生 SQL 文本。
"""

from __future__ import annotations

import io
import os
import time

import pytest
from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import get_session, make_engine
from app.main import app
from app.models import (
    Document,
    ExtractExecution,
    ExtractItem,
    ExtractRow,
    Material,
)


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


def _session():
    import app.db as appdb

    assert appdb._SessionLocal is not None
    return appdb._SessionLocal()


TENDER_TEXT = """第一章 招标公告
本项目为智慧园区信息化平台建设。

第二章 投标人须知前附表
递交截止时间：2026-10-08 09:00
投标有效期：90 天
投标保证金：5 万元

第三章 评标办法前附表
评标方法：综合评分法
技术部分 60 分，商务部分 40 分

第四章 合同条款
合同工期：120 天
质保期：24 个月
"""


def _docx_bytes(paras: list[str]) -> bytes:
    d = Docx()
    for p in paras:
        d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _upload(client, name: str, kind: str, paras: list[str]) -> str:
    r = client.post(
        "/api/documents/upload",
        files=[("files", (name, _docx_bytes(paras)))],
        data={"kind": kind},
    )
    assert r.status_code == 200, r.text
    doc_id = r.json()["created"][0]["id"]
    for _ in range(90):
        st = client.get(f"/api/documents/{doc_id}/extract").json().get("status")
        if st == 2:
            return doc_id
        time.sleep(1)
    raise AssertionError("拆解未在 90s 内完成")


def _rule_id(client, kind: str, name: str) -> str:
    for g in client.get("/api/extract-rules").json()["groups"]:
        if g["kind"] == kind:
            for i in g["items"]:
                if i["name"] == name:
                    return i["id"]
    raise AssertionError(f"规则不存在：{kind}/{name}")


def _preview(client, doc_id: str, **kw) -> dict:
    body = {"document_id": doc_id, **kw}
    r = client.post("/api/extract-rules/preview", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _match(payload: dict, name: str) -> dict:
    return next(m for m in payload["matches"] if m["name"] == name)


def _business_counts(client, doc_id: str) -> dict:
    """业务态快照：四类业务表计数 + 文档字段 + 规则集版本（契约 §11.6）。"""
    s = _session()
    try:
        doc = s.get(Document, doc_id)
        item_ids = [i.id for i in s.query(ExtractItem).filter(ExtractItem.doc_id == doc_id).all()]
        rows = s.query(ExtractRow).filter(ExtractRow.item_id.in_(item_ids)).count() if item_ids else 0
        return {
            "items": len(item_ids),
            "rows": rows,
            "materials": s.query(Material).filter(Material.doc_name == doc.filename).count(),
            "executions": s.query(ExtractExecution).filter(ExtractExecution.document_id == doc_id).count(),
            "status": doc.status,
            "error_msg": doc.error_msg,
            "prov_version": doc.extract_ruleset_version,
            "prov_hash": doc.extract_rules_hash,
            "ruleset_version": client.get("/api/extract-rules").json()["ruleset_version"],
        }
    finally:
        s.close()


# ---------------------------------------------------------------- 命中证据


def test_preview_returns_hit_evidence(client):
    doc_id = _upload(client, "招标文件-试运行.docx", "tender", TENDER_TEXT.split("\n"))
    p = _preview(client, doc_id)

    assert p["document"] == {"id": doc_id, "filename": "招标文件-试运行.docx", "kind": "tender"}
    assert p["guard"]["ok"] is True
    names = [m["name"] for m in p["matches"]]
    assert names == ["投标人须知前附表", "评标办法前附表", "投标文件格式"]  # seq 顺序

    m = _match(p, "投标人须知前附表")
    assert m["matched"] is True
    assert m["anchor"] == "投标人须知前附表"
    loc = m["location"]
    assert loc["line_start"] >= 1 and loc["line_end"] >= loc["line_start"]
    assert loc["page_start"] is None and loc["page_end"] is None  # docx 无分页 → null
    assert m["rows_count"] > 0
    assert m["preview"], "命中应给出预览"
    joined = "\n".join(m["preview"])
    assert "递交截止时间" in joined and "投标有效期" in joined
    assert m["truncated"] is False


def test_preview_unmatched_gives_reason(client):
    doc_id = _upload(client, "招标文件-未命中.docx", "tender", TENDER_TEXT.split("\n"))
    created = client.post("/api/extract-rules", json={
        "kind": "tender", "name": "履约要求", "anchors": [{"text": "履约保证金"}, {"text": "履约保函"}],
    })
    assert created.status_code == 200, created.text
    rid = created.json()["rule"]["id"]

    p = _preview(client, doc_id, rules=[rid])
    assert len(p["matches"]) == 1
    m = p["matches"][0]
    assert m["matched"] is False
    assert m["anchor"] is None and m["location"] is None and m["rows_count"] == 0
    assert m["reason"] == "全文未找到：履约保证金 / 履约保函"


def test_preview_rules_subset(client):
    doc_id = _upload(client, "招标文件-子集.docx", "tender", TENDER_TEXT.split("\n"))
    rid = _rule_id(client, "tender", "投标人须知前附表")
    p = _preview(client, doc_id, rules=[rid])
    assert [m["rule_id"] for m in p["matches"]] == [rid]
    assert p["matches"][0]["matched"] is True


def test_preview_row_preview_truncates(client):
    """预览上限：>20 行置 truncated=true 且只返回 20 行（契约 §11.3）。"""
    paras = ["第二章 投标人须知前附表"] + [f"条款内容第{i}行，用于撑起预览长度。" for i in range(1, 31)]
    doc_id = _upload(client, "招标文件-截断.docx", "tender", paras)
    p = _preview(client, doc_id, rules=[_rule_id(client, "tender", "投标人须知前附表")])
    m = p["matches"][0]
    assert m["matched"] is True
    assert m["truncated"] is True
    assert len(m["preview"]) == 20


# ---------------------------------------------------------------- 错误与门禁


def test_preview_version_mismatch_409(client):
    doc_id = _upload(client, "招标文件-版本.docx", "tender", TENDER_TEXT.split("\n"))
    r = client.post("/api/extract-rules/preview", json={
        "document_id": doc_id, "rule_set_version": 999,
    })
    assert r.status_code == 409
    assert "版本不一致" in r.json()["detail"]


def test_preview_foreign_kind_rule_409(client):
    doc_id = _upload(client, "招标文件-异类.docx", "tender", TENDER_TEXT.split("\n"))
    bid_rid = _rule_id(client, "bid", "近年完成的类似项目情况表")
    r = client.post("/api/extract-rules/preview", json={
        "document_id": doc_id, "rules": [bid_rid],
    })
    assert r.status_code == 409
    assert "不属于该文档类型" in r.json()["detail"]


def test_preview_404_and_no_archive(client):
    assert client.post("/api/extract-rules/preview", json={"document_id": "nope"}).status_code == 404

    doc_id = _upload(client, "招标文件-无档案.docx", "tender", TENDER_TEXT.split("\n"))
    s = _session()
    try:
        doc = s.get(Document, doc_id)
        doc.office_document_id = None
        s.commit()
    finally:
        s.close()
    r = client.post("/api/extract-rules/preview", json={"document_id": doc_id})
    assert r.status_code == 409


def test_guard_thresholds():
    """资源门禁纯函数：文件与页数阈值、边界值。"""
    from app.services.extract_preview import (
        PREVIEW_MAX_FILE_BYTES,
        PREVIEW_MAX_PAGES,
        PreviewTooLargeError,
        guard_check,
    )

    ok = guard_check(PREVIEW_MAX_FILE_BYTES, PREVIEW_MAX_PAGES)
    assert ok["ok"] is True
    with pytest.raises(PreviewTooLargeError):
        guard_check(PREVIEW_MAX_FILE_BYTES + 1, None)
    with pytest.raises(PreviewTooLargeError):
        guard_check(1, PREVIEW_MAX_PAGES + 1)


def test_preview_page_limit_413(client, monkeypatch):
    """页数超限 → 413 + 固定文案（契约 §11.5）。"""
    import app.services.extract_preview as ep
    from app.services.ingest.parser import ParsedDoc

    doc_id = _upload(client, "招标文件-超页.docx", "tender", TENDER_TEXT.split("\n"))

    real_parse = ep.parse_any

    def _many_pages(data, ft):
        parsed = real_parse(data, ft)
        return ParsedDoc(
            text=parsed.text,
            pages=["page"] * (ep.PREVIEW_MAX_PAGES + 1),
            page_conf=parsed.page_conf,
            doc_tables=parsed.doc_tables,
        )

    monkeypatch.setattr(ep, "parse_any", _many_pages)
    r = client.post("/api/extract-rules/preview", json={"document_id": doc_id})
    assert r.status_code == 413
    assert r.json()["detail"] == "文档较大，请执行正式拆解查看结果"


# ---------------------------------------------------------------- §11.6 验收


def test_preview_is_dry_run_no_business_mutation(client):
    """验收 1/2/3：不产生版本、不改文档状态、不写业务表。"""
    doc_id = _upload(client, "招标文件-干跑.docx", "tender", TENDER_TEXT.split("\n"))
    before = _business_counts(client, doc_id)

    rid = _rule_id(client, "tender", "投标人须知前附表")
    _preview(client, doc_id, rule_set_version=before["ruleset_version"], rules=[rid])
    _preview(client, doc_id)  # 再跑一次全量

    after = _business_counts(client, doc_id)
    assert after == before, f"preview 产生了业务态变更：\nbefore={before}\nafter={after}"


def test_preview_deterministic_and_idempotent(client):
    """验收 6 + 项目负责人补充：同一请求连续多次，业务 JSON 完全一致。"""
    doc_id = _upload(client, "招标文件-幂等.docx", "tender", TENDER_TEXT.split("\n"))
    rid = _rule_id(client, "tender", "评标办法前附表")
    body = {"document_id": doc_id, "rules": [rid]}
    versions = client.get("/api/extract-rules").json()["ruleset_version"]
    body["rule_set_version"] = versions

    results = [
        client.post("/api/extract-rules/preview", json=body).json()
        for _ in range(3)
    ]
    assert results[0] == results[1] == results[2], "同输入三次调用结果应完全一致"


def test_preview_consistent_with_actual_extraction(client):
    """契约 §11.4：预览所见 = 正式拆解所得（行数与内容逐行一致）。"""
    from app.services.extract_preview import _row_to_text

    doc_id = _upload(client, "招标文件-一致性.docx", "tender", TENDER_TEXT.split("\n"))
    p = _preview(client, doc_id)
    m = _match(p, "投标人须知前附表")

    groups = client.get(f"/api/documents/{doc_id}/extract").json()["groups"]
    actual_rows = next(
        it["rows"] for g in groups for it in g["items"] if it["name"] == "投标人须知前附表"
    )
    assert m["rows_count"] == len(actual_rows)
    assert m["preview"] == [_row_to_text(r) for r in actual_rows][:20]

    # 行号区间覆盖锚点行：line_start 落在文档行内
    total_lines = len(TENDER_TEXT.split("\n"))
    assert 1 <= m["location"]["line_start"] <= total_lines
    assert m["location"]["line_end"] <= total_lines
