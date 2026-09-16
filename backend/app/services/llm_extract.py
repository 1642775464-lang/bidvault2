"""GLM-4-Flash 大模型结构化抽取（M4.5 验收反馈：规则抽取效果差，接入 LLM）。

对文档正文按固定 schema 让模型输出结构化 JSON，与规则抽取合并（LLM 优先）。
仅配置了 zhipu_api_key 时启用；调用失败静默回退规则结果。每次调用写审计。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .audit import audit

logger = logging.getLogger(__name__)

_MAX_CHARS = 12000  # 每段字数（glm-4-flash 免费模型，分段调用覆盖全文）

_SCHEMA: dict[str, str] = {
    "tender": (
        "这是招标文件。招标文件只拆以下三项，输出 JSON 数组，每项 {\"group\":\"项名\",\"name\":\"项名\",\"rows\":[[\"字段\",\"值\"],...]}，组名与结构项名相同："
        "①「投标人须知前附表」：投标人须知前附表是关键条款汇总表（常见列：条款号/条款名称/编列内容），"
        "rows 收录表内全部条款——递交截止时间、投标有效期、投标保证金（金额与形式）、正副本份数、"
        "招标人、招标代理机构、招标范围、交货期、交货地点、资质/财务/业绩/信誉要求、联合体、澄清、"
        "偏差、分包、开标、评标等，一行一个条款，字段=条款名称，值=编列内容全文；"
        "②「评标办法前附表」：评标办法前附表是评分规则表（常见列：条款号/评审因素/评审标准），"
        "rows 收录——评标方法与中标候选人排序规则、形式评审标准（逐项）、资格评审标准（逐项）、"
        "详细评审的评分项与分值（技术分/商务分/价格分等），字段=评审因素，值=评审标准或分值；"
        "③「投标文件格式」：投标文件格式章节列明了投标文件需包含的格式文件，"
        "rows 收录格式清单与格式表格——按阶段列出的文件名称、开标一览表、投标函、承诺函、"
        "报价表、偏差表、资格文件等，字段=格式/表格名称，值=用途或要求说明。"
        "值必须来自原文；文中不存在的项输出空 rows。只输出 JSON，不要解释。"
    ),
    "bid": (
        "从投标文件正文抽取以下结构，输出 JSON 数组，每项 {\"group\":\"组名\",\"name\":\"结构项名\",\"rows\":[[\"字段\",\"值\"],...]}。"
        "必须包含的组与结构项："
        "①「资格与身份文件」：法定代表人（单位负责人）身份证明、授权委托书；"
        "②「单位与财务状况」：基本情况表、近年财务状况；"
        "③「业绩与履约情况」：近年完成的类似项目情况表、正在供货和新承接的项目情况表；"
        "④「项目团队」：项目团队成员及相关人员简历（每人一行：姓名+职务）。"
        "正文中表格行以「 | 」分隔单元格，两列表格第一列为字段、第二列为值，请把它们还原为 rows 的键值对。字段与值必须来自原文，某结构项在文中不存在就整体跳过，不要编造。只输出 JSON，不要解释。"
    ),
}


def llm_available() -> bool:
    from ..config import get_settings

    return bool(get_settings().zhipu_api_key)


def llm_extract(text: str, kind: str) -> list[dict[str, Any]]:
    """调 GLM-4-Flash 分段抽取（每段 1.2 万字、重叠 500，覆盖全文），合并去重。"""
    from ..config import get_settings

    if not get_settings().zhipu_api_key or len(text.strip()) < 200:
        return []
    segs: list[str] = []
    step, overlap = _MAX_CHARS // 2, 500
    i = 0
    while i < len(text) and len(segs) < 6:
        segs.append(text[i: i + step])
        if i + step >= len(text):
            break
        i += step - overlap
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for seg in segs:
        for it in _call(seg, kind):
            key = (it["group"], it["name"])
            if key in merged:
                seen = {r[0] for r in merged[key]["rows"]}
                merged[key]["rows"] += [r for r in it["rows"] if r[0] not in seen]
            else:
                merged[key] = it
    return list(merged.values())


def _call(text: str, kind: str) -> list[dict[str, Any]]:
    from ..config import get_settings

    settings = get_settings()
    body = {
        "model": "glm-4-flash",
        "messages": [
            {"role": "system", "content": "你是招投标文件结构化抽取引擎，只输出合法 JSON。"},
            {"role": "user", "content": f"{_SCHEMA.get(kind, _SCHEMA['bid'])}\n\n文档正文：\n{text}"},
        ],
        "temperature": 0.1,
    }
    try:
        r = httpx.post(
            f"{settings.zhipu_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.zhipu_api_key}"},
            json=body,
            timeout=120,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        audit("glm_extract_call", detail=f"glm-4-flash {kind} {len(text) // 1000}k字")
        return _parse(content)
    except Exception:
        logger.warning("llm extract failed for kind=%s", kind, exc_info=True)
        return []


def _parse(content: str) -> list[dict[str, Any]]:
    try:
        start = content.index("[")
        end = content.rindex("]") + 1
        arr = json.loads(content[start:end])
    except (ValueError, json.JSONDecodeError):
        try:
            start = content.index("{")
            end = content.rindex("}") + 1
            arr = [json.loads(content[start:end])]
        except (ValueError, json.JSONDecodeError):
            return []
    out: list[dict[str, Any]] = []
    for it in arr if isinstance(arr, list) else []:
        group = str(it.get("group") or "").strip()
        name = str(it.get("name") or "").strip()
        rows = it.get("rows") or []
        if not name or not group:
            continue
        clean = [[str(r[0]).strip(), str(r[1]).strip()] for r in rows if isinstance(r, (list, tuple)) and len(r) >= 2 and str(r[0]).strip()]
        out.append({"group": group, "name": name[:120], "rows": clean[:30]})
    return out
