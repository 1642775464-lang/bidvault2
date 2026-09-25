"""E-1B：历史资料导入服务（E-0 契约 §2/§3，流程经项目负责人冻结）。

流程：源文件/压缩包 → 中转暂存（解包安全检查）→ 扫描+hash → 本地规则分类建议
→ 【用户确认】→ 移动归档 → 注册建档 → 触发拆解（招标自动/投标询问式）。

边界（项目负责人裁决，必须保持）：
- 不 AI 自动移动文件；不自动删除重复文件；不自动覆盖已有项目；不自动拆解全部历史材料。
- 系统建议 → 用户确认 → 执行。

安全：
- zip 解包防炸（条目数/解压比/单文件上限）与防穿越（safe_join 预校验 + zipfile.extract 官方净化提取）；
- 分类不启用任何 XML 解析器实体展开（classifier 用去标签正则）；
- 数据库全部参数绑定（ORM）。
"""

from __future__ import annotations

import re
import shutil
import uuid
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import (
    ClassificationLog,
    Document,
    ImportBatch,
    ImportItem,
    Material,
)
from .import_classifier import classify_import, _pdf_text, _strip_docx_text
from .workspace_fs import is_ignored_rel, sanitize_dir_name

MAX_ENTRIES = 10_000
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 2 * 1024 * 1024 * 1024
RATIO_LIMIT = 100
RATIO_FLOOR_BYTES = 10 * 1024 * 1024  # 小压缩包不适用解压比

ALLOWED_EXTS = {".zip", ".docx", ".doc", ".pdf", ".xlsx", ".xls", ".csv", ".txt",
                ".jpg", ".jpeg", ".png", ".bmp"}


class ImportError_(Exception):
    """导入失败（message 可直接展示）。"""


def safe_join(base: Path, rel: str) -> Path:
    """把相对路径拼到 base 下，并校验结果仍在 base 内（拒绝绝对路径/盘符/../）。"""
    rel_posix = rel.replace("\\", "/")
    if rel_posix.startswith("/") or re.match(r"^[A-Za-z]:", rel_posix) or ".." in Path(rel_posix).parts:
        raise ValueError(f"非法路径条目：{rel}")
    base_r = base.resolve()
    target = (base_r / rel_posix).resolve()
    if target != base_r and base_r not in target.parents:
        raise ValueError(f"路径越出目标目录：{rel}")
    return target


# ---------------- 批次创建：暂存 + 扫描 + 分类建议 ----------------

def create_batch(session: Session, workspace_id: str, ws_root: Path, paths: list[str],
                 label: str = "") -> ImportBatch:
    batch = ImportBatch(workspace_id=workspace_id, label=label or "历史资料导入",
                        source_desc="; ".join(Path(p).name for p in paths[:5]))
    session.add(batch)
    session.commit()

    staging = safe_join(ws_root, f"中转/导入-{batch.id[:8]}")
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for raw in paths:
            src = Path(raw)
            if not src.is_file():
                raise ImportError_(f"源文件不存在：{src}")
            if src.suffix.lower() not in ALLOWED_EXTS:
                raise ImportError_(f"不支持的类型：{src.name}")
            if src.suffix.lower() == ".zip":
                try:
                    _extract_zip_guarded(src, staging)
                except ValueError as e:  # zip-slip 等可疑条目 → 400 语义
                    raise ImportError_(str(e)) from e
            else:
                _copy_guarded(src, safe_join(staging, src.name))
    except Exception as e:
        batch.status = "failed"
        batch.error = str(e)
        session.commit()
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _scan_and_suggest(session, batch, staging)
    return batch


def _extract_zip_guarded(src: Path, staging: Path) -> None:
    """zip 安全解包：防炸（条目/总量/单文件/解压比）+ 防穿越。

    两段式：先逐条 safe_join 预校验（任何可疑条目整体拒绝），再统一
    zipfile.extract 官方净化提取（其本身也会剥离 .. 与绝对路径成分）。
    """
    try:
        zf = zipfile.ZipFile(src)
    except zipfile.BadZipFile as e:
        raise ImportError_(f"损坏的压缩包：{src.name}") from e
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ImportError_(f"压缩包条目过多（>{MAX_ENTRIES}）：{src.name}")
        total_unc = sum(i.file_size for i in infos)
        if total_unc > MAX_TOTAL_UNCOMPRESSED:
            raise ImportError_(f"解压总量超限（>2GB）：{src.name}")
        packed = max(src.stat().st_size, 1)
        if total_unc > RATIO_FLOOR_BYTES and total_unc / packed > RATIO_LIMIT:
            raise ImportError_(f"压缩比异常（疑似压缩炸弹）：{src.name}")
        for info in infos:
            if info.is_dir():
                continue
            if info.file_size > MAX_FILE_BYTES:
                raise ImportError_(f"压缩包内单文件超 200MB：{info.filename}")
            safe_join(staging, info.filename)  # 预校验：可疑即整体拒绝
        for info in infos:
            if info.is_dir():
                continue
            zf.extract(info, str(staging))


