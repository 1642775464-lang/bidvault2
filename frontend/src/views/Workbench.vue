<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ApiError, api, postNativeEditor } from "../api/client";
import type { WorkspaceInfo } from "../api/client";
import OfficeEditor from "../components/office/OfficeEditor.vue";
import type { Material, PersonCard, Project, RiskItemT , HistCase } from "../api/types";
import {
  RISK_TYPES,
  askConfirm,
  dueInfo,
  navTo,
  openDrawer,
  openUpload,
  refreshCore,
  store,
  toast,
} from "../store";

/* ---------------- 项目上下文 ---------------- */
const cur = computed<Project | null>(() => store.projects.find((x) => x.id === store.activeProjectId) ?? null);
const ddOpen = ref(false);
const expOpen = ref(false);


function pickProject(p: Project): void {
  if (p.id === store.activeProjectId) {
    ddOpen.value = false;
    return;
  }
  store.activeProjectId = p.id;
  ddOpen.value = false;
  toast(`已切换到「${p.name}」`);
}

const myDocs = computed(() => (cur.value ? store.docsByProject[cur.value.id] ?? [] : []));
const bidDocs = computed(() => myDocs.value.filter((d) => d.kind === "bid"));
const tenderDoc = computed(() => myDocs.value.find((d) => d.kind === "tender") ?? null);

/* ---------------- Workspace 工作稿（C-2：正文唯一真源） ---------------- */
const wsOid = ref<string | null>(null);
/** D-1R4：请求编辑器壳以原生 DX 编辑器打开指定 OfficeDocument（仅壳内 WebView2 可用）。 */
function openNative(oid: string | null): void {
  if (!oid) return;
  if (!postNativeEditor(oid)) toast("原生编辑需在 BidVault 编辑器壳内打开（当前为普通浏览器）");
}
const wsVersion = ref<number | null>(null);
const wsSync = ref<"synced" | "staged" | "creating" | "saved" | "syncing" | "failed" | "idle">("idle");
const wsVerOpen = ref(false);
const wsVerList = ref<{ id: string; version: number; source: string; created_at: string | null; is_current: boolean; size: number | null; comment: string | null }[]>([]);
let wsPollTimer: ReturnType<typeof setInterval> | null = null;
let wsPollSeq = 0;

async function initWorkspace(): Promise<void> {
  const mySeq = ++wsPollSeq;
  const pid = cur.value?.id;
  if (!pid) {
    wsOid.value = null;
    wsSync.value = "idle";
    return;
  }
  try {
    const r = await api.ensureWorkspace(pid);
    if (mySeq !== wsPollSeq || cur.value?.id !== pid) return; // 项目已切换，丢弃过期响应
    wsOid.value = r.office_document_id;
    wsVersion.value = r.version;
    wsSync.value = "synced"; // 稳态：正式版本 + 派生数据已一致（编辑器打开本身不算保存状态）
    startWsPoll();
  } catch (e) {
    if (mySeq !== wsPollSeq) return;
    wsOid.value = null;
    wsSync.value = "failed";
    toast(e instanceof Error ? e.message : "工作稿初始化失败");
  }
}

async function pollWorkspace(): Promise<void> {
  const mySeq = wsPollSeq;
  const pid = cur.value?.id;
  const oid = wsOid.value;
  if (!pid || !oid || wsPollSeq !== mySeq) return;
  try {
    const st = await api.workspaceState(pid);
    if (wsPollSeq !== mySeq || st.office_document_id !== oid) return; // 过期响应丢弃
    if (st.version != null && st.version !== wsVersion.value) {
      wsVersion.value = st.version;
      // 完成信号：callback-state 出现 processor 结果（事件信号，非猜测延迟）
      for (let i = 0; i < 90; i++) {
        const cs = await api.officeCallbackState(oid);
        if (cs.state.includes("processor=") || cs.state.includes("保存处理失败")) break;
        await new Promise((r2) => setTimeout(r2, 1000));
      }
      if (wsPollSeq !== mySeq) return;
      wsSync.value = "saved";
      void loadWsVersions();
      // 派生同步状态：derived_from_version_id vs current_version_id
      const ws2 = await api.workspaceState(pid);
      if (wsPollSeq !== mySeq || ws2.office_document_id !== oid) return;
      if (ws2.derived_from_version_id !== ws2.current_version_id) {
        wsSync.value = "syncing";
        for (let i = 0; i < 90; i++) {
          const ws3 = await api.workspaceState(pid);
          if (wsPollSeq !== mySeq) return;
          if (ws3.derived_from_version_id === ws3.current_version_id) break;
          await new Promise((r2) => setTimeout(r2, 1500));
        }
      }
      if (wsPollSeq !== mySeq) return;
      wsSync.value = "saved";
    } else {
      const cs = await api.officeCallbackState(oid);
      if (cs.state.includes("正在处理保存")) wsSync.value = "creating";
      else if (cs.state.includes("工作快照")) wsSync.value = "staged";
      else if (cs.state.includes("失败")) wsSync.value = "failed";
      else {
        // 稳态判定：派生数据是否追平 current（正式版本状态 + 派生同步状态两个维度）
        const dFrom = st.derived_from_version_id ?? null;
        const dCur = st.current_version_id ?? null;
        wsSync.value = dFrom && dCur && dFrom !== dCur ? "syncing" : "synced";
      }
    }
  } catch {
    /* 轮询失败下轮重试 */
  }
}

function startWsPoll(): void {
  if (wsPollTimer) clearInterval(wsPollTimer);
  void pollWorkspace();
  wsPollTimer = setInterval(() => void pollWorkspace(), 5000);
}

function stopWsPoll(): void {
  if (wsPollTimer) clearInterval(wsPollTimer);
  wsPollTimer = null;
}

async function loadWsVersions(): Promise<void> {
  if (!wsOid.value) return;
  try {
    wsVerList.value = (await api.officeVersions(wsOid.value)).items;
  } catch {
    wsVerList.value = [];
  }
}

async function restoreWsVersion(v: { id: string; version: number }): Promise<void> {
  if (!wsOid.value) return;
  if (!window.confirm(`确认恢复工作稿 v${v.version}？将以新建版本方式回滚。`)) return;
  try {
    await api.restoreOfficeVersion(wsOid.value, v.id);
    toast(`工作稿已恢复 v${v.version}`);
    wsSync.value = "syncing";
    void pollWorkspace();
    void loadWsVersions();
  } catch (e) {
    toast(e instanceof Error ? e.message : "恢复失败");
  }
}

function downloadWsCurrent(): void {
  if (wsOid.value) window.open(`/api/office-documents/${wsOid.value}/files`);
}

/** 派生数据处理失败后的重试：重新入队当前版本的 processor（幂等）。 */
async function retrySync(): Promise<void> {
  if (!wsOid.value) {
    void initWorkspace(); // 初始化失败（无 oid）时的重试 = 重新初始化
    return;
  }
  try {
    wsSync.value = "syncing";
    await api.retryProcessor(wsOid.value);
    toast("已重新触发派生数据处理");
    void pollWorkspace();
  } catch (e) {
    wsSync.value = "failed";
    toast(e instanceof Error ? e.message : "重试失败");
  }
}

