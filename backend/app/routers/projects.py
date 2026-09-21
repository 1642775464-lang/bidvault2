"""P2 项目库路由：台账 CRUD + 阶段 stepper + 详情数据。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import BUILTIN_INDUSTRIES, BUILTIN_PLATFORMS, STAGES, Document, Project
from ..services.audit import audit

router = APIRouter(prefix="/api/projects", tags=["F1.0 项目库"])


class ProjectIn(BaseModel):
    name: str
    role: str = "主标"
    industry: str | None = None
    open_at: str | None = None
    platform: str | None = None
    cust: str | None = None
    contact: str | None = None
    sales: str | None = None


class StageIn(BaseModel):
    stage: int


def _p_dict(p: Project, session: Session) -> dict[str, Any]:
    docs = session.query(Document).filter(Document.project_id == p.id).count()
    return {
        "id": p.id, "code": p.code, "name": p.name, "role": p.role,
        "industry": p.industry, "open_at": p.open_at, "platform": p.platform,
        "cust": p.cust, "contact": p.contact, "sales": p.sales,
        "stage": p.stage, "stage_label": STAGES[p.stage] if p.stage < len(STAGES) else "?",
        "is_archived": bool(p.is_archived), "result": p.result,
        "result_note": p.result_note, "result_amount": p.result_amount,
        "doc_count": docs,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("")
def list_projects(
    mode: str = "active",
    role: str | None = None,
    stage: int | None = None,
    q: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = session.query(Project)
    if mode == "archived":
        query = query.filter(Project.is_archived == 1)
    else:
        query = query.filter(Project.is_archived == 0)
    if role and role in ("主标", "陪标"):
        query = query.filter(Project.role == role)
    if stage is not None and 0 <= stage <= 4:
        query = query.filter(Project.stage == stage)
    if q:
        like = f"%{q}%"
        query = query.filter(
            Project.name.like(like) | Project.cust.like(like) | Project.code.like(like)
        )
    order = Project.open_at.desc() if mode == "archived" else Project.open_at.asc()
    rows = query.order_by(order).all()
    return {"total": len(rows), "items": [_p_dict(p, session) for p in rows]}


@router.get("/meta")
def meta() -> dict[str, Any]:
    return {
        "industries": list(BUILTIN_INDUSTRIES),
        "platforms": list(BUILTIN_PLATFORMS),
        "stages": list(STAGES),
    }


def _unique_code(session: Session, base: str) -> str:
    """编号冲突自动加后缀（重名建档不再 500）。"""
    code = base
    n = 2
    while session.query(Project).filter(Project.code == code).one_or_none() is not None:
        code = f"{base}-{n}"
        n += 1
    return code


@router.post("")
def create_project(body: ProjectIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    if not body.name.strip():
        raise HTTPException(400, "项目名称不能为空")
    p = Project(
        code=_unique_code(session, "NEW-" + body.name[:8]),
        name=body.name.strip(), role=body.role, industry=body.industry,
        open_at=body.open_at, platform=body.platform,
        cust=body.cust, contact=body.contact, sales=body.sales,
    )
    session.add(p)
    session.commit()
    audit("project_created", detail=f"{p.code} {p.name}")
    return {"id": p.id, "code": p.code}


@router.get("/cases")
def list_cases(project_id: str | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    """客户历史合作案例：已归档且登记「中标」的项目，按真实匹配度推荐。"""
    cur = session.get(Project, project_id) if project_id else None
    q = session.query(Project).filter(Project.is_archived == 1, Project.result == "中标")
    if cur is not None:
        q = q.filter(Project.id != cur.id)
    items: list[dict[str, Any]] = [
        {
            "id": p.id, "name": p.name, "cust": p.cust, "industry": p.industry,
            "open_at": p.open_at, "platform": p.platform,
            "amount": p.result_amount, "note": p.result_note,
            "match": _case_match(cur, p),
        }
        for p in q.all()
    ]
    items.sort(key=lambda x: (-x["match"], x["open_at"] or ""))
    return {"items": items, "total": len(items)}


def _case_match(cur: Project | None, p: Project) -> int:
    """匹配度：同行业 +45，同客户 +30，项目名二字组重叠至多 +25。"""
    if cur is None:
        return 60
    score = 0
    if cur.industry and p.industry and cur.industry == p.industry:
        score += 45
    if cur.cust and p.cust and cur.cust == p.cust:
        score += 30
    if cur.name and p.name:
        a_grams = {cur.name[i:i + 2] for i in range(len(cur.name) - 1)}
        b_grams = {p.name[i:i + 2] for i in range(len(p.name) - 1)}
        common = a_grams & b_grams
        if common:
            score += min(25, int(50 * len(common) / max(1, min(len(cur.name), len(p.name)) - 1)))
    return max(5, min(99, score))


@router.post("/{project_id}/workspace")
def ensure_workspace_api(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """C-1：幂等获取/创建项目投标工作稿（OfficeDocument type=workspace）。"""
    from ..models import OfficeDocumentVersion
    from ..services.workspace import ensure_workspace

    try:
        od = ensure_workspace(session, project_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    cur = session.get(OfficeDocumentVersion, od.current_version_id) if od.current_version_id else None
    audit("workspace_ensured", detail=f"project {project_id} · office_document {od.id}")
    return {
        "ok": True, "office_document_id": od.id, "title": od.title,
        "version": cur.version if cur else None,
    }


@router.get("/{project_id}/workspace")
def workspace_state(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """C-1：工作稿状态（未创建返回 exists=False；不隐式创建）。"""
    p = session.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    from ..models import OfficeDocument, OfficeDocumentVersion, WorkspaceDocument

    if not p.workspace_office_document_id:
        return {"exists": False}
    od = session.get(OfficeDocument, p.workspace_office_document_id)
    if od is None:
        return {"exists": False}
    cur = session.get(OfficeDocumentVersion, od.current_version_id) if od.current_version_id else None
    wd = (
        session.query(WorkspaceDocument)
        .filter(WorkspaceDocument.office_document_id == od.id)
        .first()
    )
    return {
        "exists": True, "office_document_id": od.id, "title": od.title,
        "version": cur.version if cur else None,
        "derived_from_version_id": wd.derived_from_version_id if wd else None,
        "derived_chars": len(wd.derived_text or "") if wd else 0,
    }


@router.get("/{project_id}")
def get_project(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    p = session.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    docs = session.query(Document).filter(Document.project_id == p.id).all()
    d = _p_dict(p, session)
    d["documents"] = [
        {
            "id": x.id, "filename": x.filename, "kind": x.kind, "size": x.size,
            "status": x.status, "doc_date": x.doc_date,
        }
        for x in docs
    ]
    return d


@router.patch("/{project_id}")
def update_project(project_id: str, body: ProjectIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    p = session.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    p.name = body.name.strip()
    p.role = body.role
    p.industry = body.industry
    p.open_at = body.open_at
    p.platform = body.platform
    p.cust = body.cust
    p.contact = body.contact
    p.sales = body.sales
    session.commit()
    audit("project_updated", detail=p.name)
    return {"ok": True}


@router.post("/{project_id}/stage")
def set_stage(project_id: str, body: StageIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    p = session.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    if not 0 <= body.stage <= 4:
        raise HTTPException(400, "stage 须为 0-4")
    old = p.stage
    p.stage = body.stage
    session.commit()
    audit("project_stage", detail=f"{p.name}: {STAGES[old]} → {STAGES[p.stage]}")
    return {"ok": True, "stage": p.stage}


class ArchiveIn(BaseModel):
    result: str = "中标"
    result_note: str | None = None
    result_amount: str | None = None


@router.post("/{project_id}/archive")
def archive(project_id: str, body: ArchiveIn | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    """归档项目，可同时登记中标结果（归档且中标的项目进入案例库）。"""
    p = session.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    if body is not None:
        if body.result not in ("中标", "未中标"):
            raise HTTPException(400, "result 须为 中标/未中标")
        p.result = body.result
        p.result_note = (body.result_note or "").strip() or None
        p.result_amount = (body.result_amount or "").strip() or None
    p.is_archived = 1
    session.commit()
    audit("project_archived", detail=f"{p.name} · {p.result or '未登记结果'}")
    return {"ok": True}


@router.post("/{project_id}/unarchive")
def unarchive(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    p = session.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    p.is_archived = 0
    session.commit()
    return {"ok": True}
