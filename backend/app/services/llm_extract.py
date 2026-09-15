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

_MAX_CHARS = 24000  # glm-4-flash 128K 上下文，控制时延与费用

_SCHEMA: dict[str, str] = {
    "tender": (
        "从招标文件正文抽取以下结构，输出 JSON 数组，每项 {\"group\":\"组名\",\"name\":\"结构项名\",\"rows\":[[\"字段\",\"值\"],...]}。"
        "必须包含的组与结构项："
        "①组「前附表与格式抽取」：投标人须知前附表（字段：递交截止时间/投标有效期/投标保证金/正副本份数/资质要求，以文中实际值为准，没有的字段不要编造）、"
        "评标办法前附表（字段：评标办法/技术分/商务分/价格分等评分项及分值）；"
        "②组「章节内容抽取」：章节目录（字段=章节号，值=章节标题，尽量全）。"
        "值必须来自原文，缺失就跳过该字段。只输出 JSON，不要解释。"
    ),
    "bid": (
        "从投标文件正文抽取以下结构，输出 JSON 数组，每项 {\"group\":\"组名\",\"name\":\"结构项名\",\"rows\":[[\"字段\",\"值\"],...]}。"
        "必须包含的组与结构项："
        "①「资格与身份文件」：法定代表人（单位负责人）身份证明、授权委托书；"
        "②「单位与财务状况」：基本情况表、近年财务状况；"
        "③「业绩与履约情况」：近年完成的类似项目情况表、正在供货和新承接的项目情况表；"
        "④「项目团队」：项目团队成员及相关人员简历（每人一行：姓名+职务）。"
        "字段与值必须来自原文，某结构项在文中不存在就整体跳过，不要编造。只输出 JSON，不要解释。"
    ),
}


def llm_available() -> bool:
    from ..config import get_settings

    return bool(get_settings().zhipu_api_key)


def llm_extract(text: str, kind: str) -> list[dict[str, Any]]:
    """调 GLM-4-Flash 抽取；返回 [{group,name,rows}]，失败返回空列表。"""
    from ..config import get_settings

    if not get_settings().zhipu_api_key or len(text.strip()) < 200:
        return []
    return _call(text[:_MAX_CHARS], kind)


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