/* ---------------- 素材插入（C-3c）：结束编辑 → 等 status=2 → 最新 base → insert → 重开 ---------------- */
type InsState =
  | "idle" | "confirming" | "closing_editor" | "waiting_save"
  | "inserting" | "processing" | "reopening" | "done" | "error";
const INS_BUSY = new Set<InsState>(["closing_editor", "waiting_save", "inserting", "processing", "reopening"]);
const INS_SOURCE_MAP: Record<string, string> = {
  material_insert: "素材插入", onlyoffice: "编辑保存", restore: "恢复",
  extract: "原始提取", init: "初始版本", upload: "上传",
};
const INS_FEATURE_MAP: Record<string, string> = {
  section_break: "分节符", footnote_endnote: "脚注/尾注", comments: "批注",
  tracked_changes: "修订记录", ole_object: "OLE/嵌入对象", content_control: "内容控件",
  internal_hyperlink: "内部锚点链接",
};

const insState = ref<InsState>("idle");
const insBusy = computed(() => INS_BUSY.has(insState.value));
const insMat = ref<Material | null>(null);
const insVerId = ref("");
const insVerList = ref<{ id: string; version: number; source: string; is_current: boolean }[]>([]);
const insMsg = ref("");
const insErr = ref("");
const wsEditorMounted = ref(true);
const wsMaterials = ref<Material[]>([]);

const insVerLabel = (v: { version: number; source: string; is_current: boolean }): string =>
  `v${v.version} ${v.is_current ? "当前版本 · " : ""}${INS_SOURCE_MAP[v.source] ?? v.source}`;

async function loadWsMaterials(): Promise<void> {
  const pid = cur.value?.id;
  if (!pid) {
    wsMaterials.value = [];
    return;
  }
  try {
    wsMaterials.value = (await api.materials()).items.filter((x) => x.project_id === pid);
  } catch {
    wsMaterials.value = [];
  }
}

function openInsert(mat: Material): void {
  // idle 正常入口；error 允许「重新确认」（刷新版本与基线后重来）
  if ((insState.value !== "idle" && insState.value !== "error") || !mat.office_document_id
      || !wsOid.value) {
    return;
  }
  insMat.value = mat;
  insVerId.value = "";
  insVerList.value = [];
  insMsg.value = "";
  insErr.value = "";
  insState.value = "confirming";
  void (async () => {
    try {
      const r = await api.officeVersions(mat.office_document_id as string);
      insVerList.value = r.items;
      insVerId.value = (r.items.find((x) => x.is_current) ?? r.items[0])?.id ?? "";
    } catch (e) {
      insState.value = "error";
      insErr.value = e instanceof Error ? e.message : "素材版本列表加载失败";
    }
  })();
}

function closeInsert(): void {
  if (insBusy.value) return; // mutation 进行中不允许退出（防半途状态）
  insMat.value = null;
  insMsg.value = "";
  insErr.value = "";
  wsEditorMounted.value = true; // 错误路径退出时恢复编辑器
  insState.value = "idle";
}

/** 等 DS 编辑会话结束（callback-state 不再处于编辑/保存中）。 */
async function waitSessionSettled(oid: string, timeoutS: number): Promise<boolean> {
  const ACTIVE = ["编辑中", "正在处理保存", "工作快照", "强制保存失败"];
  const deadline = Date.now() + timeoutS * 1000;
  while (Date.now() < deadline) {
    try {
      const cs = await api.officeCallbackState(oid);
      if (!ACTIVE.some((k) => cs.state.includes(k))) return true;
    } catch {
      /* 轮询失败继续等 */
    }
    await new Promise((r2) => setTimeout(r2, 2000));
  }
  return false;
}

/** 等 WorkspaceProcessor 追平（derived_from == current）。 */
async function waitDerivedSync(pid: string, oid: string, timeoutS: number): Promise<boolean> {
  const deadline = Date.now() + timeoutS * 1000;
  while (Date.now() < deadline) {
    try {
      const st = await api.workspaceState(pid);
      if (st.office_document_id === oid && st.derived_from_version_id
          && st.derived_from_version_id === st.current_version_id) {
        return true;
      }
    } catch {
      /* 继续等 */
    }
    await new Promise((r2) => setTimeout(r2, 2000));
  }
  return false;
}

async function confirmInsert(): Promise<void> {
  const mat = insMat.value;
  const pid = cur.value?.id;
  const oid = wsOid.value;
  if (!mat || !pid || !oid || !mat.office_document_id || insState.value !== "confirming") return;
  if (!insVerId.value) {
    insErr.value = "请选择素材版本";
    return;
  }
  insErr.value = "";
  const baseAtConfirm = wsVersion.value;

  // 1) 结束当前编辑会话（卸载 OfficeEditor → destroyEditor → DS 结算）
  insState.value = "closing_editor";
  wsEditorMounted.value = false;

  // 2) 等待编辑会话稳定（可能的 status=2 落版）
  insState.value = "waiting_save";
  insMsg.value = "正在保存当前工作稿…";
  if (!(await waitSessionSettled(oid, 90))) {
    insState.value = "error";
    insErr.value = "工作稿仍处于编辑状态，请稍后重试";
    return;
  }

  // 3) 重新获取最新 current 作为并发基线（禁用弹窗打开时的旧 base）
  let latest: Awaited<ReturnType<typeof api.workspaceState>>;
  try {
    latest = await api.workspaceState(pid);
  } catch (e) {
    insState.value = "error";
    insErr.value = e instanceof Error ? e.message : "无法获取工作稿最新版本";
    return;
  }
  if (!latest.current_version_id) {
    insState.value = "error";
    insErr.value = "无法获取工作稿最新版本，请重试";
    return;
  }
  if (latest.version != null && baseAtConfirm != null && latest.version > baseAtConfirm) {
    // 版本前进必须归因为刚结束的编辑保存，否则停止并要求重新确认
    const versions = (await api.officeVersions(oid)).items;
    const nv = versions.find((x) => x.id === latest.current_version_id);
    if (!nv || nv.source !== "onlyoffice") {
      insState.value = "error";
      insErr.value = `工作稿在保存期间发生了非编辑类版本变化（v${latest.version}），请重新确认插入`;
      return;
    }
    insMsg.value = `当前编辑已保存为 v${nv.version}，素材将插入到 v${nv.version} 末尾`;
  }
  const baseId = latest.current_version_id;

  // 4) 执行插入（423 有限重试；409/422 停下交给用户）
  insState.value = "inserting";
  let done: { version: number; version_id: string } | null = null;
  for (let attempt = 0; attempt < 3 && !done; attempt++) {
    try {
      done = await api.insertMaterial(oid, {
        base_workspace_version_id: baseId,
        material_id: mat.id,
        material_office_document_id: mat.office_document_id,
        material_version_id: insVerId.value,
        insert_mode: "append_end",
      });
    } catch (e) {
      const err = e as ApiError;
      if (err.status === 423) {
        insMsg.value = "工作稿仍在保存中…";
        await new Promise((r2) => setTimeout(r2, 5000));
        continue;
      }
      if (err.status === 409) {
        insState.value = "error";
        insErr.value = "工作稿在插入前发生了版本变化，已刷新到最新版本，请重新确认插入";
        return;
      }
      if (err.status === 422) {
        const feats: string[] =
          (err.detail as { features?: string[] } | undefined)?.features ?? [];
        const readable = feats.map((f) => INS_FEATURE_MAP[f.split(":")[0]] ?? f).join(" / ");
        insState.value = "error";
        insErr.value = `该素材暂不支持直接插入工作稿${readable ? `（检测到：${readable}）` : ""}`;
        return;
      }
      insState.value = "error";
      insErr.value = err.message || "插入失败";
      return;
    }
  }
  if (!done) {
    insState.value = "error";
    insErr.value = "工作稿仍处于编辑状态，请稍后重试";
    return;
  }

  // 5) 等 WorkspaceProcessor 追平 → 重开编辑器（:key 变化 → 新 key 干净重开）
  insState.value = "processing";
  insMsg.value = "正在同步派生数据…";
  await waitDerivedSync(pid, oid, 120);
  insState.value = "reopening";
  insMsg.value = "正在重新打开工作稿…";
  wsVersion.value = done.version;
  wsEditorMounted.value = true;
  void loadWsVersions();
  void pollWorkspace();

  const selV = insVerList.value.find((x) => x.id === insVerId.value);
  toast(`“${mat.title}” v${selV?.version ?? "?"} 已插入工作稿，工作稿已更新至 v${done.version}`);
  insMat.value = null;
  insState.value = "done";
  insState.value = "idle";
}

