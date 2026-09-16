export type DocKind = "tender" | "bid";

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
}

export interface PersonCard {
  name: string;
  role: string;
  certs: string[];
  source: string;
  status: "ok" | "warn";
}

export interface ExtractItemT {
  id: string;
  name: string;
  sum: string | null;
  rows: string[][];
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
  rows: string[][] | null;
  text: string | null;
  file_name: string | null;
  file_size: string | null;
  source_page: number | null;
  used: number;
  updated_at: string | null;
}

export interface WatchPathT {
  id: string;
  path: string;
  enabled: number;
  last_scan_at: string | null;
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
