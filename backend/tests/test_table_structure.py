"""Word 表格合并结构回归（TableModel V2）。

覆盖：横向合并 / 纵向合并 / 横纵混合 / 空 cell 保留 / 不同列宽 /
首行 colspan=8 + 次行 8 格 / 合并区域后接普通行 / 整行小计合并；
invariant：cell.id 唯一、merged 必有 mergeParentId；
round-trip：编辑保存只允许 text 变化，结构零漂移。
"""

from __future__ import annotations

import copy
import io
import os
import time

import pytest
from docx import Document as Docx
from docx.shared import Cm
from fastapi.testclient import TestClient

from app.db import get_session, make_engine
from app.main import app
from app.services.ingest.parser import parse_docx, validate_table_model


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


def _vis(row: dict) -> list[tuple[str, int, int]]:
    """行内可见格（非 merged）：(text, colSpan, rowSpan)。"""
    return [(c["text"], c["colSpan"], c["rowSpan"]) for c in row["cells"] if not c.get("merged")]


def _assert_unique_ids(model: dict) -> None:
    ids = [c["id"] for row in model["rows"] for c in row["cells"] if not c.get("merged")]
    assert len(ids) == len(set(ids)), f"实体 cell id 重复：{sorted(ids)}"


def _assert_merge_parent_exists(model: dict) -> None:
    all_ids = {c["id"] for row in model["rows"] for c in row["cells"]}
    for row in model["rows"]:
        for c in row["cells"]:
            if c.get("merged"):
                assert c.get("mergeParentId") in all_ids, f"mergeParentId 不存在：{c}"


def _assert_model(model: dict) -> None:
    _assert_unique_ids(model)
    _assert_merge_parent_exists(model)
    assert validate_table_model(model) == [], validate_table_model(model)


def _docx_from(build) -> bytes:
    d = Docx()
    d.add_paragraph("投标人须知前附表")
    build(d)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _model(data: bytes) -> dict:
    parsed = parse_docx(data)
    assert parsed.doc_tables, "表格未解析出"
    return parsed.doc_tables[0].model


def test_horizontal_merge_single_cell():
    """场景1/6：首行横向合并 8 列 = 1 个 colSpan=8 的 cell；次行 8 个普通格。"""

    def build(d: Docx) -> None:
        t = d.add_table(rows=2, cols=8)
        t.cell(0, 0).merge(t.cell(0, 7)).text = "产品明细报价"
        for i, v in enumerate(["1", "专用工具", "", "", "", "", "", ""]):
            t.cell(1, i).text = v

    m = _model(_docx_from(build))
    assert m["columnCount"] == 8
    assert _vis(m["rows"][0]) == [("产品明细报价", 8, 1)], m["rows"][0]
    assert len(m["rows"][1]["cells"]) == 8  # 空 cell 全保留
    _assert_model(m)


def test_vertical_merge_rowspan_continuation():
    """场景2：纵向合并 → 起点 rowSpan=2，continuation 标 merged 且不复制文本。"""

    def build(d: Docx) -> None:
        t = d.add_table(rows=3, cols=2)
        t.cell(0, 0).merge(t.cell(1, 0)).text = "小计"
        t.cell(0, 1).text = "100 万"
        t.cell(1, 1).text = "200 万"
        t.cell(2, 0).text = "合计"
        t.cell(2, 1).text = "300 万"

    m = _model(_docx_from(build))
    assert _vis(m["rows"][0])[0] == ("小计", 1, 2)
    cont = m["rows"][1]["cells"][0]
    assert cont.get("merged") is True
    assert cont.get("mergeParentId") == m["rows"][0]["cells"][0]["id"]
    assert cont["text"] == ""  # 不复制原格内容
    _assert_model(m)


def test_mixed_horizontal_vertical_merge():
    """场景3：横纵混合——两行，各自行内横向合并，第一列纵向合并。"""

    def build(d: Docx) -> None:
        t = d.add_table(rows=3, cols=4)
        t.cell(0, 0).merge(t.cell(2, 0)).text = "分区"  # 纵向 3 行
        t.cell(0, 1).merge(t.cell(0, 3)).text = "第一阶段"  # 横向 3 列
        t.cell(1, 1).text = "甲"
        t.cell(1, 2).text = "乙"
        t.cell(1, 3).text = "丙"
        t.cell(2, 1).merge(t.cell(2, 3)).text = "收尾"  # 横向 3 列

    m = _model(_docx_from(build))
    assert _vis(m["rows"][0]) == [("分区", 1, 3), ("第一阶段", 3, 1)]
    assert _vis(m["rows"][1]) == [("甲", 1, 1), ("乙", 1, 1), ("丙", 1, 1)]
    assert _vis(m["rows"][2]) == [("收尾", 3, 1)]
    _assert_model(m)


def test_empty_cells_and_rows_kept():
    """场景4/边界：行内空 cell 保留；表内全空行也保留（emptyRow !== uselessRow）。"""

    def build(d: Docx) -> None:
        t = d.add_table(rows=3, cols=3)
        t.cell(0, 0).text = "表头"
        t.cell(2, 1).text = "末行有值"

    m = _model(_docx_from(build))
    assert len(m["rows"]) == 3  # 中间全空行保留
    assert len(m["rows"][2]["cells"]) == 3  # 行内空格保留
    assert m["rows"][1]["cells"][1]["text"] == ""
    _assert_model(m)