const hasDoc = computed(() => !!cur.value && bidDocs.value.length > 0);

/* ---------------- AI 助手（C-4c）：出网确认 → 生成 → 草稿编辑 → 审批写入 ---------------- */
type AiUiState =
  | "idle" | "egress_confirm" | "generating" | "draft_ready"
  | "closing_editor" | "waiting_save" | "applying" | "processing" | "reopening"
  | "rebind_confirm" | "done" | "error";
const AI_BUSY_STATES = new Set<AiUiState>([
  "closing_editor", "waiting_save", "applying", "processing", "reopening",
]);
const aiUi = ref<AiUiState>("idle");
const aiBusy = computed(() => AI_BUSY_STATES.has(aiUi.value));
const aiStep = ref<"compose" | "egress" | "result">("compose");
const aiCfg = ref<{ provider: string; model: string; has_key: boolean; base_url: string } | null>(null);
const aiInstruction = ref("");
const aiMaterialId = ref("");
const aiManual = ref("");
const aiPid = ref<string | null>(null);
/** 当前提案绑定的 Workspace 基线（生成时固定；关闭编辑器后据此比对，禁止静默改绑） */
const aiBaseVid = ref("");
const aiBaseVer = ref(0);
const aiDraft = ref("");
const aiModel = ref("");
const aiMsg = ref("");
const aiErr = ref("");
const aiTest = ref<{ ok: boolean; latency_ms: number } | null>(null);

const AI_FEATURE_MAP: Record<string, string> = {
  section_break: "分节符", footnote_endnote: "脚注/尾注", comments: "批注",
  tracked_changes: "修订记录", ole_object: "OLE/嵌入对象", content_control: "内容控件",
  internal_hyperlink: "内部锚点链接",
};

const aiMaterial = computed(() =>
  wsMaterials.value.find((m) => m.id === aiMaterialId.value && m.office_document_id) ?? null);

const aiEgressSummary = computed(() => ({
  instruction: aiInstruction.value.length,
  manual: aiManual.value.length,
  material: aiMaterial.value ? (aiMaterial.value.text || "").length : 0,
  model: aiCfg.value?.model || "—",
  host: (() => {
    try {
      const u = new URL(aiCfg.value?.base_url || "https://pending");
      return u.host;
    } catch {
      return "—";
    }
  })(),
}));

async function loadAiCfg(): Promise<void> {
  if (!wsOid.value) {
    aiCfg.value = null;
    return;
  }
  try {
    const c = await api.aiGatewayConfig(wsOid.value);
    aiCfg.value = { provider: c.provider, model: c.model, has_key: c.has_key, base_url: c.base_url };
  } catch {
    aiCfg.value = null;
  }
}

async function aiConnectionTest(): Promise<void> {
  if (!wsOid.value) return;
  aiTest.value = null;
  aiErr.value = "";
  try {
    aiTest.value = await api.aiGatewayTest(wsOid.value);
  } catch (e) {
    const err = e as ApiError;
    const d = err.detail as { message?: string } | undefined;
    aiErr.value = d?.message || err.message || "连接测试失败";
  }
}

function aiBegin(): void {
  if (aiUi.value !== "idle" && aiUi.value !== "error") return;
  if (!aiInstruction.value.trim()) {
    aiErr.value = "请先输入生成要求";
    return;
  }
  // 契约：上下文必须显式提交（至少一项），不允许隐式读取整份文档
  if (!aiMaterialId.value && !aiManual.value.trim()) {
    aiErr.value = "请至少提供一项上下文（引用素材或补充上下文）";
    return;
  }
  aiErr.value = "";
  aiStep.value = "egress"; // 第一次确认：允许把选定上下文发往云端
  aiUi.value = "egress_confirm";
}

const _AI_TEXT_MAX = 8000;
const _AI_TOTAL_MAX = 16000;

async function aiConfirmEgress(): Promise<void> {
  const oid = wsOid.value;
  const pid = cur.value?.id;
  if (!oid || !pid) return;
  const total = aiInstruction.value.length + aiManual.value.length
    + (aiMaterial.value ? (aiMaterial.value.text || "").length : 0);
  if (total > _AI_TOTAL_MAX) {
    aiErr.value = `上下文合计 ${total} 字，超出 ${_AI_TOTAL_MAX} 字出网预算，请缩减`;
    aiUi.value = "error";
    return;
  }
  aiErr.value = "";
  aiUi.value = "generating";
  aiMsg.value = "正在请求云端模型…";
  const st0 = await api.workspaceState(pid);
  const context: { kind: string; ref: string; label?: string; text?: string }[] = [];
  if (aiManual.value.trim()) {
    context.push({ kind: "manual_input", ref: "manual", label: "人工补充", text: aiManual.value });
  }
  if (aiMaterial.value?.office_document_id) {
    context.push({ kind: "material_excerpt", ref: aiMaterial.value.id,
                   label: aiMaterial.value.title });
  }
  try {
    const r = await api.aiGenerate(oid, {
      base_workspace_version_id: st0.current_version_id ?? "",
      instruction: aiInstruction.value,
      context,
    });
    aiPid.value = r.proposal.id;
    aiDraft.value = r.proposal.text || "";
    aiModel.value = r.proposal.model || "";
    aiBaseVid.value = st0.current_version_id ?? "";
    aiBaseVer.value = st0.version ?? 0;
    aiStep.value = "result";
    aiUi.value = "draft_ready";
  } catch (e) {
    const err = e as ApiError;
    const d = err.detail as { message?: string; code?: string } | undefined;
    aiUi.value = "error";
    aiErr.value = d?.message || err.message || "生成失败";
  }
}

async function aiSaveDraft(): Promise<void> {
  if (!aiPid.value) return;
  try {
    await api.aiProposalContent(aiPid.value, aiDraft.value);
    toast("草稿已保存修改");
  } catch (e) {
    toast(e instanceof Error ? e.message : "保存修改失败");
  }
}

async function aiReject(): Promise<void> {
  if (!aiPid.value) return;
  try {
    await api.aiProposalAction(aiPid.value, "reject");
    toast("AI 提案已放弃");
  } catch {
    /* 已是终态也视为放弃成功 */
  }
  aiReset();
}

