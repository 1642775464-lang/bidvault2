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

_TOKEN_TTL_SECONDS = 6 * 3600  # DS 打开文档后长会话中途不会再拉取，token 给足余量


def _secret() -> str:
    return get_settings().onlyoffice_jwt_secret


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document_key(document_id: str, data: bytes, version: int = 1) -> str:
    """key 与文档+版本+内容绑定；阶段三版本管理落地后 version 来自 DocumentVersion。"""
    return f"{document_id}-v{version}-{file_hash(data)[:12]}"


def sign_file_token(document_id: str, *, vid: str = "", ttl: int = _TOKEN_TTL_SECONDS) -> str:
    """DS 拉取文件的短期签名 token（HMAC，防篡改+限时；vid 绑定历史版本维度）。"""
    exp = int(time.time()) + ttl
    msg = f"{document_id}:{vid}:{exp}".encode()
    sig = hmac.new(_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]
    return f"{exp}:{vid}:{sig}"


def verify_file_token(document_id: str, token: str) -> tuple[bool, str]:
    try:
        exp_str, vid, sig = token.split(":", 2)
        exp = int(exp_str)
    except ValueError:
        return False, ""
    if exp < int(time.time()):
        return False, vid
    msg = f"{document_id}:{vid}:{exp}".encode()
    expect = hmac.new(_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expect), vid


def build_editor_config(document_id: str, filename: str, data: bytes, *, mode: str | None = None, key: str | None = None) -> dict[str, Any]:
    """ONLYOFFICE Document Editor 配置（JWT 签名后的完整载荷）。

    document.url 必须是 Document Server 容器可达的地址（ONLYOFFICE_BACKEND_INTERNAL_URL），
    不能是 127.0.0.1；浏览器加载 api.js 用 PUBLIC_URL，两者职责分离。
    """
    s = get_settings()
    if mode is None:
        mode = "edit" if s.onlyoffice_edit_enabled else "view"
    edit = mode == "edit"
    if key is None:
        key = document_key(document_id, data)
    doc_url = f"{s.onlyoffice_backend_internal_url}/api/onlyoffice/files/{document_id}?token={sign_file_token(document_id)}"
    config: dict[str, Any] = {
        "documentType": "word",
        "document": {
            "fileType": "docx",
            "key": key,
            "title": filename,
            "url": doc_url,
            "permissions": {"edit": edit, "download": True, "print": True,
                            "comment": edit, "review": edit},
        },
        "editorConfig": {
            "callbackUrl": f"{s.onlyoffice_backend_internal_url}/api/onlyoffice/callback/{document_id}",
            "lang": "zh-CN",
            "mode": mode,
            "user": {"id": "local-user", "name": "本地用户"},
            "customization": {
                "compactHeader": False,
                "hideRightMenu": True,  # 默认收起右侧格式面板（正文宽度优先）；用户仍可手动展开，DS 会记住其选择
                "autosave": True,
                "forcesave": True,  # 用户 Ctrl+S → callback status=6（工作快照）；关闭文档 → status=2 正式版本
            },
        },
    }
    config["token"] = jwt.encode(config, _secret(), algorithm="HS256")
    return config


# ---------------- 阶段三：版本管理 ----------------


def working_snapshot_path(document_id: str) -> Any:
    """status=6 工作快照路径（不属于正式版本）。"""
    from ..config import get_settings

    d = get_settings().files_dir / "working"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{document_id}.docx"


def hash_file(path: Any) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_to(url: str, dest_path: Any) -> None:
    """流式下载 DS 回调给出的文件（支持大文档，原子写：tmp + replace）。"""
    import httpx

    tmp = f"{dest_path}.tmp"
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1024 * 1024):
                f.write(chunk)
    import os

    os.replace(tmp, dest_path)


def current_version(session: Any, doc: Any) -> Any:
    """当前正式版本（current_version_id 优先；旧数据惰性初始化 v1）。"""
    from ..models import DocumentVersion

    if doc.current_version_id:
        return session.get(DocumentVersion, doc.current_version_id)
    return ensure_version(session, doc)