def _copy_guarded(src: Path, dest: Path) -> None:
    if src.stat().st_size > MAX_FILE_BYTES:
        raise ImportError_(f"单文件超 200MB：{src.name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _scan_and_suggest(session: Session, batch: ImportBatch, staging: Path) -> None:
    import json

    from .workspace_fs import file_hash

    for p in sorted(staging.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(staging).as_posix()
        if is_ignored_rel(rel):
            continue
        if p.suffix.lower() not in ALLOWED_EXTS or p.suffix.lower() == ".zip":
            continue  # zip 本体不会出现在解包结果里；未支持类型跳过
        data_hash = file_hash(p) or ""
        content = ""
        if p.suffix.lower() == ".docx":
            content = _strip_docx_text(p.read_bytes())
        elif p.suffix.lower() == ".pdf":
            content = _pdf_text(p.read_bytes())
        kind, conf, reasons = classify_import(p.name, p.suffix, content)
        reasons_json = json.dumps(reasons, ensure_ascii=False)
        session.add(ImportItem(
            batch_id=batch.id, workspace_id=batch.workspace_id,
            file_name=p.name, staging_rel=f"中转/导入-{batch.id[:8]}/{rel}",
            file_hash=data_hash, size=p.stat().st_size, ext=p.suffix.lower(),
            kind_suggested=kind, confidence=conf, reasons_json=reasons_json,
            status="classified",
        ))
        session.add(ClassificationLog(
            file_hash=data_hash, kind=kind, confidence=conf,
            reasons_json=reasons_json, decided_by="rule"))
    session.commit()


# ---------------- 用户确认：归档 + 注册建档 ----------------

def _unique_dest(folder: Path, name: str) -> Path:
    dest = folder / name
    stem, ext = dest.stem, dest.suffix
    i = 2
    while dest.exists():
        dest = folder / f"{stem}-{i}{ext}"
        i += 1
    return dest


def _archive_project_doc(session: Session, ws_root: Path, item: ImportItem,
                         kind: str, project_name: str, decompose: bool) -> str:
    """招标/投标：移动到项目子文件夹并注册 Document（source=path）。

    招标：归档即入队拆解；投标：decompose=True 才入队（询问式），否则仅归档。
    """
    sub = "招标文件" if kind == "tender" else "投标文件"
    folder = safe_join(ws_root, f"项目/{sanitize_dir_name(project_name)}/{sub}")
    folder.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(folder, item.file_name)
    shutil.move(str(safe_join(ws_root, item.staging_rel)), str(dest))

    doc: Document | None = None
    if kind == "tender" or decompose:
        doc = Document(
            filename=dest.name, kind="tender" if kind == "tender" else "bid",
            source="path", origin_path=str(dest), file_type=dest.suffix.lower().lstrip("."),
            size=item.size,
        )
        session.add(doc)
        session.flush()  # 取 id；status=0 由既有队列拾取拆解（OCR 阻塞态自动跳过）
    item.decided_kind = kind
    item.target_rel = dest.relative_to(ws_root).as_posix()
    item.doc_id = doc.id if doc is not None else None
    item.status = "archived"
    return item.target_rel


# E-0 契约 §1.1 素材库文件夹（用户可见）↔ 内部 MATERIAL_CATS（数据库词表）
# 契约文件夹是冻结协议；MATERIAL_CATS 是既有内部词表——双向映射，不改动既有枚举。
MATERIAL_FOLDERS = {
    "企业资质": "资质文件",
    "人员简历": "人员团队",
    "项目案例": "业绩案例",
    "技术方案": "技术方案",
}
FOLDER_BY_CAT = {v: k for k, v in MATERIAL_FOLDERS.items()}


def _resolve_material_cat(cat: str) -> tuple[str, str]:
    """返回 (内部分类, 文件夹名)。接受契约文件夹名或内部词表值。"""
    cat = (cat or "").strip()
    if cat in MATERIAL_FOLDERS:
        return MATERIAL_FOLDERS[cat], cat
    if cat in MATERIAL_CATS:
        return cat, FOLDER_BY_CAT.get(cat, "企业资质")
    return "资质文件", "企业资质"


def _archive_material(session: Session, ws_root: Path, item: ImportItem,
                      cat: str, settings_files_dir: Path) -> str:
    """素材：投影副本落素材库/<契约文件夹>/，规范副本按既有纪律存 files_dir 并建 Material。"""
    from ..models import MATERIAL_CATS

    internal_cat, folder_name = _resolve_material_cat(cat)
    folder = safe_join(ws_root, f"素材库/{folder_name}")
    folder.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(folder, item.file_name)
    shutil.move(str(safe_join(ws_root, item.staging_rel)), str(dest))

    stored = uuid.uuid4().hex + dest.suffix.lower()
    shutil.copy2(dest, settings_files_dir / stored)
    _ = MATERIAL_CATS
    mat = Material(
        title=dest.stem[:300],
        type="图片" if dest.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp"} else "附件",
        cat=internal_cat,
        src="手动上传",
        text=None,
        file_name=stored,
        file_size=f"{item.size / 1024:.0f} KB",
    )
    session.add(mat)
    session.flush()
    item.decided_kind = "material"
    item.target_rel = dest.relative_to(ws_root).as_posix()
    item.material_id = mat.id
    item.status = "archived"
    return item.target_rel


def confirm_batch(session: Session, ws_root: Path, batch: ImportBatch,
                  decisions: list[dict], settings_files_dir: Path) -> list[dict]:
    """按用户裁决归档。返回逐条结果。重复/未知不移动（留在中转）。"""
    from ..models import OfficeDocumentVersion

    batch.status = "archiving"
    session.commit()
    results: list[dict] = []
    seen_hashes: set[str] = set()
    try:
        for d in decisions:
            item = session.get(ImportItem, d.get("item_id", ""))
            if item is None or item.batch_id != batch.id:
                results.append({"item_id": d.get("item_id", ""), "status": "failed", "error": "条目不存在"})
                continue
            if item.status == "duplicate":
                results.append({"item_id": item.id, "status": "duplicate"})
                continue
            kind = d.get("kind") or item.kind_suggested
            if item.file_hash in seen_hashes:
                item.status = "duplicate"
                item.error = "批次内 hash 重复"
                results.append({"item_id": item.id, "status": "duplicate"})
                continue
            dup = (session.query(OfficeDocumentVersion)
                   .filter(OfficeDocumentVersion.file_hash == item.file_hash).first())
            prev = (session.query(ImportItem)
                    .filter(ImportItem.status == "archived",
                            ImportItem.file_hash == item.file_hash,
                            ImportItem.batch_id != batch.id)
                    .first())
            if (dup is not None or prev is not None) and d.get("allow_duplicate") is not True:
                item.status = "duplicate"
                item.error = (f"与既有版本重复（v{dup.version}）" if dup is not None
                              else f"与已归档文件重复（{prev.file_name}）")
                results.append({"item_id": item.id, "status": "duplicate"})
                continue
            seen_hashes.add(item.file_hash)
            try:
                if kind in ("tender", "bid"):
                    project = d.get("project_name") or "未命名项目"
                    target = _archive_project_doc(session, ws_root, item, kind, project,
                                                  bool(d.get("decompose")))
                elif kind == "material":
                    target = _archive_material(session, ws_root, item,
                                               d.get("material_cat") or "",
                                               settings_files_dir)
                else:
                    item.status = "classified"  # unknown：留在中转等人工
                    results.append({"item_id": item.id, "status": "skipped_unknown"})
                    continue
                session.add(ClassificationLog(
                    file_hash=item.file_hash, kind=kind,
                    confidence=item.confidence, reasons_json=None, decided_by="user"))
                results.append({"item_id": item.id, "status": "archived",
                                "target": target, "doc_id": item.doc_id,
                                "material_id": item.material_id})
            except Exception as e:  # noqa: BLE001
                item.status = "failed"
                item.error = str(e)[:500]
                results.append({"item_id": item.id, "status": "failed", "error": str(e)[:200]})
        session.commit()
        batch.status = "done"
        session.commit()
        return results
    except Exception as e:
        batch.status = "failed"
        batch.error = str(e)[:500]
        session.commit()
        raise
