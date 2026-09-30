"""契约 v1.3：DOCX 内嵌图片作为图片证据（区间内照片级图片挂素材）。

覆盖：行槽映射（段落/空段/表格）/ 阈值（小图标不收）/ hash 去重 / 区间归属 /
素材 images_json 往返 / 区间外图片不收 / 无规则文档不收。

图片用真实 PNG 字节生成（cv2 解码校验尺寸，故不能用占位文本）。
"""

from __future__ import annotations

import io
import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import get_session, make_engine
from app.main import app
from app.services.images import MIN_SIDE, extract_docx_images
from app.services.ingest.parser import parse_docx


def _png(width: int, height: int, seed: int = 0) -> bytes:
    """生成指定尺寸的真实 PNG（内容带 seed 变化用于去重测试）。"""
    import cv2
    import numpy as np

    img = np.full((height, width, 3), 200, dtype=np.uint8)
    # 画点噪声，避免不同 seed 产生完全相同的压缩结果
    rng = np.random.default_rng(seed)
    noise = rng.integers(0, 255, size=(height, width, 3), dtype=np.uint8)
    img = cv2.addWeighted(img, 0.7, noise, 0.3, 0)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _docx_with_images(blocks: list[tuple[str, bytes | None]], table_img: bytes | None = None) -> bytes:
    """blocks: [(文本, 图片或 None)]；尾部可选带图表格。"""
    from docx import Document as Docx
    from docx.shared import Inches

    d = Docx()
    for text, img in blocks:
        if text:
            d.add_paragraph(text)
        if img is not None:
            d.add_picture(io.BytesIO(img), width=Inches(3))
    if table_img is not None:
        t = d.add_table(rows=1, cols=1)
        cell = t.rows[0].cells[0]
        cell.paragraphs[0].add_run().add_picture(io.BytesIO(table_img), width=Inches(3))
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


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


# ---------------------------------------------------------------- 解析与提取


def test_parse_collects_image_refs_with_line_slots():
    big = _png(400, 300, seed=1)
    data = _docx_with_images([
        ("第三章 评标办法", None),
        ("身份证", None),
        (None, big),           # 证书图（独立段）
        ("第五章 合同条款", None),
    ])
    parsed = parse_docx(data)
    assert parsed.doc_image_refs, "应收集到图片引用"
    slot, rid = parsed.doc_image_refs[0]
    lines = parsed.text.split("\n")
    # 图片所在行槽应对应「身份证」之后的紧邻位置（L0=第三章, L1=身份证, 图片槽=2）
    assert 0 <= slot < len(lines) + 1
    assert rid


def test_extract_docx_images_threshold_and_dedup():
    small = _png(80, 80, seed=2)       # 低于 MIN_SIDE → 不收
    big1 = _png(400, 300, seed=3)
    data = _docx_with_images([
        ("身份证", None), (None, small),
        ("学历证书", None), (None, big1),
        ("劳动合同", None), (None, big1),   # 同一图重复引用 → hash 去重
    ])
    parsed = parse_docx(data)
    imgs = extract_docx_images(data, "doc-test-1", parsed.doc_image_refs, parsed.text.split("\n"))
    # 小图被阈值滤掉；big1 两次引用只收一次
    assert len(imgs) == 1, [(i.rel_path, i.size) for i in imgs]
    assert imgs[0].page_no is None and imgs[0].line_no is not None
    assert MIN_SIDE == 200


def test_extract_docx_images_context_is_nearest_heading():
    big = _png(400, 300, seed=4)
    data = _docx_with_images([("身份证", None), (None, big)])
    parsed = parse_docx(data)
    imgs = extract_docx_images(data, "doc-test-2", parsed.doc_image_refs, parsed.text.split("\n"))
    assert imgs and "身份证" in imgs[0].context


def test_table_image_maps_to_table_start_line():
    big = _png(400, 300, seed=5)
    data = _docx_with_images([("表格区", None)], table_img=big)
    parsed = parse_docx(data)
    imgs = extract_docx_images(data, "doc-test-3", parsed.doc_image_refs, parsed.text.split("\n"))
    assert len(imgs) == 1
    assert imgs[0].line_no is not None


# ---------------------------------------------------------------- 端到端


def _upload(client, name: str, data: bytes) -> str:
    r = client.post(
        "/api/documents/upload",
        files=[("files", (name, data))],
        data={"kind": "bid"},
    )
    assert r.status_code == 200, r.text
    return r.json()["created"][0]["id"]


