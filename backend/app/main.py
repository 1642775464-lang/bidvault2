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
from .routers import backup, detect, documents, materials, paths, people, preview, projects, search
from .services.archive.watcher import WatchService
from .services.backup import apply_pending_restore, start_backup_scheduler
from .workers.queue import start_workers, stop_workers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

_watcher: WatchService | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _watcher
    if apply_pending_restore():
        logging.getLogger(__name__).info("pending restore applied")
    make_engine()
    settings = get_settings()
    start_workers(interval_seconds=settings.scan_interval_seconds)
    start_backup_scheduler()
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
app.include_router(backup.router)
app.include_router(documents.router)
app.include_router(materials.router)
app.include_router(search.router)
app.include_router(people.router)
app.include_router(preview.router)
app.include_router(detect.router)


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
    import shutil

    usage = shutil.disk_usage(s.data_dir)
    free_gb = round(usage.free / 1073741824, 1)
    used_pct = round(usage.used / usage.total * 100, 1)
    return {
        "status": "ok",
        "glm_configured": bool(s.zhipu_api_key),
        "scan_interval": s.scan_interval_seconds,
        "data_dir": str(s.data_dir),
        "counts": counts,
        "disk": {
            "free_gb": free_gb,
            "total_gb": round(usage.total / 1073741824, 1),
            "used_pct": used_pct,
            "warn": free_gb < 10 or used_pct >= 80,
        },
    }


# ---- M5 一键启动：生产模式托管前端 dist（单进程，无需 node）----
# 注意：必须在所有 API 路由之后注册，path 转换器会吞掉一切未匹配路径
from fastapi import HTTPException as _HTTPException  # noqa: E402
from fastapi.responses import FileResponse as _FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles as _StaticFiles  # noqa: E402

_DIST = get_settings().data_dir.parent / "frontend" / "dist"
if (_DIST / "index.html").is_file():
    app.mount("/assets", _StaticFiles(directory=str(_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> _FileResponse:
        """SPA fallback：非 /api 路径一律回前端入口。"""
        if full_path.startswith(("api/", "docs", "openapi")):
            raise _HTTPException(404, "Not Found")
        candidate = _DIST / full_path
        if full_path and candidate.is_file():
            return _FileResponse(candidate)
        return _FileResponse(_DIST / "index.html")
else:
    logging.getLogger(__name__).warning("frontend/dist 未构建——生产静态托管未启用（开发模式请用 vite 5173）")
