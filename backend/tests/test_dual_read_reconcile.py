"""B-1c-2 验收：Legacy/Office 双读对账——explicit compare，无 fallback。

- 一致场景：全部 matched（含 current 为空 → inferred 不算 mismatch）
- 差异场景：hash/path/current/restore_chain/missing version 逐一检出
- 真机：民机文档 restore 链（v1→v2→v3 restore）old/new restored_from 完全一致
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.db import make_engine
from app.services.version_stores import (
    reconcile_source_documents,
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
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


def _insert_source(s, doc_id: str, *, versions: list[tuple], current: str | None,
                   with_od: bool) -> None:
    """直插 legacy 行；with_od=True 时同时建 OfficeDocument + 复制版本（模拟回填后状态）。"""
    s.execute(text(
        "INSERT INTO documents (id, filename, kind, file_type, stored_name, source, size, mtime, "
        "status, is_secret, bid_type, needs_review, current_version_id, office_document_id, created_at, updated_at) "
        "VALUES (:i, 'doc.docx', 'tender', 'docx', 'p.docx', 'upload', 100, 0, 2, 0, '自主', 0, :cur, :oid, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    ), {"i": doc_id, "cur": current, "oid": (doc_id + "-od") if with_od else None})
    for vid, ver, fhash, fpath, src, restored in versions:
        s.execute(text(
            "INSERT INTO document_versions (id, document_id, version, file_path, file_hash, source, restored_from_version_id, created_at) "
            "VALUES (:v, :i, :n, :fp, :fh, :src, :rf, CURRENT_TIMESTAMP)"
        ), {"v": vid, "i": doc_id, "n": ver, "fp": fpath, "fh": fhash, "src": src, "rf": restored})
        if with_od:
            s.execute(text(
                "INSERT INTO office_document_versions (id, office_document_id, version, file_path, file_hash, file_size, source, restored_from_version_id, created_at) "
                "VALUES (:v, :o, :n, :fp, :fh, 100, :src, :rf, CURRENT_TIMESTAMP)"
            ), {"v": vid, "o": doc_id + "-od", "n": ver, "fp": fpath, "fh": fhash, "src": src, "rf": restored})
    if with_od:
        # 正确回填语义：legacy current 为空时，office current = 最新 version id（inferred）
        od_cur = current if current is not None else (versions[-1][0] if versions else None)
        s.execute(text(
            "INSERT INTO office_documents (id, document_type, title, file_type, current_version_id, created_at, updated_at) "
            "VALUES (:o, 'source', 'doc.docx', 'docx', :cur, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {"o": doc_id + "-od", "cur": od_cur})
    s.commit()


def test_reconcile_matched_when_backfilled(db):
    """回填后状态：old/new 完全一致 → matched，mismatch 全 0。"""
    _insert_source(db, "d-ok", versions=[
        ("v1", 1, "h1", "p1.docx", "upload", None),
        ("v2", 2, "h2", "p2.docx", "onlyoffice", None),
    ], current="v2", with_od=True)
    r = reconcile_source_documents(db)
    assert r["summary"]["documents_total"] == 1
    assert r["summary"]["matched_documents"] == 1
    assert r["summary"]["mismatched_documents"] == 0
    assert r["summary"]["hash_mismatch"] == 0 and r["summary"]["path_mismatch"] == 0
    assert r["summary"]["restore_chain_mismatch"] == 0
    assert r["mismatches"] == []


def test_reconcile_current_inferred_when_legacy_empty(db):
    """旧 current 为空：new 应为最新 version id → inferred（不算 mismatch）。"""
    _insert_source(db, "d-inf", versions=[
        ("v1", 1, "h1", "p1.docx", "upload", None),
        ("v2", 2, "h2", "p2.docx", "onlyoffice", None),
    ], current=None, with_od=True)
    r = reconcile_source_documents(db)
    assert r["summary"]["inferred_current"] == 1
    assert r["summary"]["mismatched_documents"] == 0


def test_reconcile_detects_hash_and_restore_chain_mismatch(db):
    """差异场景：hash 不同 / restored_from 不同 / current 不同 / 缺版本 —— 逐项检出，不掩盖。"""
    _insert_source(db, "d-bad", versions=[
        ("v1", 1, "h1", "p1.docx", "upload", None),
        ("v2", 2, "h2-old", "p2.docx", "onlyoffice", "v1"),
    ], current="v2", with_od=False)
    # 手工建一份"不一致"的 office 侧（hash 变、restored_from 丢、缺 v1、current 指 v1）
    s = db
    s.execute(text(
        "INSERT INTO office_documents (id, document_type, title, file_type, current_version_id, created_at, updated_at) "
        "VALUES ('d-bad-od', 'source', 'doc.docx', 'docx', 'v1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    ))
    s.execute(text(
        "INSERT INTO office_document_versions (id, office_document_id, version, file_path, file_hash, file_size, source, restored_from_version_id, created_at) "
        "VALUES ('v2', 'd-bad-od', 2, 'p2-x.docx', 'h2-new', 100, 'onlyoffice', NULL, CURRENT_TIMESTAMP)"
    ))
    s.execute(text("UPDATE documents SET office_document_id='d-bad-od' WHERE id='d-bad'"))
    s.commit()

    r = reconcile_source_documents(s)
    sm = r["summary"]
    assert sm["mismatched_documents"] == 1
    assert sm["hash_mismatch"] == 1
    assert sm["path_mismatch"] == 1
    assert sm["restore_chain_mismatch"] == 1
    assert sm["missing_versions"] == 1
    assert sm["current_mismatch"] == 1
    issues = r["mismatches"][0]["issues"]
    assert any("hash" in x for x in issues)
    assert any("restored_from" in x for x in issues)
    assert any("current" in x for x in issues)


def test_reconcile_restore_chain_consistency(db):
    """restore 链：old/new 的 restored_from_version_id 一致时不得误报。"""
    _insert_source(db, "d-chain", versions=[
        ("v1", 1, "h1", "p1.docx", "upload", None),
        ("v2", 2, "h2", "p2.docx", "onlyoffice", None),
        ("v3", 3, "h1", "p3.docx", "restore", "v1"),  # 恢复自 v1
    ], current="v3", with_od=True)
    r = reconcile_source_documents(db)
    assert r["summary"]["matched_documents"] == 1
    assert r["summary"]["restore_chain_mismatch"] == 0