def test_region_images_attached_to_material(client):
    """端到端：规则区间内的照片级图片 → 素材 images_json（区间外的图不收）。"""
    cert = _png(420, 320, seed=11)
    outside = _png(420, 320, seed=12)
    data = _docx_with_images([
        ("候选人员档案", None),
        ("项目经理-张三", None),
        ("身份证", None), (None, cert),
        ("技术服务方案", None), (None, outside),   # 结束锚点之后（区间外）
    ])
    # 规则：start=候选人员档案，end=技术服务方案（锚点须与本 kind 其他规则不冲突）
    created = client.post("/api/extract-rules", json={
        "kind": "bid", "name": "团队简历图片",
        "anchors": [{"text": "候选人员档案", "role": "start"},
                    {"text": "技术服务方案", "role": "end"}],
    })
    assert created.status_code == 200, created.text

    doc_id = _upload(client, "图片证据文档.docx", data)
    for _ in range(60):
        st = client.get(f"/api/documents/{doc_id}/extract").json().get("status")
        if st == 2:
            break
        import time
        time.sleep(1)
    assert st == 2

    s = _session()
    try:
        from app.models import Material

        mat = (
            s.query(Material)
            .filter(Material.doc_name == "图片证据文档.docx")
            .first()
        )
        assert mat is not None
        imgs = json.loads(mat.images_json or "[]")
    finally:
        s.close()
    assert len(imgs) == 1, imgs
    assert imgs[0]["file_name"].startswith("img/")
    assert imgs[0]["line_no"] >= 0
    # 契约 v1.4：图片带归属行下标（渲染时就地插入到该行之后）
    assert "after_row" in imgs[0] and imgs[0]["after_row"] >= 0
    # 区间外图片不得入库
    assert outside != cert


def test_no_rule_means_no_images(client):
    """未配置规则的文档（无区间）→ 不收图片（防淹没素材库）。"""
    img = _png(420, 320, seed=13)
    data = _docx_with_images([("无关章节", None), (None, img)])
    doc_id = _upload(client, "无规则图片文档.docx", data)
    import time

    for _ in range(60):
        if client.get(f"/api/documents/{doc_id}/extract").json().get("status") == 2:
            break
        time.sleep(1)
    s = _session()
    try:
        from app.models import Material

        mats = s.query(Material).filter(Material.doc_name == "无规则图片文档.docx").all()
        assert all(not m.images_json for m in mats), [m.images_json for m in mats]
    finally:
        s.close()


# ---------------------------------------------------------------- v1.4 归属精度


def test_v14_image_attaches_to_preceding_block():
    """契约 v1.4：独立图片段归属「紧邻其前的块」——图片段自身不占槽，不再整体偏移。"""
    cert = _png(400, 300, seed=21)
    data = _docx_with_images([
        ("身份证", None),
        (None, cert),          # 图片紧跟「身份证」
        ("学历证书", None),
    ])
    parsed = parse_docx(data)
    lines = parsed.text.split("\n")
    assert parsed.doc_image_refs, "应收集图片引用"
    slot, _rid = parsed.doc_image_refs[0]
    # 「身份证」是第 1 行（0-based 0），图片应归属该行槽而非下一行
    assert lines[slot] == "身份证", f"图片应归属「身份证」行，实际 slot={slot} -> {lines[slot]!r}"


def _docx_with_hyperlink_title() -> bytes:
    """标题文本包在 w:hyperlink 里（python-docx para.text 读不到）——本次断根因形态。"""
    from docx import Document as Docx
    from docx.oxml.ns import qn
    from docx.shared import Inches

    d = Docx()
    p = d.add_paragraph()
    hl = p._p.makeelement(qn("w:hyperlink"), {})
    r = p._p.makeelement(qn("w:r"), {})
    t = p._p.makeelement(qn("w:t"), {})
    t.text = "身份证"
    r.append(t)
    hl.append(r)
    p._p.append(hl)
    d.add_picture(io.BytesIO(_png(400, 300, seed=51)), width=Inches(3))
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_hyperlink_wrapped_title_enters_line_ledger():
    """契约 v1.5：超链接包裹的标题必须入行号账本，且图片归属该标题行。"""
    parsed = parse_docx(_docx_with_hyperlink_title())
    lines = parsed.text.split("\n")
    assert "身份证" in lines, "超链接包裹的标题被漏记（para.text 只读直接子 run）"
    slot, _rid = parsed.doc_image_refs[0]
    assert lines[slot] == "身份证", f"图片应归属标题行，实际 slot={slot} -> {lines[slot]!r}"


def test_v14_regions_expose_src_lines():
    """契约 v1.4：抽取区间暴露与每行对应的源行号，且与图片归属槽同基准。"""
    cert = _png(400, 300, seed=22)
    data = _docx_with_images([
        ("候选人员档案", None),
        ("身份证", None), (None, cert),
        ("学历证书", None), (None, _png(400, 300, seed=23)),
        ("技术服务方案", None),
    ])
    parsed = parse_docx(data)
    from app.services.ingest.extractor import extract_structured_with_regions

    rules = [{"id": "t", "name": "团队", "summary": "",
              "anchors": [{"text": "候选人员档案", "priority": 1},
                          {"text": "技术服务方案", "priority": 2, "role": "end"}]}]
    reg = extract_structured_with_regions(parsed.text, "bid", parsed.doc_tables, rules=rules)[0]
    assert reg.src_lines, "区间应携带源行号"
    assert len(reg.src_lines) == len(reg.group.rows), "源行号应与行一一对应"
    assert list(reg.src_lines) == sorted(reg.src_lines)  # 文档顺序
    idx = next(i for i, r in enumerate(reg.group.rows)
               if (r[0] if isinstance(r, list) else "") == "身份证")
    assert parsed.text.split("\n")[reg.src_lines[idx]] == "身份证"
    slots = [s for s, _ in parsed.doc_image_refs]
    assert reg.src_lines[idx] in slots, f"图片应归属「身份证」行 {reg.src_lines[idx]}，实际槽 {slots}"


