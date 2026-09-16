import type {
  DetectResp,
  DocItem,
  ExtractResp,
  Health,
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
  async archiveOp(id: string, op: "archive" | "unarchive"): Promise<{ ok: boolean }> {
    return handle(fetch(`/api/projects/${id}/${op}`, { method: "POST" }));
  },

  /* ---- 文档库 ---- */
  async documents(params: { kind?: string; project_id?: string; q?: string } = {}): Promise<{ total: number; items: DocItem[] }> {
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
  async detectRules(): Promise<{ rules: { id: string; name: string; sev: string; type: string; desc: string }[] }> {
    return handle(fetch("/api/detect/rules"));
  },
  async runExtract(docId: string): Promise<{ status: number; extracted: number; materials: number }> {
    return handle(fetch(`/api/documents/${docId}/extract`, { method: "POST" }));
  },
  async updateExtractItem(docId: string, itemId: string, rows: string[][]): Promise<{ ok: boolean }> {
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
  async useMaterial(id: string, where: string): Promise<{ ok: boolean; used: number }> {
    return handle(
      fetch(`/api/materials/${id}/use`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ where }),
      }),
    );
  },

  /* ---- 监视路径 ---- */
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