def ensure_version(session: Any, doc: Any) -> Any:
    """存量文档惰性补 v1（以当前 stored 文件为版本内容）；并发下以 UNIQUE 兜底。"""
    from ..config import get_settings
    from ..models import DocumentVersion

    if doc.current_version_id:
        return session.get(DocumentVersion, doc.current_version_id)
    path = get_settings().files_dir / doc.stored_name
    digest = hash_file(path)
    latest = (
        session.query(DocumentVersion)
        .filter(DocumentVersion.document_id == doc.id)
        .order_by(DocumentVersion.version.desc())
        .first()
    )
    v = DocumentVersion(
        document_id=doc.id,
        version=(latest.version + 1) if latest else 1,
        file_path=doc.stored_name,
        file_hash=digest,
        source="init",
    )
    session.add(v)
    session.flush()  # 可能触发 UNIQUE 冲突（并发初始化）→ 由调用方捕获重查
    doc.current_version_id = v.id
    session.commit()
    return v


def create_version_from_data(session: Any, doc: Any, data: bytes, *, editor_user: str | None, source: str = "onlyoffice") -> Any:
    """status=2 内容 → 新正式版本（version=max+1 事务内分配 + 原子更新 current_version_id）。

    内容未变化（哈希与当前版本一致）时不产生新版本，返回当前版本。
    """
    import uuid as _uuid

    from ..config import get_settings
    from ..models import DocumentVersion

    current = ensure_version(session, doc)
    digest = hashlib.sha256(data).hexdigest()
    if current is not None and current.file_hash == digest:
        return current
    stored = _uuid.uuid4().hex + ".docx"
    (get_settings().files_dir / stored).write_bytes(data)
    latest = (
        session.query(DocumentVersion)
        .filter(DocumentVersion.document_id == doc.id)
        .order_by(DocumentVersion.version.desc())
        .first()
    )
    v = DocumentVersion(
        document_id=doc.id,
        version=(latest.version + 1) if latest else 1,
        file_path=stored,
        file_hash=digest,
        editor_user=editor_user,
        source=source,
    )
    session.add(v)
    session.flush()
    doc.current_version_id = v.id
    doc.stored_name = stored
    doc.size = len(data)
    session.commit()
    return v


# ---------------- 阶段四：历史版本产品化 ----------------


def list_versions(session: Any, doc: Any, limit: int = 50) -> list[dict[str, Any]]:
    """版本列表（新→旧），带 is_current 标记。"""
    from ..models import DocumentVersion

    rows = (
        session.query(DocumentVersion)
        .filter(DocumentVersion.document_id == doc.id)
        .order_by(DocumentVersion.version.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": v.id,
            "version": v.version,
            "file_hash": v.file_hash,
            "source": v.source,
            "editor_user": v.editor_user,
            "restored_from_version_id": v.restored_from_version_id,
            "comment": v.comment,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "is_current": v.id == doc.current_version_id,
            "size": (get_settings().files_dir / v.file_path).stat().st_size
            if (get_settings().files_dir / v.file_path).is_file()
            else None,
        }
        for v in rows
    ]


def restore_version(session: Any, doc: Any, target: Any, *, editor_user: str | None, comment: str | None = None) -> Any:
    """恢复历史版本：以「创建新版本」方式落地（绝不指回旧版本、绝不覆盖历史文件）。

    当前 v4 恢复 v2 → 生成 v5：source=restore、restored_from_version_id=v2.id、
    文件为 v2 文件的完整副本；current_version_id 前移到新版本；重拆由调用方触发。
    """
    import shutil
    import uuid as _uuid

    from ..models import DocumentVersion

    current = ensure_version(session, doc)
    if current is not None and current.id == target.id:
        raise ValueError("该版本已是当前版本")
    src = get_settings().files_dir / target.file_path
    if not src.is_file():
        raise FileNotFoundError("历史版本文件已丢失")
    digest = hash_file(src)
    stored = _uuid.uuid4().hex + ".docx"
    shutil.copyfile(src, get_settings().files_dir / stored)  # 历史文件不可变：只复制
    latest = (
        session.query(DocumentVersion)
        .filter(DocumentVersion.document_id == doc.id)
        .order_by(DocumentVersion.version.desc())
        .first()
    )
    v = DocumentVersion(
        document_id=doc.id,
        version=(latest.version + 1) if latest else 1,
        file_path=stored,
        file_hash=digest,
        editor_user=editor_user,
        source="restore",
        restored_from_version_id=target.id,
        comment=(comment or "").strip() or None,
    )
    session.add(v)
    session.flush()
    doc.current_version_id = v.id
    doc.stored_name = stored
    doc.size = src.stat().st_size
    session.commit()
    return v

