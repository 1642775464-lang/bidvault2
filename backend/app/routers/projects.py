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


@router.post("")
def create_project(body: ProjectIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    if not body.name.strip():
        raise HTTPException(400, "项目名称不能为空")
    p = Project(
        code="NEW-" + body.name[:8],
        name=body.name.strip(), role=body.role, industry=body.industry,
        open_at=body.open_at, platform=body.platform,
        cust=body.cust, contact=body.contact, sales=body.sales,
    )
    session.add(p)
    session.commit()
    audit("project_created", detail=f"{p.code} {p.name}")
    return {"id": p.id, "code": p.code}


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


@router.post("/{project_id}/archive")
def archive(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    p = session.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    p.is_archived = 1
    session.commit()
    return {"ok": True}


@router.post("/{project_id}/unarchive")
def unarchive(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    p = session.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    p.is_archived = 0
    session.commit()
    return {"ok": True}
