"""E-1A：标书工作区文件监视器（E-0 契约 §7）。

语义：
- 忽略：Word 锁文件 ~$*、*.tmp、.bidvault/、其他隐藏项（workspace_fs.is_ignored_rel）；
- 稳定判定：事件后静默 settle 秒，且连续两次 hash 一致才产出事件（防杀软/半写）；
- 事件：added / modified / deleted → WorkspaceEvent(status=detected)，供 E-1B/C 消费；
- 自写抑制：hash 与该路径最近一次记录一致时不重复产出。

实现：watchdog Observer + 每路径 debounce 定时器；测试可注入 settle=0 走同步路径。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from sqlalchemy.orm import Session

from .workspace_fs import file_hash, is_ignored_rel, rel_of

logger = logging.getLogger(__name__)


class WorkspaceMonitor:
    """监听工作区根（递归），产出 WorkspaceEvent。单工作区；start/stop 幂等。"""

    def __init__(self, session_factory, *, settle_seconds: float = 5.0) -> None:
        self._session_factory = session_factory
        self._settle = settle_seconds
        self._observer = None
        self._root: Path | None = None
        self._workspace_id: str | None = None
        self._lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}
        self._last_hash: dict[str, str | None] = {}  # rel_path -> 最近一次产出事件的 hash
        self._stopped = threading.Event()

    # ---- 生命周期 ----

    @property
    def running(self) -> bool:
        return self._observer is not None

    def start(self, root: Path, workspace_id: str) -> None:
        with self._lock:
            if self._observer is not None:
                return
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            monitor = self

            class _Handler(FileSystemEventHandler):
                def on_created(self, event):
                    monitor._schedule(getattr(event, "dest_path", "") or event.src_path)

                def on_modified(self, event):
                    monitor._schedule(event.src_path)

                def on_moved(self, event):
                    monitor._schedule(event.dest_path or event.src_path)
                    monitor._schedule(event.src_path)

                def on_deleted(self, event):
                    monitor._schedule(event.src_path, deleted=True)

            self._root = Path(root).resolve()
            self._workspace_id = workspace_id
            self._stopped.clear()
            self._observer = Observer(timeout=1.0)
            self._observer.schedule(_Handler(), str(self._root), recursive=True)
            self._observer.daemon = True
            self._observer.start()
            logger.info("workspace monitor started: %s", self._root)

    def stop(self) -> None:
        with self._lock:
            obs, self._observer = self._observer, None
        self._stopped.set()
        for t in list(self._timers.values()):
            t.cancel()
        self._timers.clear()
        if obs is not None:
            obs.stop()
            obs.join(timeout=5)

    # ---- 事件路径 ----

    def _schedule(self, raw_path, *, deleted: bool = False) -> None:
        """去抖：同一路径的事件重置定时器，静默 settle 后统一判定。"""
        if self._root is None or self._stopped.is_set():
            return
        try:
            p = Path(raw_path)
            rel = rel_of(self._root, p)
        except Exception:  # noqa: BLE001  工作区外路径直接忽略
            return
        if not rel or is_ignored_rel(rel):
            return

        def _fire() -> None:
            self._timers.pop(rel, None)
            try:
                self._emit(rel, deleted=deleted)
            except Exception:  # noqa: BLE001
                logger.exception("workspace event failed: %s", rel)

        with self._lock:
            old = self._timers.get(rel)
            if old is not None:
                old.cancel()
            if self._settle <= 0:
                # 测试/同步路径
                _fire()
                return
            t = threading.Timer(self._settle, _fire)
            t.daemon = True
            self._timers[rel] = t
            t.start()

    def _emit(self, rel: str, *, deleted: bool) -> None:
        root = self._root
        assert root is not None and self._workspace_id is not None
        target = root / rel
        if deleted or not target.exists():
            if rel not in self._last_hash:
                return  # 从未见过的删除不产事件
            self._record(rel, "deleted", None)
            self._last_hash[rel] = None
            return
        h1 = file_hash(target)
        if h1 is None:
            return
        if self._settle > 0:
            import time

            time.sleep(min(self._settle, 1.0))  # 二次读校验（防半写），上限 1s
        h2 = file_hash(target)
        if h2 is None or h2 != h1:
            return  # 仍在写，等下一个事件周期
        if self._last_hash.get(rel) == h2:
            return  # 自写抑制/重复事件
        self._record(rel, "modified" if rel in self._last_hash else "added", h2)
        self._last_hash[rel] = h2

    def _record(self, rel: str, kind: str, file_hash_value: str | None) -> None:
        session: Session = self._session_factory()
        try:
            from ..models import WorkspaceEvent

            session.add(WorkspaceEvent(
                workspace_id=self._workspace_id, rel_path=rel, kind=kind,
                file_hash=file_hash_value, status="detected",
            ))
            session.commit()
            logger.info("workspace event: %s %s", kind, rel)
        finally:
            session.close()
