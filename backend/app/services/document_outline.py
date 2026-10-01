"""E-2A：DocumentOutline 统一章节树服务（E-0 契约 §4）。

唯一锚点源，三处消费：阅读器导航 / 素材引用定位 / 废标风险依据引用。

- 构建：python-docx 遍历段落，标题识别 = 样式名（Heading N/标题 N）或 outlineLvl；
- 节点：{id, parent_id, level, title, order, para_index, anchor_hash}；
  anchor_hash = sha256(标题 + 后续至下一标题前正文前 120 字)，支撑漂移语义；
- 存储：按 version_id 不可变持久化（懒构建：首次查询时构建）；
- 漂移：drift_report(old, new) → 完好 / 漂移（标题在、锚点变）/ 丢失。

安全：仅解析工作区/内部受控 DOCX；与既有链一致不做实体展开（python-docx 侧）。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

_STYLE_RE = re.compile(r"^(?:heading|标题)\s*([1-9])", re.IGNORECASE)
TAIL_CHARS = 120
MAX_LEVEL = 6


def _heading_level(paragraph: Any) -> int:
    """标题级别：样式名优先，outlineLvl 兜底；非标题返回 0。"""
    try:
        name = paragraph.style.name if paragraph.style is not None else ""
    except Exception:  # noqa: BLE001  样式缺失按普通段
        name = ""
    m = _STYLE_RE.match((name or "").strip())
    if m:
        return min(int(m.group(1)), MAX_LEVEL)
    try:
        ppr = paragraph._p.pPr
        if ppr is not None and ppr.outlineLvl is not None:
            return min(int(ppr.outlineLvl.val) + 1, MAX_LEVEL)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _anchor(title: str, tail: str) -> str:
    return hashlib.sha256((title + "\x00" + tail[:TAIL_CHARS]).encode("utf-8")).hexdigest()[:32]


def build_outline(docx_bytes: bytes) -> dict[str, Any]:
    """从 DOCX 字节构建章节树。返回 {version, nodes:[...]}（纯函数，不落库）。"""
    from docx import Document as DocxDocument
    from docx.text.paragraph import Paragraph

    doc = DocxDocument(io.BytesIO(docx_bytes))
    paras = list(doc.paragraphs)

    # 预收集段落顺序文本，用于锚点 tail
    texts = [p.text for p in paras]

    nodes: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []  # 父节点栈
    for idx, p in enumerate(paras):
        level = _heading_level(p)
        title = (p.text or "").strip()
        if level == 0 or not title:
            continue
        # 锚点 tail：本标题之后、下一标题之前的正文
        tail_parts: list[str] = []
        for j in range(idx + 1, len(paras)):
            if _heading_level(paras[j]) > 0:
                break
            tail_parts.append(texts[j])
            if sum(len(t) for t in tail_parts) >= TAIL_CHARS:
                break
        # 同级弹栈必须先于取父：否则 L1 会挂到前一个 L1 下，整棵树串成单链
        # （实机：开标一览表/分项报价表等全部 L1 被链到投标函下，根节点只剩 1 个）
        while stack and stack[-1]["level"] >= level:
            stack.pop()
        node = {
            "id": uuid.uuid4().hex,
            "parent_id": stack[-1]["id"] if stack else None,
            "level": level,
            "title": title[:300],
            "order": len(nodes),
            "para_index": idx,
            "anchor_hash": _anchor(title, "".join(tail_parts)),
        }
        nodes.append(node)
        stack.append(node)

    return {"version": 1, "nodes": nodes}


def get_or_build(session: Session, oid: str, version_id: str, docx_bytes: bytes) -> dict[str, Any]:
    """按版本懒构建并持久化（幂等）；已有则直接返回存储树。"""
    from ..models import DocumentOutline

    row = session.query(DocumentOutline).filter(DocumentOutline.version_id == version_id).first()
    if row is not None:
        return {"version": 1, "nodes": json.loads(row.nodes_json)["nodes"]}
    tree = build_outline(docx_bytes)
    session.add(DocumentOutline(
        version_id=version_id, oid=oid, node_count=len(tree["nodes"]),
        nodes_json=json.dumps(tree, ensure_ascii=False)))
    session.commit()
    return tree


def drift_report(old_nodes: list[dict], new_nodes: list[dict]) -> dict[str, Any]:
    """漂移比对（供 W-3 引用闭环）：逐旧节点给出 完好/漂移/丢失。"""
    new_by_title: dict[str, list[dict]] = {}
    for n in new_nodes:
        new_by_title.setdefault(n["title"], []).append(n)
    report = []
    counts = {"intact": 0, "drifted": 0, "lost": 0}
    for n in old_nodes:
        cands = new_by_title.get(n["title"], [])
        if any(c["anchor_hash"] == n["anchor_hash"] for c in cands):
            status = "intact"
        elif cands:
            status = "drifted"
        else:
            status = "lost"
        counts[status] += 1
        report.append({"title": n["title"], "level": n["level"],
                       "old_anchor": n["anchor_hash"], "status": status})
    return {"items": report, "counts": counts}