# ---------------------------------------------------------------- v1.5 顺序标题吸附


def _docx_floating_layout(anchor: str, captions: list[str], end: str,
                          t1_rows: list[str], t2_rows: list[str],
                          imgs: list[bytes]) -> bytes:
    """WPS 浮动排版形态：证书图在 body 中先于其标题（中间隔着简历表行）。

    body 顺序：anchor → 表1(项目行) → 图×2 → 表2(1行) → 图×1 → 标题×N → end。
    图片记账槽因此落在表格行上（复现 rId 级比对证实的错位形态）。
    """
    from docx import Document as Docx
    from docx.shared import Inches

    d = Docx()
    d.add_paragraph(anchor)
    for rows in (t1_rows, t2_rows):
        t = d.add_table(rows=len(rows), cols=2)
        for ri, txt in enumerate(rows):
            t.rows[ri].cells[0].text = txt
            t.rows[ri].cells[1].text = f"{txt}规模"
        if rows is t1_rows:
            for b in imgs[:2]:
                d.add_picture(io.BytesIO(b), width=Inches(3))
    d.add_picture(io.BytesIO(imgs[2]), width=Inches(3))
    for c in captions:
        d.add_paragraph(c)
    d.add_paragraph(end)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _row_idx(rows: list, text: str) -> int:
    return next(i for i, r in enumerate(rows)
                if (r[0] if isinstance(r, list) else r) == text)


def _extract_and_read(client, name: str, data: bytes, rule_name: str,
                      start: str, end: str) -> tuple[list, list]:
    """建规则 → 上传 → 等拆解完成 → 返回 (素材行, images_json)。"""
    created = client.post("/api/extract-rules", json={
        "kind": "bid", "name": rule_name,
        "anchors": [{"text": start, "role": "start"},
                    {"text": end, "role": "end"}],
    })
    assert created.status_code == 200, created.text
    doc_id = _upload(client, name, data)
    st = None
    for _ in range(60):
        st = client.get(f"/api/documents/{doc_id}/extract").json().get("status")
        if st == 2:
            break
        import time
        time.sleep(1)
    assert st == 2
    s = _session()
    try:
        from app.models import Material

        mat = (s.query(Material)
               .filter(Material.doc_name == name,
                       Material.title == rule_name).first())
        assert mat is not None, "规则区间素材应生成"
        rows = json.loads(mat.rows_json or "[]")
        imgs = json.loads(mat.images_json or "[]")
    finally:
        s.close()
    return rows, imgs


def test_v15_caption_snap_from_table_rows(client):
    """契约 v1.5：图片槽落在表格行 → 向后吸附到 ≤8 行内首个未占用标题行。"""
    data = _docx_floating_layout(
        "候选人员档案", ["身份证", "学历证书"], "技术服务依据",
        ["项目A", "项目B", "项目C"], ["项目D"],
        [_png(420, 320, seed=31), _png(420, 320, seed=32), _png(420, 320, seed=33)],
    )
    rows, imgs = _extract_and_read(
        client, "吸附回归文档.docx", data, "吸附回归素材",
        "候选人员档案", "技术服务依据")
    assert len(imgs) == 3, imgs
    i_id, i_xl = _row_idx(rows, "身份证"), _row_idx(rows, "学历证书")
    by_line: dict[int, list[int]] = {}
    for im in imgs:
        by_line.setdefault(im["line_no"], []).append(im["after_row"])
    pairs = [v for v in by_line.values() if len(v) == 2]
    singles = [v for v in by_line.values() if len(v) == 1]
    assert len(pairs) == 1 and len(singles) == 1, by_line
    # 批1（2 张，槽在表1 末行）→ 吸附「身份证」；批2 → 身份证已被占，顺延「学历证书」
    assert pairs[0] == [i_id, i_id], f"批1 应吸附「身份证」行 {i_id}，实际 {pairs[0]}"
    assert singles[0] == [i_xl], f"批2 应吸附「学历证书」行 {i_xl}，实际 {singles[0]}"


def test_v15_inline_form_keeps_v14_attribution(client):
    """v1.4 正常形态（图紧跟标题）不受吸附扰动：after_row 保持标题行。"""
    data = _docx_with_images([
        ("候选人员档案", None),
        ("身份证", None), (None, _png(420, 320, seed=41)),
        ("学历证书", None), (None, _png(420, 320, seed=42)),
        ("技术服务方案", None),
    ])
    rows, imgs = _extract_and_read(
        client, "就地回归文档.docx", data, "就地回归素材",
        "候选人员档案", "技术服务方案")
    i_id, i_xl = _row_idx(rows, "身份证"), _row_idx(rows, "学历证书")
    got = sorted(im["after_row"] for im in imgs)
    assert len(got) == 2
    assert got == [i_id, i_xl], f"应就地归属标题行 {(i_id, i_xl)}，实际 {got}"
