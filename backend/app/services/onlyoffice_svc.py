"""ONLYOFFICE Document Server 集成服务（阶段二）。

- editor-config：JWT 签名的编辑器配置（document.url 指向本服务的签名文件接口）
- 文件读取：短期 HMAC 签名 token，DS 通过 HTTP 拉取原始 DOCX，不碰本地路径
- document.key：{document_id}-v{version}-{hash12}，与文件版本绑定；
  阶段二尚无版本管理，version 固定 1，文件哈希变化时自然变化

阶段三（callback/版本管理）将扩展本模块：status=2 正式版本 / status=6 仅工作快照，
编辑会话期间不更换 key。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import jwt

from ..config import get_settings

_TOKEN_TTL_SECONDS = 3600


def _secret() -> str:
    return get_settings().onlyoffice_jwt_secret


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document_key(document_id: str, data: bytes, version: int = 1) -> str:
    """key 与文档+版本+内容绑定；阶段三版本管理落地后 version 来自 DocumentVersion。"""
    return f"{document_id}-v{version}-{file_hash(data)[:12]}"


def sign_file_token(document_id: str, *, ttl: int = _TOKEN_TTL_SECONDS) -> str:
    """DS 拉取文件的短期签名 token（HMAC，防篡改+限时）。"""
    exp = int(time.time()) + ttl
    msg = f"{document_id}:{exp}".encode()
    sig = hmac.new(_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]
    return f"{exp}:{sig}"


def verify_file_token(document_id: str, token: str) -> bool:
    try:
        exp_str, sig = token.split(":", 1)
        exp = int(exp_str)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    msg = f"{document_id}:{exp}".encode()
    expect = hmac.new(_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expect)


def build_editor_config(document_id: str, filename: str, data: bytes, *, mode: str = "edit") -> dict[str, Any]:
    """ONLYOFFICE Document Editor 配置（JWT 签名后的完整载荷）。

    document.url 必须是 Document Server 容器可达的地址（ONLYOFFICE_BACKEND_INTERNAL_URL），
    不能是 127.0.0.1；浏览器加载 api.js 用 PUBLIC_URL，两者职责分离。
    """
    s = get_settings()
    key = document_key(document_id, data)
    doc_url = f"{s.onlyoffice_backend_internal_url}/api/onlyoffice/files/{document_id}?token={sign_file_token(document_id)}"
    config: dict[str, Any] = {
        "documentType": "word",
        "document": {
            "fileType": "docx",
            "key": key,
            "title": filename,
            "url": doc_url,
            "permissions": {"edit": True, "download": True, "print": True, "comment": True, "review": True},
        },
        "editorConfig": {
            "callbackUrl": f"{s.onlyoffice_backend_internal_url}/api/onlyoffice/callback/{document_id}",
            "lang": "zh-CN",
            "mode": mode,
            "user": {"id": "local-user", "name": "本地用户"},
            "customization": {
                "compactHeader": False,
                "hideRightMenu": False,
                "autosave": True,
                "forcesave": False,
            },
        },
    }
    config["token"] = jwt.encode(config, _secret(), algorithm="HS256")
    return config
