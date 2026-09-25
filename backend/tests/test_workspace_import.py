"""E-1B：历史资料导入管线验收测试（项目负责人任务单样本）。

覆盖：混合 ZIP 全链路（分类建议→确认→归档→注册建档→招标自动拆解）；
批次内 hash 重复；扫描件无 OCR 时拆解显式降级（不转云）；
zip-slip 拒绝；压缩比炸弹拒绝。
"""

from __future__ import annotations

import io
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


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


from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


def _db_session():
    import app.config as cfg
    from app.db import make_engine

    return sessionmaker(bind=make_engine(cfg.get_settings().db_path), expire_on_commit=False)()


def _docx(content: str) -> bytes:
    from docx import Document as Docx

    buf = io.BytesIO()
    d = Docx()
    d.add_paragraph(content)
    d.save(buf)
    return buf.getvalue()


def _setup_ws(client, tmp_path) -> str:
    r = client.post("/api/workspace", json={"path": str(tmp_path / "ws")})
    assert r.status_code == 201, r.text
    return str(tmp_path / "ws")


def _make_history_zip(path) -> None:
    members = {
        "招标文件.docx": _docx("某医院项目招标文件 评标办法 前附表 投标人须知"),
        "投标文件.docx": _docx("投标文件 投标函 授权委托书 法定代表人身份证明"),
        "营业执照.pdf": "%PDF-1.4 营业执照 扫描图像占位".encode("utf-8"),
        "人员简历.docx": _docx("张三 个人简历 学历 职称"),
        "案例.xlsx": b"PK\x03\x04 fake xlsx bytes",
        "扫描图片.jpg": b"\xff\xd8\xff\xe0 fake jpeg",
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)


def test_history_zip_full_chain(client, tmp_path):
    """验收样本①：历史项目 ZIP 全链路——建议→确认→归档→建档→招标自动拆解。"""
    ws = _setup_ws(client, tmp_path)
    zp = tmp_path / "2025医院项目.zip"
    _make_history_zip(zp)
    r = client.post("/api/workspace/imports", json={"paths": [str(zp)], "label": "2025医院项目"})
    assert r.status_code == 201, r.text
    batch_id = r.json()["batch_id"]

    detail = client.get(f"/api/workspace/imports/{batch_id}").json()
    items = {i["file_name"]: i for i in detail["items"]}
    assert set(items) == {"招标文件.docx", "投标文件.docx", "营业执照.pdf",
                          "人员简历.docx", "案例.xlsx", "扫描图片.jpg"}
    assert items["招标文件.docx"]["kind"] == "tender" and items["招标文件.docx"]["confidence"] >= 50
    assert items["投标文件.docx"]["kind"] == "bid"
    for m in ("营业执照.pdf", "人员简历.docx", "案例.xlsx", "扫描图片.jpg"):
        assert items[m]["kind"] == "material", (m, items[m])

    # 用户确认归档
    decisions = []
    for name, i in items.items():
        decisions.append({
            "item_id": i["id"],
            "kind": i["kind"],
            "project_name": "2025医院项目",
            "material_cat": "人员简历" if "简历" in name else ("项目案例" if "案例" in name else "企业资质"),
            "decompose": False,
        })
    r = client.post(f"/api/workspace/imports/{batch_id}/confirm", json={"decisions": decisions})
    assert r.status_code == 200, r.text
    results = {x["item_id"]: x for x in r.json()["results"]}
    assert all(x["status"] == "archived" for x in results.values())

    # 归档落位
    assert (tmp_path / "ws" / "项目" / "2025医院项目" / "招标文件" / "招标文件.docx").is_file()
    assert (tmp_path / "ws" / "项目" / "2025医院项目" / "投标文件" / "投标文件.docx").is_file()
    assert (tmp_path / "ws" / "素材库" / "企业资质" / "营业执照.pdf").is_file()
    assert (tmp_path / "ws" / "素材库" / "人员简历" / "人员简历.docx").is_file()

    # 注册建档：招标自动拆解（等 worker），投标未建（询问式未拆）
    deadline = time.time() + 60
    tender_status = None
    while time.time() < deadline:
        docs = client.get("/api/documents").json()["items"]
        tender = [d for d in docs if d["filename"] == "招标文件.docx"]
        if tender and tender[0]["status"] == 2:
            tender_status = 2
            break
        time.sleep(1)
    assert tender_status == 2, "招标文件应自动拆解完成"
    docs = client.get("/api/documents").json()["items"]
    assert not [d for d in docs if d["filename"] == "投标文件.docx"], "投标文件询问式：未确认拆解不建档"
    # 素材已入库
    mats = client.get("/api/materials").json()["items"]
    assert {m["title"] for m in mats} >= {"营业执照", "人员简历", "案例"}