function aiReset(): void {
  aiPid.value = null;
  aiDraft.value = "";
  aiMsg.value = "";
  aiErr.value = "";
  aiStep.value = "compose";
  aiUi.value = "idle";
  wsEditorMounted.value = true;
}

/** 写入工作稿：结束编辑会话 → 等真实 status=2 → 复查 base → 一致 approve/apply；不一致交用户重新确认。 */
async function aiWrite(): Promise<void> {
  const pid = cur.value?.id;
  const oid = wsOid.value;
  if (!pid || !oid || !aiPid.value || aiBusy.value) return;

  aiUi.value = "closing_editor";
  wsEditorMounted.value = false; // 结束编辑会话 → DS status=2

  aiUi.value = "waiting_save";
  aiMsg.value = "正在保存当前工作稿…";
  if (!(await waitSessionSettled(oid, 90))) {
    aiUi.value = "error";
    aiErr.value = "工作稿仍处于编辑状态，请稍后重试";
    return;
  }

  // 关闭编辑器后重新取得 Workspace current —— 与提案基线比对（不假设 Ctrl+S = 正式保存）
  const stNow = await api.workspaceState(pid);
  const latestVid = stNow.current_version_id ?? "";
  if (latestVid !== aiBaseVid.value) {
    // 基线不一致：暂停自动写入，交用户显式重新确认（禁止静默重绑/自动写入）
    aiUi.value = "rebind_confirm";
    aiMsg.value = `当前工作稿已从 v${aiBaseVer.value} 更新至 v${stNow.version ?? "?"}，请重新确认写入。`;
    return;
  }

  aiUi.value = "applying";
  aiMsg.value = "正在写入 AI 内容…";
  let applied: { status: string; applied_version_id: string | null } | null = null;
  try {
    // 人工确认 = approve（写入前必须显式审批，审批边界由 DB 层强制）
    await api.aiProposalAction(aiPid.value, "approve");
    applied = await api.aiProposalAction(aiPid.value, "apply");
  } catch (e) {
    const err = e as ApiError;
    const d = err.detail as { message?: string } | undefined;
    aiUi.value = "error";
    aiErr.value = d?.message || err.message || "写入失败";
    return;
  }
  await aiFinishWrite(pid, oid, applied);
}

/** 用户点「基于最新版本重新确认」：新建绑定最新 base 的提案（来源关联原提案，不再调用云 AI）→ 重新审批 → 写入。 */
async function aiRebindConfirm(): Promise<void> {
  const pid = cur.value?.id;
  const oid = wsOid.value;
  const oldPid = aiPid.value;
  if (!pid || !oid || !oldPid || aiBusy.value) return;
  aiErr.value = "";
  aiUi.value = "applying";
  aiMsg.value = "正在基于最新工作稿重新确认…";
  try {
    const stNow = await api.workspaceState(pid);
    // 新建提案：复用用户已确认的文本（无第二次云 AI 调用），保留原提案 id 作为来源关联
    const created = await api.aiProposalCreate(oid, {
      base_workspace_version_id: stNow.current_version_id ?? "",
      title: `重新确认写入（原提案 ${oldPid.slice(0, 8)}）`,
      text: aiDraft.value,
      operation: "append_end",
      model: aiModel.value || null,
      derived_from_proposal_id: oldPid,
    });
    aiPid.value = created.id;
    aiBaseVid.value = stNow.current_version_id ?? "";
    aiBaseVer.value = stNow.version ?? 0;
    await api.aiProposalAction(created.id, "approve"); // 重新经过人工审批
    const applied = await api.aiProposalAction(created.id, "apply");
    await aiFinishWrite(pid, oid, applied);
  } catch (e) {
    const err = e as ApiError;
    const d = err.detail as { message?: string } | undefined;
    aiUi.value = "error";
    aiErr.value = d?.message || err.message || "重新确认写入失败";
  }
}

/** 用户点「取消写入」：保留草稿与用户修改内容，不创建任何版本。 */
function aiCancelRebind(): void {
  aiUi.value = "draft_ready";
  aiMsg.value = "";
  aiErr.value = "";
  wsEditorMounted.value = true; // 恢复编辑器（工作稿内容已随 status=2 落版）
  void pollWorkspace();
}

async function aiFinishWrite(
  pid: string, oid: string,
  applied: { status: string; applied_version_id: string | null } | null,
): Promise<void> {
  if (!applied || !applied.applied_version_id) {
    aiUi.value = "error";
    aiErr.value = "写入未完成，请重试";
    return;
  }
  aiUi.value = "processing";
  aiMsg.value = "正在同步派生数据…";
  await waitDerivedSync(pid, oid, 120);

  aiUi.value = "reopening";
  aiMsg.value = "正在重新打开工作稿…";
  const stF = await api.workspaceState(pid);
  const finalV = stF.version ?? 0;
  wsVersion.value = finalV;
  wsEditorMounted.value = true;
  void loadWsVersions();
  void pollWorkspace();

  toast(`AI 内容已写入工作稿（v${finalV}）`);
  aiStep.value = "compose";
  aiInstruction.value = "";
  aiManual.value = "";
  aiUi.value = "done";
  aiUi.value = "idle";
}


/* ---------------- 检测面板 ---------------- */
const tab = ref<"scan" | "score" | "cases" | "people" | "mats" | "ai">("scan");
const filter = ref<string>("all");
const onlyOpen = ref(false);
const doneOpen = ref(false);
const openDetail = ref(new Set<string>());
const casesSort = ref<"match" | "time">("match");

const risks = ref<RiskItemT[]>([]);
const riskIndex = ref(0);
const detectedAt = ref<string | null>(null);
const hasResult = ref(false);
const detecting = ref(false);
const scoreRows = ref<string[][]>([]);

const pending = computed(() =>
  risks.value.filter((r) => !r.done && (filter.value === "all" || r.type === filter.value)),
);
const pendingFatal = computed(() => risks.value.filter((r) => r.sev === "fatal" && !r.done).length);
const doneList = computed(() => risks.value.filter((r) => r.done && (filter.value === "all" || r.type === filter.value)));
const countByType = (t: string): number => risks.value.filter((r) => t === "all" || r.type === t).length;

async function loadRisks(): Promise<void> {
  risks.value = [];
  hasResult.value = false;
  riskIndex.value = 0;
  detectedAt.value = null;
  scoreRows.value = [];
  if (!cur.value) return;
  try {
    const r = await api.risks(cur.value.id);
    risks.value = r.items;
    riskIndex.value = r.risk_index;
    detectedAt.value = r.detected_at;
    hasResult.value = r.has_result;
  } catch {
    /* 空态 */
  }
}

