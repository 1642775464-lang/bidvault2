"""watchdog 实时监听：收藏路径下新建/移入的文件即时入队。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ...db import get_session
from .scanner import ARCHIVE_EXT, ingest_single_file

logger = logging.getLogger(__name__)


class _NewFileHandler(FileSystemEventHandler):
    def on_created(self, event: FileSystemEvent) -> None:
        self._ingest(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._ingest(event.dest_path)

    def _ingest(self, raw_path: str | bytes) -> None:
        if not raw_path:
            return
        p = Path(str(raw_path))
        if p.suffix.lower() not in ARCHIVE_EXT or p.is_dir():
            return
        try:
            import time

            prev = -1
            for _ in range(10):
                size = p.stat().st_size if p.exists() else -1
                if size == prev and size >= 0:
                    break
                prev = size
                time.sleep(0.5)
            _with_next_session(lambda s: ingest_single_file(s, p))
        except Exception:
            logger.exception("watch ingest failed: %s", p)


def _with_next_session(fn: Callable[[Session], bool]) -> None:
    session = next(get_session())
    try:
        fn(session)
    finally:
        session.close()


class WatchService:
    """对每个启用的收藏路径挂 Observer；路径增删后需 restart()。"""

    def __init__(self) -> None:
        self._observer: Any | None = None
        self._lock = threading.Lock()

    def restart(self) -> None:
        with self._lock:
            self._stop_locked()
            session = next(get_session())
            try:
                from ...models import WatchPath

                paths = [wp.path for wp in session.query(WatchPath).filter(WatchPath.enabled.is_(True)).all()]
            finally:
                session.close()
            if not paths:
                return
            observer = Observer(timeout=5)
            handler = _NewFileHandler()
            for p in paths:
                if Path(p).is_dir():
                    observer.schedule(handler, p, recursive=True)
            observer.daemon = True
            observer.start()
            self._observer = observer
            logger.info("watchdog watching %d paths", len(paths))

    def _stop_locked(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._observer = None

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
