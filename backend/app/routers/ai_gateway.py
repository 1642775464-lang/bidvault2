"""AI 网关全局配置路由（设置页「模型服务」）。

与 /api/office-documents/{oid}/ai-gateway/* 的区别：本组不依赖具体工作文档，
供设置页在无项目/无工作稿时也能读写全局网关配置。

安全边界：
- 密钥本体永不返回（视图只给 api_key_ref 与 has_key）；写入经 Windows 凭据管理器。
- base_url 沿用出网校验（防 SSRF，拒绝环回/私有地址）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_session
from ..services.audit import audit

router = APIRouter(prefix="/api/ai-gateway", tags=["AI 网关全局配置"])

_ALLOWED_PROVIDERS = ("", "mock", "openai_compatible")


class GatewayGlobalIn(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    timeout_s: int | None = None
    api_key_ref: str | None = None
    # 密钥本体：仅写入通道（凭据管理器），绝不回读
    api_key: str | None = None


def _view(session: Session) -> dict[str, Any]:
    from ..services.ai_gateway import load_gateway_config, public_config_view, resolve_secret_store

    config = load_gateway_config(session)
    store = resolve_secret_store()
    try:
        has_key = config["provider"] == "mock" or bool(store.get_key(config["api_key_ref"]))
    except Exception:  # noqa: BLE001  凭据后端不可用时不虚报
        has_key = False
    view = public_config_view(config, has_key)
    view["secret_source"] = store.source()
    return view


@router.get("")
def get_global_gateway_config(session: Session = Depends(get_session)) -> dict[str, Any]:
    return _view(session)


@router.put("")
def put_global_gateway_config(body: GatewayGlobalIn,
                              session: Session = Depends(get_session)) -> dict[str, Any]:
    from ..services.ai_gateway import load_gateway_config, save_gateway_config

    provider = body.provider if body.provider is not None else None
    if provider is not None and provider not in _ALLOWED_PROVIDERS:
        raise HTTPException(400, {"code": "GATEWAY_INVALID_PROVIDER",
                                  "message": f"provider 仅支持：{', '.join(x or '(空)' for x in _ALLOWED_PROVIDERS)}"})
    if body.timeout_s is not None and not (5 <= body.timeout_s <= 600):
        raise HTTPException(400, {"code": "GATEWAY_INVALID_TIMEOUT",
                                  "message": "timeout_s 取值范围 5~600"})
    if body.base_url:
        from ..services.ai_gateway import GatewayUrlError, validate_egress_url

        try:
            validate_egress_url(body.base_url)
        except GatewayUrlError as e:
            raise HTTPException(400, {"code": e.code, "message": str(e)}) from e

    current = load_gateway_config(session)
    updates = {k: v for k, v in {
        "provider": body.provider, "base_url": body.base_url, "model": body.model,
        "timeout_s": body.timeout_s, "api_key_ref": body.api_key_ref,
    }.items() if v is not None}
    config = save_gateway_config(session, {**current, **updates})

    # 密钥：仅当显式提交时写入凭据管理器（空串=保留原密钥；"__clear__"=清除）
    if body.api_key is not None:
        ref = config.get("api_key_ref") or "BIDVAULT_AI_KEY"
        from ..services.ai_gateway import resolve_secret_store

        store = resolve_secret_store()
        setter = getattr(store, "set_key", None)
        if setter is None:
            raise HTTPException(503, {"code": "GATEWAY_SECRET_STORE_UNAVAILABLE",
                                      "message": "当前环境不支持保存密钥（仅支持环境变量）"})
        key = body.api_key.strip()
        if key == "__clear__":
            setter(ref, "")
            audit("ai_gateway_key_cleared", detail=f"api_key_ref={ref}")
        elif key:
            ok = setter(ref, key)
            if not ok:
                raise HTTPException(500, {"code": "GATEWAY_SECRET_WRITE_FAILED",
                                          "message": "密钥写入系统凭据失败，请重试或改用环境变量"})
            audit("ai_gateway_key_saved", detail=f"api_key_ref={ref}")
    audit("ai_gateway_config_saved",
          detail=f"provider={config['provider']} · model={config['model']}")
    return _view(session)


@router.post("/test")
def test_global_gateway(session: Session = Depends(get_session)) -> dict[str, Any]:
    """全局连接测试：用当前配置发一次最小请求（不建提案、不触正文）。"""
    import time as _time

    from ..services.ai_gateway import AiGateway, GatewayError, load_gateway_config, resolve_secret_store

    config = load_gateway_config(session)
    gateway = AiGateway(config, resolve_secret_store())
    t0 = _time.perf_counter()
    try:
        text, manifest = gateway.generate_proposal_text("连接测试：请回复一个汉字「通」。", [])
    except GatewayError as e:
        from ..services.ai_gateway import sanitize_error

        raise HTTPException(e.http_status, {"code": e.code, "message": sanitize_error(e)}) from e
    return {"ok": True, "provider": manifest.provider, "model": manifest.model,
            "latency_ms": int((_time.perf_counter() - t0) * 1000), "chars": len(text)}
