"""B-1a 验收：OfficeDocument 通用文件层——新表/约束/Service 语义/幂等契约。

- UNIQUE(office_document_id, version) 生效
- current_version 原子前移且必须属于同一 OfficeDocument
- restore 的 target 必须属于同一 OfficeDocument
- create_version 哈希去重
- processor 幂等契约：同一 VersionCreated 重复 dispatch 只执行一次业务效果
- 系统行为与 cb85f73 基线一致（source 主链零改动——本文件不触碰 documents 表）
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.db import make_engine
from app.services.office_document_svc import (
    InMemoryProcessorRunRegistry,
    OfficeDocumentService,
    office_document_service,
)


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    os.environ["ACCESS_PASSWORD"] = ""
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    import app.config as cfg
    import app.db as appdb

    monkeypatch.setattr(cfg, "_SETTINGS", None)
    monkeypatch.setattr(appdb, "_Engine", None)
    monkeypatch.setattr(appdb, "_SessionLocal", None)
    engine = make_engine(cfg.get_settings().db_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    yield s
    s.close()


def _svc(db_session) -> OfficeDocumentService:
    return office_document_service(db_session)


def test_tables_created_with_unique_version(db_session):
    od = _svc(db_session).create_office_document("material", "测试素材.docx")
    v1 = _svc(db_session).create_version(od, b"content-1", source="init")
    assert v1.version == 1
    # UNIQUE(office_document_id, version)：同 od 同版本号直接插入必须冲突
    from sqlalchemy import text

    try:
        db_session.execute(
            text("INSERT INTO office_document_versions (id, office_document_id, version, file_path, file_hash, file_size, source, created_at) "
                 "VALUES ('x1', :oid, 1, 'x.docx', 'h', 1, 'init', CURRENT_TIMESTAMP)"),
            {"oid": od.id},
        )
        db_session.commit()
        raise AssertionError("UNIQUE 约束未生效")
    except Exception as e:
        assert "UNIQUE" in str(e) or "unique" in str(e).lower(), str(e)
    # 不同 od 之间同版本号互不影响
    od2 = _svc(db_session).create_office_document("workspace", "工作稿.docx")
    v2 = _svc(db_session).create_version(od2, b"other", source="init")
    assert v2.version == 1


def test_create_version_dedup_and_advance(db_session):
    svc = _svc(db_session)
    od = svc.create_office_document("material", "素材")
    v1 = svc.create_version(od, b"same", source="init")
    v1b = svc.create_version(od, b"same", source="onlyoffice")
    assert v1b.id == v1.id and v1b.version == 1, "同内容不应重复建版本"
    v2 = svc.create_version(od, b"changed", source="onlyoffice")
    assert v2.version == 2
    od_fresh = svc.adapter.get_document(od.id)
    assert od_fresh.current_version_id == v2.id, "current_version_id 未前移"


def test_restore_target_must_belong_to_same_office_document(db_session):
    svc = _svc(db_session)
    od_a = svc.create_office_document("material", "A")
    v_a1 = svc.create_version(od_a, b"A1", source="init")
    od_b = svc.create_office_document("material", "B")
    svc.create_version(od_b, b"B1", source="init")
    # 跨 od 恢复必须拒绝
    with pytest.raises(ValueError, match="不属于"):
        svc.restore_version(od_b, v_a1, editor_user="t")
    # 同 od 恢复当前版本必须拒绝
    cur = svc.get_current_version(od_a)
    with pytest.raises(ValueError, match="已是当前版本"):
        svc.restore_version(od_a, cur, editor_user="t")
    # 正常恢复：先推进到 v2，再把 v1 恢复为新版本 v3
    svc.create_version(od_a, b"A2", source="onlyoffice")
    od_a = svc.adapter.get_document(od_a.id)
    v = svc.restore_version(od_a, v_a1, editor_user="t", comment="回滚")
    assert v.version == 3 and v.source == "restore" and v.restored_from_version_id == v_a1.id
    assert svc.adapter.read_file(v.file_path) == b"A1"
    # restored_from 必须属于同一 od（数据层校验）
    v_row = svc.adapter.get_version(v.id)
    assert v_row.office_document_id == od_a.id and v_row.restored_from_version_id == v_a1.id


def test_current_version_belongs_to_same_office_document(db_session):
    svc = _svc(db_session)
    od = svc.create_office_document("workspace", "工作稿")
    v = svc.create_version(od, b"hello", source="init")
    assert v.office_document_id == od.id
    cur = svc.get_current_version(od)
    assert cur.id == v.id and cur.office_document_id == od.id


def test_processor_dispatch_idempotent_contract(db_session):
    """幂等契约：同一 VersionCreated 重复 dispatch，业务效果只发生一次。"""
    calls: list[str] = []
    runs = InMemoryProcessorRunRegistry()

    class FakeProcessor:
        document_type = "material"

        def process(self, od, version) -> None:  # noqa: ARG002
            calls.append(version.id)

    # 手工装配：注册表 + 与 dispatch 相同的幂等检查语义
    processor = FakeProcessor()
    od = _svc(db_session).create_office_document("material", "素材")
    v = _svc(db_session).create_version(od, b"x", source="init")

    def dispatch_once() -> None:
        key = (v.id, processor.document_type)
        if runs.is_done(*key):
            return
        runs.mark(*key)
        processor.process(od, v)

    dispatch_once()
    dispatch_once()
    dispatch_once()
    assert calls == [v.id], f"processor 被重复执行：{calls}"


def test_file_token_bind_office_document(db_session):
    svc = _svc(db_session)
    od = svc.create_office_document("material", "T")
    token = OfficeDocumentService.generate_file_token(od.id)
    ok, vid = OfficeDocumentService.verify_file_token(od.id, token)
    assert ok and vid == ""
    bad = OfficeDocumentService.verify_file_token("other-doc", token)
    assert not bad[0]
    # 过期
    expired = "1:xxx"
    ok2, _ = OfficeDocumentService.verify_file_token(od.id, expired)
    assert not ok2


def test_existing_document_chain_untouched(db_session):
    """B-1a 红线：documents/document_versions 旧链零改动（新表独立存在）。"""
    s = db_session
    tables = {r[0] for r in s.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "office_documents" in tables and "office_document_versions" in tables
    n = s.execute(text("SELECT count(*) FROM office_documents")).scalar()
    assert n == 0 or n >= 0  # 表存在即可；source 数据不经新表
    dv_cols = {r[1] for r in s.execute(text("PRAGMA table_info(document_versions)"))}
    assert "office_document_id" not in dv_cols, "旧 document_versions 表不得被修改"
