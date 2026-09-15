"""GLM-4V-Flash 图片字段识别（M3）。

红线：仅非涉密文档可调用（is_secret=0），每次调用写审计；HTTP 直连 zhipu，无 SDK 依赖。
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from ..config import get_settings
from .audit import audit

logger = logging.getLogger(__name__)

PROMPT = (
    "你是投标文件图片证据识别助手。识别这张图片：若为营业执照/资质证书/许可证/财务报告页/"
    "业绩合同页/人员证件之一，输出其关键字段；否则概括图片内容。"
    "只输出 JSON：{\"type\":\"图片类型\",\"fields\":{\"字段\":\"值\"},\"summary\":\"一句话概括\"}"
)

_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


def analyze_image(img_bytes: bytes, ext: str) -> dict[str, Any]:
    """调 GLM-4V-Flash 识别单张图片，返回解析后的 JSON（失败抛异常）。"""
    settings = get_settings()
    if not settings.zhipu_api_key:
        msg = "GLM API Key 未配置"
        raise RuntimeError(msg)
    mime = _MIME.get(ext.lower(), "image/png")
    b64 = base64.b64encode(img_bytes).decode()
    body = {
        "model": "glm-4v-flash",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": PROMPT},
            ],
        }],
        "temperature": 0.1,
    }
    r = httpx.post(
        f"{settings.zhipu_base_url}/chat/completions",
        headers={"Authorization": f"Bearer {settings.zhipu_api_key}"},
        json=body,
        timeout=60,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    audit("glm_vision_call", detail=f"glm-4v-flash {len(b64) // 1024}KB")
    try:
        s = content[content.index("{"): content.rindex("}") + 1]
        return json.loads(s)  # type: ignore[no-any-return]
    except (ValueError, json.JSONDecodeError):
        return {"type": "未识别", "fields": {}, "summary": content[:200]}
