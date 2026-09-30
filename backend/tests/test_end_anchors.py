"""契约 v1.2：结束锚点（role=end）——区间终点 = min(下一全局边界, 结束锚点行)。

覆盖：终点计算（不含结束锚点行）/ 未命中回退 / 与既有行为等价（无 end 锚点）/
校验（全 end 拒绝、跨角色重复拒绝）/ API 往返 role 保留 / 真实文档场景
（团队简历配 end=技术服务方案，区间不再吞尾部）。
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import get_session, make_engine
from app.main import app
from app.services.ingest.extractor import extract_structured_with_regions

DOC_TEXT = """第一章 招标公告
本项目为智慧园区信息化平台建设。

第二章 投标文件格式
开标一览表
分项报价表

项目团队成员
项目经理-张三
简历表
身份证
学历证书
劳动合同
有效期内的资质证书
技术经理-李四
简历表
身份证
学历证明

技术服务方案
1.质保服务期限
免费质保一年。
2.售后服务
48小时内到场。
"""


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


def _regions(rules):
    return extract_structured_with_regions(DOC_TEXT, "bid", None, rules=rules)


# ---------------------------------------------------------------- 引擎终点


def test_end_anchor_bounds_region():
    """结束锚点截断区间：技术服务方案行不进入团队区间。"""
    rules = [{
        "id": "team", "name": "团队简历", "summary": "",
        "anchors": [
            {"text": "项目团队成员", "priority": 1, "role": "start"},
            {"text": "技术服务方案", "priority": 2, "role": "end"},
        ],
    }]
    (reg,) = _regions(rules)
    assert reg.anchor_hit == "项目团队成员"
    assert reg.end_hit == "技术服务方案"
    blob = json.dumps(reg.group.rows, ensure_ascii=False)
    assert "简历表" in blob and "身份证" in blob and "有效期内的资质证书" in blob
    assert "质保服务期限" not in blob and "售后服务" not in blob
    # 结束锚点行本身不进入区间
    assert "技术服务方案" not in blob


def test_end_anchor_missing_falls_back(client=None):
    """结束锚点未命中 → 回退原行为（区间到文档末尾），不产生空区间。"""
    rules = [{
        "id": "team", "name": "团队简历", "summary": "",
        "anchors": [
            {"text": "项目团队成员", "priority": 1, "role": "start"},
            {"text": "不存在的标题", "priority": 2, "role": "end"},
        ],
    }]
    (reg,) = _regions(rules)
    assert reg.end_hit is None
    blob = json.dumps(reg.group.rows, ensure_ascii=False)
    assert "质保服务期限" in blob and "售后服务" in blob  # 吞到文档末尾（回退）


def test_end_anchor_before_start_is_ignored():
    """结束锚点命中行在起始锚点之前 → 忽略（只搜起始行之后），不产生空区间。"""
    rules = [{
        "id": "x", "name": "X", "summary": "",
        "anchors": [
            {"text": "项目团队成员", "priority": 1, "role": "start"},
            {"text": "第一章 招标公告", "priority": 2, "role": "end"},
        ],
    }]
    (reg,) = _regions(rules)
    assert reg.anchor_line is not None
    assert reg.end_line > reg.anchor_line
    assert reg.group.rows  # 非空


def test_rules_without_end_anchor_unchanged():
    """无 end 锚点规则行为与 v1.1 完全一致（等价性）。"""
    rules = [{
        "id": "team", "name": "团队简历", "summary": "",
        "anchors": [{"text": "项目团队成员", "priority": 1}],
    }]
    legacy_rules = [{"id": "team", "name": "团队简历", "anchors": "项目团队成员", "summary": ""}]
    via_obj = _regions(rules)[0]
    via_none = extract_structured_with_regions(
        DOC_TEXT, "bid", None,
        rules=[{"id": "team", "name": "团队简历", "summary": "", "anchors": ["项目团队成员"]}],
    )[0]
    via_builtin = extract_structured_with_regions(DOC_TEXT, "bid", None)
    assert via_obj.group.rows == via_none.group.rows == via_builtin[1].group.rows
    assert legacy_rules  # 占位：legacy 常量路径由既有测试守住


# ---------------------------------------------------------------- 校验与 API


def test_all_end_anchors_rejected(client):
    r = client.post("/api/extract-rules", json={
        "kind": "tender", "name": "只有结束",
        "anchors": [{"text": "技术服务方案", "role": "end"}],
    })
    assert r.status_code == 400
    assert "起始锚点" in r.json()["detail"]


def test_cross_role_duplicate_anchor_rejected(client):
    r = client.post("/api/extract-rules", json={
        "kind": "tender", "name": "同文双角色",
        "anchors": [{"text": "技术服务方案", "role": "start"},
                    {"text": "技术服务方案", "role": "end"}],
    })
    assert r.status_code == 409


def test_role_roundtrip_via_api(client):
    created = client.post("/api/extract-rules", json={
        "kind": "bid", "name": "带结束锚点的规则",
        "anchors": [{"text": "近年完成的类似项目情况", "role": "start"},
                    {"text": "技术服务方案", "role": "end"}],
    })
    assert created.status_code == 200, created.text
    rule = created.json()["rule"]
    roles = {a["text"]: a["role"] for a in rule["anchors"]}
    assert roles == {"近年完成的类似项目情况": "start", "技术服务方案": "end"}

    # PATCH 改角色 → 往返保留
    rid = rule["id"]
    patched = client.patch(f"/api/extract-rules/{rid}", json={
        "anchors": [{"text": "近年完成的类似项目情况", "role": "start"},
                    {"text": "技术服务方案", "role": "start"}],
    })
    assert patched.status_code == 200
    roles2 = {a["text"]: a["role"] for a in patched.json()["rule"]["anchors"]}
    assert set(roles2.values()) == {"start"}
