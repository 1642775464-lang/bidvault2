"""V3-1：章节级精准插入验收测试（八项专项，项目负责人任务单）。

① Material v1（17 行/4 空白/8 横合并）插入指定章节后结构守恒且落在章节界内（含表格首块素材）；
② 民机真实章节落点（OOXML 顺序断言）；③ 过期版本冲突；④ 锚点漂移拒绝（同名不自动误配）；
⑤ 范围含 sectPr → SECTION_BREAK_IN_RANGE（不静默回退）；⑥ 系统写入自愈（不误报漂移）；
⑦ 占用写回回归；⑧ append_end 显式模式回归。
"""

from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path

import pytest
from docx import Document as Docx
from docx.enum.section import WD_SECTION
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

SAMPLE = Path(__file__).resolve().parents[2] / "回归证据" / "D1R" / "dx-console" / "DxConsole" / "民机_test.docx"
MAT_V1 = Path(__file__).resolve().parents[2] / "回归证据" / "D1R" / "dx-console" / "DxConsole" / "mat_v1.docx"


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
    from app.db import get_session, make_engine
    from app.main import app

    monkeypatch.setattr(cfg, "_SETTINGS", None)
    engine = make_engine(cfg.get_settings().db_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def _make():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = _make
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _db():
    import app.config as cfg
    from app.db import make_engine

    return sessionmaker(bind=make_engine(cfg.get_settings().db_path), expire_on_commit=False)()


def _docx(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    d = Docx()
    for ln in lines:
        if ln.startswith("#1 "):
            d.add_heading(ln[3:], level=1)
        elif ln.startswith("#2 "):
            d.add_heading(ln[3:], level=2)
        else:
            d.add_paragraph(ln)
    d.save(buf)
    return buf.getvalue()


def _setup(client, tmp_path) -> tuple[str, str, str]:
    client.post("/api/workspace", json={"path": str(tmp_path / "ws")})
    pid = client.post("/api/projects", json={"name": "V31 项目"}).json()["id"]
    ws = client.post(f"/api/projects/{pid}/workspace").json()
    return pid, ws["office_document_id"], str(tmp_path / "ws")


def _seed_ws_version(ws_oid: str, data: bytes) -> None:
    from app.models import OfficeDocument
    from app.services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )

    s = _db()
    try:
        od = s.get(OfficeDocument, ws_oid)
        OfficeDocumentService(session=s, adapter=OfficeDocumentTableAdapter(s)).create_version(
            od, data, source="upload")
        s.commit()
    finally:
        s.close()


def _import_material(client, tmp_path, name: str, content: bytes | None = None) -> str:
    """导入一个人员简历类素材（文件名前缀保证本地规则分类=material）并确认归档。"""
    zp = tmp_path / f"素材-{name}.zip"
    data = content if content is not None else _docx([f"{name} 素材内容"])
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(f"人员简历-{name}.docx", data)
    batch = client.post("/api/workspace/imports", json={"paths": [str(zp)]}).json()["batch_id"]
    items = client.get(f"/api/workspace/imports/{batch}").json()["items"]
    r = client.post(f"/api/workspace/imports/{batch}/confirm", json={"decisions": [
        {"item_id": i["id"], "kind": "material", "material_cat": "人员简历"} for i in items]})
    assert r.status_code == 200, r.text
    return client.get("/api/materials").json()["items"][0]["id"]


def _insert(client, oid: str, node_id: str | None, material_id: str, **kw) -> dict:
    payload = {"oid": oid, "node_id": node_id, "material_id": material_id,
               "purpose": "项目负责人", "insert_mode": kw.pop("insert_mode", "chapter")}
    payload.update(kw)
    r = client.post("/api/workspace/references/insert", json=payload)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
    return {"status": r.status_code, **body}


def _body_order(oid: str) -> list[str]:
    """当前版本 body 顶层元素顺序标记（p:段落文本 / tbl:表格首格）。"""
    from app.models import OfficeDocument, OfficeDocumentVersion
    from app.services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )
    from app.services.xml_safety import safe_parse_xml

    s = _db()
    try:
        od = s.get(OfficeDocument, oid)
        v = s.get(OfficeDocumentVersion, od.current_version_id)
        from app.config import get_settings

        data = OfficeDocumentService(
            session=s, adapter=OfficeDocumentTableAdapter(s)).adapter.read_file(v.file_path)
    finally:
        s.close()
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    docx_zip = zipfile.ZipFile(io.BytesIO(data))
    root = safe_parse_xml(docx_zip.read("word/document.xml"))
    body = root.find(W + "body")
    out = []
    for child in body:
        if child.tag == W + "p":
            out.append("p:" + "".join(t.text or "" for t in child.iter(W + "t")).strip())
        elif child.tag == W + "tbl":
            first = next(child.iter(W + "t"), None)
            out.append("tbl:" + (first.text or "" if first is not None else ""))
    return out


