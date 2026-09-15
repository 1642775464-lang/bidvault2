"""检索路由（M2：关键词 + 自然语言双通道 + 向量补齐）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_session
from ..services.audit import audit
from ..services.ingest.pipeline import backfill_embeddings
from ..services.search.hybrid import SearchFilters, search_hybrid, search_nl

router = APIRouter(prefix="/api/search", tags=["检索"])


@router.get("")
def search(
    q: str,
    kind: str | None = None,
    mode: str = "fts",
    limit: int = 20,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = q.strip()
    if not query:
        raise HTTPException(400, "查询不能为空")
    conn = session.connection().connection
    filters = SearchFilters(file_type=kind or None)
    if mode == "nl":
        hits = search_nl(conn, query, limit=limit)
    else:
        hits = search_hybrid(conn, query, limit=limit, filters=filters)
    return {
        "query": query,
        "mode": mode,
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


@router.post("/backfill")
def backfill(session: Session = Depends(get_session)) -> dict[str, Any]:
    """为存量切片补齐向量（NL 检索升级后一次性回填）。"""
    n = backfill_embeddings(session)
    audit("embed_backfilled", detail=f"{n} chunks")
    return {"embedded": n}
