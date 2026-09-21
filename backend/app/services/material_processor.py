"""Material 文档处理器（阶段六 B-2）：Material DOCX 保存后更新素材派生数据。

职责边界（任务单五）：
- parse Material DOCX → Material.text / Material.rows_json（结构化预览/AI 分析/搜索用）
- 素材搜索的 q 过滤覆盖 text（LIKE，无独立全文索引——素材现无 chunks 索引）
禁止：
- 触发招标文件 ExtractItem 全拆解 / 废标条件 / 评分项 / source material extraction /
  Source content_text ——那些属于 SourceDocumentProcessor（source 链）。

幂等：dispatch 层以 (office_document_version_id, material) 抢占（processor_runs 表）；
本 processor 自身为全量覆盖式更新，即使重复执行结果也一致（双保险）。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings

logger = logging.getLogger(__name__)


def _parse_material_docx(data: bytes) -> tuple[str, list[list[str]]]:
    """Material DOCX → (text, rows_json 行)。

    段落 → text 行；表格 → V2 模型行转键值行（多列行首列起拼接为字段、末列为值，
    与 source 落库的检索兼容语义一致），空单元格/合并 continuation 不产出行。
    """
    from ..services.ingest.parser import parse_docx

    parsed = parse_docx(data)
    text_lines: list[str] = []
    rows: list[list[str]] = []
    for tb in parsed.doc_tables:
        model = tb.model
        for row in (model["rows"] if model else []):
            vis = [c["text"].strip() for c in row["cells"] if not c.get("merged")]
            vis = [t for t in vis if t]
            if len(vis) > 1:
                rows.append([vis[0], " ".join(vis[1:])])
            elif vis:
                rows.append([vis[0], ""])
    for ln in parsed.text.split("\n"):
        s = ln.strip()
        if s:
            text_lines.append(s)
    return "\n".join(text_lines), rows


def process_material_document(session: Any, od: Any, version: Any) -> None:
    """更新 od 对应 Material 的派生数据（text/rows_json）。"""
    from ..models import Material

    mat = session.query(Material).filter(Material.office_document_id == od.id).first()
    if mat is None:
        logger.warning("material for office_document %s not found", od.id)
        return
    data = (get_settings().files_dir / version.file_path).read_bytes()
    text, rows = _parse_material_docx(data)
    mat.text = text or None
    # 派生数据完全镜像当前 DOCX：无表格则 rows_json 置空（不保留旧拆解残影）
    mat.rows_json = json_dumps(rows) if rows else None
    session.commit()
    logger.info("material derived data updated: material=%s version=%s text=%d rows=%d",
                mat.id, version.version, len(text), len(rows))


def json_dumps(rows: list[list[str]]) -> str:
    import json

    return json.dumps(rows, ensure_ascii=False)


class MaterialDocumentProcessor:
    """post-save processor：document_type = material。"""

    document_type = "material"

    def process(self, od: Any, version: Any) -> None:
        from sqlalchemy.orm import sessionmaker

        from ..db import get_engine

        # 独立会话：callback 后台线程自带会话生命周期
        factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
        ses = factory()
        try:
            process_material_document(ses, od, version)
        finally:
            ses.close()