def test_1_material_v1_structural_conservation_in_chapter(client, tmp_path):
    """① Material v1（17 行/4 空白/8 横合并）插入第三章后结构守恒且落在章节界内。"""
    assert MAT_V1.is_file(), "Material v1 样本缺失"
    pid, ws_oid, _ = _setup(client, tmp_path)
    _seed_ws_version(ws_oid, _docx(["#1 第三章 评标办法", "评标办法正文。", "#1 第四章 合同条款", "合同正文。"]))
    # 素材内容 = Material v1 原始 17 行表（文件名前缀保证分类=素材）
    material_id = _import_material(client, tmp_path, "格式表", MAT_V1.read_bytes())

    node = next(n for n in client.get(f"/api/office-documents/{ws_oid}/outline").json()["nodes"]
                if n["title"].startswith("第三章"))
    r = _insert(client, ws_oid, node["id"], material_id)
    assert r["status"] == 201, r

    order = _body_order(ws_oid)
    t_i = next(i for i, m in enumerate(order) if m.startswith("p:第三章 评标办法"))
    n_i = next(i for i, m in enumerate(order) if m.startswith("p:第四章 合同条款"))
    tbl_i = next((i for i, m in enumerate(order) if t_i < i < n_i and m.startswith("tbl:")), None)
    assert tbl_i is not None, "素材表应落在 第三章..第四章 之间（OOXML 顺序断言）"

    # Material v1 结构守恒：17 行/4 空白/8 合并
    from app.config import get_settings
    from app.models import OfficeDocument, OfficeDocumentVersion
    from app.services.xml_safety import safe_parse_xml

    s = _db()
    try:
        od = s.get(OfficeDocument, ws_oid)
        v = s.get(OfficeDocumentVersion, od.current_version_id)
        data = (get_settings().files_dir / v.file_path).read_bytes()
    finally:
        s.close()
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = safe_parse_xml(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))
    body = root.find(W + "body")
    mat_tbl = next((t for t in body.iter(W + "tbl") if len(t.findall(W + "tr")) == 17), None)
    assert mat_tbl is not None, "17 行素材表应整体存在"
    blank = sum(1 for tr in mat_tbl.findall(W + "tr")
                if not any((x.text or "").strip() for x in tr.iter(W + "t")))
    gs = sum(1 for tc in mat_tbl.iter(W + "tc") if tc.find(W + "tcPr/" + W + "gridSpan") is not None)
    assert blank == 4 and gs == 8


