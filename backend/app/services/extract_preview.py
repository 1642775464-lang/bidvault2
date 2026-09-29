"""规则试运行 preview（契约《标库-拆解规则契约》§十一 v1.1 冻结）。

定位（硬约束）：preview = **规则命中证据**（dry-run），与最终结构化结果分离。

可观测性边界（§11.7 确认）：允许写日志/审计/耗时统计；禁止任何业务态变更——
不创建 extract_executions、不改 documents（status/error_msg/provenance）、
不写 extract_items/extract_rows/materials/AI 字段/版本链/规则集版本。

一致性（§11.4）：与正式拆解复用同一解析器与同一匹配实现
（ingest.extractor.extract_structured_with_regions），保证「预览所见 = 正式拆解所得」。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Document, OfficeDocument, OfficeDocumentVersion
from .audit import audit
from .extract_rules import ExtractRuleError, current_version, resolve_rules
from .ingest.extractor import extract_structured_with_regions
from .ingest.parser import parse_any

PREVIEW_MAX_FILE_BYTES = 100 * 1024 * 1024  # ≤100MB（契约 §11.5）
PREVIEW_MAX_PAGES = 300                     # ≤300 页
PREVIEW_ROWS_LIMIT = 20                     # 单规则预览行数上限
PREVIEW_CHARS_LIMIT = 2000                  # 单规则预览字数上限
PREVIEW_TOO_LARGE_MSG = "文档较大，请执行正式拆解查看结果"


class PreviewTooLargeError(Exception):
    """资源门禁超限（契约 §11.5）→ 路由层映射 413 + 固定文案。"""


def guard_check(file_size: int, page_count: int | None) -> dict[str, Any]:
    """资源门禁（纯函数，便于单测）：超限抛 PreviewTooLargeError。"""
    if file_size > PREVIEW_MAX_FILE_BYTES:
        raise PreviewTooLargeError(PREVIEW_TOO_LARGE_MSG)
    if page_count is not None and page_count > PREVIEW_MAX_PAGES:
        raise PreviewTooLargeError(PREVIEW_TOO_LARGE_MSG)
    return {
        "ok": True,
        "file_size_mb": round(file_size / 1024 / 1024, 2),
        "pages": page_count,
    }


def _page_for_line(line_1based: int, pages: list[str]) -> int | None:
    """行号 → 页号（1-based）。docx 等整篇一页形态返回 None（契约：page 以 line 为准）。"""
    if not pages or len(pages) <= 1:
        return None
    acc = 0
    for i, page_text in enumerate(pages):
        n = page_text.count("\n") + 1
        if line_1based <= acc + n:
            return i + 1
        acc += n
    return len(pages)


def _row_to_text(row: Any) -> str:
    """行 → 证据文本：列表行按「单元格 | 单元格」；V2 表格行加 [表格] 前缀。"""
    if isinstance(row, dict):
        cells = [
            str(c.get("text", "")).strip()
            for c in (row.get("cells") or [])
            if isinstance(c, dict) and not c.get("merged")
        ]
        return "[表格] " + " | ".join(x for x in cells if x)
    return " | ".join(x for x in (str(c).strip() for c in row) if x)


def _preview_rows(rows: list[Any]) -> tuple[list[str], bool]:
    """区间行 → 预览文本（≤20 行 / ≤2000 字），超出置 truncated（契约 §11.3）。"""
    out: list[str] = []
    chars = 0
    for r in rows:
        text = _row_to_text(r)
        if not text:
            continue
        if len(out) >= PREVIEW_ROWS_LIMIT or chars + len(text) > PREVIEW_CHARS_LIMIT:
            return out, True
        out.append(text)
        chars += len(text)
    return out, False


def run_preview(
    session: Session, *, document_id: str,
    rule_set_version: int | None = None, rule_ids: list[str] | None = None,
) -> dict[str, Any]:
    """规则试运行：返回命中证据，不落任何业务态（契约 §11.1/§11.6）。"""
    doc = session.get(Document, document_id)
    if doc is None:
        raise ExtractRuleError("文档不存在", http_status=404)
    if not doc.office_document_id:
        raise ExtractRuleError("文档尚未建立 Office 档案，无法试运行", http_status=409)
    od = session.get(OfficeDocument, doc.office_document_id)
    if od is None or not od.current_version_id:
        raise ExtractRuleError("Office 档案无正式版本", http_status=409)

    # 声明版本 ≠ 当前 → 409（不静默按当前版本试跑，契约 §11.2）
    cur = current_version(session)
    if rule_set_version is not None and int(rule_set_version) != cur:
        raise ExtractRuleError(
            f"规则集版本不一致：声明 v{rule_set_version}，当前 v{cur}，请刷新规则后重试",
            http_status=409,
        )

    # 规则加载：缺省=该文档类型全部启用规则；指定 id 须同类型且启用（契约 §11.2）
    enabled = resolve_rules(session, doc.kind)
    if rule_ids:
        wanted = list(dict.fromkeys(str(x) for x in rule_ids))
        by_id = {r["id"]: r for r in enabled}
        missing = [x for x in wanted if x not in by_id]
        if missing:
            raise ExtractRuleError(
                f"规则不属于该文档类型或未启用：{', '.join(missing[:3])}", http_status=409
            )
        selected = [by_id[x] for x in wanted]
    else:
        selected = enabled
    if not selected:
        return {
            "document": {"id": doc.id, "filename": doc.filename, "kind": doc.kind},
            "rule_set_version": cur,
            "ruleset_hash": "",
            "guard": {"ok": True, "file_size_mb": None, "pages": None},
            "matches": [],
        }

    # Guard：先 stat 再读（超限不读入内存）；页数在解析后校验（契约 §11.5）
    version = session.get(OfficeDocumentVersion, od.current_version_id)
    file_path = get_settings().files_dir / version.file_path
    size = file_path.stat().st_size
    guard = guard_check(size, None)  # 文件级门禁；页级门禁在解析后补查

    data = file_path.read_bytes()
    parsed = parse_any(data, doc.file_type)
    guard["pages"] = len(parsed.pages) if parsed.pages else None
    if guard["pages"] is not None:
        guard_check(size, guard["pages"])  # 页数超限 → 413

    from .ingest.extractor import extract_structured_with_regions

    regions = extract_structured_with_regions(        parsed.text, doc.kind, parsed.doc_tables, rules=selected,
    )

    matches: list[dict[str, Any]] = []
    for rule, reg in zip(selected, regions):
        matched = reg.anchor_line is not None
        item: dict[str, Any] = {
            "rule_id": rule["id"],
            "name": rule["name"],
            "matched": matched,
            "anchor": reg.anchor_hit if matched else None,
            "location": None,
            "rows_count": len(reg.group.rows),
            "preview": [],
            "truncated": False,
        }
        if matched:
            line_start = reg.anchor_line + 1  # 1-based 闭区间（契约 §11.3）
            line_end = reg.end_line if reg.end_line is not None else line_start
            item["location"] = {
                "line_start": line_start,
                "line_end": line_end,
                "page_start": _page_for_line(line_start, parsed.pages),
                "page_end": _page_for_line(line_end, parsed.pages),
            }
            preview, truncated = _preview_rows(reg.group.rows)
            item["preview"] = preview
            item["truncated"] = truncated
        else:
            anchors = [a["text"] for a in (rule.get("anchors") or [])]
            item["reason"] = "全文未找到：" + " / ".join(anchors)
        matches.append(item)

    result = {
        "document": {"id": doc.id, "filename": doc.filename, "kind": doc.kind},
        "rule_set_version": cur,
        "ruleset_hash": "",
        "guard": guard,
        "matches": matches,
    }
    # 可观测性日志（§11.7 允许）：仅审计留痕，无业务态变更
    audit("preview_executed", doc_id=doc.id,
          detail=f"ruleset v{cur} · 规则 {len(selected)} 条 · 命中 {sum(1 for m in matches if m['matched'])}")
    return result
