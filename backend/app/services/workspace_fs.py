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
