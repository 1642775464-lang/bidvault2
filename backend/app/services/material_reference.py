"""E-2C：素材引用闭环服务（E-0 契约 §6 + 项目负责人 C1-C4 裁决）。

- C1 引用模型：material_references 台账（outline_node_id/anchor_hash/purpose/status）；
- C2 插入：复用 C-3a/C-3b/C-4 链（WorkspaceMutationService，append_end 引擎唯一模式，
  禁止修改引擎）；插入后同提交点写工作区投影，被占用时候选落 中转/待写回；
- C3 漂移：按 E-2A outline 漂移模型刷新 台账状态（正常/漂移/丢失）+ 重定位；
- C4 写回重试：pending_writeback 候选在文件可写时由用户确认重试。

安全：全部 ORM 参数绑定；不引入编辑能力。
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .document_outline import get_or_build
from .workspace_fs import active_root, safe_join


class ReferenceError_(Exception):
    """引用操作失败（message 可直接展示）。"""


def _ws_service(session: Session) -> Any:
    from .office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )
    from .ooxml_insert import OOXMLInsertEngine
    from .workspace_mutation import WorkspaceMutationService

    def _dispatch(od_id: str, version_id: str) -> None:
        import threading

        from ..models import OfficeDocument, OfficeDocumentVersion

        def _run() -> None:
            from sqlalchemy.orm import sessionmaker

            from ..db import get_engine

            ses = sessionmaker(bind=get_engine(), expire_on_commit=False)()
            try:
                od = ses.get(OfficeDocument, od_id)
                v = ses.get(OfficeDocumentVersion, version_id)
                if od is not None and v is not None:
                    from .office_document_svc import dispatch_post_save

                    dispatch_post_save(od, v)
            except Exception:  # noqa: BLE001
                pass
            finally:
                ses.close()

        threading.Thread(target=_run, daemon=True).start()

    return WorkspaceMutationService(
        session=session,
        active_edit_probe=lambda doc_id: False,  # 引用插入不经编辑会话；锁保护由路由层探针承担
        engine=OOXMLInsertEngine(),
        dispatch_fn=lambda od_id, version_id: _dispatch(od_id, version_id),
    )


def ensure_material_od(session: Session, material: Any) -> str:
    """素材若无 OfficeDocument 档案（E-1B 导入件），以 files_dir 规范副本建档 v1。"""
    from ..config import get_settings
    from ..models import OfficeDocument, OfficeDocumentVersion

    if material.office_document_id:
        return material.office_document_id
    src = get_settings().files_dir / (material.file_name or "")
    if not src.is_file():
        raise ReferenceError_(f"素材规范副本缺失：{material.file_name}")
    od = OfficeDocument(document_type="material", title=material.title[:200],
                        file_type=Path(material.file_name).suffix.lower().lstrip("."))
    session.add(od)
    session.flush()
    stored = f"material_{od.id[:8]}_{uuid.uuid4().hex[:6]}{Path(material.file_name).suffix.lower()}"
    data = src.read_bytes()
    (get_settings().files_dir / stored).write_bytes(data)
    v = OfficeDocumentVersion(
        id=uuid.uuid4().hex, office_document_id=od.id, version=1,
        file_path=stored,
        file_hash=__import__("hashlib").sha256(data).hexdigest(),
        file_size=len(data), source="extract", created_at=None,
    )
    _ = v.created_at
    session.add(v)
    od.current_version_id = v.id
    material.office_document_id = od.id
    session.commit()
    return od.id


def insert_reference(session: Session, *, ws_root: Path, target_oid: str, node_id: str | None,
                     material_id: str, purpose: str, project_id: str | None,
                     insert_mode: str = "chapter") -> dict[str, Any]:
    """E-2C/V3-1 阅读器 [引用]：素材 → 目标章节（chapter=章节末尾精准插入 / append_end=文档末尾）
    → 新版本（C-3a/C-3b 链）→ 台账 + 全量引用统一重估 + 投影写回。"""
    from ..models import (
        Material,
        MaterialReference,
        OfficeDocument,
        OfficeDocumentVersion,
        Project,
    )

    target = session.get(OfficeDocument, target_oid)
    if target is None:
        raise ReferenceError_("目标文档不存在")
    mat = session.get(Material, material_id)
    if mat is None:
        raise ReferenceError_("素材不存在")
    mat_oid = ensure_material_od(session, mat)

    base = (session.query(OfficeDocumentVersion)
            .filter(OfficeDocumentVersion.id == target.current_version_id).first() if target.current_version_id else None)

    # 章节定位（服务端权威构建）：node_id 必须命中当前树；不命中→要求刷新重选（同名不自动误配）
    node = node_title = boundary = None
    tree = None
    if insert_mode == "chapter":
        if not node_id:
            raise ReferenceError_("请先选择目标章节")
        tree = _outline_for_target(session, target, ws_root)
        node = next((n for n in tree["nodes"] if n["id"] == node_id), None)
        if node is None:
            raise ReferenceError_("章节节点不存在或已更新，请刷新章节树重新选择")
        after = tree["nodes"][node["order"] + 1:]
        boundary = next((n for n in after if n["level"] <= node["level"]), None)
        node_title = node["title"]

    svc = _ws_service(session)
    req = __import__("app.services.workspace_mutation", fromlist=["MaterialInsertRequest"]).MaterialInsertRequest(
        workspace_office_document_id=target_oid,
        base_workspace_version_id=base.id if base else "",
        material_id=mat.id,
        material_office_document_id=mat_oid,
        material_version_id=(session.query(OfficeDocumentVersion)
                             .filter(OfficeDocumentVersion.office_document_id == mat_oid)
                             .order_by(OfficeDocumentVersion.version.desc()).first().id),
        insert_mode=insert_mode,
        target_locator=({
            "type": "chapter", "node_id": node["id"], "title": node["title"],
            "anchor_hash": node["anchor_hash"], "para_index": node["para_index"],
            "boundary_para_index": boundary["para_index"] if boundary else None,
            "boundary_title": boundary["title"] if boundary else None,
            "position": "section_end",
        } if insert_mode == "chapter" else None),
    )
    result = svc.insert_material(req)
    new_version_id = result["version_id"]

    proj = (session.query(Project)
            .filter(Project.workspace_office_document_id == target_oid).first())
    project_id_eff = project_id or (proj.id if proj else None)
    ref = MaterialReference(
        project_id=project_id_eff, source_material_id=mat.id,
        source_version_id=req.material_version_id if hasattr(req, "material_version_id") else None,
        target_document_id=target_oid, target_version_id=new_version_id,
        outline_node_id=node_id, anchor_hash=node["anchor_hash"] if node else None,
        node_title=node_title, purpose=(purpose or "")[:100], status="active",
        insert_mode=insert_mode,
    )
    session.add(ref)
    session.commit()

    # C4 投影写回：目标为项目工作稿时，同步 工作区/项目/<名>/投标文件/<名>.docx
    note = _sync_projection(session, ws_root, target_oid, new_version_id, proj)
    if note is not None:
        ref.status = "pending_writeback"
        ref.writeback_note = note
        session.commit()

    # V3-1 约束②：新版本生成后统一重估该文档全部受影响引用（系统写入自愈，不误报外部漂移）
    _ = MaterialReference  # 保持导入面
    reeval = _reevaluate_after_insert(session, target_oid, ws_root,
                                      base_version_id=base.id if base else None,
                                      new_version_id=new_version_id,
                                      new_ref=ref)
    _ = reeval
    return {"reference_id": ref.id, "version_id": new_version_id,
            "writeback": "ok" if note is None else "pending_writeback",
            "note": note or ""}


def _reevaluate_after_insert(session: Session, target_oid: str, ws_root: Path,
                             *, base_version_id: str | None, new_version_id: str,
                             new_ref: Any) -> dict[str, Any]:
    """V3-1 约束②：插入后对该文档全部引用统一重估。

    分类（相对 base 树）：active（锚点命中）/ drifted（标题命中锚点变）/ lost（标题消失）；
    新树落位：active → 重绑新节点 + last_system_anchor_hash=新锚点 + active（系统写入非漂移）；
    drifted → 重绑 id 保留 drifted（外部基线不变）；lost → 维持。
    """
    import json

    from ..models import MaterialReference, OfficeDocumentVersion
    from .document_outline import get_or_build
    from .office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )

    target = session.get(__import__("app.models", fromlist=["OfficeDocument"]).OfficeDocument, target_oid)
    svc = OfficeDocumentService(session=session, adapter=OfficeDocumentTableAdapter(session))

    base_tree: dict[str, Any] = {"nodes": []}
    if base_version_id:
        base_ver = session.get(OfficeDocumentVersion, base_version_id)
        if base_ver is not None:
            base_tree = get_or_build(session, target_oid, base_version_id,
                                     svc.adapter.read_file(base_ver.file_path))
    new_ver = session.get(OfficeDocumentVersion, new_version_id)
    new_tree = get_or_build(session, target_oid, new_version_id,
                            svc.adapter.read_file(new_ver.file_path))

    def _find(nodes: list[dict], *, by_id: str | None = None, by_title: str | None = None):
        if by_id:
            return next((n for n in nodes if n["id"] == by_id), None)
        hits = [n for n in nodes if by_title and n["title"] == by_title]
        return hits[0] if len(hits) == 1 else None  # 同名多候选 → None（不自动误配）

    out = []
    refs = (session.query(MaterialReference)
            .filter(MaterialReference.target_document_id == target_oid,
                    MaterialReference.status.in_(["active", "drifted", "pending_writeback"])).all())
    for ref in refs:
        pre_node = next((n for n in base_tree["nodes"] if n["id"] == ref.outline_node_id), None)
        pre_active = pre_node is not None and pre_node["anchor_hash"] == ref.anchor_hash
        title = ref.node_title or (pre_node["title"] if pre_node else "")
        post_node = _find(new_tree["nodes"], by_title=title) if title else None

        if post_node is None:
            ref.outline_node_id = None
            ref.status = "lost"
            out.append({"reference_id": ref.id, "status": "lost"})
            continue
        ref.outline_node_id = post_node["id"]
        if pre_active:
            # 系统写入自愈：重绑新锚点（系统写入不产生 drifted）
            ref.anchor_hash = post_node["anchor_hash"]
            ref.last_system_anchor_hash = post_node["anchor_hash"]
            ref.status = "active" if ref.status != "pending_writeback" else "pending_writeback"
        else:
            ref.status = "drifted"  # 外部漂移基线保持，提示用户重定位
        out.append({"reference_id": ref.id, "status": ref.status})
    _ = json
    session.commit()
    return {"items": out}


def _sync_projection(session: Session, ws_root: Path, target_oid: str,
                     new_version_id: str, proj: Any) -> str | None:
    """投影写回（D-2A v1.1 DB 先行协议）：
    ① 事务A：projection_state(pending, target_hash) 先提交（写前意图持久化）
    ② os.replace 原子替换投影
    ③ 事务B：标 synced
    被占用 → 撤销意图、候选落 中转/待写回（C4）。"""
    import hashlib

    from ..models import OfficeDocumentVersion, ProjectionState
    from ..services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )

    if proj is None or not proj.workspace_rel:
        return None
    svc = OfficeDocumentService(session=session, adapter=OfficeDocumentTableAdapter(session))
    v = session.get(OfficeDocumentVersion, new_version_id)
    data = svc.adapter.read_file(v.file_path)
    folder = safe_join(ws_root, f"{proj.workspace_rel}/投标文件")
    folder.mkdir(parents=True, exist_ok=True)
    name = (proj.name or "工作稿") + ".docx"
    dest = folder / name
    target_hash = hashlib.sha256(data).hexdigest()

    # ① 写前意图持久化（崩溃窗口：pending+投影旧 → 启动恢复重投影）
    intent = ProjectionState(workspace_id=proj.id, rel_path=dest.relative_to(ws_root).as_posix(),
                             oid=target_oid, target_hash=target_hash, state="pending")
    session.add(intent)
    session.commit()
    try:
        # ② 原子替换
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(str(tmp), str(dest))
    except PermissionError:
        session.rollback()  # 撤销意图；候选落中转（C4）
        cand = safe_join(ws_root, f"中转/待写回/{target_oid[:8]}-{new_version_id[:8]}.docx")
        cand.parent.mkdir(parents=True, exist_ok=True)
        cand.write_bytes(data)
        return f"目标文件被占用，新版本候选已存 {cand.name}，请关闭文件后确认写回"
    # ③ 标 synced
    intent.state = "synced"
    session.commit()
    return None


def retry_writeback(session: Session, ref: Any, ws_root: Path) -> dict[str, Any]:
    """C4：占用候选重试写回（用户关闭文件后确认）。"""
    from ..models import OfficeDocumentVersion
    from ..services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )

    if ref.status != "pending_writeback":
        return {"ok": False, "detail": "该引用无待写回候选"}
    from ..models import Project

    proj = (session.query(Project)
            .filter(Project.workspace_office_document_id == ref.target_document_id).first())
    if proj is None or not proj.workspace_rel:
        return {"ok": False, "detail": "目标未绑定工作区目录"}
    svc = OfficeDocumentService(session=session, adapter=OfficeDocumentTableAdapter(session))
    v = session.get(OfficeDocumentVersion, ref.target_version_id)
    data = svc.adapter.read_file(v.file_path)
    folder = safe_join(ws_root, f"{proj.workspace_rel}/投标文件")
    dest = folder / ((proj.name or "工作稿") + ".docx")
    try:
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(str(tmp), str(dest))
    except PermissionError:
        return {"ok": False, "detail": "文件仍被占用，请先关闭 Word/WPS"}
    ref.status = "active"
    ref.writeback_note = None
    cand = safe_join(ws_root, f"中转/待写回/{ref.target_document_id[:8]}-{ref.target_version_id[:8]}.docx")
    if cand.is_file():
        cand.unlink()
    session.commit()
    return {"ok": True}


def refresh_drift(session: Session, target_oid: str, ws_root: Path) -> dict[str, Any]:
    """C3：按当前版本 outline 刷新该文档全部引用状态（正常/漂移/丢失）。

    判定顺序：outline_node_id 命中 → 锚点一致=正常 / 不一致=漂移；
    id 未命中（树已重建换 id）→ 锚点一致=正常 → 标题一致=漂移（并回填新 id）→ 否则丢失。
    """
    from ..models import MaterialReference, OfficeDocument

    target = session.get(OfficeDocument, target_oid)
    tree = _outline_for_target(session, target, ws_root)
    by_id = {n["id"]: n for n in tree["nodes"]}
    out = []
    refs = (session.query(MaterialReference)
            .filter(MaterialReference.target_document_id == target_oid,
                    MaterialReference.status.in_(["active", "drifted", "pending_writeback"])).all())
    for r in refs:
        node = by_id.get(r.outline_node_id or "")
        if node is not None:
            r.status = "active" if node["anchor_hash"] == r.anchor_hash else "drifted"
            if node["anchor_hash"] == r.anchor_hash:
                r.outline_node_id = node["id"]
        else:
            by_title = [n for n in tree["nodes"]
                        if r.node_title and n["title"] == r.node_title]
            if by_title:
                r.outline_node_id = by_title[0]["id"]
                r.anchor_hash = by_title[0]["anchor_hash"]
                r.status = "drifted"
            else:
                r.status = "lost"
        out.append({"reference_id": r.id, "status": r.status,
                    "purpose": r.purpose, "node_title": r.node_title or ""})
    session.commit()
    return {"items": out}


def relocate(session: Session, ref: Any, new_node_id: str, ws_root: Path) -> dict[str, Any]:
    """漂移重定位：引用改指当前树的新节点。"""
    from ..models import OfficeDocument

    target = session.get(OfficeDocument, ref.target_document_id)
    tree = _outline_for_target(session, target, ws_root)
    node = next((n for n in tree["nodes"] if n["id"] == new_node_id), None)
    if node is None:
        raise ReferenceError_("新章节节点不存在")
    ref.outline_node_id = node["id"]
    ref.anchor_hash = node["anchor_hash"]
    ref.status = "active"
    session.commit()
    return {"ok": True, "node_title": node["title"]}


def _outline_for_target(session: Session, target: Any, ws_root: Path) -> dict[str, Any]:
    from ..models import OfficeDocumentVersion
    from ..services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )

    svc = OfficeDocumentService(session=session, adapter=OfficeDocumentTableAdapter(session))
    cur = svc.get_current_version(target)
    if cur is None:
        raise ReferenceError_("目标文档尚无版本")
    data = svc.adapter.read_file(cur.file_path)
    return get_or_build(session, target.id, cur.id, data)


def _version_file(session: Session, version_id: str) -> bytes:
    """读版本字节（引擎输入需要文件对象包装前的 bytes）。"""
    from ..models import OfficeDocumentVersion
    from ..services.office_document_svc import (
        OfficeDocumentService,
        OfficeDocumentTableAdapter,
    )

    s = session if isinstance(session, Session) else None
    v = session.get(OfficeDocumentVersion, version_id)
    svc = OfficeDocumentService(session=session, adapter=OfficeDocumentTableAdapter(session))
    return svc.adapter.read_file(v.file_path)
