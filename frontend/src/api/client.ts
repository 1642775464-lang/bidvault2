import type {
  DetectResp,
  ExtractRowV2,
  DocItem,
  ExtractResp,
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

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function handle<T>(resP: Response | Promise<Response>): Promise<T> {
  const res = await resP;
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") msg = body.detail;
    } catch {
      /* 保持默认信息 */
    }
    throw new ApiError(res.status, msg);
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
  async editorConfigOffice(oid: string, versionId?: string): Promise<{ scriptUrl: string; config: Record<string, unknown>; mode: "view" | "edit"; version: number | null; enabled: boolean }> {
    return handle(fetch(`/api/office-documents/${oid}/editor-config${versionId ? `?version_id=${versionId}` : ""}`));
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
  async onlyofficeVersions(docId: string): Promise<{
    items: {
      id: string; version: number; file_hash: string; source: string;
      editor_user: string | null; restored_from_version_id: string | null;
      comment: string | null; created_at: string | null; is_current: boolean; size: number | null;
    }[];
  }> {
    return handle(fetch(`/api/onlyoffice/versions/${docId}`));
  },
  async restoreVersion(docId: string, versionId: string, comment?: string): Promise<{ ok: boolean; version: number; restored_from: number }> {
    return handle(
      fetch(`/api/onlyoffice/versions/${docId}/${versionId}/restore`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ comment }),
      }),
    );
  },
  async onlyofficeVersion(docId: string): Promise<{ version: number | null; file_hash?: string; source?: string; editor_user?: string | null; created_at?: string | null }> {
    return handle(fetch(`/api/onlyoffice/version/${docId}`));
  },
  async editorConfig(docId: string, versionId?: string): Promise<{ scriptUrl: string; config: Record<string, unknown>; mode: "view" | "edit"; version: number | null; enabled: boolean }> {
    return handle(fetch(`/api/documents/${docId}/editor-config${versionId ? `?version_id=${versionId}` : ""}`));
  },
  async runExtract(docId: string): Promise<{ status: number; extracted: number; materials: number }> {
    return handle(fetch(`/api/documents/${docId}/extract`, { method: "POST" }));
  },
  async updateExtractItem(docId: string, itemId: string, rows: ExtractRowV2[]): Promise<{ ok: boolean }> {
    return handle(
      fetch(`/api/documents/${docId}/extract/${itemId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows }),
      }),
    );
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
  async uploadMaterial(file: File, cat: string): Promise<{ ok: boolean; id: string; type: string; cat: string }> {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("cat", cat);
    return handle(fetch("/api/materials/upload", { method: "POST", body: fd }));
  },
  async materials(cat = "", q = ""): Promise<{ total: number; items: Material[] }> {
    return handle(fetch(`/api/materials${qs({ cat, q })}`));
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
