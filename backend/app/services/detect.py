"""M4 废标检测规则引擎（本地规则驱动，不调云端）。

以招标文件拆解结果为基准，对投标文件逐规则核对；产出三极（致命/风险/提示）
四型（未响应/偏离/缺项/缺证）风险项，全部带依据条款与投标原文引用定位。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Document, ExtractItem, ExtractRow, RiskItem
from .audit import audit

# 内置检测规则清单（F3.4 设置页展示用）
BUILTIN_RULES: list[dict[str, str]] = [
    {"id": "legal_rep", "name": "法定代表人身份证明", "sev": "致命", "type": "缺项",
     "desc": "投标文件须包含法定代表人身份证明拆解项"},
    {"id": "authorization", "name": "授权委托书", "sev": "致命", "type": "缺项",
     "desc": "投标文件须包含授权委托书及委托代理人信息"},
    {"id": "bond", "name": "投标保证金响应", "sev": "致命", "type": "未响应",
     "desc": "招标要求保证金时，投标文件须有保证金缴纳/承诺表述"},
    {"id": "validity", "name": "投标有效期比对", "sev": "致命", "type": "偏离",
     "desc": "投标有效期不得短于招标要求（如 90 日历天）"},
    {"id": "qualification", "name": "资质等级响应", "sev": "风险", "type": "缺项",
     "desc": "招标要求资质等级时，核对投标基本情况表资质等级"},
    {"id": "performance", "name": "类似业绩响应", "sev": "风险", "type": "缺证",
     "desc": "招标要求类似业绩时，投标须列示业绩表"},
    {"id": "copies", "name": "正副本份数核对", "sev": "提示", "type": "缺证",
     "desc": "提醒核对正副本份数与要求一致"},
]


@dataclass
class Finding:
    sev: str
    type: str
    title: str
    meta: str
    req: str = ""
    quote: str = ""
    advice: str = ""
    fix: str = ""
    page_no: int | None = None
    doc_id: str | None = None


@dataclass
class DetectResult:
    items: list[Finding] = field(default_factory=list)
    score_rows: list[list[str]] = field(default_factory=list)
    detected_at: str = ""


def _tender_items(session: Session, doc_id: str) -> dict[str, list[list[str]]]:
    """招标拆解项：{结构项名: [(字段,值)]}。"""
    items = session.query(ExtractItem).filter(ExtractItem.doc_id == doc_id).all()
    out: dict[str, list[list[str]]] = {}
    for it in items:
        rows = session.query(ExtractRow).filter(ExtractRow.item_id == it.id).order_by(ExtractRow.seq).all()
        out[it.name] = [[r.field, r.value or ""] for r in rows]
    return out


def _tender_value(items: dict[str, list[list[str]]], item_kw: str, field_kw: str) -> str:
    """从拆解项里取值：字段关键词在行内任一列匹配，返回该行最后一列内容。"""
    for name, rows in items.items():
        if item_kw in name:
            for r in rows:
                cells = [c for c in r if c]
                if any(field_kw in c for c in cells):
                    return cells[-1] if len(cells) > 1 else cells[0]
    return ""


def _find_page(session: Session, doc_id: str, keyword: str) -> int | None:
    """关键词 → 首个命中切片的页码。"""
    from ..models import Chunk

    row = (
        session.query(Chunk)
        .filter(Chunk.document_id == doc_id, Chunk.content.like(f"%{keyword}%"))
        .order_by(Chunk.chunk_index)
        .first()
    )
    return row.page_no if row and row.page_no else None


def _sentence_with(text: str, keyword: str, *, width: int = 60) -> str:
    """含关键词的一句话（原文引用）。"""
    idx = text.find(keyword)
    if idx < 0:
        return ""
    start = max(0, text.rfind("\n", 0, idx) + 1)
    end = text.find("\n", idx)
    if end < 0:
        end = min(len(text), idx + width)
    s = text[start:end].strip()
    return s[: width * 2]


def _days(text: str) -> int | None:
    m = re.search(r"(\d+)\s*日历天", text)
    if not m:
        m = re.search(r"(\d+)\s*天", text)
    return int(m.group(1)) if m else None


def _grade(text: str) -> str | None:
    for g in ("特级", "一级", "二级", "三级", "甲级", "乙级"):
        if g in text:
            return g
    return None


def detect_project(session: Session, project_id: str) -> DetectResult:
    """执行检测：招标基准 × 投标响应。缺件抛 ValueError。"""
    tender = (
        session.query(Document)
        .filter(Document.project_id == project_id, Document.kind == "tender", Document.status == 2)
        .first()
    )
    bids = (
        session.query(Document)
        .filter(Document.project_id == project_id, Document.kind == "bid", Document.status == 2)
        .all()
    )
    if tender is None:
        msg = "缺招标文件或未拆解"
        raise ValueError(msg)
    if not bids:
        msg = "缺投标文件或未拆解"
        raise ValueError(msg)

    t_items = _tender_items(session, tender.id)
    req_validity = _tender_value(t_items, "须知前附表", "投标有效期")
    req_bond = _tender_value(t_items, "须知前附表", "投标保证金")
    req_copies = _tender_value(t_items, "须知前附表", "正副本份数")
    req_qual = _tender_value(t_items, "须知前附表", "资质")
    if not req_qual:
        # 资质要求常不在前附表抽取白名单内，直接从招标正文抓
        m = re.search(r"资质[^\n：:]{0,6}[:：]\s*([^\n]{1,80})", tender.content_text or "")
        req_qual = m.group(1).strip() if m else ""

    findings: list[Finding] = []
    # 全文缺项/响应类规则只对主投标件生效（正文最长的那份），
    # 营业执照/资质证书等扫描附件不承担格式完整性，避免刷假缺失。
    main_bid = max(bids, key=lambda b: len(b.content_text or ""))
    for bid in [main_bid]:
        text = bid.content_text or ""
        bid_items = _tender_items(session, bid.id)

        # 1 法定代表人身份证明（投标拆解为业绩/团队两项，改全文关键词判断）
        if "法定代表人" not in text or "身份证明" not in text:
            findings.append(Finding(
                sev="fatal", type="缺项", title="法定代表人身份证明缺失",
                meta=f"投标文件《{bid.filename}》全文未找到法定代表人身份证明相关内容",
                req="招标要求投标文件含法定代表人身份证明（资格审查必备件）",
                advice="补编法定代表人身份证明（按招标格式），盖章后插入资格审查文件章节。",
                fix="填入模板", doc_id=bid.id,
            ))
        # 2 授权委托书
        if "授权委托书" not in text and "委托代理人" not in text:
            findings.append(Finding(
                sev="fatal", type="缺项", title="授权委托书缺失",
                meta=f"投标文件《{bid.filename}》全文未找到授权委托书相关内容",
                req="招标要求投标文件含法定代表人授权委托书（格式清单必备项）",
                advice="补编授权委托书，明确委托代理人与权限，法定代表人签字并盖章。",
                fix="填入模板", doc_id=bid.id,
            ))
        # 3 保证金响应
        if req_bond and "保证金" not in text:
            findings.append(Finding(
                sev="fatal", type="未响应", title="投标保证金未响应",
                meta=f"招标要求「{req_bond}」，《{bid.filename}》全文未提及保证金",
                req=f"招标文件投标人须知前附表：投标保证金 {req_bond}",
                advice="按招标要求缴纳保证金并在投标文件中附缴纳凭证或保函复印件。",
                fix="补充保证金", doc_id=bid.id, page_no=None,
            ))
        # 4 投标有效期比对
        if req_validity:
            req_days = _days(req_validity)
            bid_quote = _sentence_with(text, "有效期")
            bid_days = _days(bid_quote) if bid_quote else None
            if not bid_quote:
                findings.append(Finding(
                    sev="fatal", type="未响应", title="投标有效期未响应",
                    meta=f"招标要求「{req_validity}」，《{bid.filename}》未提及投标有效期",
                    req=f"招标要求：投标有效期 {req_validity}",
                    advice="在投标函中明确承诺满足招标要求的投标有效期。",
                    fix="补充有效期承诺", doc_id=bid.id,
                ))
            elif req_days and bid_days and bid_days < req_days:
                findings.append(Finding(
                    sev="fatal", type="偏离", title=f"投标有效期 {bid_days} 天 < 要求 {req_days} 天",
                    meta=f"《{bid.filename}》有效期承诺短于招标要求",
                    req=f"招标要求：投标有效期 {req_validity}",
                    quote=bid_quote,
                    advice="修改投标函有效期为招标要求天数或以上，重新签字盖章。",
                    fix="调整有效期", doc_id=bid.id,
                    page_no=_find_page(session, bid.id, "有效期"),
                ))
        # 5 资质等级
        if req_qual:
            req_grade = _grade(req_qual)
            qual_val = ""
            for name, rows in bid_items.items():
                if "基本情况" in name:
                    qual_val = next((v for f, v in rows if "资质" in f), "")
            bid_grade = _grade(qual_val)
            if req_grade and qual_val and bid_grade and bid_grade != req_grade:
                findings.append(Finding(
                    sev="risk", type="偏离", title=f"资质等级「{bid_grade}」低于要求「{req_grade}」",
                    meta=f"《{bid.filename}》基本情况表资质等级与招标要求不一致",
                    req=f"招标要求资质：{req_qual}",
                    quote=f"资质等级：{qual_val}",
                    advice="核对资质证书等级；若确实不满足，评估是否构成资格性废标。",
                    fix="核对资质", doc_id=bid.id,
                    page_no=_find_page(session, bid.id, "资质等级"),
                ))
            elif req_grade and not qual_val:
                findings.append(Finding(
                    sev="risk", type="缺证", title="资质等级未在基本情况表标注",
                    meta=f"招标要求资质「{req_qual}」，《{bid.filename}》基本情况表无资质等级行",
                    req=f"招标要求资质：{req_qual}",
                    advice="在基本情况表补填资质等级并附证书扫描件。",
                    fix="补填资质", doc_id=bid.id,
                ))
        # 6 业绩
        if any("业绩" in name for name in bid_items):
            pass  # 有业绩结构项即视为响应
        elif re.search(r"业绩", json.dumps(t_items, ensure_ascii=False)):
            findings.append(Finding(
                sev="risk", type="缺证", title="类似项目业绩表缺失",
                meta=f"《{bid.filename}》未拆解出业绩表，招标文件含业绩要求",
                req="招标文件含类似业绩要求，投标须附业绩表及证明",
                advice="补编近年完成的类似项目情况表，附合同/验收证明页码索引。",
                fix="补业绩表", doc_id=bid.id,
            ))
        # 7 正副本提示
        if req_copies:
            findings.append(Finding(
                sev="hint", type="缺证", title="正副本份数需人工核对",
                meta=f"招标要求「{req_copies}」——文本检测无法确认纸质份数",
                req=f"招标要求：正副本份数 {req_copies}",
                advice="打印装订前核对正副本份数与要求一致，逐份盖章。",
                fix="人工核对", doc_id=bid.id,
            ))

    score_rows = _score_compare(session, bids, t_items)
    result = DetectResult(
        items=sorted(findings, key=lambda f: {"fatal": 0, "risk": 1, "hint": 2}[f.sev]),
        score_rows=score_rows,
        detected_at=datetime.now().strftime("%m-%d %H:%M"),
    )
    _save(session, project_id, result)
    audit("detect_run", detail=f"project {project_id} · 风险 {len(result.items)} 项")
    return result


def _score_compare(session: Session, bids: list[Document], t_items: dict[str, list[list[str]]]) -> list[list[str]]:
    """F3.2 评分办法对照：评标前附表字段 × 投标响应核对。"""
    rows_out: list[list[str]] = []
    bid_text = " ".join(b.content_text or "" for b in bids)
    bid_names = " ".join(_tender_items(session, b.id).__str__() for b in bids)
    checks: list[tuple[str, str, str]] = [
        ("技术", "技术方案/施工组织", r"技术方案|施工组织"),
        ("商务", "商务文件/资质商务部分", r"商务|资质"),
        ("价格", "开标一览表/报价", r"报价|开标一览表|价格"),
        ("业绩", "类似业绩表", r"类似项目|业绩"),
        ("团队", "项目团队简历", r"团队|简历|项目经理"),
    ]
    for name, rows in t_items.items():
        if "评标办法" not in name:
            continue
        for f, v in rows:
            hit = next((c for c in checks if c[0] in f), None)
            if hit:
                ok = bool(re.search(hit[2], bid_text)) or (hit[0] in bid_names)
                resp = hit[1] if ok else "未找到对应内容"
                rows_out.append([f"{f} · {v}", hit[1], resp, "ok" if ok else "warn", "达标" if ok else "缺口"])
            else:
                rows_out.append([f"{f} · {v}", "按评标办法核对", "—", "warn", "待人工"])
    return rows_out


def _save(session: Session, project_id: str, result: DetectResult) -> None:
    session.query(RiskItem).filter(RiskItem.project_id == project_id).delete()
    for f in result.items:
        session.add(RiskItem(
            project_id=project_id, sev=f.sev, type=f.type, title=f.title[:290],
            meta=f.meta, doc_id=f.doc_id, req=f.req, quote=f.quote,
            advice=f.advice, fix=f.fix[:95] if f.fix else None, page_no=f.page_no,
        ))
    session.commit()


def risk_index_of(items: list[RiskItem]) -> int:
    """废标风险指数：0 安全 · 100 极高（未处理项计分，越高越危险）。"""
    s = 0
    for r in items:
        if not r.done:
            s += 15 if r.sev == "fatal" else (8 if r.sev == "risk" else 3)
    return min(100, s)
