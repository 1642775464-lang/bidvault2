"""E-1A：标书工作区文件区服务（E-0 契约 §1/§7）。

职责：工作区创建（目录骨架 + .bidvault 绑定清单 + DB 注册）、骨架修复、
路径合法性与忽略规则、相对路径换算。投影模型：文件夹=当前版本文件（用户
真实资产），DB=索引与分析结果；系统写入面收敛到 输出文件/拆解结果/.bidvault/
与工作稿投影。

安全：路径全部做规范化与包含校验；workspace.json 只写非敏感元数据。
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import WorkspaceRegistry

SCHEMA_VERSION = 1

# 骨架目录（E-0 契约 §1.1 v1.1）
SKELETON_DIRS = (
    "项目",
    "素材库/企业资质",
    "素材库/人员简历",
    "素材库/项目案例",
    "素材库/技术方案",
    "中转",
)


class WorkspaceError(Exception):
    """工作区操作失败（4xx 语义，message 可直接展示）。"""


def sanitize_dir_name(name: str) -> str:
    """目录名清洗：Windows 非法字符替换、去首尾空格与点、限长。"""
    cleaned = re.sub(r'[\\/:*?"<>|]', "-", (name or "").strip())
    cleaned = cleaned.strip(" .")
    return cleaned[:100]


def is_ignored_rel(rel_posix: str) -> bool:
    """监视忽略规则（E-0 §7）：Word 锁文件/临时文件/.bidvault/隐藏项。"""
    parts = Path(rel_posix).parts
    if not parts:
        return True
    name = parts[-1]
    if name.startswith("~$") or name.endswith(".tmp"):
        return True
    return any(p.startswith(".") for p in parts)


def rel_of(root: Path, target: Path) -> str:
    """相对工作区根的 POSIX 路径；越界抛错。"""
    root_r, target_r = root.resolve(), target.resolve()
    if target_r == root_r:
        return ""
    if root_r not in target_r.parents:
        raise WorkspaceError(f"路径越出工作区：{target}")
    return target_r.relative_to(root_r).as_posix()


def _assert_safe_root(root: Path, data_dir: Path) -> None:
    """根路径安全校验：不得在应用数据目录/后端目录内，不得是盘根或系统目录。"""
    root_r = root.resolve()
    if root_r.drive and root_r == Path(root_r.anchor):
        raise WorkspaceError("工作区不能位于盘根目录，请选择一个专用文件夹")
    for guarded in (data_dir.resolve(), Path(__file__).resolve().parents[2]):
        try:
            root_r.relative_to(guarded)
            raise WorkspaceError(f"工作区不能位于系统/应用目录内：{guarded}")
        except ValueError:
            continue
    protected_names = {"windows", "program files", "program files (x86)", "system volume information"}
    if any(p.lower() in protected_names for p in root_r.parts):
        raise WorkspaceError("工作区不能位于系统保护目录内")


def _write_manifest(root: Path, workspace_id: str, name: str) -> None:
    meta_dir = root / ".bidvault"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "workspace.json").write_text(
        json.dumps({
            "schema_version": SCHEMA_VERSION,
            "workspace_id": workspace_id,
            "name": name,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def ensure_skeleton(root: Path) -> list[str]:
    """幂等补齐骨架目录，返回本次补建的相对路径（用于 UI 反馈）。"""
    repaired: list[str] = []
    for rel in SKELETON_DIRS:
        d = root / rel
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            repaired.append(rel)
    (root / ".bidvault").mkdir(parents=True, exist_ok=True)
    return repaired


def create_workspace(session: Session, root_input: str, name: str = "", data_dir: Path | None = None) -> WorkspaceRegistry:
    """创建工作区：校验 → 骨架 → 绑定清单 → DB 注册（单工作区：置活其余）。

    幂等护栏：已有 active 工作区时拒绝（409 语义由路由层翻译）。
    """
    from ..config import get_settings

    root = Path(root_input).expanduser()
    if not root.is_absolute():
        raise WorkspaceError("请选择绝对路径（如 D:\\标书工作区）")
    _assert_safe_root(root, data_dir or get_settings().data_dir)
    active = session.query(WorkspaceRegistry).filter(WorkspaceRegistry.active == 1).first()
    if active is not None:
        raise WorkspaceError(f"已存在活动工作区：{active.root_path}（换绑请走迁移向导）")
    if any(root.resolve() in Path(w.root_path).resolve().parents or Path(w.root_path).resolve() in root.resolve().parents
           for w in session.query(WorkspaceRegistry).all()):
        raise WorkspaceError("新工作区与既有工作区路径嵌套")

    root.mkdir(parents=True, exist_ok=True)
    wid = uuid.uuid4().hex
    safe_name = sanitize_dir_name(name or root.name)
    ensure_skeleton(root)
    _write_manifest(root, wid, safe_name)

    row = WorkspaceRegistry(id=wid, root_path=str(root.resolve()), name=safe_name,
                            schema_version=SCHEMA_VERSION, active=1)
    session.add(row)
    session.commit()
    return row


def get_active(session: Session) -> WorkspaceRegistry | None:
    return session.query(WorkspaceRegistry).filter(WorkspaceRegistry.active == 1).first()


def skeleton_status(root: Path) -> tuple[bool, list[str]]:
    """检查骨架完整性并幂等修复，返回 (完整, 本次修复列表)。"""
    repaired = ensure_skeleton(root)
    ok = all((root / rel).is_dir() for rel in SKELETON_DIRS)
    return ok, repaired


def read_manifest(root: Path) -> dict | None:
    f = root / ".bidvault" / "workspace.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def file_hash(path: Path) -> str | None:
    import hashlib

    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def deactivate_all(session: Session) -> None:
    for w in session.query(WorkspaceRegistry).filter(WorkspaceRegistry.active == 1).all():
        w.active = 0
    session.commit()


def remove_workspace_tree(root: Path) -> None:
    """仅删除本服务创建的空骨架（用于测试/回滚）；非空目录原样保留。"""
    for rel in reversed(SKELETON_DIRS):
        try:
            (root / rel).rmdir()
        except OSError:
            pass
    shutil.rmtree(root / ".bidvault", ignore_errors=True)


# ---------------- E-1D：项目文件夹 ----------------

PROJECT_SUBDIRS = ("招标文件", "投标文件", "输出文件")


def safe_join(base: Path, rel: str) -> Path:
    """把相对路径拼到 base 下，并校验结果仍在 base 内（拒绝绝对路径/盘符/../）。"""
    rel_posix = rel.replace("\\", "/")
    if rel_posix.startswith("/") or re.match(r"^[A-Za-z]:", rel_posix) or ".." in Path(rel_posix).parts:
        raise WorkspaceError(f"非法路径：{rel}")
    base_r = base.resolve()
    target = (base_r / rel_posix).resolve()
    if target != base_r and base_r not in target.parents:
        raise WorkspaceError(f"路径越出目标目录：{rel}")
    return target


def active_root(session: Session) -> Path | None:
    """活动工作区根（无工作区返回 None）。"""
    row = get_active(session)
    return Path(row.root_path) if row is not None else None


def ensure_project_folder(session: Session, project_name: str,
                          existing_rel: str | None = None) -> tuple[str, Path] | None:
    """为项目创建/补齐工作区投影目录（幂等）：项目/<名>/{招标文件,投标文件,输出文件}。

    existing_rel：项目已绑定的相对路径——直接补齐子目录，不加后缀。
    新建时撞非空目录 → -2 后缀（不复用他人目录）。无活动工作区返回 None。
    """
    root = active_root(session)
    if root is None:
        return None
    base = sanitize_dir_name(project_name) or "未命名项目"
    if existing_rel:
        rel = existing_rel
        folder = root / rel
        folder.mkdir(parents=True, exist_ok=True)
    else:
        rel = f"项目/{base}"
        folder = root / rel
        i = 2
        while folder.exists() and any(folder.iterdir()):
            rel = f"项目/{base}-{i}"
            folder = root / rel
            i += 1
    for sub in ("", *PROJECT_SUBDIRS):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    return rel, folder


def _unique_dest(folder: Path, name: str) -> Path:
    dest = folder / name
    stem, ext = dest.stem, dest.suffix
    i = 2
    while dest.exists():
        dest = folder / f"{stem}-{i}{ext}"
        i += 1
    return dest


def project_upload_projection(session: Session, project_rel: str, kind_dir: str,
                              filename: str, data: bytes) -> str:
    """上传投影：把上传文件字节写入项目子文件夹（唯一名），返回相对路径。"""
    root = active_root(session)
    if root is None:
        raise WorkspaceError("无活动工作区")
    folder = safe_join(root, f"{project_rel}/{kind_dir}")
    folder.mkdir(parents=True, exist_ok=True)
    src_name = Path(filename)
    safe_stem = sanitize_dir_name(src_name.stem) or "未命名"
    dest = _unique_dest(folder, safe_stem + src_name.suffix.lower())
    dest.write_bytes(data)
    return dest.relative_to(root).as_posix()
