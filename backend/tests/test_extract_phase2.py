"""Phase 2：Extractor 接入与执行追溯（契约《标库-拆解规则契约》v1.0）。

验收重点（项目负责人 Phase 2 批准单）：
1. 内置规则下输出与改造前一致（rules=None 对照）
2. 自定义规则真实生效（含停用不抽、公告规则生效）
3. Execution snapshot 可解释（执行前冻结、成功/失败落库）
4. 规则修改后旧文档进入 RULE_CHANGED
5. 老文档（无 provenance）进入 NEED_REBUILD
6. provenance 写入与状态机（BUILDING/FAILED 优先）

数据访问纪律：全部走 ORM 会话对象（参数化），不使用原生 SQL 文本。
"""

from __future__ import annotations

import io
import json
import os
import time

import pytest
from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import get_session, make_engine
from app.main import app
from app.models import Document, ExtractExecution


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


def _executions(session, doc_id: str) -> list[ExtractExecution]:
    return (
        session.query(ExtractExecution)
        .filter(ExtractExecution.document_id == doc_id)
        .order_by(ExtractExecution.started_at)
        .all()
    )


def _add_execution(session, *, ex_id: str, doc_id: str, status: str) -> None:
    """夹具：插一条执行记录（ORM 参数化写入）。"""
    session.add(ExtractExecution(
        id=ex_id, document_id=doc_id, ruleset_version=1, rules_hash="fixture",
        rule_snapshot_json="[]", trigger="manual", status=status,
    ))
    session.commit()


# ---------------------------------------------------------------- 构造素材

