"""D-1R3：OCR 扩展缺失时的显式降级——禁止静默假成功/禁止自动云端 OCR。

红线（项目负责人裁决）：
- 主包不含 paddle/cv2，未安装扩展时主程序必须正常启动；
- 扫描 PDF / 图片文字路径必须显式落 OCR_UNAVAILABLE 并给出安装提示；
- 禁止返回空文本却显示解析成功。
"""

from __future__ import annotations

import io
import os
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.services.ingest import pipeline
from app.services.ingest.parser import (
    OCR_UNAVAILABLE,
    OCR_UNAVAILABLE_MSG,
    OCREngine,
    OcrUnavailableError,
    parse_any,
)


@pytest.fixture()
def no_paddle(monkeypatch):
    """模拟主包环境：paddleocr / cv2 完全不可导入（sys.modules 置 None 即 ImportError）。"""
    monkeypatch.setitem(sys.modules, "paddleocr", None)
    monkeypatch.setitem(sys.modules, "cv2", None)
    # 复位懒加载单例，确保下次 ocr 走真实 _load 路径
    monkeypatch.setattr(OCREngine, "_instance", None, raising=False)
    monkeypatch.setattr(OCREngine, "_ocr", None, raising=False)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    os.environ["ACCESS_PASSWORD"] = ""
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    os.environ["SCAN_INTERVAL_SECONDS"] = "3600"
    import app.config as cfg
    import app.db as appdb
    from app.db import get_session, make_engine
    from app.main import app

    monkeypatch.setattr(cfg, "_SETTINGS", None)
    monkeypatch.setattr(appdb, "_Engine", None)
    monkeypatch.setattr(appdb, "_SessionLocal", None)
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


def _db_session():
    import app.config as cfg
    from app.db import make_engine

    return sessionmaker(bind=make_engine(cfg.get_settings().db_path), expire_on_commit=False)()


def test_main_imports_survive_without_paddle(no_paddle):
    """主包无 paddle 时，parser/pipeline/app.main 模块导入与文本型解析不受影响。"""
    import app.main  # noqa: F401  启动链不因缺 OCR 失败
    from app.services.ingest import pipeline as pl  # noqa: F401

    doc = parse_any("纯文本招标公告 内容如下".encode("utf-8"), "txt")
    assert "招标公告" in doc.text


def test_scanned_pdf_raises_ocr_unavailable(no_paddle):
    """无文字层的 PDF（扫描件）必须显式 OcrUnavailableError，不得返回空文本假成功。"""
    # 最小 PDF：一页且无文本算子（无 BT/ET 文本层）
    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n" \
          b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n" \
          b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>endobj\n" \
          b"trailer<</Root 1 0 R>>\n%%EOF\n"
    with pytest.raises(OcrUnavailableError) as ei:
        parse_any(pdf, "pdf")
    assert OCR_UNAVAILABLE_MSG in str(ei.value)


def test_image_ocr_raises_ocr_unavailable(no_paddle):
    with pytest.raises(OcrUnavailableError):
        parse_any(b"\x89PNG\r\n\x1a\nfake", "scan")


def test_pipeline_marks_ocr_unavailable(client, monkeypatch):
    """管线命中 OCR 缺失：status=0 + error_msg 带 OCR_UNAVAILABLE:: 前缀，不写假成功文本。"""
    from app.models import Document

    s = _db_session()
    doc = Document(filename="扫描件.pdf", kind="tender", source="upload",
                   stored_name="ocrunavail01.pdf", file_type="pdf", size=100)
    s.add(doc)
    s.commit()
    doc_id = doc.id
    s.close()
    import app.config as cfg

    # 合法的无文字层 PDF（扫描件形态），否则 pdf_has_text 阶段就报流错误
    (cfg.get_settings().files_dir / "ocrunavail01.pdf").write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )

    def _raise(data):  # noqa: ANN001
        raise OcrUnavailableError(OCR_UNAVAILABLE_MSG)

    monkeypatch.setattr(OCREngine, "get", classmethod(lambda cls: SimpleNamespace(ocr_pdf=_raise)))
    s = _db_session()
    fresh = s.get(Document, doc_id)
    res = pipeline.process_document(s, fresh)
    assert res.error == OCR_UNAVAILABLE
    assert fresh.status == 0
    assert fresh.error_msg is not None and fresh.error_msg.startswith("OCR_UNAVAILABLE::")
    assert OCR_UNAVAILABLE_MSG in fresh.error_msg
    assert fresh.content_text is None  # 未写入任何假成功文本
    s.close()


def test_material_upload_reports_ocr_unavailable(client, monkeypatch):
    """素材图片上传：OCR 缺失时素材仍入库但响应带 ocr_unavailable + 提示文案。"""

    def _raise(data):  # noqa: ANN001
        raise OcrUnavailableError(OCR_UNAVAILABLE_MSG)

    monkeypatch.setattr(OCREngine, "get", classmethod(lambda cls: SimpleNamespace(ocr_image=_raise)))
    r = client.post(
        "/api/materials/upload",
        files={"file": ("现场照片.png", io.BytesIO(b"\x89PNG\r\n\x1a\nfake").getvalue(), "image/png")},
        data={"cat": ""},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["ocr_unavailable"] is True
    assert body["ocr_message"] == OCR_UNAVAILABLE_MSG


def test_queue_skips_ocr_blocked_docs(client):
    """D-1R3 队头阻塞修复：OCR_UNAVAILABLE 文档不参与自动重试，其后文档照常拾取。"""
    from app.models import Document
    from app.services.ingest.pipeline import load_pending, requeue_failed

    s = _db_session()
    blocked = Document(filename="阻塞扫描件.pdf", kind="tender", source="upload",
                       file_type="pdf", status=0, error_msg="OCR_UNAVAILABLE::请先安装扩展")
    fresh = Document(filename="队列后续.docx", kind="tender", source="upload",
                     file_type="docx", status=0)
    s.add_all([blocked, fresh])
    s.commit()

    picked = load_pending(s, limit=5)
    assert [d.id for d in picked] == [fresh.id]

    # 显式重排后阻塞文档重新可拾取
    assert requeue_failed(s, blocked.id) is True
    picked2 = load_pending(s, limit=5)
    assert blocked.id in [d.id for d in picked2]
    s.close()
