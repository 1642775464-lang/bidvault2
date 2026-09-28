"""D-2B：投影写回 DB 先行协议 + 启动对账（D-2A 契约 §6 v1.1 冻结）。

写回四步（顺序冻结）：
  ① SQLite 事务A：projection_state(pending, target_hash) 与版本记录一并提交
  ② os.replace 投影文件（原子）
  ③ SQLite 事务B：state → synced
崩溃窗口枚举：①后②前 → pending+投影旧 → 启动恢复重投影；②后③前 → pending+投影新
(hash==target) → 启动恢复补标 synced。"投影新+DB 旧"协议下不可达。

外部修改保护：对账发现投影 hash ≠ DB 当前版本 hash 且无 pending 意图 → 视为用户
未经确认的外部修改，保留原件并落 workspace_event 候选（不覆盖）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .workspace_fs import file_hash


def record_intent(session: Session, workspace_id: str, rel_path: str, oid: str,
                  target_hash: str) -> Any:
    """写前意图持久化（事务A）。"""
    from ..models import ProjectionState

    row = ProjectionState(workspace_id=workspace_id, rel_path=rel_path, oid=oid,
                          target_hash=target_hash, state="pending")
    session.add(row)
    session.commit()
    return row


def mark_synced(session: Session, row: Any) -> None:
    row.state = "synced"
    session.commit()


def reconcile_projections(session: Session, workspace_id: str, ws_root: Path,
                          files_dir: Path) -> dict[str, Any]:
    """启动对账：逐条 pending 意图按 D-2A 协议收口；外部修改保留原件落事件。"""
    from ..models import OfficeDocument, OfficeDocumentVersion, ProjectionState, WorkspaceEvent

    rows = (session.query(ProjectionState)
            .filter(ProjectionState.workspace_id == workspace_id,
                    ProjectionState.state == "pending").all())
    resynced = marked_synced = external = 0
    for row in rows:
        dest = ws_root / row.rel_path
        od = session.get(OfficeDocument, row.oid)
        cur = (session.query(OfficeDocumentVersion)
               .filter(OfficeDocumentVersion.id == od.current_version_id).first()
               if od is not None and od.current_version_id else None)
        if cur is None:
            continue
        svc_data = files_dir / cur.file_path
        if not svc_data.is_file():
            continue
        desired = svc_data.read_bytes()
        cur_hash = __import__("hashlib").sha256(desired).hexdigest()

        existing = dest.read_bytes() if dest.is_file() else None
        if existing == desired:
            row.state = "synced"
            marked_synced += 1
        elif existing is not None and existing != desired:
            # 外部修改保护：保留用户原件，落事件候选（E-1A 确认流）
            ev = WorkspaceEvent(workspace_id=workspace_id, rel_path=row.rel_path,
                                kind="modified", file_hash=cur_hash, status="detected",
                                detail="启动对账：工作区文件与内部版本不一致（外部修改候选）")
            session.add(ev)
            external += 1
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            tmp.write_bytes(desired)
            os.replace(str(tmp), str(dest))
            row.state = "synced"
            resynced += 1
    session.commit()
    return {"pending": len(rows), "resynced": resynced, "marked_synced": marked_synced,
            "external_candidates": external}
