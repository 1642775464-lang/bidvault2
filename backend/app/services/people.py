"""F2.5 人员档案卡：素材（人员团队类）+ 投标拆解（团队/身份/授权）双源聚合。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..models import Document, ExtractItem, ExtractRow, Material

_ROLE_WORDS = ["项目经理", "技术负责人", "商务负责人", "法定代表人", "委托代理人", "技术总监"]


@dataclass
class PersonCard:
    name: str
    role: str
    title: str = ""
    certs: list[str] = field(default_factory=list)
    source: str = ""
    status: str = "ok"  # ok | warn


def _split_person(text: str) -> tuple[str, str] | None:
    """「王志强 · 项目经理」/「王志强-项目经理」→ (姓名, 角色)。"""
    m = re.match(r"^([\u4e00-\u9fa5·]{2,4})\s*[·\-–—]\s*(.+)$", text.strip())
    if m:
        return m.group(1), m.group(2).strip()
    return None


def _certs_of(text: str) -> list[str]:
    return re.findall(r"[一-龥A-Za-z0-9]*?(?:建造师|工程师|注册[A-Za-z安全]{0,4}工程师|PMP|造价工程师)", text)


_ROLE_PRIORITY = {w: i for i, w in enumerate(
    ["项目经理", "技术负责人", "商务负责人", "技术总监", "法定代表人", "委托代理人", "项目成员"],
)}

_NAME_STOP = ("兹证明", "特此证明", "证明", "本人", "本公司", "我方", "单位", "法定代表人", "负责人",
              "授权", "委托", "同志", "先生", "女士", "有效的", "有效")


def _extract_name(val: str) -> str | None:
    """从字段值里抠人名：去括号内容、前缀套话与角色词，取剩余纯汉字段（2-4 字）。"""
    s = re.sub(r"[（（][^））]*[）)]", "，", val)

    for w in _NAME_STOP:
        s = s.replace(w, "，")
    for seg in re.split(r"[，。;；,、\s（）()【】\[\]·:：]+", s):
        seg = seg.strip("为系是的")
        if 2 <= len(seg) <= 4 and re.fullmatch(r"[\u4e00-\u9fa5·]+", seg):
            return seg
    return None


def collect_people(session: Session, project_id: str) -> list[PersonCard]:
    cards: dict[str, PersonCard] = {}

    def upsert(name: str, role: str, title: str, source: str) -> None:
        c = cards.setdefault(name, PersonCard(name=name, role=role or "项目成员"))
        # 同人出现多角色（如既是委托代理人又是项目经理）时保留优先级高的
        if role and _ROLE_PRIORITY.get(role, 99) < _ROLE_PRIORITY.get(c.role, 99):
            c.role = role
        if title and title not in c.certs:
            c.certs.append(title)
        if source and not c.source:
            c.source = source

    # 源 1：素材库「人员团队」类素材（rows 键值 / text 逐行）
    mats = (
        session.query(Material)
        .filter(Material.project_id == project_id, Material.cat == "人员团队")
        .all()
    )
    for m in mats:
        src = m.doc_name or "手动上传"
        rows: list[Any] = json.loads(m.rows_json) if m.rows_json else []
        for r in rows:
            if isinstance(r, dict):
                # V2 表格行：取可见格文本做同人解析
                texts = [str(c.get("text", "")).strip() for c in r.get("cells", []) if not c.get("merged")]
                texts = [t for t in texts if t]
                if not texts:
                    continue
                head, val = texts[0], (" ".join(texts[1:]) if len(texts) > 1 else texts[0])
            else:
                head = r[0] if len(r) > 0 else ""
                val = r[1] if len(r) > 1 else (r[0] if r else "")
            sp = _split_person(head) or _split_person(val)
            if sp:
                upsert(sp[0], sp[1], " ".join(_certs_of(val)) or val, src)
            elif _certs_of(val) and _extract_name(head) and not any(w in head for w in _ROLE_WORDS):
                upsert(head, "", " ".join(_certs_of(val)), src)
        if m.text:
            for ln in m.text.splitlines():
                sp = _split_person(ln)
                if sp:
                    upsert(sp[0], sp[1], " ".join(_certs_of(ln)), src)

    # 源 2：投标文件拆解（团队简历/身份证明/授权委托的字段行）
    docs = (
        session.query(Document)
        .filter(Document.project_id == project_id, Document.kind == "bid")
        .all()
    )
    for d in docs:
        items = session.query(ExtractItem).filter(ExtractItem.doc_id == d.id).all()
        for it in items:
            if not any(w in it.name for w in ("团队", "简历", "身份证明", "授权委托")):
                continue
            extract_rows = (
                session.query(ExtractRow)
                .filter(ExtractRow.item_id == it.id)
                .order_by(ExtractRow.seq)
                .all()
            )
            for er in extract_rows:
                for w in _ROLE_WORDS:
                    if w in er.field:
                        val = er.value or ""
                        name = _extract_name(val)
                        if name:
                            upsert(name, w, val, d.filename)

    # 材料齐全度：团队成员有证书线索=ok；缺证书或仅出现于拆解=warn
    out = sorted(cards.values(), key=lambda c: (c.role != "项目经理", c.name))
    for c in out:
        if not c.certs:
            c.status = "warn"
    return out


def people_payload(session: Session, project_id: str) -> list[dict[str, Any]]:
    return [
        {
            "name": c.name,
            "role": c.role,
            "certs": c.certs,
            "source": c.source,
            "status": c.status,
        }
        for c in collect_people(session, project_id)
    ]
