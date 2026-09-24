"""D-1R3：原生编辑器（DX）编辑会话服务。

会话协议（替代 DS callback status=6/2 的编辑生命周期）：
- 打开会话 = 取编辑锁：同一 OfficeDocument 仅一个 active 会话（并发 → 409）；
- 保存 = 受控临时 DOCX → 包校验 → fence（base 仍为 current）→ 新正式版本
  （source=native_editor）→ Processor 追平 → 会话 base 前移；
- 关闭/异常退出：显式 close，或 last_seen 超过 TTL 的懒过期回收；
- AI/Material/restore 写入经 active_edit_probe 检查本表 → 423；
- 不绕过 Proposal 审批与 fencing；原版本文件不可变（新版本走 create_version）。
"""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from .office_document_svc import OfficeDocumentService
from .xml_safety import safe_parse_xml

logger = logging.getLogger(__name__)

SESSION_TTL = timedelta(minutes=10)  # 心跳超时 = 异常退出回收窗口


class EditorSessionError(Exception):
    """会话操作失败（409 语义）。"""

    code = "EDITOR_SESSION_ERROR"


class EditorSessionConflictError(EditorSessionError):
    """同文档已有活跃编辑会话 / 版本 fence 冲突（409 语义）。"""

    code = "EDITOR_SESSION_CONFLICT"


def _expired(s: Any, now: datetime) -> bool:
    ls = s.last_seen
    if ls is None:
        return True
    if ls.tzinfo is None:
        ls = ls.replace(tzinfo=UTC)
    return now - ls > SESSION_TTL


class EditorSessionService:
    """session 注入；版本创建复用 OfficeDocumentService（哈希去重/原子前移不变）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ---- 内部 ----

    def _od(self, oid: str) -> Any:
        from ..models import OfficeDocument

        od = self.session.get(OfficeDocument, oid)
        if od is None:
            raise EditorSessionError("OfficeDocument 不存在")
        return od

    def _reap_expired(self, oid: str | None = None) -> None:
        """懒过期：心跳超时的 active 会话视为异常退出，关闭之。"""
        from ..models import EditorSession

        now = datetime.now(UTC)
        q = self.session.query(EditorSession).filter(EditorSession.state == "active")
        if oid:
            q = q.filter(EditorSession.office_document_id == oid)
        for s in q.all():
            if _expired(s, now):
                s.state = "closed"
                logger.warning("editor session expired: sid=%s oid=%s", s.id, s.office_document_id)
        self.session.commit()

    def _get_active(self, sid: str) -> Any:
        from ..models import EditorSession

        s = self.session.get(EditorSession, sid)
        if s is None or s.state != "active":
            raise EditorSessionError("编辑会话不存在或已关闭")
        self.session.refresh(s)
        if _expired(s, datetime.now(UTC)):
            s.state = "closed"
            self.session.commit()
            raise EditorSessionError("编辑会话已超时（异常退出回收），请重新打开")
        return s

    def _svc(self) -> OfficeDocumentService:
        from .office_document_svc import OfficeDocumentTableAdapter

        return OfficeDocumentService(session=self.session, adapter=OfficeDocumentTableAdapter(self.session))

    # ---- 协议操作 ----

    def open(self, oid: str, editor_user: str = "local") -> Any:
        from ..models import EditorSession

        self._reap_expired(oid)
        od = self._od(oid)
        cur = self._svc().get_current_version(od)
        if cur is None:
            raise EditorSessionError("该 Office 文件尚无正式版本")
        held = (
            self.session.query(EditorSession)
            .filter(EditorSession.office_document_id == oid, EditorSession.state == "active")
            .first()
        )
        if held is not None:
            raise EditorSessionConflictError(f"该文档正在编辑中（会话 {held.id[:8]}），已拒绝重复打开")
        s = EditorSession(office_document_id=oid, base_version_id=cur.id, base_version=cur.version,
                          editor_user=editor_user)
        self.session.add(s)
        self.session.commit()
        return s, cur

    def heartbeat(self, sid: str) -> Any:
        s = self._get_active(sid)
        s.last_seen = datetime.now(UTC)
        self.session.commit()
        return s

    def read_file(self, sid: str) -> tuple[Any, bytes]:
        """返回 (session, 版本文件字节)。文件只读副本，编辑器在临时区修改。"""
        s = self._get_active(sid)
        svc = self._svc()
        v = svc.get_version(s.base_version_id)
        if v is None:
            raise EditorSessionError("基线版本文件已丢失")
        return s, svc.adapter.read_file(v.file_path)

    def save(self, sid: str, data: bytes) -> Any:
        """受控保存：校验 → fence → 新正式版本 → Processor 追平 → base 前移。"""
        validate_docx_package(data)
        s = self._get_active(sid)
        od = self._od(s.office_document_id)
        svc = self._svc()
        cur = svc.get_current_version(od)
        if cur is None or cur.id != s.base_version_id:
            raise EditorSessionConflictError(
                f"版本冲突：会话基线 v{s.base_version} 已不是当前版本（当前 "
                f"v{cur.version if cur else '无'}），请重开会话后再保存"
            )
        v = svc.create_version(od, data, source="native_editor", editor_user=s.editor_user,
                               comment="原生编辑器保存")
        if v.id != s.base_version_id:  # 同内容去重时返回 current，也视为保存成功
            s.base_version_id = v.id
            s.base_version = v.version
        s.last_seen = datetime.now(UTC)
        self.session.commit()
        from .office_document_svc import dispatch_post_save

        dispatch_post_save(od, v)
        return s, v

    def close(self, sid: str) -> None:
        s = self._get_active(sid)
        s.state = "closed"
        self.session.commit()


def validate_docx_package(data: bytes) -> None:
    """受控 DOCX 包校验：ZIP 完整 + ContentTypes/document.xml 可安全解析 + python-docx 可开。"""
    from docx import Document as Docx

    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        names = set(z.namelist())
    except zipfile.BadZipFile as e:
        raise EditorSessionError("保存内容不是有效的 DOCX 包") from e
    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        raise EditorSessionError("DOCX 包缺少关键部件")
    safe_parse_xml(z.read("[Content_Types].xml"))
    safe_parse_xml(z.read("word/document.xml"))
    try:
        Docx(io.BytesIO(data))
    except Exception as e:
        raise EditorSessionError("DOCX 包无法被解析器打开") from e


def active_session_exists(session: Session, oid: str) -> bool:
    """编辑锁探针（供 AI/Material/restore 写入路径使用 → 423）。"""
    from ..models import EditorSession

    row = (
        session.query(EditorSession)
        .filter(EditorSession.office_document_id == oid, EditorSession.state == "active")
        .first()
    )
    if row is None:
        return False
    return not _expired(row, datetime.now(UTC))
