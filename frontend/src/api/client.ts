import type {
  DetectResp,
  DocItem,
  DocKind,
  ExtractDiffT,
  ExtractPreviewT,
  ExtractResp,
  ExtractRuleGroupT,
  ExtractRuleT,
  Health,
  HistCase,
  Material,
  PersonCard,
  Project,
  ProjectMeta,
  ScanReportT,
  SearchHit,
  WatchPathT,
} from "./types";

/* ---- E-1A：标书工作区 ---- */
export interface WorkspaceInfo {
  active: boolean;
  id?: string;
  root?: string;
  name?: string;
  schema_version?: number;
  skeleton_ok?: boolean;
  repaired?: string[];
  created_at?: string | null;
}

export interface WorkspaceEventT {
  id: string;
  rel_path: string;
  kind: "added" | "modified" | "deleted";
  file_hash: string | null;
  status: string;
  created_at: string | null;
}

/* ---- E-1C：历史资料导入 ---- */
export interface ImportItemT {
  id: string;
  file_name: string;
  staging_rel: string;
  size: number;
  kind: "tender" | "bid" | "material" | "unknown";
  confidence: number;
  reasons: string[];
  status: "pending_confirm" | "confirmed" | "archived" | "registered" | "skipped" | "discarded" | "failed";
  decided_kind: string | null;
  dup_note: string | null;
  target_rel: string | null;
  doc_id: string | null;
  material_id: string | null;
  error: string | null;
}

export interface ImportBatchT {
  batch_id: string;
  label: string;
  status: string;
  created_at: string | null;
  counts: Record<string, number>;
  total: number;
}

export interface ImportBatchDetailT {
  batch_id: string;
  status: string;
  error: string | null;
  items: ImportItemT[];
}

/* ---- E-2B：结构保真阅读器 ---- */
export interface OutlineNodeT {
  id: string;
  parent_id: string | null;
  level: number;
  title: string;
  order: number;
  para_index: number;
  anchor_hash: string;
}

export type ReaderBlockT = {
  i: number;
  para_no: number;
} & (
  | { type: "heading"; level: number; text: string }
  | { type: "para"; text: string }
  | { type: "image"; text: string }
  | { type: "table"; rows: Array<{ cells: Array<{ text: string; gridspan: number; vmerge: string }>; blank: boolean }> }
);

export class ApiError extends Error {
  status: number;
  /** 非 2xx 响应体（可能是 {"code","features"} 等结构化错误），调用方按需读取 */
  detail: unknown;
  constructor(status: number, message: string, detail: unknown = null) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function handle<T>(resP: Response | Promise<Response>): Promise<T> {
  const res = await resP;
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    let parsed: unknown = null;
    try {
      const body = (await res.json()) as { detail?: unknown };
      parsed = body.detail ?? null;
      if (typeof body.detail === "string") msg = body.detail;
    } catch {
      /* 保持默认信息 */
    }
    throw new ApiError(res.status, msg, parsed);
  }
  return (await res.json()) as T;
}

