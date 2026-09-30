"""Phase 1：拆解规则中心（契约《标库-拆解规则契约》v1.0）。

覆盖：播种幂等 / 内置保护 / 版本递增 / 变更留痕 / 校验与错误码 / 排序 / 恢复默认 /
只读接口形状。抽取行为改造属 Phase 2，本阶段不动 extractor（另由既有拆解测试守）。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

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
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def _make():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    return _make


def _rules(client) -> dict:
    r = client.get("/api/extract-rules")
    assert r.status_code == 200, r.text
    return r.json()


def _group(data: dict, kind: str) -> dict:
    return next(g for g in data["groups"] if g["kind"] == kind)


# ---------------------------------------------------------------- 播种与清单


def test_builtin_seeded_with_expected_shape(client):
    data = _rules(client)
    tender = _group(data, "tender")
    bid = _group(data, "bid")
    notice = _group(data, "notice")
    assert [i["name"] for i in tender["items"]] == [
        "投标人须知前附表", "评标办法前附表", "投标文件格式",
    ]
    assert [i["name"] for i in bid["items"]] == [
        "近年完成的类似项目情况表", "项目团队成员及相关人员简历",
    ]
    assert notice["items"] == []  # 公告默认空规则集（不改变现有行为）
    first = tender["items"][0]
    assert first["builtin"] is True
    assert first["enabled"] is True
    assert first["scope"] == "default"
    assert first["anchors"][0] == {
        "text": "投标人须知前附表", "priority": 1, "match_mode": "contains", "role": "start",
    }
    assert first["summary"] == "关键条款表"
    assert first["focus_fields"], "关注字段应随内置规则播种"


def test_seed_is_idempotent(client):
    before = _rules(client)
    again = _rules(client)
    ids_before = [i["id"] for i in _group(before, "tender")["items"]]
    ids_again = [i["id"] for i in _group(again, "tender")["items"]]
    assert ids_before == ids_again
    assert again["ruleset_version"] == before["ruleset_version"]  # 只读不推版本


def test_ruleset_version_stable_across_reads(client):
    v1 = _rules(client)["ruleset_version"]
    v2 = _rules(client)["ruleset_version"]
    assert v1 == v2


# ---------------------------------------------------------------- 版本递增


def test_version_bumps_on_each_mutation(client):
    v0 = _rules(client)["ruleset_version"]

    created = client.post("/api/extract-rules", json={
        "kind": "tender", "name": "自定义规则A", "anchors": [{"text": "自定义锚点A", "priority": 1}],
    })
    assert created.status_code == 200, created.text
    rid = created.json()["rule"]["id"]
    v1 = _rules(client)["ruleset_version"]
    assert v1 > v0

    assert client.patch(f"/api/extract-rules/{rid}", json={"name": "自定义规则A2"}).status_code == 200
    v2 = _rules(client)["ruleset_version"]
    assert v2 > v1

    assert client.post("/api/extract-rules/reorder", json={
        "kind": "tender", "ids": [rid],
    }).status_code == 200
    v3 = _rules(client)["ruleset_version"]
    assert v3 > v2

    assert client.delete(f"/api/extract-rules/{rid}").status_code == 200
    v4 = _rules(client)["ruleset_version"]
    assert v4 > v3


def test_noop_patch_does_not_bump_version(client):
    rid = _group(_rules(client), "tender")["items"][0]["id"]
    v0 = _rules(client)["ruleset_version"]
    r = client.patch(f"/api/extract-rules/{rid}", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert _rules(client)["ruleset_version"] == v0


# ---------------------------------------------------------------- 变更留痕


def test_history_records_snapshot_and_note(client):
    rid = _group(_rules(client), "tender")["items"][0]["id"]
    assert client.patch(f"/api/extract-rules/{rid}", json={"enabled": False}).status_code == 200

    h = client.get("/api/extract-rules/history")
    assert h.status_code == 200
    items = h.json()["items"]
    assert items, "变更后应有留痕"
    top = items[0]
    assert top["rule_id"] == rid
    assert "停用" in top["note"]
    assert top["rule_name"] == "投标人须知前附表"
    assert top["created_at"]


def test_history_hides_unsaved_changes(client):
    assert client.get("/api/extract-rules/history").json()["items"] == []


# ---------------------------------------------------------------- 校验与错误码


def test_reject_empty_name(client):
    r = client.post("/api/extract-rules", json={
        "kind": "tender", "name": "   ", "anchors": [{"text": "锚点X"}],
    })
    assert r.status_code == 400
    assert "名称" in r.json()["detail"]


def test_reject_empty_anchors(client):
    r = client.post("/api/extract-rules", json={"kind": "tender", "name": "无锚点规则", "anchors": []})
    assert r.status_code == 400
    assert "锚点" in r.json()["detail"]


def test_reject_duplicate_anchor_inside_rule(client):
    r = client.post("/api/extract-rules", json={
        "kind": "tender", "name": "内部重复",
        "anchors": [{"text": "重复锚点"}, {"text": "重复 锚点"}],  # 归一化后相同
    })
    assert r.status_code == 409


def test_reject_anchor_conflict_with_existing_rule(client):
    r = client.post("/api/extract-rules", json={
        "kind": "tender", "name": "撞车规则", "anchors": [{"text": "评标办法前附表"}],
    })
    assert r.status_code == 409
    assert "重复" in r.json()["detail"]


def test_reject_bad_kind_and_match_mode(client):
    bad_kind = client.post("/api/extract-rules", json={
        "kind": "contract", "name": "X", "anchors": [{"text": "锚点Y"}],
    })
    assert bad_kind.status_code == 400

    bad_mode = client.post("/api/extract-rules", json={
        "kind": "notice", "name": "公告规则", "anchors": [{"text": "公告锚点", "match_mode": "regex"}],
    })
    assert bad_mode.status_code == 400
    assert "match_mode" in bad_mode.json()["detail"]


def test_patch_missing_rule_404(client):
    assert client.patch("/api/extract-rules/not-exist", json={"name": "X"}).status_code == 404
    assert client.delete("/api/extract-rules/not-exist").status_code == 404


def test_builtin_cannot_be_deleted(client):
    rid = _group(_rules(client), "tender")["items"][0]["id"]
    r = client.delete(f"/api/extract-rules/{rid}")
    assert r.status_code == 409
    assert "内置规则不可删除" in r.json()["detail"]


# ---------------------------------------------------------------- 排序


def test_reorder_changes_sequence(client):
    tender = _group(_rules(client), "tender")["items"]
    reversed_ids = [i["id"] for i in reversed(tender)]
    r = client.post("/api/extract-rules/reorder", json={"kind": "tender", "ids": reversed_ids})
    assert r.status_code == 200, r.text
    assert r.json()["order"] == reversed_ids
    after = _group(_rules(client), "tender")["items"]
    assert [i["id"] for i in after] == reversed_ids
    assert [i["seq"] for i in after] == [0, 1, 2]


def test_reorder_rejects_foreign_or_duplicate_ids(client):
    bid_id = _group(_rules(client), "bid")["items"][0]["id"]
    assert client.post("/api/extract-rules/reorder", json={
        "kind": "tender", "ids": [bid_id],
    }).status_code == 409

    tid = _group(_rules(client), "tender")["items"][0]["id"]
    assert client.post("/api/extract-rules/reorder", json={
        "kind": "tender", "ids": [tid, tid],
    }).status_code == 400


def test_reorder_keeps_unlisted_rules_after(client):
    tender = _group(_rules(client), "tender")["items"]
    last_id = tender[-1]["id"]
    r = client.post("/api/extract-rules/reorder", json={"kind": "tender", "ids": [last_id]})
    assert r.status_code == 200
    assert r.json()["order"][0] == last_id
    assert len(r.json()["order"]) == len(tender)


# ---------------------------------------------------------------- 恢复默认


def test_reset_restores_builtin_and_removes_custom(client):
    created = client.post("/api/extract-rules", json={
        "kind": "bid", "name": "临时规则", "anchors": [{"text": "临时锚点"}],
    })
    assert created.status_code == 200
    rid = created.json()["rule"]["id"]

    bid_first = _group(_rules(client), "bid")["items"][0]["id"]
    assert client.patch(f"/api/extract-rules/{bid_first}", json={"enabled": False}).status_code == 200
    assert client.patch(f"/api/extract-rules/{bid_first}", json={"name": "改过的名字"}).status_code == 200

    r = client.post("/api/extract-rules/reset", json={"kind": "bid"})
    assert r.status_code == 200, r.text
    assert r.json()["removed"] >= 1

    bid = _group(_rules(client), "bid")["items"]
    assert [i["id"] for i in bid] == ["bid_similar_projects", "bid_team_resume"]
    assert all(i["enabled"] for i in bid)
    assert bid[0]["name"] == "近年完成的类似项目情况表"
    assert rid not in [i["id"] for i in bid]

    # 其他类型不受影响
    assert len(_group(_rules(client), "tender")["items"]) == 3


def test_reset_all_keeps_notice_empty(client):
    r = client.post("/api/extract-rules/reset", json={})
    assert r.status_code == 200
    data = _rules(client)
    assert _group(data, "notice")["items"] == []
    assert len(_group(data, "tender")["items"]) == 3
    assert len(_group(data, "bid")["items"]) == 2


# ---------------------------------------------------------------- 只读统计


def test_stale_docs_zero_on_fresh_install(client):
    assert _rules(client)["stale_docs"] == 0


def test_notice_rule_can_be_created(client):
    r = client.post("/api/extract-rules", json={
        "kind": "notice", "name": "公告正文",
        "anchors": [{"text": "招标公告"}], "focus_fields": ["采购人", "预算金额"],
    })
    assert r.status_code == 200, r.text
    notice = _group(_rules(client), "notice")["items"]
    assert len(notice) == 1
    assert notice[0]["focus_fields"] == ["采购人", "预算金额"]
    assert notice[0]["builtin"] is False