def test_2_real_sample_chapter_placement(client, tmp_path):
    """② 民机 114 页：选"1.4 投标人资格要求"（Recon 实证范围内无分节符）插入 →
    素材段落落在该章节范围内（段落序号 ∈ (node.para_index, 下一同级/更高级标题 para_index)）。"""
    if not SAMPLE.is_file():
        pytest.skip("民机样本不存在")
    pid, ws_oid, _ = _setup(client, tmp_path)
    _seed_ws_version(ws_oid, _docx(["占位工作稿"]))
    from app.models import OfficeDocument
    from app.services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )

    s = _db()
    try:
        od = s.get(OfficeDocument, ws_oid)
        OfficeDocumentService(session=s, adapter=OfficeDocumentTableAdapter(s)).create_version(
            od, SAMPLE.read_bytes(), source="upload")
        s.commit()
    finally:
        s.close()
    outline = client.get(f"/api/office-documents/{ws_oid}/outline").json()
    nodes = outline["nodes"]
    node = next(n for n in nodes if n["title"].startswith("1.4 投标人资格要求"))
    nxt = next((n for n in nodes[node["order"] + 1:] if n["level"] <= node["level"]), None)
    material_id = _import_material(client, tmp_path, "章节佐证", _docx(["人员简历素材 内容占位"]))
    r = _insert(client, ws_oid, node["id"], material_id)
    assert r["status"] == 201, r
    # 插入后重取章节树（新版本已重建）：边界以下一个同级/更高级标题为准
    outline2 = client.get(f"/api/office-documents/{ws_oid}/outline").json()
    node2 = next(n for n in outline2["nodes"] if n["title"].startswith("1.4 投标人资格要求"))
    nxt2 = next((n for n in outline2["nodes"][node2["order"] + 1:] if n["level"] <= node2["level"]), None)
    order = _body_order(ws_oid)
    p_only = [m for m in order if m.startswith("p:")]
    pos = p_only.index("p:人员简历素材 内容占位")
    assert node2["para_index"] < pos, "素材应在目标章节标题之后"
    if nxt2 is not None:
        assert pos < nxt2["para_index"], "素材不应越过下一同级/更高级标题"


def test_3_stale_version_conflict(client, tmp_path):
    """③ 基线前移（并发写入）→ 版本闸 409，不产生错误落位。"""
    pid, ws_oid, _ = _setup(client, tmp_path)
    _seed_ws_version(ws_oid, _docx(["#1 第三章 评标办法", "正文。"]))
    material_id = _import_material(client, tmp_path, "并发素材")
    node = client.get(f"/api/office-documents/{ws_oid}/outline").json()["nodes"][0]
    from app.models import OfficeDocument
    from app.services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )

    s = _db()
    try:
        od = s.get(OfficeDocument, ws_oid)
        OfficeDocumentService(session=s, adapter=OfficeDocumentTableAdapter(s)).create_version(
            od, _docx(["并发版本"]), source="upload")
        s.commit()
    finally:
        s.close()
    r = _insert(client, ws_oid, node["id"], material_id)
    assert r["status"] == 409, r
    detail = r.get("detail") or r
    assert any(k in str(detail) for k in ("前移", "已更新", "CONFLICT", "DRIFT"))


def test_4_anchor_drift_rejected(client, tmp_path, monkeypatch):
    """④ 锚点漂移：locator 标题与实际元素不一致 → ANCHOR_DRIFT；同名不自动误配。"""
    pid, ws_oid, _ = _setup(client, tmp_path)
    _seed_ws_version(ws_oid, _docx(["#1 第三章 技术要求", "负责人要求。"]))
    material_id = _import_material(client, tmp_path, "漂移素材")

    import app.services.material_reference as mr

    real_outline = mr._outline_for_target

    def _stale_outline(session, target, ws_root):
        """返回被篡改的 locator 源（模拟锚点漂移后的旧树）。"""
        tree = real_outline(session, target, ws_root)
        stale = {"version": 1, "nodes": [{
            "id": "stale0000000000000000000000000", "parent_id": None, "level": 1,
            "title": "第三章 不存在的旧标题", "order": 0, "para_index": 0,
            "anchor_hash": "f" * 32,
        }]}
        _ = tree
        return stale

    monkeypatch.setattr(mr, "_outline_for_target", _stale_outline)
    r = client.post("/api/workspace/references/insert", json={
        "oid": ws_oid, "node_id": "stale0000000000000000000000000",
        "material_id": material_id, "purpose": "项目负责人", "insert_mode": "chapter",
    })
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    code = detail["code"] if isinstance(detail, dict) else detail
    assert code in ("ANCHOR_DRIFT", "REFERENCE_ERROR")