def test_column_widths_preserved():
    """场景5：不同列宽——tcW(dxa) 读取为 widthTwips，tblGrid 建立列数。"""

    def build(d: Docx) -> None:
        t = d.add_table(rows=1, cols=3)
        t.cell(0, 0).width = Cm(6)
        t.cell(0, 1).width = Cm(3)
        t.cell(0, 2).width = Cm(1)
        t.cell(0, 0).text = "宽"
        t.cell(0, 1).text = "中"
        t.cell(0, 2).text = "窄"

    m = _model(_docx_from(build))
    assert m["columnCount"] == 3
    widths = [c.get("widthTwips") for c in m["rows"][0]["cells"]]
    assert widths[0] and widths[1] and widths[2], widths
    assert widths[0] > widths[1] > widths[2], widths  # 6cm > 3cm > 1cm
    _assert_model(m)


def test_normal_rows_after_merged_region():
    """场景7：合并区域之后继续出现普通行，网格不错位。"""

    def build(d: Docx) -> None:
        t = d.add_table(rows=4, cols=3)
        t.cell(0, 0).merge(t.cell(1, 2)).text = "大合并区"  # 横纵混合 2x3
        t.cell(2, 0).text = "A"
        t.cell(2, 1).text = "B"
        t.cell(2, 2).text = "C"
        t.cell(3, 0).text = "D"
        t.cell(3, 1).text = "E"
        t.cell(3, 2).text = "F"

    m = _model(_docx_from(build))
    assert _vis(m["rows"][0]) == [("大合并区", 3, 2)]
    assert _vis(m["rows"][2]) == [("A", 1, 1), ("B", 1, 1), ("C", 1, 1)]
    assert _vis(m["rows"][3]) == [("D", 1, 1), ("E", 1, 1), ("F", 1, 1)]
    _assert_model(m)


def test_full_row_subtotal_merge():
    """场景8：整行小计合并（colspan 全表宽）+ 后接普通行。"""

    def build(d: Docx) -> None:
        t = d.add_table(rows=3, cols=4)
        t.cell(1, 0).merge(t.cell(1, 3)).text = "其他报价"
        t.cell(2, 0).merge(t.cell(2, 3)).text = "小计"
        t.cell(0, 0).merge(t.cell(0, 3))
        t.cell(0, 0).text = "汇总"
        t.cell(0, 0).paragraphs[0].add_run("")

    m = _model(_docx_from(build))
    # 场景6：首行只有一个 colspan=4 cell
    assert _vis(m["rows"][0]) == [("汇总", 4, 1)]
    assert _vis(m["rows"][1]) == [("其他报价", 4, 1)]
    assert _vis(m["rows"][2]) == [("小计", 4, 1)]
    _assert_model(m)


ANCHOR_LINE = "投标人须知前附表"


def _merged_docx() -> bytes:
    d = Docx()
    d.add_paragraph(ANCHOR_LINE)
    t = d.add_table(rows=2, cols=8)
    t.cell(0, 0).merge(t.cell(0, 7)).text = "产品明细报价"
    for i, v in enumerate(["1", "专用工具", "", "", "", "", "", ""]):
        t.cell(1, i).text = v
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _upload(client, data: bytes) -> str:
    pid = client.post("/api/projects", json={"name": "表格合并项目"}).json()["id"]
    r = client.post(
        "/api/documents/upload",
        files=[("files", ("表格合并.docx", data, "application/octet-stream"))],
        data={"kind": "tender", "project_id": pid, "is_secret": "false"},
    )
    assert r.status_code == 200
    doc_id = r.json()["created"][0]["id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        d = next(x for x in docs if x["id"] == doc_id)
        if d["status"] == 2:
            return doc_id
        time.sleep(0.2)
    raise AssertionError("拆解超时")


def _table_rows(client, doc_id: str) -> list[dict]:
    ex = client.get(f"/api/documents/{doc_id}/extract").json()
    return [r for g in ex["groups"] for it in g["items"] for r in it["rows"] if isinstance(r, dict)]


def test_e2e_roundtrip_structure_zero_drift(client):
    """端到端 round-trip：GET → 编辑一格文本 → PUT → GET，除 text 外全部不变。"""
    doc_id = _upload(client, _merged_docx())
    rows1 = _table_rows(client, doc_id)
    assert len(rows1) == 2, rows1
    r0, r1 = rows1
    assert r0["cells"][0]["colSpan"] == 8 and r0["cells"][0]["text"] == "产品明细报价"
    assert len(r1["cells"]) == 8 and r1["cells"][1]["text"] == "专用工具"
    assert r1["cells"][2]["text"] == ""  # 空 cell 保留

    # 编辑：改第 2 行第 1 格文本
    ex = client.get(f"/api/documents/{doc_id}/extract").json()
    item = next(it for g in ex["groups"] for it in g["items"] if any(isinstance(r, dict) for r in it["rows"]))
    edited = copy.deepcopy(item["rows"])
    for r in edited:
        if isinstance(r, dict):
            for c in r["cells"]:
                if c["text"] == "专用工具":
                    c["text"] = "专用工装"
    put = client.put(f"/api/documents/{doc_id}/extract/{item['id']}", json={"rows": edited})
    assert put.status_code == 200

    rows2 = _table_rows(client, doc_id)
    assert len(rows2) == 2
    for a, b in zip(rows1, rows2, strict=False):
        assert a["columnCount"] == b["columnCount"]
        assert a["columnWidthsTwips"] == b["columnWidthsTwips"]
        assert a["tid"] == b["tid"]
        for ca, cb in zip(a["cells"], b["cells"], strict=False):
            for k in ("id", "logicalCol", "colSpan", "rowSpan"):
                assert ca[k] == cb[k], f"{k} 漂移：{ca} -> {cb}"
            assert ca.get("merged") == cb.get("merged")
            assert ca.get("mergeParentId") == cb.get("mergeParentId")
    texts1 = [c["text"] for r in rows1 for c in r["cells"]]
    texts2 = [c["text"] for r in rows2 for c in r["cells"]]
    assert texts1 != texts2 and "专用工装" in texts2, "编辑未生效"
