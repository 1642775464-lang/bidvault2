"""标库 BidVault v2 · 数据模型（对齐原型 v1.2：项目库/文档库/素材库/检测）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Project(Base):
    """项目（F1.0 项目库台账）。stage：0 技术标制作/1 商务标制作/2 合标/3 打印盖章/4 上传。"""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(10), default="主标")  # 主标 | 陪标
    industry: Mapped[str | None] = mapped_column(String(50), default=None)
    open_at: Mapped[str | None] = mapped_column(String(30), default=None)  # 开标时间
    platform: Mapped[str | None] = mapped_column(String(100), default=None)
    cust: Mapped[str | None] = mapped_column(String(300), default=None)
    contact: Mapped[str | None] = mapped_column(String(100), default=None)
    sales: Mapped[str | None] = mapped_column(String(50), default=None)
    stage: Mapped[int] = mapped_column(Integer, default=0)
    # C-1：项目投标工作稿（OfficeDocument type=workspace，唯一真源）
    workspace_office_document_id: Mapped[str | None] = mapped_column(String(32), default=None)
    is_archived: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[str | None] = mapped_column(String(10), default=None)  # 中标 | 未中标
    result_note: Mapped[str | None] = mapped_column(String(300), default=None)
    result_amount: Mapped[str | None] = mapped_column(String(50), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (Index("ix_proj_arch", "is_archived", "stage"),)


class Document(Base):
    """文档主表（kind: tender 招标 / bid 投标；status: 0 未拆解/1 拆解中/2 已拆解）。"""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    filename: Mapped[str] = mapped_column(String(500))
    # [deprecated B-1c-4] 旧文件名字段：真源在 OfficeDocument.current_version_id → version.file_path。
    # 新业务代码禁止读取/写入；仅 upload 落盘记录与历史异常数据兜底使用。
    stored_name: Mapped[str | None] = mapped_column(String(64), default=None)
    source: Mapped[str] = mapped_column(String(10), default="upload")  # upload | path
    origin_path: Mapped[str | None] = mapped_column(String(1000), unique=True)
    # [deprecated B-1c-4] 旧 current 指针：真源在 OfficeDocument.current_version_id。新代码禁止读取/写入。
    current_version_id: Mapped[str | None] = mapped_column(String(32), default=None)
    office_document_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)  # B-1c：Office 主身份
    kind: Mapped[str] = mapped_column(String(10), default="tender")  # tender | bid
    file_type: Mapped[str] = mapped_column(String(8), default="docx")  # docx|pdf|txt|xlsx|xls|csv|scan
    size: Mapped[int] = mapped_column(Integer, default=0)
    mtime: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[int] = mapped_column(Integer, default=0)
    error_msg: Mapped[str | None] = mapped_column(Text, default=None)
    project_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    is_secret: Mapped[int] = mapped_column(Integer, default=0)  # 遗留列：已取消涉密概念，恒 0
    bid_type: Mapped[str] = mapped_column(String(10), default="自主")
    doc_date: Mapped[str | None] = mapped_column(String(20), default=None)
    # 派生缓存：当前正式 DocumentVersion 经 parser 生成的纯文本；仅 process_document 写入，勿直写
    content_text: Mapped[str | None] = mapped_column(Text, default=None)
    ocr_conf: Mapped[float | None] = mapped_column(Float, default=None)  # OCR 全文均置信度
    needs_review: Mapped[int] = mapped_column(Integer, default=0)  # 1=OCR 低置信度待复核
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        Index("ix_doc_status2", "status"),
        Index("ix_doc_proj", "project_id"),
    )


class ExtractItem(Base):
    """拆解结构项（文档库拆解产出）：招标 4 组 / 投标 7 项。"""

    __tablename__ = "extract_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    doc_id: Mapped[str] = mapped_column(String(32), index=True)
    group_name: Mapped[str] = mapped_column(String(50))  # 组名
    name: Mapped[str] = mapped_column(String(300))  # 结构项名
    sum: Mapped[str | None] = mapped_column(String(300), default=None)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ExtractRow(Base):
    """拆解结构项的字段值行。"""

    __tablename__ = "extract_rows"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    field: Mapped[str] = mapped_column(String(200))
    value: Mapped[str | None] = mapped_column(Text, default=None)
    cells_json: Mapped[str | None] = mapped_column(Text, default=None)  # 表格行全列 JSON（多列还原用）
    seq: Mapped[int] = mapped_column(Integer, default=0)


class DocumentVersion(Base):
    """[deprecated B-1c-4] legacy 版本表：写链已切 OfficeDocumentVersion，本表 read-only。

    保留仅为迁移期对账与历史数据兜底；新代码禁止写入。
    历史语义：upload 初始版本 / ONLYOFFICE status=2 保存版本（阶段三）。

    status=6（forcesave，编辑中）只写工作快照，绝不产生本表记录、不换 key。
    """

    __tablename__ = "document_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    document_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    file_path: Mapped[str] = mapped_column(String(500))  # 相对 files_dir 的文件名
    file_hash: Mapped[str] = mapped_column(String(64))
    editor_user: Mapped[str | None] = mapped_column(String(100), default=None)
    source: Mapped[str] = mapped_column(String(20), default="upload")  # upload | onlyoffice | init | restore
    restored_from_version_id: Mapped[str | None] = mapped_column(String(32), default=None)  # 恢复来源版本
    comment: Mapped[str | None] = mapped_column(String(300), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (UniqueConstraint("document_id", "version", name="uq_docver"),)


class OfficeDocument(Base):
    """通用 Office 文件层（阶段六B-1a）：只负责文件生命周期，业务字段留在业务表。

    document_type: source | material | workspace
    current_version_id 是唯一正式版本指针（阶段三机制泛化）。
    title 仅编辑器显示缓存，业务名称真源在业务对象。
    """

    __tablename__ = "office_documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    document_type: Mapped[str] = mapped_column(String(16), index=True)  # source|material|workspace
    title: Mapped[str] = mapped_column(String(500), default="")
    file_type: Mapped[str] = mapped_column(String(8), default="docx")
    current_version_id: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class OfficeDocumentVersion(Base):
    """通用 Office 版本表：UNIQUE(office_document_id, version)。

    source: upload | onlyoffice | init | restore | extract | material_insert
    status=6 工作快照不落本表（阶段三语义延续）。
    """

    __tablename__ = "office_document_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    office_document_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    file_path: Mapped[str] = mapped_column(String(500))
    file_hash: Mapped[str] = mapped_column(String(64))
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(20), default="upload")
    restored_from_version_id: Mapped[str | None] = mapped_column(String(32), default=None)
    editor_user: Mapped[str | None] = mapped_column(String(100), default=None)
    comment: Mapped[str | None] = mapped_column(String(300), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (UniqueConstraint("office_document_id", "version", name="uq_odver"),)


class ProcessorRun(Base):
    """post-save processor 持久幂等登记（B-2）：UNIQUE 抢占，failed 可重试。

    一个 (office_document_version_id, processor_type) 只允许一条记录；
    dispatch 时 INSERT OR IGNORE 抢占，抢到的执行；status=failed 的记录
    可由重试接口删除后重新 dispatch。
    """

    __tablename__ = "processor_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    office_document_version_id: Mapped[str] = mapped_column(String(32))
    processor_type: Mapped[str] = mapped_column(String(16))  # source|material|workspace
    status: Mapped[str] = mapped_column(String(10), default="running")  # running|done|failed
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    __table_args__ = (UniqueConstraint("office_document_version_id", "processor_type", name="uq_proc_run"),)


class AppMeta(Base):
    """应用级键值元数据（迁移标记等）。"""

    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WorkspaceDocument(Base):
    """C-1：Workspace 派生数据（正文真源在 OfficeDocumentVersion；本表仅派生缓存）。"""

    __tablename__ = "workspace_documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    office_document_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    derived_text: Mapped[str | None] = mapped_column(Text, default=None)
    derived_from_version_id: Mapped[str | None] = mapped_column(String(32), default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WorkspaceMutation(Base):
    """C-3a：Material → Workspace 插入审计（回答"这段内容从哪个素材来"）。

    不是版本表——新版本仍存 office_document_versions（source=material_insert），
    本表只记录 mutation 双方精确版本与插入语义。
    """

    __tablename__ = "workspace_mutations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    workspace_office_document_id: Mapped[str] = mapped_column(String(32), index=True)
    workspace_base_version_id: Mapped[str] = mapped_column(String(32))
    workspace_new_version_id: Mapped[str] = mapped_column(String(32))
    material_id: Mapped[str] = mapped_column(String(32), index=True)
    material_office_document_id: Mapped[str] = mapped_column(String(32))
    material_version_id: Mapped[str] = mapped_column(String(32))
    insert_mode: Mapped[str] = mapped_column(String(20), default="append_end")
    target_locator_json: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Material(Base):
    """素材（F2.6）。type: 表格/文本/图片/附件；cat: 七分类。"""

    __tablename__ = "materials"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    title: Mapped[str] = mapped_column(String(300))
    type: Mapped[str] = mapped_column(String(10))  # 表格|文本|图片|附件
    cat: Mapped[str] = mapped_column(String(20))  # 资质文件|财务状况|业绩案例|人员团队|技术方案|商务模板|投标格式
    src: Mapped[str] = mapped_column(String(20), default="手动上传")  # 文档库拆解 | 手动上传
    doc_name: Mapped[str | None] = mapped_column(String(500), default=None)
    project_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    tags_json: Mapped[str | None] = mapped_column(Text, default=None)
    rows_json: Mapped[str | None] = mapped_column(Text, default=None)  # 表格：[[字段,值],...]
    text: Mapped[str | None] = mapped_column(Text, default=None)
    file_name: Mapped[str | None] = mapped_column(String(300), default=None)
    file_size: Mapped[str | None] = mapped_column(String(30), default=None)
    source_page: Mapped[int | None] = mapped_column(Integer, default=None)  # 图片证据来源页码
    used: Mapped[int] = mapped_column(Integer, default=0)
    # 阶段六B-1b：Material 独立 DOCX（OfficeDocument type=material）
    office_document_id: Mapped[str | None] = mapped_column(String(32), default=None)
    source_document_id: Mapped[str | None] = mapped_column(String(32), default=None)
    source_document_version_id: Mapped[str | None] = mapped_column(String(32), default=None)
    source_locator_json: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (Index("ix_mat_cat", "cat", "updated_at"),)


class MaterialRef(Base):
    """素材↔项目 引用（多对多）。"""

    __tablename__ = "material_refs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    material_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)


class WatchPath(Base):
    """收藏路径（自动归档）。"""

    __tablename__ = "watch_paths"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    path: Mapped[str] = mapped_column(String(500), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ScanReport(Base):
    """扫描报告。"""

    __tablename__ = "scan_reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    path: Mapped[str] = mapped_column(String(500))
    trigger: Mapped[str] = mapped_column(String(10), default="manual")
    added: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    detail_json: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Chunk(Base):
    """检索切片。"""

    __tablename__ = "chunks"

    chunk_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    document_id: Mapped[str] = mapped_column(String(32), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    page_no: Mapped[int | None] = mapped_column(Integer, default=None)


class RiskItem(Base):
    """废标风险项（工作台检测产出，三极四型）。"""

    __tablename__ = "risk_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    sev: Mapped[str] = mapped_column(String(10))  # fatal | risk | hint
    type: Mapped[str] = mapped_column(String(10))  # 未响应 | 偏离 | 缺项 | 缺证
    title: Mapped[str] = mapped_column(String(300))
    meta: Mapped[str | None] = mapped_column(Text, default=None)  # 风险出处描述
    doc_id: Mapped[str | None] = mapped_column(String(32), default=None)  # 定位到的投标文档
    req: Mapped[str | None] = mapped_column(Text, default=None)
    quote: Mapped[str | None] = mapped_column(Text, default=None)
    advice: Mapped[str | None] = mapped_column(Text, default=None)
    fix: Mapped[str | None] = mapped_column(String(100), default=None)
    page_no: Mapped[int | None] = mapped_column(Integer, default=None)
    done: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AuditLog(Base):
    """审计日志。"""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(64), default="user")
    action: Mapped[str] = mapped_column(String(64))
    pipeline: Mapped[str] = mapped_column(String(12), default="local")  # local | glm_text | glm_vision
    doc_id: Mapped[str | None] = mapped_column(String(32), default=None)
    detail: Mapped[str | None] = mapped_column(Text, default=None)


def _chunks_table_sql() -> list[str]:
    return [
        """CREATE TABLE IF NOT EXISTS chunks (
            chunk_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            page_no INTEGER
        );""",
        "CREATE INDEX IF NOT EXISTS ix_chunks_doc ON chunks(document_id);",
        """CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content, content='chunks', content_rowid='rowid', tokenize='trigram'
        );""",
        """CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, content) VALUES (new.rowid, new.content);
        END;""",
        """CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
        END;""",
    ]


class Tag(Base):
    """行业/产品标签。"""

    __tablename__ = "tags"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    name: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(10))  # industry | product
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (Index("ix_tag_kind_name", "kind", "name", unique=True),)


BUILTIN_INDUSTRIES = ("数据中心 / ICT", "物流仓储", "医疗卫生", "政务 / 公共服务", "能源与双碳")
class DetectRule(Base):
    """检测规则（F3.4）：可启用/停用、改说明，全部变更留痕。"""

    __tablename__ = "detect_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    sev: Mapped[str] = mapped_column(String(10))
    type: Mapped[str] = mapped_column(String(10))
    desc: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class DetectRuleVersion(Base):
    """检测规则变更留痕：每次启停/修改存一条快照。"""

    __tablename__ = "detect_rule_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    rule_id: Mapped[str] = mapped_column(String(32), index=True)
    note: Mapped[str] = mapped_column(String(300))
    snapshot_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


BUILTIN_PLATFORMS = ("上海市公共资源交易平台", "中招联合招标采购平台", "优质采购云平台", "政府采购云平台", "企业自建招标平台")
STAGES = ("技术标制作", "商务标制作", "合标", "打印盖章", "上传")
MATERIAL_CATS = ("资质文件", "财务状况", "业绩案例", "人员团队", "技术方案", "商务模板", "投标格式")


@event.listens_for(Document, "before_insert")
def _doc_touch(mapper: object, connection: object, target: object) -> None:
    _ = mapper, connection, target
