"""切片。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CHUNK_SIZE = 500
_CHUNK_OVERLAP = 50
_SSE = re.compile(r"\s+")


@dataclass(frozen=True)
class ChunkOut:
    chunk_index: int
    content: str
    page_no: int | None


def chunk_text(pages: list[str]) -> list[ChunkOut]:
    outs: list[ChunkOut] = []
    idx = 0
    for page_no, page in enumerate(pages, start=1):
        clean = _SSE.sub(" ", page).strip()
        if not clean:
            continue
        start = 0
        while start < len(clean):
            end = min(start + _CHUNK_SIZE, len(clean))
            outs.append(ChunkOut(idx, clean[start:end], page_no))
            idx += 1
            if end >= len(clean):
                break
            start = end - _CHUNK_OVERLAP
    return outs
