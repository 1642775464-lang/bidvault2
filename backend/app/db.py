"""SQLite 连接（WAL）+ 建表 + FTS5 虚拟表 + 种子数据。"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import (
    BUILTIN_INDUSTRIES,
    Base,
    Tag,
    _chunks_table_sql,
)

_VEC_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    chunk_id TEXT PRIMARY KEY,
    embedding float[512]
);
"""


def _load_vec_ext(dbapi_conn: Any) -> None:
    try:
        import sqlite_vec

        dbapi_conn.enable_load_extension(True)
        sqlite_vec.load(dbapi_conn)
        dbapi_conn.enable_load_extension(False)
        dbapi_conn.executescript(_VEC_DDL)
    except Exception:
        dbapi_conn.enable_load_extension(False)


def make_engine(db_path: Path | None = None) -> Any:
    settings = get_settings()
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: object) -> None:
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA synchronous=NORMAL")
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        _load_vec_ext(dbapi_conn)

    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        for ddl in _chunks_table_sql():
            conn.execute(text(ddl))
        _migrate(conn)

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        _seed(s)
    return engine


def _seed(session: Session) -> None:
    """种子：行业 / 平台 / 阶段（阶段存常量，不建表）。行业幂等。"""

    for name in BUILTIN_INDUSTRIES:
        exists = session.query(Tag).filter(Tag.name == name, Tag.kind == "industry").one_or_none()
        if not exists:
            session.add(Tag(name=name, kind="industry", builtin=True))
    session.commit()


_Engine: Any = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Any:
    global _Engine, _SessionLocal
    if _Engine is None:
        _Engine = make_engine()
        _SessionLocal = sessionmaker(bind=_Engine, expire_on_commit=False)
    return _Engine


def _migrate(conn: Any) -> None:
    """轻量迁移：create_all 不改已有表，缺列逐个补。"""
    stmts = [
        "ALTER TABLE documents ADD COLUMN ocr_conf REAL",
        "ALTER TABLE documents ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE materials ADD COLUMN source_page INTEGER",
        "ALTER TABLE extract_rows ADD COLUMN cells_json TEXT",
        "ALTER TABLE risk_items ADD COLUMN meta TEXT",
        "ALTER TABLE risk_items ADD COLUMN doc_id TEXT",
        "ALTER TABLE documents ADD COLUMN current_version_id TEXT",
        "ALTER TABLE document_versions ADD COLUMN restored_from_version_id TEXT",
        "ALTER TABLE document_versions ADD COLUMN comment TEXT",
        "ALTER TABLE materials ADD COLUMN office_document_id TEXT",
        "ALTER TABLE documents ADD COLUMN office_document_id TEXT",
        "ALTER TABLE materials ADD COLUMN source_document_id TEXT",
        "ALTER TABLE materials ADD COLUMN source_document_version_id TEXT",
        "ALTER TABLE materials ADD COLUMN source_locator_json TEXT",
    ]
    for ddl in stmts:
        with contextlib.suppress(Exception):
            conn.execute(text(ddl))


def get_session() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
    finally:
        session.close()
