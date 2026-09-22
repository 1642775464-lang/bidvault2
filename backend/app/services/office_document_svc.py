"""OfficeDocumentService：通用 Office 文件生命周期服务（阶段六 B-1a 骨架）。

三端（source / material / workspace）共用；业务字段留在业务表，本服务只管：
版本（创建/哈希去重/原子前移）、工作快照、恢复、key、文件签名 token、编辑器配置组装、
callback 骨架、post-save processor 分发（幂等）。

持久化通过 VersionStoreAdapter 注入：
- B-1a：OfficeDocumentTableAdapter（读写 office_documents / office_document_versions 新表）；
- B-1c：SourceDocumentAdapter（source 文档适配现有 documents/document_versions，机械迁移用）。

设计硬规则见《阶段六B1-OfficeDocument通用层设计.md》第十四节：
- OfficeDocument.current_version_id 是唯一正式版本指针；
- title 只是编辑器显示缓存；
- processor 以 office_document_version_id 为幂等键；
- 文件保存与保存后的业务动作（processor）解耦。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..config import get_settings

logger = logging.getLogger(__name__)

_TOKEN_TTL_SECONDS = 6 * 3600
DOCUMENT_TYPES = ("source", "material", "workspace")


# ---------------- Adapter 接口（持久化抽象） ----------------


class VersionStoreAdapter(Protocol):
    """版本存储抽象：B-1a 提供 OfficeDocumentTableAdapter（新表）；
    B-1c 增加 SourceDocumentAdapter（source 文档适配现有表，机械迁移）。"""

    def get_document(self, od_id: str) -> Any | None: ...
    def list_versions(self, od_id: str, limit: int = 50) -> list[Any]: ...
    def get_version(self, version_id: str) -> Any | None: ...
    def latest_version(self, od_id: str) -> Any | None: ...
    def insert_version(self, od_id: str, version: int, *, file_path: str, file_hash: str,
                       file_size: int, source: str, editor_user: str | None,
                       comment: str | None, restored_from_version_id: str | None) -> Any: ...
    def set_current_version(self, od_id: str, version_id: str) -> None: ...
    def write_file(self, file_path: str, data: bytes) -> None: ...
    def read_file(self, file_path: str) -> bytes: ...
    def commit(self) -> None: ...


class OfficeDocumentTableAdapter:
    """office_documents / office_document_versions 新表适配器（B-1a 默认）。"""

    def __init__(self, session: Any) -> None:
        self._session = session

    def get_document(self, od_id: str) -> Any | None:
        from ..models import OfficeDocument

        return self._session.get(OfficeDocument, od_id)

    def list_versions(self, od_id: str, limit: int = 50) -> list[Any]:
        from ..models import OfficeDocumentVersion

        return list(
            self._session.query(OfficeDocumentVersion)
            .filter(OfficeDocumentVersion.office_document_id == od_id)
            .order_by(OfficeDocumentVersion.version.desc())
            .limit(limit)
            .all()
        )

    def get_version(self, version_id: str) -> Any | None:
        from ..models import OfficeDocumentVersion

        return self._session.get(OfficeDocumentVersion, version_id)

    def latest_version(self, od_id: str) -> Any | None:
        from ..models import OfficeDocumentVersion

        return (
            self._session.query(OfficeDocumentVersion)
            .filter(OfficeDocumentVersion.office_document_id == od_id)
            .order_by(OfficeDocumentVersion.version.desc())
            .first()
        )

    def insert_version(self, od_id: str, version: int, *, file_path: str, file_hash: str,
                       file_size: int, source: str, editor_user: str | None,
                       comment: str | None, restored_from_version_id: str | None) -> Any:
        from ..models import OfficeDocumentVersion

        v = OfficeDocumentVersion(
            office_document_id=od_id, version=version, file_path=file_path,
            file_hash=file_hash, file_size=file_size, source=source,
            editor_user=editor_user, comment=comment,
            restored_from_version_id=restored_from_version_id,
        )
        self._session.add(v)
        self._session.flush()
        return v

    def set_current_version(self, od_id: str, version_id: str) -> None:
        from ..models import OfficeDocument

        od = self._session.get(OfficeDocument, od_id)
        if od is not None:
            od.current_version_id = version_id

    def write_file(self, file_path: str, data: bytes) -> None:
        p = _safe_under_files_dir(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(f"{p}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def read_file(self, file_path: str) -> bytes:
        data: bytes = _safe_under_files_dir(file_path).read_bytes()
        return data

    def commit(self) -> None:
        self._session.commit()


# ---------------- Processor 接口 + 幂等 ----------------


class PostSaveProcessor(Protocol):
    """保存后业务处理协议。

    幂等契约：以 (office_document_version_id, document_type) 为幂等键；
    同一 VersionCreated 事件重复 dispatch，最终结果必须与执行 1 次完全相同。
    """

    document_type: str

    def process(self, od: Any, version: Any) -> None: ...


class ProcessorRunRegistry(Protocol):
    """幂等登记抽象：B-1a InMemory（契约验证）；B-2 起默认 ProcessorRunTableAdapter（持久）。"""

    def try_claim(self, version_id: str, processor_type: str) -> bool: ...
    def mark_done(self, version_id: str, processor_type: str) -> None: ...
    def mark_failed(self, version_id: str, processor_type: str, error: str) -> None: ...


class InMemoryProcessorRunRegistry:
    """内存版（仅契约测试用，生产正确性不依赖进程内状态）。"""

    def __init__(self) -> None:
        self._done: set[tuple[str, str]] = set()

    def try_claim(self, version_id: str, processor_type: str) -> bool:
        key = (version_id, processor_type)
        if key in self._done:
            return False
        self._done.add(key)
        return True

    def mark_done(self, version_id: str, processor_type: str) -> None:  # noqa: ARG002
        return None

    def mark_failed(self, version_id: str, processor_type: str, error: str) -> None:  # noqa: ARG002
        return None


def _safe_under_files_dir(rel: str) -> Any:
    """files_dir 内的受控路径解析：resolve 后拒绝逃逸（防 DB file_path 被篡改穿越）。"""
    from ..config import get_settings

    base = get_settings().files_dir.resolve()
    p = (base / rel).resolve()
    if not p.is_relative_to(base):
        raise ValueError(f"非法文件路径：{rel}")
    return p


class ProcessorRunTableAdapter:
    """processor_runs 表适配（B-2 起默认）：UNIQUE 抢占；failed 可重试（重试=删记录再 dispatch）。

    持有 engine，每次操作独立短会话——callback 后台线程与应用线程均可安全调用。
    """

    def __init__(self, engine: Any) -> None:
        from sqlalchemy.orm import sessionmaker

        self._factory = sessionmaker(bind=engine, expire_on_commit=False)

    def try_claim(self, version_id: str, processor_type: str) -> bool:
        import uuid as _uuid

        from ..models import ProcessorRun

        s = self._factory()
        try:
            s.add(ProcessorRun(
                id=_uuid.uuid4().hex,
                office_document_version_id=version_id,
                processor_type=processor_type,
                status="running",
            ))
            s.commit()
            return True
        except Exception:
            s.rollback()
            return False
        finally:
            s.close()

    def _update(self, version_id: str, processor_type: str, *, status: str, error: str | None) -> None:
        from ..models import ProcessorRun, _now

        s = self._factory()
        try:
            row = (
                s.query(ProcessorRun)
                .filter(ProcessorRun.office_document_version_id == version_id,
                        ProcessorRun.processor_type == processor_type)
                .first()
            )
            if row is not None:
                row.status = status
                row.error = error[:2000] if error else None
                row.finished_at = _now()
                s.commit()
        finally:
            s.close()

    def mark_done(self, version_id: str, processor_type: str) -> None:
        self._update(version_id, processor_type, status="done", error=None)

    def mark_failed(self, version_id: str, processor_type: str, error: str) -> None:
        self._update(version_id, processor_type, status="failed", error=error)


_PROCESSORS: dict[str, PostSaveProcessor] = {}
_RUNS: ProcessorRunRegistry = InMemoryProcessorRunRegistry()


def use_persistent_runs(engine: Any) -> None:
    """切换为 processor_runs 持久幂等（应用启动/B-2 起默认）。"""
    global _RUNS
    _RUNS = ProcessorRunTableAdapter(engine)


def register_processor(processor: PostSaveProcessor) -> None:
    _PROCESSORS[processor.document_type] = processor


def retry_processor_run(version_id: str, processor_type: str) -> bool:
    """failed → 允许重试：删除失败登记，下次 dispatch 重新抢占。"""
    if isinstance(_RUNS, ProcessorRunTableAdapter):
        s = _RUNS._factory()
        try:
            from ..models import ProcessorRun

            row = (
                s.query(ProcessorRun)
                .filter(ProcessorRun.office_document_version_id == version_id,
                        ProcessorRun.processor_type == processor_type,
                        ProcessorRun.status == "failed")
                .first()
            )
            if row is not None:
                s.delete(row)
                s.commit()
                return True
        finally:
            s.close()
    return False


def dispatch_post_save(od: Any, version: Any) -> str:
    """版本创建后分发业务处理（持久幂等：同 (version.id, type) 只执行一次）。

    抢占失败 = 已登记（done/running）→ already-dispatched；
    执行异常 → 标记 failed（可经 retry_processor_run 重试），异常不外抛。
    """
    processor = _PROCESSORS.get(od.document_type)
    if processor is None:
        logger.info("no post-save processor for type=%s version=%s", od.document_type, version.id)
        return "no-processor"
    if not _RUNS.try_claim(version.id, od.document_type):
        logger.info("post-save already claimed: version=%s type=%s", version.id, od.document_type)
        return "already-dispatched"
    try:
        processor.process(od, version)
    except Exception as e:
        logger.exception("post-save processor failed: version=%s type=%s", version.id, od.document_type)
        _RUNS.mark_failed(version.id, od.document_type, str(e))
        return "failed"
    _RUNS.mark_done(version.id, od.document_type)
    return "dispatched"


# ---------------- Service ----------------


@dataclass
class OfficeDocumentService:
    """通用 Office 文件生命周期服务。session + adapter 注入；无业务字段。"""

    session: Any
    adapter: VersionStoreAdapter

    # ---- 文档 ----

    def create_office_document(self, document_type: str, title: str, file_type: str = "docx") -> Any:
        from ..models import OfficeDocument

        if document_type not in DOCUMENT_TYPES:
            raise ValueError(f"非法 document_type：{document_type}")
        od = OfficeDocument(document_type=document_type, title=title, file_type=file_type)
        self.session.add(od)
        self.session.commit()
        return od

    # ---- 版本 ----

    def get_current_version(self, od: Any) -> Any | None:
        if od.current_version_id:
            v = self.adapter.get_version(od.current_version_id)
            if v is not None:
                return v
        return self.adapter.latest_version(od.id)

    def get_version(self, version_id: str) -> Any | None:
        return self.adapter.get_version(version_id)

    def create_version(self, od: Any, data: bytes, *, source: str, editor_user: str | None = None,
                       comment: str | None = None) -> Any:
        """创建新正式版本：哈希去重（同内容不重复建版）+ version=max+1 事务分配 +
        current_version_id 原子前移。文件名独立（历史文件不可变）。"""
        digest = hashlib.sha256(data).hexdigest()
        current = self.get_current_version(od)
        if current is not None and current.file_hash == digest:
            return current
        stored = f"{od.document_type}_{od.id[:8]}_{time.strftime('%Y%m%d%H%M%S')}_{os.urandom(3).hex()}.docx"
        self.adapter.write_file(stored, data)
        latest = self.adapter.latest_version(od.id)
        v = self.adapter.insert_version(
            od.id, (latest.version + 1) if latest else 1,
            file_path=stored, file_hash=digest, file_size=len(data),
            source=source, editor_user=editor_user, comment=comment,
            restored_from_version_id=None,
        )
        self.adapter.set_current_version(od.id, v.id)
        self.adapter.commit()
        return v

    def create_working_snapshot(self, od: Any, data: bytes) -> None:
        """status=6 工作快照：不落版本表、不换 current_version_id。"""
        p = _safe_under_files_dir(f"working/{od.id}.docx")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(f"{p}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def restore_version(self, od: Any, target: Any, *, editor_user: str | None,
                        comment: str | None = None) -> Any:
        """恢复历史版本=创建新版本（source=restore，restored_from 指向来源）。

        硬约束：target 必须属于同一 OfficeDocument；历史文件不可变（只复制）。
        恢复后由调用方 dispatch_post_save 触发业务处理。
        """
        if target.office_document_id != od.id:
            raise ValueError("restored_from_version 不属于该 OfficeDocument")
        current = self.get_current_version(od)
        if current is not None and current.id == target.id:
            raise ValueError("该版本已是当前版本")
        data = self.adapter.read_file(target.file_path)
        stored = f"restore_{od.id[:8]}_{os.urandom(4).hex()}.docx"
        self.adapter.write_file(stored, data)
        latest = self.adapter.latest_version(od.id)
        v = self.adapter.insert_version(
            od.id, (latest.version + 1) if latest else 1,
            file_path=stored, file_hash=target.file_hash, file_size=len(data),
            source="restore", editor_user=editor_user, comment=comment,
            restored_from_version_id=target.id,
        )
        self.adapter.set_current_version(od.id, v.id)
        self.adapter.commit()
        return v

    # ---- key / token / config ----

    @staticmethod
    def build_document_key(od: Any, version: Any) -> str:
        """key 与 OfficeDocument+版本绑定；编辑会话中不更换，重开文档才切换。"""
        return f"{od.id}-v{version.version}-{version.file_hash[:12]}"

    @staticmethod
    def generate_file_token(od_id: str, *, vid: str = "", ttl: int = _TOKEN_TTL_SECONDS) -> str:
        exp = int(time.time()) + ttl
        msg = f"{od_id}:{vid}:{exp}".encode()
        sig = hmac.new(get_settings().onlyoffice_jwt_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]
        return f"{exp}:{vid}:{sig}"

    @staticmethod
    def verify_file_token(od_id: str, token: str) -> tuple[bool, str]:
        try:
            exp_str, vid, sig = token.split(":", 2)
            exp = int(exp_str)
        except ValueError:
            return False, ""
        if exp < int(time.time()):
            return False, vid
        msg = f"{od_id}:{vid}:{exp}".encode()
        expect = hmac.new(get_settings().onlyoffice_jwt_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(sig, expect), vid


def office_document_service(session: Any) -> OfficeDocumentService:
    """默认实例：新表适配器（material/workspace 用）；source 适配器 B-1c 引入。"""
    return OfficeDocumentService(session=session, adapter=OfficeDocumentTableAdapter(session))