function qs(params: Record<string, string | number | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export const api = {
  /* ---- E-1A：标书工作区 ---- */
  async getWorkspace(): Promise<WorkspaceInfo> {
    return handle(fetch("/api/workspace"));
  },
  async createWorkspace(path: string, name: string): Promise<WorkspaceInfo> {
    return handle(fetch("/api/workspace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, name }),
    }));
  },
  async uploadImportFiles(files: File[], label: string): Promise<{ batch_id: string; status: string }> {
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    fd.append("label", label);
    return handle(fetch("/api/workspace/imports/upload", { method: "POST", body: fd }));
  },
  async importBatches(): Promise<{ items: ImportBatchT[] }> {
    return handle(fetch("/api/workspace/imports"));
  },
  async importBatch(batchId: string): Promise<ImportBatchDetailT> {
    return handle(fetch(`/api/workspace/imports/${batchId}`));
  },
  async confirmImport(batchId: string, decisions: Array<Record<string, unknown>>): Promise<{ results: Array<Record<string, unknown>> }> {
    return handle(fetch(`/api/workspace/imports/${batchId}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decisions }),
    }));
  },
  async discardImportBatch(batchId: string): Promise<{ ok: boolean; removed_files: number; status: string }> {
    return handle(fetch(`/api/workspace/imports/${batchId}/discard`, { method: "POST" }));
  },
  async undoImportItem(batchId: string, itemId: string): Promise<{ ok: boolean; detail?: string }> {
    return handle(fetch(`/api/workspace/imports/${batchId}/items/${itemId}/undo`, { method: "POST" }));
  },
  async officeOutline(oid: string): Promise<{ oid: string; version_id: string; version: number; node_count: number; nodes: OutlineNodeT[] }> {
    return handle(fetch(`/api/office-documents/${oid}/outline`));
  },
  async readerBlocks(oid: string): Promise<{ oid: string; version_id: string; version: number; blocks: ReaderBlockT[] }> {
    return handle(fetch(`/api/office-documents/${oid}/reader`));
  },
  async insertReference(payload: { oid: string; node_id: string | null; material_id: string; purpose: string; project_id?: string | null }): Promise<{ reference_id: string; version_id: string; writeback: string; note?: string }> {
    return handle(fetch("/api/workspace/references/insert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }));
  },
  /* ---- 项目库 ---- */
  async projects(mode: "active" | "archived", q = ""): Promise<{ total: number; items: Project[] }> {
    return handle(fetch(`/api/projects${qs({ mode, q })}`));
  },
  async projectMeta(): Promise<ProjectMeta> {
    return handle(fetch("/api/projects/meta"));
  },
  async project(id: string): Promise<Project> {
    return handle(fetch(`/api/projects/${id}`));
  },
  async createProject(body: Partial<Project>): Promise<{ id: string; code: string }> {
    return handle(
      fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  },
  async updateProject(id: string, body: Partial<Project>): Promise<{ ok: boolean }> {
    return handle(
      fetch(`/api/projects/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  },
  async setStage(id: string, stage: number): Promise<{ ok: boolean }> {
    return handle(
      fetch(`/api/projects/${id}/stage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage }),
      }),
    );
  },
  async archiveOp(id: string, op: "archive" | "unarchive", body?: { result: string; result_note?: string; result_amount?: string }): Promise<{ ok: boolean }> {
    return handle(fetch(`/api/projects/${id}/${op}`, { method: "POST", body: body ? JSON.stringify(body) : undefined }));
  },

  /* ---- 文档库 ---- */
  async documents(params: { kind?: string; project_id?: string; q?: string; status?: string; file_type?: string } = {}): Promise<{ total: number; items: DocItem[] }> {
    return handle(fetch(`/api/documents${qs(params)}`));
  },
  async upload(files: File[], kind: string, projectId: string | null, isSecret: boolean): Promise<{ created: DocItem[]; queued: number }> {
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    fd.append("kind", kind);
    if (projectId) fd.append("project_id", projectId);
    fd.append("is_secret", String(isSecret));
    return handle(fetch("/api/documents/upload", { method: "POST", body: fd }));
  },
  async extract(docId: string): Promise<ExtractResp> {
    return handle(fetch(`/api/documents/${docId}/extract`));
  },
  async content(docId: string): Promise<{ filename: string; text: string }> {
    return handle(fetch(`/api/documents/${docId}/content`));
  },
  async pageCount(docId: string): Promise<{ pages: number; previewable: boolean }> {
    return handle(fetch(`/api/documents/${docId}/page_count`));
  },
  async analyzeImages(docId: string): Promise<{ analyzed: number; failed: number; total: number }> {
    return handle(fetch(`/api/documents/${docId}/analyze_images`, { method: "POST" }));
  },

  /* ---- F3 废标检测 ---- */
  async runDetect(projectId: string): Promise<DetectResp> {
    return handle(fetch(`/api/detect/${projectId}`, { method: "POST" }));
  },
  async risks(projectId: string): Promise<DetectResp & { has_result: boolean }> {
    return handle(fetch(`/api/detect/${projectId}/risks`));
  },
  async markRiskDone(riskId: string, done: boolean): Promise<{ ok: boolean; risk_index: number }> {
    return handle(
      fetch(`/api/detect/risks/${riskId}/done`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ done }),
      }),
    );
  },
  detectReportUrl(projectId: string): string {
    return `/api/detect/${projectId}/report.xlsx`;
  },
  async patchDetectRule(ruleId: string, body: { enabled?: boolean; desc?: string }): Promise<{ ok: boolean }> {
    return handle(
      fetch(`/api/detect/rules/${ruleId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  },
  async detectRuleHistory(limit = 30): Promise<{ items: { rule_id: string; rule_name: string; note: string; created_at: string | null }[] }> {
    return handle(fetch(`/api/detect/rules/history${qs({ limit })}`));
  },
  async detectRules(): Promise<{ rules: { id: string; name: string; sev: string; type: string; desc: string; enabled: boolean }[] }> {
    return handle(fetch("/api/detect/rules"));
  },
  /* ---------- 拆解规则中心（契约 v1.0） ---------- */
  async extractRules(): Promise<{
    groups: ExtractRuleGroupT[];
    ruleset_version: number;
    stale_docs: number;
  }> {
    return handle(fetch("/api/extract-rules"));
  },
  async createExtractRule(body: {
    kind: DocKind; name: string; anchors: { text: string; priority?: number }[];
    group_name?: string; summary?: string | null; focus_fields?: string[]; enabled?: boolean;
  }): Promise<{ ok: boolean; rule: ExtractRuleT; ruleset_version: number }> {
    return handle(
      fetch("/api/extract-rules", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  },
  async patchExtractRule(ruleId: string, body: {
    name?: string; anchors?: { text: string; priority?: number }[]; group_name?: string;
    summary?: string | null; focus_fields?: string[]; enabled?: boolean; seq?: number;
  }): Promise<{ ok: boolean; changed?: boolean; rule?: ExtractRuleT; ruleset_version?: number }> {
    return handle(
      fetch(`/api/extract-rules/${ruleId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  },
  async deleteExtractRule(ruleId: string): Promise<{ ok: boolean; ruleset_version: number }> {
    return handle(fetch(`/api/extract-rules/${ruleId}`, { method: "DELETE" }));
  },
  async reorderExtractRules(kind: DocKind, ids: string[]): Promise<{ ok: boolean; order: string[]; ruleset_version: number }> {
    return handle(
      fetch("/api/extract-rules/reorder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, ids }),
      }),
    );
  },
  async resetExtractRules(kind?: DocKind): Promise<{ ok: boolean; restored: number; removed: number; ruleset_version: number }> {
    return handle(
      fetch("/api/extract-rules/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: kind ?? null }),
      }),
    );
  },
  async extractRuleHistory(limit = 30): Promise<{ items: { rule_id: string; rule_name: string; note: string; created_at: string | null }[] }> {
    return handle(fetch(`/api/extract-rules/history${qs({ limit })}`));
  },
  async extractDiff(docId: string): Promise<ExtractDiffT> {
    return handle(fetch(`/api/documents/${docId}/extract-diff`));
  },
  /** 规则试运行（契约 §十一 v1.1）：dry-run 命中证据，不落任何业务态 */
  async previewExtractRules(body: {
    document_id: string; rule_set_version?: number; rules?: string[];
  }): Promise<ExtractPreviewT> {
    return handle(
      fetch("/api/extract-rules/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  },
  async ensureWorkspace(projectId: string): Promise<{ ok: boolean; office_document_id: string; title: string; version: number | null }> {
    return handle(fetch(`/api/projects/${projectId}/workspace`, { method: "POST" }));
  },
  async workspaceState(projectId: string): Promise<{ exists: boolean; office_document_id?: string; title?: string; version?: number | null; current_version_id?: string | null; derived_from_version_id?: string | null; derived_chars?: number }> {
    return handle(fetch(`/api/projects/${projectId}/workspace`));
  },
  async restoreOfficeVersion(oid: string, versionId: string, comment?: string): Promise<{ ok: boolean; version: number }> {
    return handle(
      fetch(`/api/office-documents/${oid}/versions/${versionId}/restore`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ comment }),
      }),
    );
  },
  async officeCallbackState(oid: string): Promise<{ doc_id: string; state: string }> {
    return handle(fetch(`/api/office-documents/${oid}/callback-state`));
  },
  async retryProcessor(oid: string): Promise<{ ok: boolean; version: number; retried: boolean }> {
    return handle(fetch(`/api/office-documents/${oid}/retry-processor`, { method: "POST" }));
  },
  async insertMaterial(
    wsOid: string,
    body: {
      base_workspace_version_id: string;
      material_id: string;
      material_office_document_id: string;
      material_version_id: string;
      insert_mode: string;
      target_locator?: { para_index: number; title: string; anchor_hash: string } | null;
    },
  ): Promise<{ ok: boolean; workspace_office_document_id: string; version: number; version_id: string; material_version_id: string; insert_mode: string }> {
    return handle(fetch(`/api/office-documents/${wsOid}/material-insert`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
  },
  async aiGatewayConfig(wsOid: string): Promise<{ provider: string; base_url: string; model: string; timeout_s: number; api_key_ref: string; has_key: boolean }> {
    return handle(fetch(`/api/office-documents/${wsOid}/ai-gateway/config`));
  },
  async aiGatewayTest(wsOid: string): Promise<{ ok: boolean; provider: string; model: string; latency_ms: number; chars: number }> {
    return handle(fetch(`/api/office-documents/${wsOid}/ai-gateway/test`, { method: "POST" }));
  },
  async aiGenerate(
    wsOid: string,
    body: { base_workspace_version_id: string; instruction: string; context: { kind: string; ref: string; label?: string; text?: string }[] },
  ): Promise<{ proposal: { id: string; status: string; title: string; text: string; model: string | null }; egress_manifest: { provider: string; model: string; blocks: { kind: string; ref: string; label: string; chars: number }[] } }> {
    return handle(fetch(`/api/office-documents/${wsOid}/ai-proposals/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
  },
  async aiProposalCreate(
    wsOid: string,
    body: { base_workspace_version_id: string; title: string; text: string; operation: string; model: string | null; derived_from_proposal_id?: string | null },
  ): Promise<{ id: string; status: string; base_workspace_version_id: string; derived_from_proposal_id: string | null }> {
    return handle(fetch(`/api/office-documents/${wsOid}/ai-proposals`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
  },
  async aiProposalGet(pid: string): Promise<{ id: string; status: string; text: string; model: string | null; applied_version_id: string | null; error: string | null }> {
    return handle(fetch(`/api/ai-proposals/${pid}`));
  },
  async aiProposalContent(pid: string, text: string, title?: string): Promise<{ id: string; status: string }> {
    return handle(fetch(`/api/ai-proposals/${pid}/content`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, title }),
    }));
  },
  async aiProposalAction(pid: string, action: "approve" | "reject" | "expire" | "apply"): Promise<{ id: string; status: string; applied_version_id: string | null; error: string | null }> {
    return handle(fetch(`/api/ai-proposals/${pid}/${action}`, { method: "POST" }));
  },
  async officeVersions(oid: string): Promise<{ items: { id: string; version: number; source: string; created_at: string | null; is_current: boolean; size: number | null; comment: string | null; restored_from_version_id: string | null; editor_user: string | null; file_hash: string }[] }> {
    return handle(fetch(`/api/office-documents/${oid}/versions`));
  },
  async runExtract(docId: string): Promise<{ status: number; extracted: number; materials: number }> {
    return handle(fetch(`/api/documents/${docId}/extract`, { method: "POST" }));
  },
  async bind(docId: string, body: { project_id?: string | null; new_project_name?: string; new_project_industry?: string }): Promise<{ ok: boolean; project_id: string | null }> {
    return handle(
      fetch(`/api/documents/${docId}/bind`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  },

  /* ---- 素材库 ---- */
  async uploadMaterial(file: File, cat: string): Promise<{ ok: boolean; id: string; type: string; cat: string; ocr_unavailable?: boolean; ocr_message?: string }> {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("cat", cat);
    return handle(fetch("/api/materials/upload", { method: "POST", body: fd }));
  },
  async materials(cat = "", q = ""): Promise<{ total: number; items: Material[] }> {
    return handle(fetch(`/api/materials${qs({ cat, q })}`));
  },
  async materialWordify(materialId: string): Promise<{ ok: boolean; office_document_id: string | null; version: number | null; existed?: boolean }> {
    return handle(fetch(`/api/materials/${materialId}/office-document`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    }));
  },
  async materialMeta(): Promise<{ cats: string[]; types: string[] }> {
    return handle(fetch("/api/materials/meta"));
  },
  async createMaterial(body: Partial<Material>): Promise<{ id: string }> {
    return handle(
      fetch("/api/materials", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  },
  async updateMaterial(id: string, body: Partial<Material>): Promise<{ ok: boolean }> {
    return handle(
      fetch(`/api/materials/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  },
  async deleteMaterial(id: string): Promise<{ ok: boolean }> {
    return handle(fetch(`/api/materials/${id}`, { method: "DELETE" }));
  },
  async useMaterial(id: string, projectId: string): Promise<{ ok: boolean; used: number }> {
    return handle(
      fetch(`/api/materials/${id}/use`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId }),
      }),
    );
  },

  /* ---- 监视路径 ---- */
  async cases(projectId: string): Promise<{ items: HistCase[]; total: number }> {
    return handle(fetch(`/api/projects/cases${qs({ project_id: projectId })}`));
  },
  async paths(): Promise<{ items: WatchPathT[] }> {
    return handle(fetch("/api/paths"));
  },
  async addPath(path: string): Promise<{ id: string; existed: boolean }> {
    return handle(
      fetch("/api/paths", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      }),
    );
  },
  async removePath(id: string): Promise<{ ok: boolean }> {
    return handle(fetch(`/api/paths/${id}`, { method: "DELETE" }));
  },
  async scanNow(id: string): Promise<unknown> {
    return handle(fetch(`/api/paths/${id}/scan`, { method: "POST" }));
  },
  async backups(): Promise<{ items: { name: string; label: string; size_mb: number }[]; keep: number; schedule: string }> {
    return handle(fetch("/api/backup"));
  },
  async backupNow(): Promise<{ name: string; size_mb: number }> {
    return handle(fetch("/api/backup", { method: "POST" }));
  },
  async backupRestore(name: string): Promise<{ ok: boolean; restart_required: boolean }> {
    return handle(fetch(`/api/backup/${name}/restore`, { method: "POST" }));
  },
  async scanReports(limit = 20): Promise<{ items: ScanReportT[] }> {
    return handle(fetch(`/api/paths/reports${qs({ limit })}`));
  },

  /* ---- 检索 ---- */
  async search(q: string, kind = "", mode = "fts"): Promise<{ query: string; hits: SearchHit[] }> {
    return handle(fetch(`/api/search${qs({ q, kind, mode })}`));
  },

  /* ---- 人员档案 ---- */
  async people(projectId: string): Promise<{ items: PersonCard[] }> {
    return handle(fetch(`/api/people${qs({ project_id: projectId })}`));
  },

  /* ---- 系统 ---- */
  async health(): Promise<Health> {
    return handle(fetch("/api/health"));
  },
};
