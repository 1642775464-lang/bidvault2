"""F2.5 人员档案路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_session
from ..services.people import people_payload

router = APIRouter(prefix="/api/people", tags=["F2.5 人员档案"])


@router.get("")
def people(project_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    if not project_id:
        raise HTTPException(400, "缺少 project_id")
    return {"items": people_payload(session, project_id)}