async function rescan(): Promise<void> {
  if (detecting.value || !cur.value) return;
  if (!hasDoc.value || !tenderDoc.value) {
    await askConfirm(
      "无法开始检测",
      `<p>「${cur.value.name}」还没有可检测的<b>投标文件</b>${tenderDoc.value ? "" : "或<b>招标文件</b>"}。废标检测需要以招标文件条款为基准，对投标文件逐条比对。</p><p style="margin-top:6px">在文档库上传并拆解文件后，即可运行检测。</p>`,
      "去文档库上传",
    ).then((yes) => {
      if (yes) {
        navTo("docs");
        openUpload(cur.value?.id ?? null, "bid");
      }
    });
    return;
  }
  detecting.value = true;
  try {
    const r = await api.runDetect(cur.value.id);
    risks.value = r.items;
    riskIndex.value = r.risk_index;
    detectedAt.value = r.detected_at;
    hasResult.value = true;
    scoreRows.value = r.score_rows ?? [];
    toast(`检测完成：${r.items.length} 项风险 · 指数 ${r.risk_index}`);
  } catch (e) {
    toast(e instanceof Error ? e.message : "检测失败");
  } finally {
    detecting.value = false;
  }
}

async function toggleDone(r: RiskItemT): Promise<void> {
  try {
    const resp = await api.markRiskDone(r.id, !r.done);
    r.done = !r.done;
    riskIndex.value = resp.risk_index;
    toast(r.done ? "已标记处理" : "已撤销处理标记");
  } catch (e) {
    toast(e instanceof Error ? e.message : "操作失败");
  }
}

function locate(r: RiskItemT): void {
  tab.value = "scan";
  toast(r.quote ? "引用原文：" + r.quote.slice(0, 40) : "未找到原文引用");
}

function downloadReport(): void {
  if (!cur.value) return;
  window.open(api.detectReportUrl(cur.value.id));
}

watch(() => store.activeProjectId, loadRisks);

function idxClass(): string {
  if (!hasResult.value || !risks.value.length) return "lv-na";
  return riskIndex.value >= 80 ? "lv-red" : riskIndex.value >= 50 ? "lv-amber" : "lv-green";
}
function idxLabel(): string {
  if (!hasResult.value || !risks.value.length) return hasResult.value ? "低风险" : "尚未检测";
  return riskIndex.value >= 80 ? "高风险" : riskIndex.value >= 50 ? "中风险" : riskIndex.value > 0 ? "低风险" : "已清零";
}

/* ---------------- 评分对照 / 案例 / 人员 ---------------- */
const cases = ref<HistCase[]>([]);
const peopleCards = ref<PersonCard[]>([]);

async function loadPanels(): Promise<void> {
  cases.value = [];
  peopleCards.value = [];
  if (!cur.value) return;
  try {
    const cc = await api.cases(cur.value.id);
    cases.value = cc.items;
    const pp = await api.people(cur.value.id);
    peopleCards.value = pp.items;
  } catch {
    /* 空态 */
  }
}

const sortedCases = computed(() =>
  cases.value.slice().sort((a, b) =>
    casesSort.value === "match"
      ? b.match - a.match
      : (b.open_at ?? "").localeCompare(a.open_at ?? ""),
  ),
);

watch(() => store.activeProjectId, loadPanels);
watch(cur, (p) => {
  // 项目切换/首次加载：初始化（或切换）工作稿
  if (p) void initWorkspace();
});
watch(tenderDoc, loadPanels);

function goEditOriginal(): void {
  navTo("docs");
}

function goLib(): void {
  navTo("lib");
}
function goDocsUpload(): void {
  navTo("docs");
  openUpload(cur.value?.id ?? null, "bid");
}
async function doExport(fmt?: string): Promise<void> {
  const f = fmt ?? "Word";
  if (fmt === "检测报告") {
    if (hasResult.value) downloadReport();
    else toast("请先运行废标检测");
    return;
  }
  // F3.3 导出拦截：存在未处理致命项时确认
  if (pendingFatal.value > 0) {
    const yes = await askConfirm(
      "导出前确认",
      `<p>仍有 <b>${pendingFatal.value} 项致命风险</b>未处理，导出的${f}将包含这些问题内容。</p><p style="margin-top:6px">建议先在「废标检测」面板处理致命项，或确认本次导出用于内部评审。</p>`,
      "继续导出",
    );
    if (!yes) return;
  }
  if (f === "Word") {
    const d = bidDocs.value[0];
    if (!d) {
      toast("没有可导出的投标文件");
      return;
    }
    window.open(`/api/documents/${bidDocs.value[0]?.id ?? ""}/download`);
    toast("已导出 Word（投标文件原件）");
    return;
  }
}


watch(
  () => store.projects.length,
  (n) => {
    if (n && !store.projects.some((x) => x.id === store.activeProjectId)) {
      store.activeProjectId = store.projects.find((x) => !x.is_archived)?.id ?? store.projects[0].id;
    }
  },
  { immediate: true },
);

watch(() => store.activeProjectId, loadWsMaterials);
watch(() => store.activeProjectId, loadAiCfg);

onMounted(async () => {
  try {
    wsSetup.value = await api.getWorkspace();
  } catch { /* 健康检查失败时下次再试 */ }
  await initWorkspace();
  await loadPanels();
  await refreshCore();
  void loadWsMaterials();
  void loadAiCfg();
});

/* ---------------- E-1A：工作区首启引导 ---------------- */
const wsSetup = ref<WorkspaceInfo | null>(null);
const wsSetupPath = ref("D:\\标书工作区");
const wsSetupName = ref("我的标书工作区");
const wsSetupBusy = ref(false);
async function createWs(): Promise<void> {
  if (wsSetupBusy.value) return;
  wsSetupBusy.value = true;
  try {
    wsSetup.value = await api.createWorkspace(wsSetupPath.value.trim(), wsSetupName.value.trim());
    toast("工作区已创建，文件将按目录协议归档");
  } catch (e) {
    toast(e instanceof Error ? e.message : "创建失败");
  } finally {
    wsSetupBusy.value = false;
  }
}
</script>