TENDER_TEXT = """第一章 招标公告
本项目为信息化平台建设。

第二章 投标人须知前附表
递交截止时间：2026-10-01 09:00
投标有效期：90 天

第三章 评标办法前附表
评标方法：综合评分法
技术部分 60 分

第四章 合同条款
合同工期 120 天
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


def _items(client, doc_id: str) -> list[dict]:
    return client.get(f"/api/documents/{doc_id}/extract").json()["groups"]


def _rows_of(client, doc_id: str, item_name: str) -> list:
    for g in _items(client, doc_id):
        for it in g["items"]:
            if it["name"] == item_name:
                return it["rows"]
    return []


def _doc_row(client, doc_id: str) -> dict:
    return next(x for x in client.get("/api/documents").json()["items"] if x["id"] == doc_id)


def _rule_id(client, kind: str, name: str) -> str:
    for g in client.get("/api/extract-rules").json()["groups"]:
        if g["kind"] != kind:
            continue
        for i in g["items"]:
            if i["name"] == name:
                return i["id"]
    raise AssertionError(f"规则不存在：{kind}/{name}")


# ---------------------------------------------------------------- 1. 行为等价


def test_rules_none_matches_builtin_snapshot(client):
    """安全阀等价性：rules=None 与「内置规则集」产出必须逐行一致。"""
    from app.services.extract_rules import ensure_rules, resolve_rules
    from app.services.ingest.extractor import extract_structured

    s = _session()
    try:
        ensure_rules(s)
        for kind in ("tender", "bid"):
            builtin = resolve_rules(s, kind)
            legacy = extract_structured(TENDER_TEXT, kind, None)
            via_db = extract_structured(TENDER_TEXT, kind, None, rules=builtin)
            assert len(legacy) == len(via_db) == len(builtin)
            for a, b in zip(legacy, via_db):
                assert (a.group_name, a.name, a.sum) == (b.group_name, b.name, b.sum)
                assert a.rows == b.rows, f"{kind} 输出不一致"
    finally:
        s.close()


def test_legacy_kind_guard_preserved(client):
    """未配置规则时，公告等类型仍不抽取（默认行为不变）。"""
    from app.services.ingest.extractor import extract_structured

    assert extract_structured(TENDER_TEXT, "notice", None) == []


def test_empty_rules_means_no_extraction(client):
    from app.services.ingest.extractor import extract_structured

    assert extract_structured(TENDER_TEXT, "tender", None, rules=[]) == []


# ---------------------------------------------------------------- 2. 配置生效


def test_custom_rule_takes_effect(client):
    r = client.post("/api/extract-rules", json={
        "kind": "tender", "name": "合同条款",
        "anchors": [{"text": "合同条款"}],
    })
    assert r.status_code == 200, r.text

    doc_id = _upload(client, "招标文件-自定义规则.docx", "tender", TENDER_TEXT.split("\n"))
    names = [it["name"] for g in _items(client, doc_id) for it in g["items"]]
    assert "合同条款" in names, f"自定义规则未生效：{names}"
    rows = _rows_of(client, doc_id, "合同条款")
    flat = [str(c) for r in rows for c in (r if isinstance(r, list) else [r])]
    assert any("合同工期 120 天" in c for c in flat), f"自定义规则区间内容不对：{flat}"


def test_disabled_rule_not_extracted(client):
    rid = _rule_id(client, "tender", "评标办法前附表")
    assert client.patch(f"/api/extract-rules/{rid}", json={"enabled": False}).status_code == 200

    doc_id = _upload(client, "招标文件-停用规则.docx", "tender", TENDER_TEXT.split("\n"))
    names = [it["name"] for g in _items(client, doc_id) for it in g["items"]]
    assert "评标办法前附表" not in names, f"停用规则仍被抽取：{names}"


def test_notice_rule_takes_effect_after_config(client):
    r = client.post("/api/extract-rules", json={
        "kind": "notice", "name": "公告正文",
        "anchors": [{"text": "招标公告"}],
    })
    assert r.status_code == 200, r.text
    doc_id = _upload(client, "招标公告.docx", "notice", TENDER_TEXT.split("\n"))
    names = [it["name"] for g in _items(client, doc_id) for it in g["items"]]
    assert "公告正文" in names, f"公告规则未生效：{names}"


# ---------------------------------------------------------------- 3. 执行追溯


def test_execution_recorded_with_frozen_snapshot(client):
    doc_id = _upload(client, "招标文件-追溯.docx", "tender", TENDER_TEXT.split("\n"))

    s = _session()
    try:
        rows = _executions(s, doc_id)
    finally:
        s.close()
    assert len(rows) == 1, "应落一条执行记录"
    ex = rows[0]
    assert ex.status == "success"
    assert ex.trigger == "import"  # 上传路径
    assert ex.items_count > 0 and ex.rows_count > 0
    assert ex.finished_at is not None
    snap = json.loads(ex.rule_snapshot_json)
    assert snap and all(r["kind"] == "tender" for r in snap), "快照应只含该文档类型的规则"
    assert ex.rules_hash and len(ex.rules_hash) == 64
    assert ex.ruleset_version >= 0  # 0 = 内置默认未改动


def test_provenance_written_on_success(client):
    doc_id = _upload(client, "招标文件-provenance.docx", "tender", TENDER_TEXT.split("\n"))
    d = _doc_row(client, doc_id)
    assert d["extract_ruleset_version"] is not None
    assert d["extract_status"] == "NORMAL"

    s = _session()
    try:
        doc = s.get(Document, doc_id)
        ex = _executions(s, doc_id)[-1]
        assert doc.extract_rules_hash == ex.rules_hash, "文档 provenance 应与执行快照摘要一致"
    finally:
        s.close()


def test_reextract_trigger_recorded(client):
    doc_id = _upload(client, "招标文件-重拆.docx", "tender", TENDER_TEXT.split("\n"))
    assert client.post(f"/api/documents/{doc_id}/extract").status_code == 200
    s = _session()
    try:
        triggers = [ex.trigger for ex in _executions(s, doc_id)]
    finally:
        s.close()
    assert triggers == ["import", "reextract"], triggers


def test_historical_snapshot_immutable_after_rule_change(client):
    """条款 A/B：历史执行快照不随后续规则修改而改变。"""
    doc_id = _upload(client, "招标文件-冻结.docx", "tender", TENDER_TEXT.split("\n"))
    first = [it["name"] for g in _items(client, doc_id) for it in g["items"]]
    assert first[0] == "投标人须知前附表"

    rid = _rule_id(client, "tender", "投标人须知前附表")
    assert client.patch(f"/api/extract-rules/{rid}", json={"name": "须知前附表(改名)"}).status_code == 200

    s = _session()
    try:
        snaps = [json.loads(ex.rule_snapshot_json) for ex in _executions(s, doc_id)]
    finally:
        s.close()
    assert snaps[0][0]["name"] == "投标人须知前附表", "历史快照不得被后续规则修改影响"


# ---------------------------------------------------------------- 4/5. 状态机


def test_rule_change_marks_document_rule_changed(client):
    doc_id = _upload(client, "招标文件-状态.docx", "tender", TENDER_TEXT.split("\n"))
    assert _doc_row(client, doc_id)["extract_status"] == "NORMAL"

    rid = _rule_id(client, "tender", "投标人须知前附表")
    assert client.patch(f"/api/extract-rules/{rid}", json={"enabled": False}).status_code == 200

    d = _doc_row(client, doc_id)
    assert d["extract_status"] == "RULE_CHANGED", d
    assert d["ruleset_version"] > d["extract_ruleset_version"]

    # 重拆后回到 NORMAL
    assert client.post(f"/api/documents/{doc_id}/extract").status_code == 200
    assert _doc_row(client, doc_id)["extract_status"] == "NORMAL"


def test_other_kind_rule_change_does_not_flag(client):
    """改投标规则不应把招标文档误标为过期（同类 hash 口径）。"""
    doc_id = _upload(client, "招标文件-异类.docx", "tender", TENDER_TEXT.split("\n"))
    bid_rid = _rule_id(client, "bid", "近年完成的类似项目情况表")
    assert client.patch(f"/api/extract-rules/{bid_rid}", json={"enabled": False}).status_code == 200
    assert _doc_row(client, doc_id)["extract_status"] == "NORMAL", "异类规则变更不应影响本文档"


def test_legacy_document_without_provenance_is_need_rebuild(client):
    """契约 §5.5 强制项：无 provenance 的历史文档必须 NEED_REBUILD，禁止 NORMAL。"""
    doc_id = _upload(client, "招标文件-老文档.docx", "tender", TENDER_TEXT.split("\n"))
    s = _session()
    try:
        doc = s.get(Document, doc_id)
        doc.extract_ruleset_version = None
        doc.extract_rules_hash = None
        s.commit()
    finally:
        s.close()
    d = _doc_row(client, doc_id)
    assert d["extract_status"] == "NEED_REBUILD", d
    assert d["extract_ruleset_version"] is None


def test_building_and_failed_take_precedence(client):
    doc_id = _upload(client, "招标文件-优先.docx", "tender", TENDER_TEXT.split("\n"))
    s = _session()
    try:
        _add_execution(s, ex_id="ex_running_fixture", doc_id=doc_id, status="running")
    finally:
        s.close()
    assert _doc_row(client, doc_id)["extract_status"] == "BUILDING"

    s = _session()
    try:
        ex = s.get(ExtractExecution, "ex_running_fixture")
        ex.status = "failed"
        ex.error_msg = "模拟失败"
        s.commit()
    finally:
        s.close()
    assert _doc_row(client, doc_id)["extract_status"] == "FAILED"


def test_orphan_running_reconciled_on_startup(client):
    from app.services.extract_provenance import reconcile_orphan_executions

    s = _session()
    try:
        _add_execution(s, ex_id="ex_orphan_fixture", doc_id="no-such-doc", status="running")
        n = reconcile_orphan_executions(s)
        ex = s.get(ExtractExecution, "ex_orphan_fixture")
        status, err = ex.status, ex.error_msg
    finally:
        s.close()
    assert n == 1
    assert status == "failed"
    assert "进程中断" in err


def test_extract_failure_records_failed_execution(client, monkeypatch):
    """执行异常 → 执行记录 failed（快照保留），provenance 不写入。"""
    import app.services.ingest.extractor as ex

    doc_id = _upload(client, "招标文件-失败.docx", "tender", TENDER_TEXT.split("\n"))

    def _boom(*a, **k):
        raise RuntimeError("模拟抽取异常")

    monkeypatch.setattr(ex, "extract_structured", _boom)
    r = client.post(f"/api/documents/{doc_id}/extract")
    assert r.status_code == 200 and r.json()["status"] == 0  # 失败落 status=0

    s = _session()
    try:
        rows = _executions(s, doc_id)
        last = rows[-1]
        status, err, snap = last.status, last.error_msg, last.rule_snapshot_json
    finally:
        s.close()
    assert status == "failed"
    assert "模拟抽取异常" in err
    assert json.loads(snap), "失败也须保留快照"
    assert _doc_row(client, doc_id)["extract_status"] == "FAILED"


# ---------------------------------------------------------------- 6. diff


def test_diff_reports_added_removed_changed(client):
    doc_id = _upload(client, "招标文件-差异.docx", "tender", TENDER_TEXT.split("\n"))
    base = client.get(f"/api/documents/{doc_id}/extract-diff").json()
    assert base["available"] is True
    assert base["added"] == [] and base["removed"] == [] and base["changed"] == []
    assert base["unchanged"] == 3

    # 新增一条 + 改名一条 + 停用一条
    assert client.post("/api/extract-rules", json={
        "kind": "tender", "name": "合同条款", "anchors": [{"text": "合同条款"}],
    }).status_code == 200
    assert client.patch(f"/api/extract-rules/{_rule_id(client, 'tender', '投标文件格式')}",
                        json={"name": "投标文件格式(改)"}).status_code == 200
    assert client.patch(f"/api/extract-rules/{_rule_id(client, 'tender', '评标办法前附表')}",
                        json={"enabled": False}).status_code == 200

    d = client.get(f"/api/documents/{doc_id}/extract-diff").json()
    assert [x["name"] for x in d["added"]] == ["合同条款"]
    # 停用不等于删除：必须报为「已修改·enabled」，不得误报 removed
    assert d["removed"] == [], f"停用被误报为删除：{d['removed']}"
    changed = {x["name"]: x["fields"] for x in d["changed"]}
    assert "name" in changed.get("投标文件格式(改)", [])
    assert "enabled" in changed.get("评标办法前附表", [])
    assert d["to_version"] > d["from_version"]


def test_anchor_with_chapter_prefix_rejected(client):
    """UX 护栏：锚点自带章节号会永不命中，保存时直接拦截。"""
    r = client.post("/api/extract-rules", json={
        "kind": "tender", "name": "带章节号", "anchors": [{"text": "第四章 合同条款"}],
    })
    assert r.status_code == 400
    assert "章节号" in r.json()["detail"]


def test_diff_reports_removed_rule_from_snapshot(client):
    created = client.post("/api/extract-rules", json={
        "kind": "tender", "name": "临时规则", "anchors": [{"text": "合同条款"}],
    }).json()["rule"]["id"]
    doc_id = _upload(client, "招标文件-删规则.docx", "tender", TENDER_TEXT.split("\n"))
    assert client.delete(f"/api/extract-rules/{created}").status_code == 200

    d = client.get(f"/api/documents/{doc_id}/extract-diff").json()
    assert [x["name"] for x in d["removed"]] == ["临时规则"], d
    assert d["available"] is True


def test_diff_unavailable_without_execution(client):
    """老文档（规则中心上线前拆解）无快照可比，且不得回查规则表伪造历史。"""
    doc_id = _upload(client, "招标文件-无快照.docx", "tender", TENDER_TEXT.split("\n"))
    s = _session()
    try:
        for ex in _executions(s, doc_id):
            s.delete(ex)
        doc = s.get(Document, doc_id)  # 同时清 provenance，还原「上线前拆解」的老文档形态
        doc.extract_ruleset_version = None
        doc.extract_rules_hash = None
        s.commit()
    finally:
        s.close()
    d = client.get(f"/api/documents/{doc_id}/extract-diff").json()
    assert d["available"] is False
    assert "无快照可比" in d["reason"]
    assert d["extract_status"] == "NEED_REBUILD"


def test_diff_404_for_missing_document(client):
    assert client.get("/api/documents/nope/extract-diff").status_code == 404


def test_stale_doc_count_reflects_rule_change(client):
    assert client.get("/api/extract-rules").json()["stale_docs"] == 0
    doc_id = _upload(client, "招标文件-统计.docx", "tender", TENDER_TEXT.split("\n"))
    assert client.get("/api/extract-rules").json()["stale_docs"] == 0
    rid = _rule_id(client, "tender", "投标人须知前附表")
    assert client.patch(f"/api/extract-rules/{rid}", json={"enabled": False}).status_code == 200
    assert client.get("/api/extract-rules").json()["stale_docs"] == 1
    assert client.post(f"/api/documents/{doc_id}/extract").status_code == 200
    assert client.get("/api/extract-rules").json()["stale_docs"] == 0