def test_duplicate_hash_flagged_not_deleted(client, tmp_path):
    """验收样本②：hash 一致的重复文件——标记 duplicate，留在中转，不自动删除。"""
    ws = _setup_ws(client, tmp_path)
    d1 = tmp_path / "招标文件A.docx"
    d1.write_bytes(_docx("招标文件 评标办法"))
    r = client.post("/api/workspace/imports", json={"paths": [str(d1)]})
    batch_id = r.json()["batch_id"]
    item = client.get(f"/api/workspace/imports/{batch_id}").json()["items"][0]
    r = client.post(f"/api/workspace/imports/{batch_id}/confirm", json={"decisions": [
        {"item_id": item["id"], "kind": "tender", "project_name": "项目A"},
    ]})
    assert r.status_code == 200

    # 同内容再导一次（改文件名）
    d2 = tmp_path / "招标文件B.docx"
    d2.write_bytes(d1.read_bytes())
    r = client.post("/api/workspace/imports", json={"paths": [str(d2)]})
    batch2 = r.json()["batch_id"]
    item2 = client.get(f"/api/workspace/imports/{batch2}").json()["items"][0]
    r = client.post(f"/api/workspace/imports/{batch2}/confirm", json={"decisions": [
        {"item_id": item2["id"], "kind": "tender", "project_name": "项目B"},
    ]})
    results = r.json()["results"]
    assert results[0]["status"] == "duplicate"
    # 原件仍在暂存区（未删除）
    assert (tmp_path / "ws" / item2["staging_rel"]).is_file()


def test_scanned_tender_pdf_degrades_without_ocr(client, tmp_path, monkeypatch):
    """验收样本③：扫描招标 PDF + 无 OCR → 拆解显式 OCR_UNAVAILABLE，不转云。"""
    import sys

    ws = _setup_ws(client, tmp_path)
    # 无文字层的 PDF（仅位图页）
    import fitz

    src = fitz.open()
    p = src.new_page(width=500, height=160)
    p.insert_text((30, 90), "SCANNED TENDER 2468", fontsize=24)
    pix = p.get_pixmap(dpi=150)
    src.close()
    scan = fitz.open()
    sp = scan.new_page(width=pix.width, height=pix.height)
    sp.insert_image(fitz.Rect(0, 0, pix.width, pix.height), stream=pix.tobytes("png"))
    scan_pdf = scan.convert_to_pdf()
    scan.close()

    zp = tmp_path / "扫描招标.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("招标文件-扫描版.pdf", scan_pdf)
    r = client.post("/api/workspace/imports", json={"paths": [str(zp)]})
    batch_id = r.json()["batch_id"]
    item = client.get(f"/api/workspace/imports/{batch_id}").json()["items"][0]
    r = client.post(f"/api/workspace/imports/{batch_id}/confirm", json={"decisions": [
        {"item_id": item["id"], "kind": "tender", "project_name": "扫描项目"},
    ]})
    assert r.status_code == 200

    # 模拟主包环境：paddle 不可导入
    monkeypatch.setitem(sys.modules, "paddleocr", None)
    monkeypatch.setitem(sys.modules, "cv2", None)
    from app.models import Document
    from app.services.ingest.parser import OCREngine

    monkeypatch.setattr(OCREngine, "_instance", None, raising=False)
    monkeypatch.setattr(OCREngine, "_ocr", None, raising=False)
    deadline = time.time() + 60
    err = ""
    while time.time() < deadline:
        s = _db_session()
        try:
            doc = s.query(Document).filter(Document.filename == "招标文件-扫描版.pdf").first()
            if doc is not None and doc.status == 0 and doc.error_msg:
                err = doc.error_msg
                break
        finally:
            s.close()
        time.sleep(1)
    assert err.startswith("OCR_UNAVAILABLE::"), err
    # 明确不转云：错误文案不含任何云端 OCR 引导
    assert "云端" not in err


def test_zip_slip_rejected(client, tmp_path):
    """安全守卫：压缩包内 ../ 穿越条目 → 整体拒绝，不落盘工作区外。"""
    ws = _setup_ws(client, tmp_path)
    zp = tmp_path / "evil.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("../evil.txt", "bad")
        z.writestr("ok.txt", "fine")
    r = client.post("/api/workspace/imports", json={"paths": [str(zp)]})
    assert r.status_code == 400
    assert "非法路径" in r.text
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()


def test_zip_bomb_ratio_rejected(client, tmp_path):
    """安全守卫：高压缩比（>100:1 且 >10MB 解压量）→ 拒绝。"""
    ws = _setup_ws(client, tmp_path)
    zp = tmp_path / "bomb.zip"
    with zipfile.ZipFile(zp, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("zeros.bin", b"\x00" * (64 * 1024 * 1024))  # 64MB 全零 → 压缩比远超 100
    r = client.post("/api/workspace/imports", json={"paths": [str(zp)]})
    assert r.status_code == 400
    assert "压缩比" in r.text