<template>
  <div class="viewscroll">
    <!-- E-1A：工作区首启引导（未创建工作区时强制先行） -->
    <div v-if="wsSetup && !wsSetup.active" class="xempty" style="position:fixed;inset:60px 0 0 0;z-index:60;background:var(--bg);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px">
      <b style="font-size:16px">欢迎使用标书工作台 · 创建您的标书工作区</b>
      <p class="sub-note" style="max-width:520px;text-align:center">
        工作区是您存放所有标书文件的本地文件夹（项目招标/投标文件、素材、中转）。
        系统在此文件夹内按目录协议组织文件，数据库仅保存索引与分析结果，您的文件始终归您所有。
      </p>
      <div style="display:flex;gap:8px;align-items:center">
        <input v-model="wsSetupPath" class="ipt" style="width:320px" placeholder="例如 D:\标书工作区" />
        <input v-model="wsSetupName" class="ipt" style="width:160px" placeholder="工作区名称" />
        <button class="act pri" :disabled="wsSetupBusy" @click="createWs">{{ wsSetupBusy ? "创建中…" : "创建工作区" }}</button>
      </div>
      <p class="sub-note">建议选择独立目录（如 D:\标书工作区），不要放在系统或应用安装目录内。</p>
    </div>
    <!-- 项目上下文条 -->
    <header class="ctx">
      <span class="ctx-label">项目</span>
      <div class="dd">
        <button class="dd-btn" @click.stop="ddOpen = !ddOpen; expOpen = false">
          <span>{{ cur ? cur.name : "—" }}</span>
          <svg class="caret" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6" /></svg>
        </button>
        <div v-if="ddOpen" class="dd-menu">
          <button v-for="d in store.projects.filter((x) => !x.is_archived)" :key="d.id" class="dd-item" @click="pickProject(d)">
            <span class="chk">{{ d.id === store.activeProjectId ? "✓" : "" }}</span>{{ d.name }}<span class="dcode">{{ d.code }}</span>
          </button>
          <div class="dd-new">
            <button class="dd-item" @click="ddOpen = false; navTo('lib')">＋ 新建项目（前往项目库）</button>
          </div>
        </div>
      </div>
      <span class="ctx-code">{{ cur ? cur.code : "—" }}</span>
      <span v-if="cur" class="badge-due" :class="dueInfo(cur).cls">{{ dueInfo(cur).text }}</span>
      <span class="ctx-owner">招标人：{{ cur ? cur.cust || "—" : "—" }}</span>
      <div class="ctx-actions">
        <button class="link-btn" @click="cur && openDrawer(cur.id)">项目详情</button>
        <button class="btn-primary" :disabled="detecting" @click="rescan">
          <svg viewBox="0 0 24 24"><path d="M9 3h6M10 3v5.2L5.6 16A3 3 0 0 0 8.2 20.5h7.6A3 3 0 0 0 18.4 16L14 8.2V3" /></svg>
          <span>{{ detecting ? "检测中…" : "开始检测" }}</span>
        </button>
        <div class="split">
          <button class="exp-main" @click="doExport()">导出 Word<span v-if="pendingFatal > 0" class="n-badge">{{ pendingFatal }}</span></button>
          <button class="exp-caret" @click.stop="expOpen = !expOpen; ddOpen = false">
            <svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6" /></svg>
          </button>
          <div v-if="expOpen" class="menu">
            <button @click="expOpen = false; doExport('Word')">导出 Word</button>
            <button @click="expOpen = false; doExport('PDF')">导出 PDF</button>
            <button @click="expOpen = false; doExport('检测报告')">导出检测报告</button>
          </div>
        </div>
      </div>
    </header>

    <div class="workspace">
      <!-- 左：文档预览 -->
      <section class="col col-doc">
        <div class="ws-toolbar">
          <span class="ws-title">{{ cur ? cur.name : "" }} · 投标工作稿</span>
          <span class="verbadge" v-if="wsVersion">v{{ wsVersion }}</span>
          <span class="syncbadge" v-if="wsSync === 'staged'">修改已暂存</span>
          <span class="syncbadge" v-else-if="wsSync === 'creating'">正在生成正式版本…</span>
          <span class="syncbadge ok" v-else-if="wsSync === 'saved'">已保存</span>
          <span class="syncbadge warn" v-else-if="wsSync === 'syncing'">派生数据同步中…</span>
          <span class="syncbadge warn" v-else-if="wsSync === 'failed'">
            同步失败<button class="retry" @click="retrySync">重试</button>
          </span>
          <span class="syncbadge ok" v-else-if="wsSync === 'synced'">已同步</span>
          <span class="tool-sep" />
          <button class="mini-btn" :disabled="!wsOid" @click="wsVerOpen = !wsVerOpen; loadWsVersions()">历史版本</button>
          <button class="mini-btn" :disabled="!wsOid" @click="downloadWsCurrent">下载当前</button>
          <button class="mini-btn" :disabled="!wsOid" @click="openNative(wsOid)">原生编辑</button>
          <button class="mini-btn" :disabled="!wsOid" @click="doExport()">导出 Word</button>
        </div>
        <div class="ws-stage">
          <template v-if="cur">
            <OfficeEditor v-if="wsOid && wsEditorMounted" :key="`${wsOid}-${wsVersion}`" :office-document-id="wsOid" mode="edit" />
            <div v-else class="empty-state" style="margin:auto"><b>工作稿初始化中…</b></div>
          </template>
          <div v-else class="empty-state" style="margin:auto"><b>请先在顶部选择项目</b></div>
        </div>
        <div v-if="wsVerOpen" class="modal-wrap" @click.self="wsVerOpen = false">
          <div class="modal" role="dialog" aria-modal="true" aria-label="工作稿历史版本">
            <h3>工作稿历史版本</h3>
            <div class="verlist">
              <div v-for="v in wsVerList" :key="v.id" class="verrow" :class="{ cur: v.is_current }">
                <div class="vl">
                  <b>v{{ v.version }}</b>
                  <span class="chip-tag">{{ { init: "初始化", onlyoffice: "编辑保存", restore: "恢复", upload: "上传", extract: "原始提取", material_insert: "素材插入", ai_generate: "AI 生成" }[v.source] || v.source }}</span>
                  <span class="vm">{{ v.created_at ? v.created_at.slice(0, 19).replace("T", " ") : "" }}{{ v.size ? " · " + Math.round(v.size / 1024) + " KB" : "" }}<template v-if="v.comment"> · “{{ v.comment }}”</template></span>
                </div>
                <div class="vr">
                  <span v-if="v.is_current" class="chip-role r-admin">当前版本</span>
                  <template v-else>
                    <button class="mini-btn" @click="restoreWsVersion(v)">恢复</button>
                  </template>
                </div>
              </div>
              <div v-if="!wsVerList.length" class="empty-state"><b>暂无历史版本</b></div>
            </div>
            <div class="m-foot"><button class="btn-ghost" @click="wsVerOpen = false">关闭</button></div>
          </div>
        </div>
        <div v-if="insMat" class="modal-wrap" @click.self="closeInsert">
          <div class="modal" role="dialog" aria-modal="true" aria-label="插入素材到工作稿">
            <h3>插入素材到工作稿</h3>
            <div class="ins-form">
              <div class="frow"><span class="fl">素材</span><b>{{ insMat.title }}</b></div>
              <div class="frow">
                <span class="fl">素材版本</span>
                <select v-model="insVerId" class="f-select" :disabled="insBusy">
                  <option v-for="v in insVerList" :key="v.id" :value="v.id">{{ insVerLabel(v) }}</option>
                </select>
              </div>
              <div class="frow"><span class="fl">插入位置</span><span>工作稿末尾</span></div>
              <div class="frow"><span class="fl">当前工作稿</span><span>v{{ wsVersion ?? "—" }}</span></div>
              <div v-if="insMsg" class="ins-status">{{ insMsg }}</div>
              <div v-if="insErr" class="ins-error">
                <span>{{ insErr }}</span>
                <button v-if="insState === 'error' && insMat" class="mini-btn" @click="openInsert(insMat)">重新确认</button>
              </div>
            </div>
            <div class="m-foot">
              <button class="btn-ghost" :disabled="insBusy" @click="closeInsert">取消</button>
              <button class="btn-primary" :disabled="insState !== 'confirming' || !insVerId" @click="confirmInsert">继续</button>
            </div>
          </div>
        </div>
      </section>


      <!-- 右：检测面板 -->
      <section class="col col-panel">
        <div class="tabs">
          <button v-for="t in [['scan', '废标检测'], ['score', '评分办法对照'], ['cases', '案例推荐'], ['people', '人员介绍'], ['mats', '素材'], ['ai', 'AI 助手']] as const"
                  :key="t[0]" class="tab" :class="{ on: tab === t[0] }" @click="tab = t[0]">{{ t[1] }}</button>
        </div>
        <div class="panel-body">
          <!-- AI 助手（C-4c） -->
          <div v-show="tab === 'ai'" class="tabpane">
            <div class="ai-status">
              <span class="ai-dot" :class="aiCfg?.has_key && aiCfg.model ? 'on' : 'off'" />
              <template v-if="aiCfg?.provider === 'openai_compatible' && aiCfg.model">
                已连接 · {{ aiCfg.model }}
              </template>
              <template v-else-if="aiCfg?.provider === 'mock'">Mock Provider（开发/测试）</template>
              <template v-else>AI 服务未配置</template>
              <button class="mini-btn" :disabled="aiBusy || !wsOid" @click="aiConnectionTest">测试连接</button>
            </div>
            <div v-if="aiTest" class="ai-test">{{ aiTest.ok ? `连接正常（${aiTest.latency_ms}ms）` : "连接失败" }}</div>

            <template v-if="aiStep === 'compose' || aiStep === 'egress'">
              <div class="ai-form">
                <label class="fl">生成要求</label>
                <textarea v-model="aiInstruction" rows="3" :disabled="aiBusy"
                          placeholder="例如：生成售后服务与质保承诺" />
                <label class="fl">引用素材</label>
                <select v-model="aiMaterialId" :disabled="aiBusy">
                  <option value="">不引用素材</option>
                  <option v-for="mt in wsMaterials.filter((x) => x.office_document_id)"
                          :key="mt.id" :value="mt.id">{{ mt.title }}</option>
                </select>
                <label class="fl">补充上下文</label>
                <textarea v-model="aiManual" rows="2" :disabled="aiBusy"
                          placeholder="手动输入需要 AI 参考的要求或事实" />
              </div>
              <div v-if="aiStep === 'egress'" class="ai-egress">
                <b>出网确认</b>
                <div>以下内容将发送至云端模型（{{ aiEgressSummary.host }} · {{ aiEgressSummary.model }}）：</div>
                <div>· 生成要求 {{ aiEgressSummary.instruction }} 字</div>
                <div>· 补充上下文 {{ aiEgressSummary.manual }} 字</div>
                <div v-if="aiMaterial">· 素材摘录「{{ aiMaterial.title }}」（发送前由服务端截断至 8000 字）</div>
                <div class="ai-note">生成结果仅作为草稿提案，需人工确认后才会写入工作稿。</div>
                <button class="btn-primary" :disabled="aiBusy" @click="aiConfirmEgress">确认出网并生成</button>
              </div>
              <div v-if="aiErr" class="ins-error"><span>{{ aiErr }}</span></div>
              <button v-if="aiStep === 'compose'" class="btn-primary ai-gen"
                      :disabled="aiBusy || !wsOid || !aiInstruction.trim()" @click="aiBegin">生成候选内容</button>
            </template>

            <template v-if="aiStep === 'result'">
              <div class="ai-result-head">
                <b>AI 生成结果</b>
                <span class="chip-tag">待确认 · {{ aiModel || "mock" }}</span>
              </div>
              <textarea v-model="aiDraft" rows="6" class="ai-draft" :disabled="aiBusy" />
              <div class="ai-actions">
                <button class="mini-btn" :disabled="aiBusy" @click="aiSaveDraft">保存修改</button>
                <button class="mini-btn" :disabled="aiBusy" @click="aiReject">放弃</button>
                <button class="btn-primary" :disabled="aiBusy || aiUi === 'rebind_confirm'" @click="aiWrite">写入工作稿</button>
              </div>
              <div v-if="aiMsg" class="ins-status">{{ aiMsg }}</div>
              <div v-if="aiErr" class="ins-error"><span>{{ aiErr }}</span></div>
              <div v-if="aiUi === 'rebind_confirm'" class="ai-rebind">
                <div>工作稿已在编辑会话结束时正式保存，AI 提案绑定的基线已过期。</div>
                <div>重新确认将创建绑定最新工作稿版本的新提案（保留你已确认的内容与来源关联），并再次经过人工审批。</div>
                <div class="ai-actions">
                  <button class="mini-btn" @click="aiCancelRebind">取消写入</button>
                  <button class="btn-primary" @click="aiRebindConfirm">基于最新版本重新确认</button>
                </div>
              </div>
            </template>

            <div v-if="aiBusy" class="ins-status" style="margin-top:10px">{{ aiMsg }}</div>
          </div>
          <!-- 素材插入（C-3c） -->
          <div v-show="tab === 'mats'" class="tabpane">
            <div v-if="!wsMaterials.length" class="empty-state"><b>本项目暂无素材</b></div>
            <template v-else>
              <div v-for="mt in wsMaterials" :key="mt.id" class="insrow">
                <div class="il">
                  <b>{{ mt.title }}</b>
                  <span class="chip-tag">{{ mt.cat }} · {{ mt.type }}</span>
                  <span v-if="!mt.office_document_id" class="chip-tag">未 Word 化</span>
                </div>
                <div class="vr">
                  <button class="mini-btn" @click="navTo('mats')">预览</button>
                  <button v-if="mt.office_document_id" class="mini-btn on" :disabled="insState !== 'idle' || !wsOid"
                          @click="openInsert(mt)">插入工作稿</button>
                </div>
              </div>
            </template>
          </div>
          <!-- 废标检测 -->
          <div v-show="tab === 'scan'" class="tabpane">
            <div class="ov">
              <div class="ov-top">
                <span class="ov-idx" :class="idxClass()">{{ hasResult && risks.length ? riskIndex : "—" }}</span>
                <span class="ov-label">废标风险指数<br><span style="font-size:10.5px">0 安全 · 100 极高</span></span>
                <span class="ov-lv" :class="idxClass()">{{ idxLabel() }}</span>
              </div>
              <div class="ov-counts">
                <template v-if="hasResult && risks.length">
                  <span><span class="dot dot-fatal" />致命 {{ risks.filter((r) => r.sev === "fatal" && !r.done).length }}</span>
                  <span><span class="dot dot-risk" />风险 {{ risks.filter((r) => r.sev === "risk" && !r.done).length }}</span>
                  <span><span class="dot dot-hint" />提示 {{ risks.filter((r) => r.sev === "hint" && !r.done).length }}</span>
                </template>
                <span v-else style="color:var(--dim)">{{ hasResult ? "本次检测未发现废标风险" : "运行检测后，这里将显示风险分级计数" }}</span>
              </div>
              <div class="ov-sub">
                <span class="ov-time">{{ detectedAt ? "最近检测：" + detectedAt.slice(5, 16).replace("T", " ") : "尚未运行检测" }}</span>
                <span style="display:flex;gap:6px">
                  <button v-if="hasResult" class="mini-btn" @click="downloadReport">导出报告</button>
                  <button class="mini-btn" :disabled="detecting" @click="rescan">
                    <svg viewBox="0 0 24 24"><path d="M20 11a8 8 0 1 0-2.3 6.3" /><path d="M20 5v6h-6" /></svg>{{ detecting ? "检测中…" : "重新检测" }}
                  </button>
                </span>
              </div>
              <div class="chips">
                <button v-for="t in ['all', ...RISK_TYPES]" :key="t" class="chip" :class="{ on: filter === t }" :disabled="!risks.length" @click="filter = t">
                  {{ t === "all" ? "全部" : t }}<span class="n">{{ countByType(t) }}</span>
                </button>
                <span class="switch-wrap">只看未处理<button class="sw" role="switch" :aria-checked="onlyOpen" @click="onlyOpen = !onlyOpen" /></span>
              </div>
            </div>

            <div v-if="!hasResult" class="emptystate es-undet">
              <div class="es-icon"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.2-4.2" /></svg></div>
              <div class="es-title">该项目尚未运行检测</div>
              <p class="es-text">点击「开始检测」后，这里将显示风险分级与处理进度</p>
            </div>
            <div v-else-if="!risks.length" class="emptystate es-clean">
              <div class="es-icon"><svg viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5" /></svg></div>
              <div class="es-title">未发现废标风险</div>
              <p class="es-text">最近检测 {{ detectedAt }} · 七类规则检查全部通过</p>
              <div class="es-act"><button class="act" @click="rescan">重新检测</button></div>
            </div>

            <div class="risklist">
              <article
                v-for="r in pending"
                :key="r.id"
                class="rc"
                :class="{ active: openDetail.has(r.id) }"
              >
                <div class="rc-head">
                  <span class="sev" :class="r.sev === 'fatal' ? 'sev-fatal' : r.sev === 'risk' ? 'sev-risk' : 'sev-hint'">
                    {{ r.sev === "fatal" ? "致命" : r.sev === "risk" ? "风险" : "提示" }}
                  </span>
                  <span class="tchip">{{ r.type }}</span>
                  <h4>{{ r.title }}</h4>
                  <button class="rc-done" :aria-pressed="r.done" :title="r.done ? '撤销已处理' : '标记已处理'" @click="toggleDone(r)">✓</button>
                </div>
                <p class="rc-meta">{{ r.meta }} <span v-if="r.page_no" class="loc">P{{ r.page_no }}</span></p>
                <div class="rc-actions">
                  <button class="act pri" @click="locate(r)">定位原文</button>
                  <button class="act" @click="openDetail.has(r.id) ? openDetail.delete(r.id) : openDetail.add(r.id)">
                    {{ openDetail.has(r.id) ? "收起建议" : "修改建议" }}
                  </button>
                  <button v-if="r.fix" class="act" @click="toast('「' + r.fix + '」在后续版本接入真实流程')">{{ r.fix }}</button>
                </div>
                <div v-if="openDetail.has(r.id)" class="rc-detail">
                  <div>
                    <div class="lab">依据</div>
                    <p class="req">{{ r.req }}</p>
                  </div>
                  <template v-if="r.quote">
                    <div>
                      <div class="lab">原文引用</div>
                      <blockquote>{{ r.quote }}</blockquote>
                    </div>
                  </template>
                  <div>
                    <div class="lab">修改建议</div>
                    <p class="adv">{{ r.advice }}</p>
                  </div>
                </div>
              </article>
            </div>

            <template v-if="doneList.length && !onlyOpen">
              <div class="done-head" :class="{ open: doneOpen }">
                <button @click="doneOpen = !doneOpen">
                  <svg class="caret" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6" /></svg>
                  已处理（{{ doneList.length }}）
                </button>
              </div>
              <div v-show="doneOpen" class="risklist">
                <article v-for="r in doneList" :key="r.id" class="rc is-done">
                  <div class="rc-head">
                    <span class="sev" :class="r.sev === 'fatal' ? 'sev-fatal' : r.sev === 'risk' ? 'sev-risk' : 'sev-hint'">
                      {{ r.sev === "fatal" ? "致命" : r.sev === "risk" ? "风险" : "提示" }}
                    </span>
                    <span class="tchip">{{ r.type }}</span>
                    <h4>{{ r.title }}</h4>
                    <button class="rc-done" :aria-pressed="true" @click="toggleDone(r)">✓</button>
                  </div>
                </article>
              </div>
            </template>
          </div>

          <!-- 评分办法对照 -->
          <div v-show="tab === 'score'" class="tabpane">
            <p class="demo-note">
              {{ tenderDoc && tenderDoc.status === 2 ? "解析自文档库拆解结果：《" + tenderDoc.filename + "》" : "对照招标评分办法逐项核对响应情况" }}
            </p>
            <div class="mini-table">
              <table>
                <thead><tr><th>评分项</th><th>投标响应</th><th>状态</th></tr></thead>
                <tbody>
                  <template v-if="scoreRows.length">
                    <tr v-for="(r, i) in scoreRows" :key="i">
                      <td>{{ r[0] }}</td>
                      <td>{{ r[2] }}</td>
                      <td><span class="st" :class="r[3] === 'ok' ? 'st-ok' : 'st-warn'">{{ r[4] }}</span></td>
                    </tr>
                  </template>
                  <tr v-else>
                    <td colspan="3" style="text-align:center;color:var(--dim);padding:18px 12px">
                      运行「开始检测」后，自动对照评分办法核对响应情况 · <span class="chip-unbind" style="margin-left:4px">招标文件缺件或未拆解</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- 案例推荐 -->
          <div v-show="tab === 'cases'" class="tabpane">
            <div class="cases-head">
              <span class="cases-title">同类中标案例 · 基于项目类型与招标要求匹配</span>
              <span class="sort-wrap">
                <button class="mini-btn" :class="{ on: casesSort === 'match' }" @click="casesSort = 'match'">按匹配度</button>
                <button class="mini-btn" :class="{ on: casesSort === 'time' }" @click="casesSort = 'time'">按中标时间</button>
              </span>
            </div>
            <div class="caselist">
              <article v-for="cs in sortedCases" :key="cs.id" class="case-card">
                <div class="case-head">
                  <span class="case-match" :class="cs.match >= 90 ? 'm-hi' : cs.match >= 70 ? 'm-mid' : 'm-lo'">{{ cs.match }}%</span>
                  <h4>{{ cs.name }}</h4>
                </div>
                <p class="case-meta">
                  {{ [cs.cust ? "客户 " + cs.cust : "", cs.industry ?? "", cs.amount ? "中标额 " + cs.amount : ""].filter(Boolean).join(" · ") || "—" }}
                </p>
                <div class="case-tags">
                  <span v-if="cs.open_at">开标 {{ cs.open_at }}</span>
                  <span v-if="cs.platform">{{ cs.platform }}</span>
                  <span v-if="cs.note">{{ cs.note }}</span>
                </div>
                <p class="case-why">客户历史合作项目 · 已归档中标</p>
                <div class="rc-actions">
                  <button class="act pri" @click="navTo('lib')">查看项目</button>
                </div>
              </article>
              <div v-if="!sortedCases.length" class="empty-state"><b>暂无历史合作案例</b>项目归档并登记「中标」结果后，将按行业/客户/名称匹配度自动推荐到这里</div>
            </div>
          </div>

          <!-- 人员介绍 -->
          <div v-show="tab === 'people'" class="tabpane">
            <div class="person">
              <div v-for="p in peopleCards" :key="p.name + p.role" class="p-row">
                <span class="p-ava">{{ p.name.slice(0, 1) }}</span>
                <span class="p-info">
                  <b>{{ p.name }} · {{ p.role }}</b>
                  <span>{{ p.certs.length ? p.certs.join(" · ") : "暂无证书线索" }}</span>
                </span>
                <span class="p-tag" :class="p.status">{{ p.status === "ok" ? "材料齐全" : "材料待补" }}</span>
              </div>
              <div v-if="!peopleCards.length" class="empty-state"><b>暂无人员团队素材</b>拆解投标文件后，团队成员与简历将汇总在这里</div>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>
