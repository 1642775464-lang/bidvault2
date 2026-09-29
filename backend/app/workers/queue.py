"""后台线程：串行导入队列 + 定时扫描。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator

from sqlalchemy.orm import Session

from ..db import get_session
from ..services.archive.scanner import scan_watch_path
from ..services.ingest.pipeline import load_pending, process_document

logger = logging.getLogger(__name__)


class QueueWorker:
    """导入队列消费：≤2 秒拾取队首一份串行处理。"""

    def __init__(self, session_factory: Callable[[], Iterator[Session]] = get_session,
                 idle_wait: float = 2.0) -> None:
        self._session_factory = session_factory
        self._idle_wait = idle_wait
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bv-queue", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._tick():
                self._stop.wait(self._idle_wait)

    def _tick(self) -> bool:
        try:
            session = next(self._session_factory())
            try:
                docs = load_pending(session, limit=1)
                if not docs:
                    return False
                process_document(session, docs[0], trigger="import")
                return True
            finally:
                session.close()
        except Exception:
            logger.exception("queue worker tick failed")
            return False


class ScanScheduler:
    """定时扫描全部启用路径。"""

    def __init__(self, session_factory: Callable[[], Iterator[Session]] = get_session,
                 interval: float = 600.0) -> None:
        self._session_factory = session_factory
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bv-scanner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._scan_all("auto")

    def _scan_all(self, trigger: str) -> None:
        try:
            session = next(self._session_factory())
            try:
                from ..models import WatchPath

                for wp in session.query(WatchPath).filter(WatchPath.enabled.is_(True)).all():
                    result = scan_watch_path(session, wp, trigger=trigger)
                    if result.added or result.updated or result.failed:
                        logger.info("scan %s: +%d ~%d !%d", wp.path, result.added, result.updated, result.failed)
            finally:
                session.close()
        except Exception:
            logger.exception("scheduled scan failed")


_worker: QueueWorker | None = None
_scheduler: ScanScheduler | None = None


def start_workers(interval_seconds: float = 600.0) -> None:
    global _worker, _scheduler
    if _worker is None:
        _worker = QueueWorker()
    _worker.start()
    if _scheduler is None:
        _scheduler = ScanScheduler(interval=interval_seconds)
    _scheduler.start()


def stop_workers() -> None:
    if _worker is not None:
        _worker.stop()
    if _scheduler is not None:
        _scheduler.stop()
