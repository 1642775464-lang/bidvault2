export type DocKind = "tender" | "bid" | "notice";

export interface Project {
  id: string;
  code: string;
  name: string;
  role: string;
  industry: string | null;
  open_at: string | null;
  platform: string | null;
  cust: string | null;
  contact: string | null;
  sales: string | null;
  stage: number;
  stage_label: string;
  is_archived: boolean;
  result: string | null;
  result_note: string | null;
  result_amount: string | null;
  doc_count: number;
  created_at: string | null;
  documents?: DocBrief[];
}

export interface DocBrief {
  id: string;
  filename: string;
  kind: DocKind;
  size: number;
  status: number;
  doc_date: string | null;
}

export interface DocItem {
  id: string;
  filename: string;
  kind: DocKind;
  file_type: string;
  size: number;
  status: number;
  project_id: string | null;
  is_secret: number;
  error_msg: string | null;
  ocr_conf: number | null;
  needs_review: number;
  created_at: string | null;
  office_document_id: string | null;
  /* 拆解规则维度（契约 §8.2，服务端派生） */
  extract_status?: ExtractStatus | null;
  extract_ruleset_version?: number | null;
  ruleset_version?: number | null;
}

export interface PersonCard {
  name: string;
  role: string;
  certs: string[];
  source: string;
  status: "ok" | "warn";
}

export interface TableCellV2 {
  id: string;
  text: string;
  logicalCol: number;
  colSpan: number;
  rowSpan: number;
  merged?: boolean;
  mergeParentId?: string;
  widthTwips?: number;
}

export interface TableBlockV2 {
  type: "table";
  schemaVersion: number;
  tid: number;
  columnCount: number;
  columnWidthsTwips: number[];
  cells: TableCellV2[];
}

export type ExtractRowV2 = string[] | TableBlockV2;

export interface ExtractItemT {
  id: string;
  name: string;
  sum: string | null;
  rows: ExtractRowV2[];
}

export interface ExtractGroup {
  name: string;
  items: ExtractItemT[];
}

export interface ExtractResp {
  status: number;
  groups: ExtractGroup[];
}

export interface Material {
  id: string;
  title: string;
  type: string;
  cat: string;
  src: string;
  doc_name: string | null;
  project_id: string | null;
  project_name: string | null;
  tags: string[];
  rows: ExtractRowV2[] | null;
  text: string | null;
  file_name: string | null;
  file_size: string | null;
  source_page: number | null;
  used: number;
  updated_at: string | null;
  office_document_id: string | null;
  source_document_id: string | null;
  source_document_version_id: string | null;
  source_locator_json: string | null;
  source_document_version: number | null;
}

export interface WatchPathT {
  id: string;
  path: string;
  enabled: number;
  last_scan_at: string | null;
}

/* ---------- 拆解规则中心（契约《标库-拆解规则契约》v1.0） ---------- */

export interface ExtractAnchorT {
  text: string;
  priority: number;
  match_mode: string; // 仅 contains 生效；其余为预留（不展示、不可编辑）
  role?: "start" | "end"; // v1.2：start=区间起点（默认），end=区间终点（不含该行）
}

export interface ExtractRuleT {
  id: string;
  kind: DocKind;
  scope: string; // 只存储不进 UI（契约 §2.4）
  group_name: string;
  name: string;
  anchors: ExtractAnchorT[];
  focus_fields: string[];
  summary: string | null;
  enabled: boolean;
  seq: number;
  builtin: boolean;
  updated_at: string | null;
}

export interface ExtractRuleGroupT {
  kind: DocKind;
  label: string;
  items: ExtractRuleT[];
  enabled_count: number;
}

/** 文档规则状态（服务端派生，契约 §五） */
export type ExtractStatus = "BUILDING" | "FAILED" | "NORMAL" | "RULE_CHANGED" | "NEED_REBUILD";

export interface ExtractDiffT {
  available: boolean;
  reason?: string;
  document_id: string;
  kind: DocKind;
  extract_status?: ExtractStatus;
  extract_ruleset_version?: number | null;
  ruleset_version?: number;
  from_version?: number;
  to_version?: number;
  from_ruleset_hash?: string;
  to_ruleset_hash?: string;
  executed_at?: string | null;
  added?: { id: string; name: string }[];
  removed?: { id: string; name: string }[];
  changed?: { id: string; name: string; fields: string[] }[];
  order_changed?: boolean;
  unchanged?: number;
}

/* ---------- 规则试运行（契约 §十一 v1.1：dry-run 命中证据） ---------- */

export interface ExtractPreviewMatchT {
  rule_id: string;
  name: string;
  matched: boolean;
  anchor: string | null;
  location: {
    line_start: number;
    line_end: number;
    page_start: number | null;
    page_end: number | null;
  } | null;
  rows_count: number;
  preview: string[];
  truncated: boolean;
  reason?: string;
}

export interface ExtractPreviewT {
  document: { id: string; filename: string; kind: DocKind };
  rule_set_version: number;
  ruleset_hash: string;
  guard: { ok: boolean; file_size_mb: number | null; pages: number | null };
  matches: ExtractPreviewMatchT[];
}

export interface ScanReportT {
  id: string;
  path: string;
  trigger: string;
  added: number;
  updated: number;
  skipped: number;
  failed: number;
  created_at: string | null;
}

export interface SearchHit {
  document_id: string;
  filename: string;
  kind: DocKind;
  is_secret: number;
  score: number;
  snippet: string;
  snippet_html: string;
  page_no: number | null;
  chunk_id: string;
}

export interface RiskItemT {
  id: string;
  sev: "fatal" | "risk" | "hint";
  type: string;
  title: string;
  meta: string | null;
  req: string | null;
  quote: string | null;
  advice: string | null;
  fix: string | null;
  page_no: number | null;
  done: boolean;
  doc_id: string | null;
  doc_name: string | null;
  created_at: string | null;
}

export interface DetectResp {
  risk_index: number;
  detected_at: string | null;
  score_rows?: string[][];
  items: RiskItemT[];
}

export interface Health {
  status: string;
  glm_configured: boolean;
  scan_interval: number;
  data_dir: string;
  counts: {
    documents: number;
    materials: number;
    watch_paths: number;
    audit_logs: number;
  };
  disk?: {
    free_gb: number;
    total_gb: number;
    used_pct: number;
    warn: boolean;
  };
}

export interface ProjectMeta {
  industries: string[];
  platforms: string[];
  stages: string[];
}

export const MATERIAL_CATS = [
  "资质文件",
  "财务状况",
  "业绩案例",
  "人员团队",
  "投标格式",
  "技术方案",
  "其他",
] as const;

export interface HistCase {
  id: string;
  name: string;
  cust: string | null;
  industry: string | null;
  open_at: string | null;
  platform: string | null;
  amount: string | null;
  note: string | null;
  match: number;
}
