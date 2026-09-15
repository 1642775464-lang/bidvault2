"""本地向量化（fastembed · BAAI/bge-small-zh-v1.5 · 512 维）。

CPU 纪律：进程内单例懒加载；涉密零出域——embedding 全程本地，不调云端。
"""

from __future__ import annotations

from typing import Any

_MODEL = "BAAI/bge-small-zh-v1.5"
_DIM = 512

_EMBEDDER: Any | None = None


def get_embedder() -> Any:
    """懒加载单例；模型缓存到 data/models（首次运行下载约 100MB）。"""
    global _EMBEDDER
    if _EMBEDDER is None:
        import os

        # CN 网络：走 hf-mirror；huggingface_hub 1.x 的 Xet 通道镜像不支持，禁用后走普通 HTTP
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        import os as _os
        from pathlib import Path as _Path

        from fastembed import TextEmbedding

        from ...config import get_settings

        # 测试环境可用 MODELS_CACHE_DIR 指向共享缓存，避免 tmp 目录反复下载模型
        cache = _Path(_os.environ["MODELS_CACHE_DIR"]) if _os.environ.get("MODELS_CACHE_DIR") else get_settings().models_dir
        cache.mkdir(parents=True, exist_ok=True)
        _EMBEDDER = TextEmbedding(model_name=_MODEL, cache_dir=str(cache))
    return _EMBEDDER


def embed(text: str) -> list[float]:
    text = (text or "").strip()
    if not text:
        return [0.0] * _DIM
    vec = next(get_embedder().embed([text[:2000]]))
    return [round(float(x), 6) for x in vec]


def dim() -> int:
    return _DIM
