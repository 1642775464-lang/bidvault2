"""检索服务（M1 基础版：FTS5 trigram + 3gram 兜底 + 短词 LIKE + 筛选）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

Hit = tuple[float, str, int | None, str, str]  # score, content, page_no, doc_id, chunk_id


@dataclass(frozen=True)
class SearchFilters:
    file_type: str | None = None
    year: str | None = None

    def matches(self, doc_row: tuple[Any, ...]) -> bool:
        if self.file_type and doc_row[1] != self.file_type:
            return False
        # 年份软条件：日期未抽取到放行（v1 沉淀）
        return not (self.year and doc_row[2] and not str(doc_row[2]).startswith(self.year))


def _fts_safe(q: str) -> str:
    return '"' + q.replace('"', '""') + '"'


def _trigram_expand(terms: list[str], *, max_grams: int = 12) -> list[str]:
    grams: list[str] = []
    for t in terms:
        for i in range(len(t) - 2):
            g = t[i: i + 3]
            if g not in grams:
                grams.append(g)
                if len(grams) >= max_grams:
                    return grams
    return grams


def _push(rows: list[tuple[Any, ...]], seen: set[str], hits: list[Hit], sign: int) -> None:
    for r in rows:
        key = f"{r[3]}:{r[2]}:{r[1][:50]}"
        if key not in seen:
            seen.add(key)
            hits.append((sign * r[0], r[1], r[2], r[3], r[4]))


def search_fts(conn: Any, terms: list[str], *, limit: int = 30) -> tuple[list[Hit], list[str]]:
    hits: list[Hit] = []
    seen: set[str] = set()
    matched: list[str] = []
    long_terms = [t for t in terms if len(t.strip()) >= 3]
    if long_terms:
        rows = conn.execute(
            "SELECT bm25(chunks_fts) AS score, chunks_fts.content, chunks.page_no, chunks.document_id, chunks.chunk_id "
            "FROM chunks_fts JOIN chunks ON chunks.rowid = chunks_fts.rowid "
            "WHERE chunks_fts MATCH :q ORDER BY score LIMIT :limit",
            {"q": " OR ".join(_fts_safe(t) for t in long_terms[:6]), "limit": limit},
        ).fetchall()
        _push(rows, seen, hits, -1)
        if hits:
            matched.extend(long_terms)
        else:
            grams = _trigram_expand(long_terms)
            if grams:
                rows = conn.execute(
                    "SELECT bm25(chunks_fts) AS score, chunks_fts.content, chunks.page_no, chunks.document_id, chunks.chunk_id "
                    "FROM chunks_fts JOIN chunks ON chunks.rowid = chunks_fts.rowid "
                    "WHERE chunks_fts MATCH :q ORDER BY score LIMIT :limit",
                    {"q": " OR ".join(_fts_safe(g) for g in grams), "limit": limit},
                ).fetchall()
                _push(rows, seen, hits, -1)
                if hits:
                    matched.extend(grams)
                    matched.extend(long_terms)
    short_terms = [t for t in terms if 0 < len(t.strip()) <= 2]
    if short_terms:
        picked = short_terms[:3]
        clauses = " OR ".join(f"content LIKE :like{i}" for i in range(len(picked)))
        params: dict[str, object] = {f"like{i}": f"%{t}%" for i, t in enumerate(picked)}
        params["limit"] = limit
        rows = conn.execute(
            f"SELECT 0.0 AS score, content, page_no, document_id, chunk_id FROM chunks WHERE {clauses} LIMIT :limit",
            params,
        ).fetchall()
        _push(rows, seen, hits, 1)
        if rows:
            matched.extend(picked)
    return hits[:limit], matched


def _first_hit_pos(content: str, terms: list[str]) -> int:
    positions = [content.find(t) for t in terms if t and content.find(t) >= 0]
    return min(positions) if positions else -1


def _snippet(content: str, terms: list[str], *, width: int = 120) -> str:
    pos = _first_hit_pos(content, terms)
    if pos < 0:
        return content[:width] + ("…" if len(content) > width else "")
    start = max(0, pos - width // 3)
    return ("…" if start > 0 else "") + content[start: start + width] + "…"


def _snippet_html(content: str, terms: list[str], *, width: int = 120) -> str:
    import html

    pos = _first_hit_pos(content, terms)
    if pos < 0:
        window, start, has_tail = content[:width], 0, len(content) > width
    else:
        start = max(0, pos - width // 3)
        window = content[start: start + width]
        has_tail = start + width < len(content)
    escaped = html.escape(window)
    for t in sorted({t for t in terms if t}, key=len, reverse=True):
        et = html.escape(t)
        escaped = escaped.replace(et, f"<mark>{et}</mark>")
    head = "…" if pos >= 0 and start > 0 else ""
    return head + escaped + ("…" if has_tail else "")


@dataclass(frozen=True)
class SearchHit:
    document_id: str
    filename: str
    kind: str
    is_secret: int
    score: float
    snippet: str
    snippet_html: str
    page_no: int | None
    chunk_id: str


def search_hybrid(conn: Any, query: str, *, limit: int = 20,
                  terms: list[str] | None = None,
                  filters: SearchFilters | None = None) -> list[SearchHit]:
    fts_terms = terms if terms is not None else [query]
    over = limit * 3 if filters else limit
    hits, matched = search_fts(conn, fts_terms, limit=over)
    if not hits:
        return []
    if not matched:
        matched = [query]

    doc_ids = list({h[3] for h in hits})
    placeholders = ",".join("?" * len(doc_ids))
    docs = {
        r[0]: r
        for r in conn.execute(
            "SELECT id, filename, kind, is_secret FROM documents "
            f"WHERE id IN ({placeholders})",
            doc_ids,
        ).fetchall()
    }
    out: list[SearchHit] = []
    for score, content, page_no, doc_id, chunk_id in hits:
        d = docs.get(doc_id)
        if d is None:
            continue
        if filters and not filters.matches(d):
            continue
        out.append(SearchHit(
            document_id=doc_id, filename=d[1], kind=d[2], is_secret=d[3],
            score=round(score, 6), snippet=_snippet(content, matched),
            snippet_html=_snippet_html(content, matched), page_no=page_no, chunk_id=chunk_id,
        ))
        if len(out) >= limit:
            break
    return out
