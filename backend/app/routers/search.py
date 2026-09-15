"""P1.4 检索路由（M1 基础版：关键词+类型筛选）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_session
from ..services.search.hybrid import SearchFilters, search_hybrid

router = APIRouter(prefix="/api/search", tags=["检索"])


@router.get("")
def search(
    q: str,
    kind: str | None = None,
    limit: int = 20,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = q.strip()
    if not query:
        raise HTTPException(400, "查询不能为空")
    conn = session.connection().connection
    filters = SearchFilters(file_type=kind or None)
    hits = search_hybrid(conn, query, limit=limit, filters=filters)
    return {
        "query": query,
        "hits": [
            {
                "document_id": h.document_id,
                "filename": h.filename,
                "kind": h.kind,
                "is_secret": h.is_secret,
                "score": h.score,
                "snippet": h.snippet,
                "snippet_html": h.snippet_html,
                "page_no": h.page_no,
                "chunk_id": h.chunk_id,
            }
            for h in hits
        ],
    }