def test_5_section_break_in_range_rejected(client, tmp_path):
    """⑤ 范围内含段级 sectPr → SECTION_BREAK_IN_RANGE（不静默回退 append_end）。"""
    pid, ws_oid, _ = _setup(client, tmp_path)
    d = Docx()
    d.add_heading("第一章 总则", level=1)
    d.add_paragraph("总则正文。")
    d.add_section(WD_SECTION.NEW_PAGE)
    d.add_paragraph("跨节正文。")
    d.add_heading("第二章 评标办法", level=1)
    buf = io.BytesIO()
    d.save(buf)
    _seed_ws_version(ws_oid, buf.getvalue())
    material_id = _import_material(client, tmp_path, "分节素材")
    node = next(n for n in client.get(f"/api/office-documents/{ws_oid}/outline").json()["nodes"]
                if n["title"] == "第一章 总则")
    r = _insert(client, ws_oid, node["id"], material_id)
    assert r["status"] == 409, r
    detail = r.get("detail") or r
    code = detail.get("code") if isinstance(detail, dict) else str(detail)
    assert code == "SECTION_BREAK_IN_RANGE", detail
    # 不静默回退：文档末尾不得出现素材内容
    assert "p:分节素材 内容" not in _body_order(ws_oid)


def test_6_system_write_self_heal(client, tmp_path):
    """⑥ 系统写入自愈：短章节插入后引用为 active 而非 drifted。"""
    pid, ws_oid, _ = _setup(client, tmp_path)
    _seed_ws_version(ws_oid, _docx(["#1 第三章 技术要求", "负责人要求。"]))
    material_id = _import_material(client, tmp_path, "自愈素材")
    node = client.get(f"/api/office-documents/{ws_oid}/outline").json()["nodes"][0]
    r = _insert(client, ws_oid, node["id"], material_id)
    assert r["status"] == 201, r
    refs = client.get(f"/api/workspace/references?target_oid={ws_oid}").json()
    assert refs["items"][0]["status"] == "active", refs["items"]
    # 全量重估后其他引用同样不被误标（本例仅一条）
    assert all(i["status"] == "active" for i in refs["items"])


def test_7_writeback_regression_after_chapter_insert(client, tmp_path, monkeypatch):
    """⑦ 占用写回回归：chapter 插入 + 投影被占用 → 候选落中转，重试成功。"""
    pid, ws_oid, _ = _setup(client, tmp_path)
    _seed_ws_version(ws_oid, _docx(["#1 第三章 技术要求", "负责人要求。"]))
    material_id = _import_material(client, tmp_path, "写回素材")
    node = client.get(f"/api/office-documents/{ws_oid}/outline").json()["nodes"][0]

    import os as _os

    real_replace = _os.replace

    def _locked(src, dst, *a, **k):
        if str(dst).endswith(".docx") and "投标文件" in str(dst):
            raise PermissionError(32, "占用")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(_os, "replace", _locked)
    r = _insert(client, ws_oid, node["id"], material_id)
    assert r["status"] == 201 and r["writeback"] == "pending_writeback"
    monkeypatch.setattr(_os, "replace", real_replace)
    ref_id = r["reference_id"]
    r2 = client.post(f"/api/workspace/references/{ref_id}/writeback")
    assert r2.json()["ok"] is True
    assert (tmp_path / "ws" / "项目" / "V31 项目" / "投标文件" / "V31 项目.docx").is_file()


def test_8_append_end_mode_regression(client, tmp_path):
    """⑧ append_end 回归：显式文档末尾插入落位在最后。"""
    pid, ws_oid, _ = _setup(client, tmp_path)
    _seed_ws_version(ws_oid, _docx(["#1 第三章 技术要求", "正文。"]))
    material_id = _import_material(client, tmp_path, "尾部素材")
    node = client.get(f"/api/office-documents/{ws_oid}/outline").json()["nodes"][0]
    r = _insert(client, ws_oid, node["id"], material_id, insert_mode="append_end")
    assert r["status"] == 201, r
    order = _body_order(ws_oid)
    assert any("尾部素材" in m for m in order[-2:]), order[-3:]
