"""标库 BidVault v2 · FastAPI 入口。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .config import get_settings
from .db import get_session, make_engine
from .routers import (
    backup,
    detect,
    documents,
    editor_sessions,
    materials,
    onlyoffice,
    paths,
    people,
    preview,
    projects,
    outline,
    search,
    workspace,
)
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
    try:  # B-2/B-1c：post-save 持久幂等 + 处理器注册 + source 文档身份迁移
        from sqlalchemy.orm import Session

        from .db import get_engine
        from .db import get_engine as _ge
        from .services.material_processor import MaterialDocumentProcessor
        from .services.office_document_svc import register_processor, use_persistent_runs
        from .services.source_migration import (
            backfill_source_office_documents,
            is_cutover_completed,
        )
        from .services.source_processor import SourceDocumentProcessor

        use_persistent_runs(_ge())
        from .services.workspace import WorkspaceDocumentProcessor


        register_processor(SourceDocumentProcessor())
        register_processor(MaterialDocumentProcessor())
        register_processor(WorkspaceDocumentProcessor())
        register_processor(WorkspaceDocumentProcessor())
        with Session(bind=get_engine()) as _s:
            if not is_cutover_completed(_s):
                backfill_source_office_documents(_s)  # B-1c-4 切换后由 marker 关闭
    except Exception:
        logging.getLogger(__name__).warning("processor registry init skipped")
    settings = get_settings()
    start_workers(interval_seconds=settings.scan_interval_seconds)
    start_backup_scheduler()
    _watcher = WatchService()
    _watcher.restart()
    app.state.watcher = _watcher
    # E-1A：标书工作区监视（有活动工作区时启动）
    from sqlalchemy.orm import Session as _Session

    with _Session(bind=get_engine()) as _ws:
        _ws_row = _ws.execute(
            text("SELECT id, root_path FROM workspace_registry WHERE active = 1 LIMIT 1")
        ).first()
    if _ws_row is not None:
        from .services.workspace_monitor import WorkspaceMonitor

        _ws_monitor = WorkspaceMonitor(get_session)
        _ws_monitor.start(Path(_ws_row.root_path), _ws_row.id)
        app.state.workspace_monitor = _ws_monitor
    try:  # F2.2 存量向量回填（无缺失时零开销）
        from sqlalchemy.orm import Session

        from .db import get_engine
        from .services.ingest.pipeline import backfill_embeddings

        with Session(bind=get_engine()) as s:
            backfill_embeddings(s)
    except Exception:
        logging.getLogger(__name__).warning("embed backfill skipped")
    yield
    _ws_mon = getattr(app.state, "workspace_monitor", None)
    if _ws_mon is not None:
        _ws_mon.stop()
    if _watcher is not None:
        _watcher.stop()
    stop_workers()


app = FastAPI(title="标库 BidVault v2", version="2.0.0", lifespan=lifespan)


@app.middleware("http")
async def local_access_guard(request: Request, call_next):
    """D-1R4 安全收口：/api 仅环回可达；业务写接口必须携带实例令牌（头或 Cookie）。

    读请求放行并在环回来源首次访问时下发 HttpOnly Cookie（同浏览器后续写请求自动携带）；
    编辑器壳/脚本以 X-BidVault-Token 头携带（令牌文件位于数据目录 config/local_token）。
    """
    from fastapi.responses import JSONResponse

    from .config import get_settings

    path = request.url.path
    if not path.startswith("/api"):
        return await call_next(request)
    settings = get_settings()
    client = request.client.host if request.client else ""
    # "testclient" 为 Starlette 测试宿主标识（仅测试进程内可达）
    loopback = client in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "testclient")
    if not loopback:
        return JSONResponse({"detail": "FORBIDDEN_REMOTE：API 仅允许本机访问"}, status_code=403)
    if not settings.local_auth_enabled or path == "/api/health":
        return await call_next(request)
    presented = request.headers.get("x-bidvault-token") or request.cookies.get("bv_token")
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and presented != settings.local_access_token:
        return JSONResponse({"detail": "UNAUTHORIZED_LOCAL_TOKEN：缺少本机访问令牌"}, status_code=401)
    response = await call_next(request)
    if presented is None:
        response.set_cookie("bv_token", settings.local_access_token, httponly=True, samesite="strict")
    return response


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
app.include_router(workspace.router)
app.include_router(outline.router)
app.include_router(people.router)
app.include_router(preview.router)
app.include_router(detect.router)
app.include_router(editor_sessions.router)
app.include_router(onlyoffice.router)
app.include_router(onlyoffice.office_router)
app.include_router(onlyoffice.ai_router)
onlyoffice.register_document_routes(documents.router)


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
