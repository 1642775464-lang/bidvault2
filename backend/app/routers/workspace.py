"""E-1A：标书工作区路由——创建/查询/骨架状态/事件列表。

所有写端点自动受本机鉴权中间件保护（D-1R4）；只读端点环回放行。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import WorkspaceEvent
from ..services import workspace_fs
from ..services.workspace_fs import WorkspaceError

router = APIRouter(prefix="/api/workspace", tags=["标书工作区"])


class WorkspaceIn(BaseModel):
    path: str
    name: str = ""


def _payload(session: Session, row) -> dict[str, Any]:
    from pathlib import Path

    root = Path(row.root_path)
    ok, repaired = workspace_fs.skeleton_status(root)
    return {
        "active": True,
        "id": row.id,
        "root": row.root_path,
        "name": row.name,
        "schema_version": row.schema_version,
        "skeleton_ok": ok,
        "repaired": repaired,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("")
def get_workspace(session: Session = Depends(get_session)) -> dict[str, Any]:
    row = workspace_fs.get_active(session)
    if row is None:
        return {"active": False}
    return _payload(session, row)


@router.post("", status_code=201)
def create_workspace(body: WorkspaceIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        row = workspace_fs.create_workspace(session, body.path, body.name)
    except WorkspaceError as e:
        code = 409 if "已存在活动工作区" in str(e) else 400
        raise HTTPException(code, {"code": "WORKSPACE_ERROR", "detail": str(e)}) from e
    return _payload(session, row)


@router.get("/events")
def list_events(status: str = "detected", limit: int = 100,
                session: Session = Depends(get_session)) -> dict[str, Any]:
    row = workspace_fs.get_active(session)
    if row is None:
        return {"items": []}
    q = (session.query(WorkspaceEvent)
         .filter(WorkspaceEvent.workspace_id == row.id, WorkspaceEvent.status == status)
         .order_by(WorkspaceEvent.created_at.desc()))
    items = [
        {"id": e.id, "rel_path": e.rel_path, "kind": e.kind, "file_hash": e.file_hash,
         "status": e.status, "created_at": e.created_at.isoformat() if e.created_at else None}
        for e in q.limit(max(1, min(limit, 500))).all()
    ]
    return {"items": items}


@router.post("/events/{event_id}/dismiss")
def dismiss_event(event_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    e = session.get(WorkspaceEvent, event_id)
    if e is None or e.status != "detected":
        raise HTTPException(404, "事件不存在或已处理")
    e.status = "dismissed"
    session.commit()
    return {"ok": True}


# ---------------- E-1B/C：历史资料导入 ----------------

import shutil  # noqa: E402
import uuid as _uuid  # noqa: E402

from fastapi import File, Form, UploadFile  # noqa: E402
from pydantic import Field  # noqa: E402

MAX_UPLOAD_BYTES = 200 * 1024 * 1024


class ImportIn(BaseModel):
    paths: list[str] = Field(min_length=1)
    label: str = ""


class ConfirmIn(BaseModel):
    decisions: list[dict[str, Any]]


def _active(session: Session):
    row = workspace_fs.get_active(session)
    if row is None:
        raise HTTPException(409, {"code": "WORKSPACE_ERROR", "detail": "尚未创建工作区"})
    return row


@router.post("/imports/upload", status_code=201)
async def upload_import(files: list[UploadFile], label: str = Form(""),
                        session: Session = Depends(get_session)) -> dict[str, Any]:
    """浏览器入口：multipart 上传（可多选/zip）→ uuid 容器名落临时目录 → 导入管线。

    安全：容器名由服务端 uuid 生成（用户文件名仅作分类信号传参），杜绝上传名穿越。
    """
    import tempfile
    from pathlib import Path

    from ..config import get_settings
    from ..services.workspace_import import ImportError_, create_batch

    row = _active(session)
    if not files:
        raise HTTPException(400, {"code": "IMPORT_ERROR", "detail": "未选择文件"})
    limit = get_settings().max_upload_bytes
    tmp = Path(tempfile.mkdtemp(prefix="bv-import-"))
    try:
        paths: list[str] = []
        names: list[str] = []
        for f in files:
            data = await f.read(limit + 1)
            if len(data) > limit:
                raise HTTPException(413, {"code": "IMPORT_ERROR",
                                          "detail": f"单文件超 200MB：{f.filename}"})
            container = tmp / f"{_uuid.uuid4().hex}.upload"
            container.write_bytes(data)
            paths.append(str(container))
            names.append(Path(f.filename or "unnamed").name or "unnamed")
        batch = create_batch(session, row.id, Path(row.root_path), paths, label, names=names)
    except ImportError_ as e:
        raise HTTPException(400, {"code": "IMPORT_ERROR", "detail": str(e)}) from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"batch_id": batch.id, "status": batch.status}


@router.get("/imports")
def list_imports(session: Session = Depends(get_session)) -> dict[str, Any]:
    from sqlalchemy import func

    from ..models import ImportBatch, ImportItem

    row = _active(session)
    batches = (session.query(ImportBatch)
               .filter(ImportBatch.workspace_id == row.id)
               .order_by(ImportBatch.created_at.desc()).limit(50).all())
    out = []
    for b in batches:
        counts = {st: n for st, n in (session.query(ImportItem.status, func.count())
                                      .filter(ImportItem.batch_id == b.id)
                                      .group_by(ImportItem.status).all())}
        out.append({"batch_id": b.id, "label": b.label, "status": b.status,
                    "created_at": b.created_at.isoformat() if b.created_at else None,
                    "counts": counts, "total": sum(counts.values())})
    return {"items": out}


@router.post("/imports/{batch_id}/discard")
def discard_import(batch_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """放弃导入：清理中转暂存副本（系统拷贝），未处理条目置 discarded。"""
    from pathlib import Path

    from ..models import ImportBatch
    from ..services.workspace_import import discard_batch

    row = _active(session)
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.workspace_id != row.id:
        raise HTTPException(404, "导入批次不存在")
    removed = discard_batch(session, batch, Path(row.root_path))
    return {"ok": True, "removed_files": removed, "status": batch.status}


@router.post("/imports/{batch_id}/items/{item_id}/undo")
def undo_import_item(batch_id: str, item_id: str,
                     session: Session = Depends(get_session)) -> dict[str, Any]:
    """撤销归档：仅限已归档未建档条目（文件移回中转）；已建档走历史恢复。"""
    from pathlib import Path

    from ..models import ImportBatch, ImportItem
    from ..services.workspace_import import undo_item

    row = _active(session)
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.workspace_id != row.id:
        raise HTTPException(404, "导入批次不存在")
    item = session.get(ImportItem, item_id)
    if item is None or item.batch_id != batch_id:
        raise HTTPException(404, "条目不存在")
    return undo_item(session, batch, item, Path(row.root_path))


@router.post("/imports", status_code=201)
def create_import(body: ImportIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    """创建导入批次：源文件/压缩包 → 中转暂存（安全解包）→ 扫描+分类建议。"""
    from pathlib import Path

    from ..services.workspace_import import ImportError_, create_batch

    row = _active(session)
    try:
        batch = create_batch(session, row.id, Path(row.root_path), body.paths, body.label)
    except ImportError_ as e:
        raise HTTPException(400, {"code": "IMPORT_ERROR", "detail": str(e)}) from e
    return {"batch_id": batch.id, "status": batch.status}


@router.get("/imports/{batch_id}")
def get_import(batch_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """批次详情：逐条分类建议（kind/confidence/reasons/dup）与状态。"""
    import json

    from ..models import ImportBatch, ImportItem

    row = _active(session)
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.workspace_id != row.id:
        raise HTTPException(404, "导入批次不存在")
    items = (session.query(ImportItem)
             .filter(ImportItem.batch_id == batch_id)
             .order_by(ImportItem.created_at).all())
    return {
        "batch_id": batch.id, "status": batch.status, "error": batch.error,
        "items": [{
            "id": i.id, "file_name": i.file_name, "staging_rel": i.staging_rel,
            "size": i.size, "kind": i.kind_suggested, "confidence": i.confidence,
            "reasons": json.loads(i.reasons_json) if i.reasons_json else [],
            "status": i.status, "dup_note": i.dup_note, "decided_kind": i.decided_kind,
            "target_rel": i.target_rel,
            "doc_id": i.doc_id, "material_id": i.material_id, "error": i.error,
        } for i in items],
    }


@router.post("/imports/{batch_id}/confirm")
def confirm_import(batch_id: str, body: ConfirmIn,
                   session: Session = Depends(get_session)) -> dict[str, Any]:
    """用户确认归档：逐条 kind/project/material_cat/decompose/duplicate_action 裁决。"""
    from pathlib import Path

    from ..config import get_settings
    from ..models import ImportBatch
    from ..services.workspace_import import confirm_batch

    row = _active(session)
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.workspace_id != row.id:
        raise HTTPException(404, "导入批次不存在")
    if batch.status not in ("pending_confirm", "done"):  # done 允许补确认未知条目
        raise HTTPException(409, f"批次状态 {batch.status} 不允许确认")
    results = confirm_batch(session, Path(row.root_path), batch, body.decisions,
                            get_settings().files_dir)
    return {"results": results}
