import { reactive } from "vue";
import { api } from "./api/client";
import type { DocItem, Project } from "./api/types";

export const STAGES = ["技术标制作", "商务标制作", "合标", "打印盖章", "上传"];
export const INDUSTRIES = ["数据中心 / ICT", "物流仓储", "医疗卫生", "政务 / 公共服务", "能源与双碳"];
export const PLATFORMS = ["上海市公共资源交易平台", "中招联合招标采购平台", "优质采购云平台", "政府采购云平台", "企业自建招标平台"];
export const CATS = ["资质文件", "财务状况", "业绩案例", "人员团队", "技术方案", "商务模板", "投标格式"];
export const SEV = {
  fatal: { label: "致命", order: 0, cls: "sev-fatal", dot: "dot-fatal" },
  risk: { label: "风险", order: 1, cls: "sev-risk", dot: "dot-risk" },
  hint: { label: "提示", order: 2, cls: "sev-hint", dot: "dot-hint" },
} as const;
export const RISK_TYPES = ["未响应", "偏离", "缺项", "缺证"] as const;

interface Store {
  toasts: { id: number; text: string }[];
  activeProjectId: string;
  navView: string;
  /** 项目 id → 该项目全部文档（工作台/项目库/文档库共用，全局刷新） */
  docsByProject: Record<string, DocItem[]>;
  projects: Project[];
  drawerProjectId: string;
  drawerOpen: boolean;
  projectModal: { open: boolean; editingId: string | null };
  uploadModal: { open: boolean; kind: "tender" | "bid"; projectId: string | null };
  /** 素材库「引用到投标文件」跨页接力：待插入素材 id（工作台消费后清空） */
  pendingInsertMaterialId: string | null;
}

export const store = reactive<Store>({
  toasts: [],
  activeProjectId: "",
  navView: "",
  docsByProject: {},
  projects: [],
  drawerProjectId: "",
  drawerOpen: false,
  projectModal: { open: false, editingId: null },
  uploadModal: { open: false, kind: "tender", projectId: null },
  pendingInsertMaterialId: null,
});

let seq = 0;
export function toast(text: string): void {
  const t = { id: ++seq, text };
  store.toasts.push(t);
  window.setTimeout(() => {
    const i = store.toasts.indexOf(t);
    if (i >= 0) store.toasts.splice(i, 1);
  }, 2400);
}

export function navTo(v: string): void {
  store.navView = v;
}

export function openDrawer(id: string): void {
  store.drawerProjectId = id;
  store.drawerOpen = true;
}
export function closeDrawer(): void {
  store.drawerOpen = false;
}
export function openProjectModal(editingId: string | null): void {
  store.projectModal = { open: true, editingId };
}
export function closeProjectModal(): void {
  store.projectModal = { open: false, editingId: null };
}

/** prefProject 给定时默认「绑定到现有项目」。 */
export function openUpload(prefProject: string | null, kind: "tender" | "bid" = "tender"): void {
  store.uploadModal = { open: true, kind, projectId: prefProject };
}

export function closeUpload(): void {
  store.uploadModal = { open: false, kind: "tender", projectId: null };
}

export function fmtSize(n: number): string {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  return Math.max(1, Math.round(n / 1024)) + " KB";
}

export function fmtOpenDateTime(v: string | null): string {
  if (!v) return "—";
  return v.replace("T", " ").slice(5, 16);
}

export function daysDiff(openAt: string | null): number {
  if (!openAt) return 999;
  const d = new Date(openAt.replace(" ", "T"));
  const now = new Date();
  const da = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const db = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((da.getTime() - db.getTime()) / 86400000);
}

export function dueInfo(p: Project): { cls: string; text: string } {
  if (p.result) return { cls: "done", text: "已开标" };
  const d = daysDiff(p.open_at);
  if (d < 0) return { cls: "done", text: "已开标" };
  if (d === 0) return { cls: "d0", text: "今天 " + fmtOpenDateTime(p.open_at).slice(6) + " 开标" };
  if (d === 1) return { cls: "d0", text: "明天 " + fmtOpenDateTime(p.open_at).slice(6) + " 开标" };
  if (d <= 3) return { cls: "d3", text: "距开标 " + d + " 天" };
  return { cls: "dn", text: "距开标 " + d + " 天" };
}

export function expectedStage(p: Project): number {
  const d = daysDiff(p.open_at);
  if (d <= 0) return 4;
  if (d <= 1) return 3;
  if (d <= 3) return 2;
  if (d <= 7) return 1;
  return 0;
}

export function isBehind(p: Project): boolean {
  return !p.result && p.stage < expectedStage(p);
}

export function tenderOf(pid: string): DocItem | null {
  const list = store.docsByProject[pid];
  if (!list) return null;
  return list.find((d) => d.kind === "tender") ?? null;
}

export function fmtBytes(size: number | string | null | undefined): string {
  if (size == null) return "";
  if (typeof size === "number") return fmtSize(size);
  return size;
}

/** 全局刷新项目+文档缓存（ mutations 之后调用）。 */
export async function refreshCore(): Promise<void> {
  
  const [active, archived, docs] = await Promise.all([
    api.projects("active"),
    api.projects("archived"),
    api.documents({}),
  ]);
  const byId: Record<string, DocItem[]> = {};
  for (const d of docs.items) {
    if (d.project_id) {
      (byId[d.project_id] ??= []).push(d);
    }
  }
  store.docsByProject = byId;
  store.projects = [...active.items, ...archived.items.map((p) => ({ ...p }))];
}

/* ---------- 确认弹窗（导出拦截等） ---------- */
import { ref } from "vue";

export const confirmState = ref<{
  open: boolean;
  title: string;
  body: string;
  okText: string;
  resolve: ((v: boolean) => void) | null;
}>({ open: false, title: "", body: "", okText: "确认", resolve: null });

export function askConfirm(title: string, body: string, okText = "确认"): Promise<boolean> {
  return new Promise((resolve) => {
    confirmState.value = { open: true, title, body, okText, resolve };
  });
}

export function settleConfirm(v: boolean): void {
  confirmState.value.resolve?.(v);
  confirmState.value = { ...confirmState.value, open: false, resolve: null };
}
