"""E-1B：导入分类器（本地规则优先，E-0 契约 §2.2/D4）。

输入：文件名、扩展名、轻量内容文本（docx 直读 word/document.xml 去标签，
pdf 取前 3 页文本——不做 OCR，扫描件天然无内容信号）。
输出：kind(tender|bid|material|unknown) + confidence(0..100) + reasons。

红线：不联网、不 OCR、不解析 XML 实体（去标签用正则，不启用任何解析器实体展开）。
AI 增强属后续阶段（需用户配置密钥并显式开启）。
"""

from __future__ import annotations

import re

TENDER_WORDS = ("招标文件", "招标公告", "招标编号", "评标办法", "投标人须知", "前附表",
                "磋商文件", "询价文件", "谈判文件", "招标项目")
BID_WORDS = ("投标文件", "应答文件", "响应文件", "投标函", "开标一览表",
             "法定代表人身份证明", "授权委托书", "投标报价")
MATERIAL_WORDS = ("营业执照", "资质证书", "许可证", "资质证明", "人员简历", "简历", "学历",
                  "职称", "业绩证明", "类似业绩", "项目案例", "案例", "荣誉证书", "检测报告")

FILENAME_WEIGHT = 90
CONTENT_WEIGHT = 75
EXT_HINT_WEIGHT = 30

MATERIAL_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}


def _strip_docx_text(data: bytes, limit: int = 4000) -> str:
    """docx 正文轻量抽取：直接取 word/document.xml 去标签（不解析 XML，无实体面）。"""
    try:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as z:
            raw = z.read("word/document.xml")
    except Exception:  # noqa: BLE001  非 docx/损坏按无内容
        return ""
    text = raw.decode("utf-8", errors="ignore")
    text = re.sub(r"<[^>]+>", "", text)
    return text[:limit]


def _pdf_text(data: bytes, pages: int = 3, limit: int = 3000) -> str:
    try:
        import fitz

        with fitz.open(stream=data, filetype="pdf") as doc:
            buf = []
            for i in range(min(pages, doc.page_count)):
                buf.append(doc[i].get_text())
                if sum(len(b) for b in buf) >= limit:
                    break
            return "".join(buf)[:limit]
    except Exception:  # noqa: BLE001
        return ""


def _match(text: str, words: tuple[str, ...]) -> list[str]:
    return [w for w in words if w in text]


def classify_import(filename: str, ext: str, content_text: str = "") -> tuple[str, int, list[str]]:
    """本地规则分类。返回 (kind, confidence 0..100, reasons)。"""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    reasons: list[str] = []
    scores = {"tender": 0, "bid": 0, "material": 0}

    fn_hits = {
        "tender": _match(stem, TENDER_WORDS),
        "bid": _match(stem, BID_WORDS),
        "material": _match(stem, MATERIAL_WORDS),
    }
    for kind, hits in fn_hits.items():
        if hits:
            scores[kind] = max(scores[kind], FILENAME_WEIGHT)
            reasons.append(f"文件名命中：{'、'.join(hits[:2])}")

    if content_text:
        ct_hits = {
            "tender": _match(content_text, TENDER_WORDS),
            "bid": _match(content_text, BID_WORDS),
            "material": _match(content_text, MATERIAL_WORDS),
        }
        for kind, hits in ct_hits.items():
            if hits and scores[kind] < CONTENT_WEIGHT:
                scores[kind] = CONTENT_WEIGHT
                reasons.append(f"内容命中：{'、'.join(hits[:2])}")

    ext_l = ext.lower()
    if ext_l in MATERIAL_EXTS and scores["material"] == 0:
        scores["material"] = max(scores["material"], EXT_HINT_WEIGHT)
        reasons.append("图片扩展名（证照/扫描常见）")

    best = max(scores, key=lambda k: scores[k])
    conf = scores[best]
    if conf < 50:
        if ext_l in MATERIAL_EXTS:
            return "material", max(conf, EXT_HINT_WEIGHT), reasons or ["图片扩展名"]
        return "unknown", conf, reasons or ["无识别信号"]
    return best, min(conf, 99), reasons
