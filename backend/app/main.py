"""标库 BidVault v2 · FastAPI 入口。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import make_engine
from .routers import documents, materials, paths, people, preview, projects, search
from .services.archive.watcher import WatchService
from .workers.queue import start_workers, stop_workers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

_watcher: WatchService | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _watcher
    make_engine()
    settings = get_settings()
    start_workers(interval_seconds=settings.scan_interval_seconds)
    _watcher = WatchService()
    _watcher.restart()
    app.state.watcher = _watcher
    try:  # F2.2 存量向量回填（无缺失时零开销）
        from sqlalchemy.orm import Session

        from .db import get_engine
        from .services.ingest.pipeline import backfill_embeddings

        with Session(bind=get_engine()) as s:
            backfill_embeddings(s)
    except Exception:
        logging.getLogger(__name__).warning("embed backfill skipped")
    yield
    if _watcher is not None:
        _watcher.stop()
    stop_workers()


app = FastAPI(title="标库 BidVault v2", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(projects.router)
app.include_router(paths.router)
app.include_router(documents.router)
app.include_router(materials.router)
app.include_router(search.router)
app.include_router(people.router)
app.include_router(preview.router)


@app.get("/api/health")
def health() -> dict[str, Any]:
    s = get_settings()
    from sqlalchemy import func, select

    from .db import get_engine
    from .models import AuditLog, Document, Material, WatchPath

    with get_engine().connect() as conn:
        counts = {
            "documents": conn.scalar(select(func.count()).select_from(Document)),
            "materials": conn.scalar(select(func.count()).select_from(Material)),
            "watch_paths": conn.scalar(select(func.count()).select_from(WatchPath)),
            "audit_logs": conn.scalar(select(func.count()).select_from(AuditLog)),
        }
    return {
        "status": "ok",
        "glm_configured": bool(s.zhipu_api_key),
        "scan_interval": s.scan_interval_seconds,
        "data_dir": str(s.data_dir),
        "counts": counts,
    }
