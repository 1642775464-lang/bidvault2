<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { api } from "../api/client";
import type { ImportBatchDetailT, ImportItemT, OutlineNodeT, ReaderBlockT } from "../api/client";
import type { RiskItemT, SearchHit } from "../api/types";
import type { DocItem, ExtractGroup } from "../api/types";
import { CATS, INDUSTRIES, fmtSize, openUpload, refreshCore, store, toast } from "../store";
import { isTableBlockV2, rowBlocks, twipsToPx } from "../utils/documentBlocks";
import type { ExtractRowV2 } from "../api/types";

const kindF = ref<"all" | "tender" | "bid" | "notice">("all");
const search = ref("");
const selId = ref("");
const docs = ref<DocItem[]>([]);
const extract = ref<ExtractGroup[]>([]);
const running = ref(false);
const extractingId = ref("");
const page = ref(1);
const PAGE_SIZE = 10;
const catFilter = ref("all");

/* 绑定弹窗 */
const bindOpen = ref(false);
const bindDoc = ref<DocItem | null>(null);
const bindChoice = ref<"existing" | "new" | "none">("existing");
const bindSel = ref("");
const bindNewName = ref("");
const bindNewInd = ref(INDUSTRIES[0]);
const bindNewErr = ref(false);
const projects = ref<{ id: string; name: string }[]>([]);
/* 原始文档（ONLYOFFICE）/ 拆解与AI分析 双页签 */
const docTab = ref<"original" | "analysis">("original");
const ooState = ref<"idle" | "loading" | "ready" | "error" | "unconfigured" | "nosource">("idle");
const ooError = ref("");
const ooHolder = ref<HTMLElement | null>(null);
const ooMode = ref<"view" | "edit">("view");
const ooVersion = ref<number | null>(null);
const ooSaveState = ref("");
const verOpen = ref(false);
const verList = ref<
  { id: string; version: number; source: string; editor_user: string | null; comment: string | null; created_at: string | null; is_current: boolean; size: number | null; restored_from_version_id: string | null }[]
>([]);
const viewingVid = ref<string | null>(null);
const viewingVer = ref<number | null>(null);
const restoring = ref(false);

async function loadVersions(): Promise<void> {
  const d = sel.value;
  if (!d) return;
  try {
    verList.value = (await api.onlyofficeVersions(d.id)).items;
  } catch {
    verList.value = [];
  }
}

function verById(vid: string): number | null {
  const v = verList.value.find((x) => x.id === vid);
  return v ? v.version : null;
}

function openVersionFile(v: { id: string }): void {
  window.open(`/api/onlyoffice/versions/${sel.value?.id}/${v.id}/file`);
}

function toggleVersions(): void {
  verOpen.value = !verOpen.value;
  if (verOpen.value) void loadVersions();
}

async function viewVersion(v: { id: string; version: number }): Promise<void> {
  viewingVid.value = v.id;
  viewingVer.value = v.version;
  await initOnlyoffice(v.id);
}

async function backToCurrent(): Promise<void> {
  viewingVid.value = null;
  viewingVer.value = null;
  await initOnlyoffice();
}

async function doRestore(v: { id: string; version: number }): Promise<void> {
  const d = sel.value;
  if (!d) return;
  if (!window.confirm(`确认恢复 v${v.version}？将以新建版本方式回滚（当前内容保留为历史版本），并自动重新拆解。`)) return;
  restoring.value = true;
  try {
    await api.restoreVersion(d.id, v.id);
    toast(`已恢复 v${v.version} 为新版本，正在重新拆解`);
    viewingVid.value = null;
    viewingVer.value = null;
    await initOnlyoffice();
    void pollVersion();
    void loadVersions();
  } catch (e) {
    toast(e instanceof Error ? e.message : "恢复失败");
  } finally {
    restoring.value = false;
  }
}
let ooPoll: ReturnType<typeof setInterval> | null = null;
let ooEditor: { destroyEditor: () => void } | null = null;
let ooScriptPromise: Promise<void> | null = null;

function loadOnlyofficeScript(scriptUrl: string): Promise<void> {
  if (ooScriptPromise) return ooScriptPromise;
  ooScriptPromise = new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = scriptUrl;
    el.onload = () => resolve();
    el.onerror = () => reject(new Error("api.js 加载失败"));
    document.head.appendChild(el);
  });
  return ooScriptPromise;
}

async function pollVersion(): Promise<void> {
  const d = sel.value;
  if (!d) return;
  try {
    const st = await api.onlyofficeVersion(d.id);
    if (st.version != null && st.version !== ooVersion.value) {
      ooVersion.value = st.version;
      ooSaveState.value = st.created_at ? `v${st.version} · ${st.created_at.slice(11, 16)}` : `v${st.version}`;
    }
  } catch {
    /* 忽略轮询失败 */
  }
}

function startPoll(): void {
  if (ooPoll) clearInterval(ooPoll);
  void pollVersion();
  ooPoll = setInterval(() => void pollVersion(), 20000);
}

function stopPoll(): void {
  if (ooPoll) clearInterval(ooPoll);
  ooPoll = null;
}

async function initOnlyoffice(vid?: string): Promise<void> {
  const d = sel.value;
  if (!d) return;
  ooState.value = "loading";
  ooError.value = "";
  try {
    const cfg = await api.editorConfig(d.id, vid);
    ooMode.value = cfg.mode === "edit" ? "edit" : "view";
    await loadOnlyofficeScript(cfg.scriptUrl);
    const DocsAPI = (window as unknown as { DocsAPI: { DocEditor: new (id: string, c: unknown) => { destroyEditor: () => void } } }).DocsAPI;
    if (!DocsAPI) throw new Error("DocsAPI 未就绪");
    await nextTick();
    if (!document.getElementById("onlyoffice-holder")) return;
    ooEditor?.destroyEditor();
    ooEditor = new DocsAPI.DocEditor("onlyoffice-holder", cfg.config);
    ooState.value = "ready";
    if (vid) {
      ooVersion.value = cfg.version ?? null;
      ooSaveState.value = `查看历史 v${cfg.version}`;
    } else {
      ooVersion.value = cfg.version ?? null;
      ooSaveState.value = cfg.version != null ? `v${cfg.version}` : "";
    }
    startPoll();
  } catch (e) {
    ooState.value = "error";
    ooError.value = e instanceof Error ? e.message : "加载失败";
  }
}

watch([selId, docTab], ([, tab]) => {
  if (tab === "original") void initOnlyoffice();
  else {
    ooEditor?.destroyEditor();
    ooEditor = null;
    ooState.value = "idle";
    stopPoll();
  }
});

const statusF = ref<"all" | "2" | "1" | "0">("all");
const extF = ref("all");
const projF = ref("all");

async function load(): Promise<void> {
  try {
    const params: Record<string, string> = {};
    if (kindF.value !== "all") params.kind = kindF.value;
    if (statusF.value !== "all") params.status = statusF.value;
    if (extF.value !== "all") params.file_type = extF.value;
    if (projF.value !== "all") params.project_id = projF.value;
    const r = await api.documents(params);
    docs.value = r.items;
  } catch (e) {
    toast(e instanceof Error ? e.message : "加载失败");
  }
}

const filtered = computed(() =>
  docs.value.slice().sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? "")).filter((d) => {
    const q = search.value.trim().toLowerCase();
    if (!q) return true;
    const pid = d.project_id ?? "";
    const pname = store.projects.find((p) => p.id === pid)?.name ?? "";
    return (d.filename + " " + pname).toLowerCase().includes(q);
  }),
);

const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / PAGE_SIZE)));
const pagedDocs = computed(() => filtered.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE));
watch([kindF, statusF, extF, projF], () => {
  page.value = 1;
  void load();
});
watch(search, () => {
  page.value = 1;
});

/** 拆解结构项 → 素材分类（与后端 guess_cat 同规则） */
function catOf(name: string): string {
  if (/身份证明|授权|基本情况/.test(name)) return "资质文件";
  if (/财务/.test(name)) return "财务状况";
  if (/业绩|供货|承接/.test(name)) return "业绩案例";
  if (/团队|简历/.test(name)) return "人员团队";
  if (/技术/.test(name)) return "技术方案";
  if (/商务/.test(name)) return "商务模板";
  return "投标格式";
}

const sel = computed(() => docs.value.find((d) => d.id === selId.value) ?? null);

/* ---------------- E-1C：历史资料导入 · 分类确认工作台 ---------------- */
const impOpen = ref(false);
const impBusy = ref(false);
const impBatches = ref<Array<{ batch_id: string; label: string; status: string; counts: Record<string, number>; total: number }>>([]);
const impBatch = ref<string | null>(null);
const impDetail = ref<ImportBatchDetailT | null>(null);
const impProject = ref("导入项目");
const impEdit = ref<Record<string, { kind: string; material_cat: string; duplicate_action: string }>>({});

function kindLabel(k: string): string {
  return { tender: "招标文件", bid: "投标文件", material: "素材", unknown: "待人工" }[k] ?? k;
}
function statusLabel(s: string): string {
  return {
    pending_confirm: "待确认", confirmed: "已确认", archived: "已归档",
    registered: "已建档", skipped: "已跳过", discarded: "已放弃", failed: "失败",
  }[s] ?? s;
}
const impPendingCount = computed(() =>
  (impDetail.value?.items ?? []).filter((x) => x.status === "pending_confirm").length);

async function openImp(): Promise<void> {
  impOpen.value = true;
  try {
    impBatches.value = (await api.importBatches()).items;
  } catch { /* 未建工作区时由引导层负责 */ }
}
function editOf(i: ImportItemT): { kind: string; material_cat: string; duplicate_action: string } {
  if (!impEdit.value[i.id]) {
    impEdit.value[i.id] = {
      kind: i.kind === "unknown" ? "unknown" : i.kind,
      material_cat: "企业资质",
      duplicate_action: "skip",
    };
  }
  return impEdit.value[i.id];
}
async function onImpFiles(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  if (!files.length) return;
  impBusy.value = true;
  try {
    const r = await api.uploadImportFiles(files, "历史资料导入");
    impBatch.value = r.batch_id;
    await loadImpBatch(r.batch_id);
    toast(`已导入 ${files.length} 个文件，请确认分类后归档`);
  } catch (err) {
    toast(err instanceof Error ? err.message : "导入失败");
  } finally {
    impBusy.value = false;
    input.value = "";
  }
}
async function loadImpBatch(batchId: string): Promise<void> {
  impBatch.value = batchId;
  impDetail.value = await api.importBatch(batchId);
  impEdit.value = {};
  for (const i of impDetail.value.items) editOf(i);
}
async function confirmImpAll(): Promise<void> {
  if (!impBatch.value || !impDetail.value) return;
  const decisions = impDetail.value.items
    .filter((x) => x.status === "pending_confirm")
    .map((x) => {
      const e = editOf(x);
      const d: Record<string, unknown> = {
        item_id: x.id, kind: e.kind, project_name: impProject.value.trim() || "导入项目",
        material_cat: e.material_cat, decompose: false, duplicate_action: e.duplicate_action,
      };
      if (e.kind === "tender") d.decompose = true;  // 招标：归档即自动拆解（既有规则）
      return d;
    });
  if (!decisions.length) { toast("没有待确认条目"); return; }
  impBusy.value = true;
  try {
    await api.confirmImport(impBatch.value, decisions);
    await loadImpBatch(impBatch.value);
    await refreshCore();
    toast("归档完成：招标文件已自动拆解，投标文件与素材按确认落位");
  } catch (err) {
    toast(err instanceof Error ? err.message : "确认失败");
  } finally {
    impBusy.value = false;
  }
}
async function discardImp(): Promise<void> {
  if (!impBatch.value) return;
  if (!window.confirm("放弃本次导入？中转暂存副本将被清理（您的原件不受影响）。")) return;
  await api.discardImportBatch(impBatch.value);
  impBatch.value = null;
  impDetail.value = null;
  toast("导入已放弃，暂存已清理");
}
async function undoImp(itemId: string): Promise<void> {
  if (!impBatch.value) return;
  const r = await api.undoImportItem(impBatch.value, itemId);
  if (r.ok) { await loadImpBatch(impBatch.value); }
  else toast(r.detail ?? "不可撤销");
}

/* ---------------- E-2B：结构保真阅读器 ---------------- */
const rdOpen = ref(false);
const rdBusy = ref(false);
const rdOid = ref("");
const rdTitle = ref("");
const rdNodes = ref<OutlineNodeT[]>([]);
const rdBlocks = ref<ReaderBlockT[]>([]);
const rdQuery = ref("");
const rdActive = ref<string | null>(null);
const rdRecs = ref<SearchHit[]>([]);
const rdMats = ref<Array<{ id: string; title: string; office_document_id: string | null }>>([]);
const rdPurpose = ref("项目负责人");
const rdInsertBusy = ref(false);
const rdActiveNode = computed(() => rdNodes.value.find((n) => n.id === rdActive.value) ?? null);
const rdRisks = ref<RiskItemT[]>([]);
const rdNav = ref<Record<string, number>>({});

async function openReader(): Promise<void> {
  const oid = sel.value?.office_document_id;
  if (!oid) { toast("该文档尚未建立 Office 档案"); return; }
  rdOid.value = oid;
  rdTitle.value = sel.value?.filename ?? "";
  rdOpen.value = true;
  rdBusy.value = true;
  rdRisks.value = [];
  rdRecs.value = [];
  try {
    const [o, r] = await Promise.all([api.officeOutline(oid), api.readerBlocks(oid)]);
    rdNodes.value = o.nodes;
    rdBlocks.value = r.blocks;
    buildRdNav();
    if (sel.value?.project_id) {
      try {
        rdRisks.value = ((await api.risks(sel.value.project_id)) as unknown as { items: RiskItemT[] }).items ?? [];
      } catch { /* 未运行检测时无风险数据 */ }
    }
  } catch (e) {
    toast(e instanceof Error ? e.message : "阅读器加载失败");
  } finally {
    rdBusy.value = false;
  }
}
function buildRdNav(): void {
  const m: Record<string, number> = {};
  let bi = 0;
  for (const n of rdNodes.value) {
    while (bi < rdBlocks.value.length && rdBlocks.value[bi].para_no < n.para_index) bi++;
    m[n.id] = rdBlocks.value[bi]?.i ?? 0;
  }
  rdNav.value = m;
}
function rdHit(n: OutlineNodeT): void {
  const el = document.getElementById(`rd-b-${rdNav.value[n.id] ?? 0}`);
  el?.scrollIntoView({ behavior: "smooth", block: "start" });
  rdActive.value = n.id;
  void rdRecommend(n.title);
}
function rdMatchesQ(b: ReaderBlockT): boolean {
  const q = rdQuery.value.trim();
  if (!q || b.type === "table" || b.type === "image") return false;
  return b.text.includes(q);
}
function rdRiskFor(text: string): RiskItemT | null {
  // 条款级风险标记（启发式）：风险标题/要求前 6 字命中正文块 → 标 ★（锚定在 E-2C 打通）
  for (const r of rdRisks.value) {
    const key = (r.title || r.req || "").replace(/\s/g, "").slice(0, 6);
    if (key.length >= 4 && text.replace(/\s/g, "").includes(key)) return r;
  }
  return null;
}
function rdCopyCitation(n: OutlineNodeT): void {
  const cite = `《${rdTitle.value}》 ${n.title}（章节引用 ${n.id}）`;
  void navigator.clipboard?.writeText(cite);
  toast("已复制条款引用");
}
async function rdRecommend(title: string): Promise<void> {
  try {
    const r = await api.materials("", title);
    rdMats.value = r.items.filter((m) => !!m.office_document_id || !!m.file_name).slice(0, 6);
  } catch { rdMats.value = []; }
}
async function doInsertRef(m: { id: string; title: string }): Promise<void> {
  if (!rdOid.value || !rdActiveNode.value) { toast("请先在目录中选择章节"); return; }
  rdInsertBusy.value = true;
  try {
    const r = await api.insertReference({
      oid: rdOid.value, node_id: rdActiveNode.value?.id ?? null, material_id: m.id,
      purpose: rdPurpose.value, project_id: sel.value?.project_id ?? null,
    });
    if (r.writeback === "pending_writeback") {
      toast(`引用已生成新版本；目标文件被占用，候选已存中转：${r.note ?? ""}`);
    } else {
      toast(`引用插入完成，已生成新版本（用途：${rdPurpose.value}）`);
    }
  } catch (err) {
    toast(err instanceof Error ? err.message : "引用插入失败");
  } finally {
    rdInsertBusy.value = false;
  }
}
const rdHitCount = computed(() => rdQuery.value.trim() ? rdBlocks.value.filter(rdMatchesQ).length : 0);
function rdLevelStyle(n: OutlineNodeT): Record<string, string> {
  return { marginLeft: (n.level - 1) * 14 + "px", fontWeight: n.level === 1 ? "600" : "400" };
}
function rdNodeByPara(paraNo: number): OutlineNodeT | null {
  return rdNodes.value.find((n) => n.para_index === paraNo) ?? null;
}
const projName = computed(() => {
  const d = sel.value;
  if (!d) return "";
  return store.projects.find((p) => p.id === d.project_id)?.name ?? "";
});

async function pick(d: DocItem): Promise<void> {
  selId.value = d.id;
  extract.value = [];
  if (d.status === 2) {
    try {
      const r = await api.extract(d.id);
      extract.value = r.groups;
      await nextTick();
      recount();
    } catch {
      extract.value = [];
    }
  }
}

/** 后端组名即原型分组标签，直接按组渲染；右栏按素材分类筛选 */
const grouped = computed(() => {
  const out: { label: string; groups: ExtractGroup[] }[] = [];
  for (const g of extract.value) {
    const items = catFilter.value === "all" ? g.items : g.items.filter((it) => catOf(it.name) === catFilter.value);
    if (!items.length) continue;
    out.push({ label: g.name, groups: [{ ...g, items }] });
  }
  return out;
});

/* ---------- Word 画布编辑 ---------- */
const docPage = ref<HTMLElement | null>(null);
const wordCount = ref(0);
const dirty = ref(false);
const savingDoc = ref(false);
const pendingSaves: Promise<unknown>[] = [];

function recount(): void {
  if (docPage.value) {
    wordCount.value = (docPage.value.innerText || "").replace(/\s/g, "").length;
  }
}
function onDocInput(): void {
  dirty.value = true;
  recount();
}
function exec(cmd: string): void {
  docPage.value?.focus();
  document.execCommand(cmd);
}

async function saveDoc(): Promise<void> {
  if (!sel.value || !dirty.value || !docPage.value) return;
  savingDoc.value = true;
  try {
    let n = 0;
    // DOM 顺序收集三类片段：段落文本 / legacy 表格行 / V2 表格（带 tid）
    type Piece = { kind: "rows"; rows: string[][] } | { kind: "v2"; tid: number; el: HTMLTableElement };
    const byId: Record<string, Piece[]> = {};
    docPage.value.querySelectorAll<HTMLElement>("[data-item]").forEach((el) => {
      const id = el.getAttribute("data-item") || "";
      const tid = el.getAttribute("data-tid");
      if (el.tagName === "TABLE" && tid) {
        (byId[id] ??= []).push({ kind: "v2", tid: Number(tid), el: el as HTMLTableElement });
      } else if (el.tagName === "TABLE") {
        const rows: string[][] = [];
        el.querySelectorAll("tr").forEach((tr) => {
          const cells = Array.from(tr.querySelectorAll("td,th")).map((td) =>
            ((td as HTMLElement).innerText || "").replace(/\u00a0/g, " ").trim(),
          );
          if (cells.some((c) => c)) rows.push(cells);
        });
        (byId[id] ??= []).push({ kind: "rows", rows });
      } else {
        const line = (el.innerText || "").replace(/\u00a0/g, " ").trim();
        (byId[id] ??= []).push({ kind: "rows", rows: [[line]] });
      }
    });
    for (const [id, pieces] of Object.entries(byId)) {
      const it = extract.value.flatMap((g) => g.items).find((x) => x.id === id);
      if (!it) continue;
      // 以拆解骨架为基准：V2 行按 cell.id 从 DOM 回填文本（结构绝不重推），legacy 行按 DOM 顺序取回
      const textQueue: string[][] = [];
      const v2Map = new Map<number, HTMLTableElement>();
      for (const p of pieces) {
        if (p.kind === "v2") v2Map.set(p.tid, p.el);
        else p.rows.forEach((r) => textQueue.push(r));
      }
      const newRows: ExtractRowV2[] = [];
      let li = 0;
      for (const r of it.rows) {
        if (isTableBlockV2(r)) {
          const el = v2Map.get(r.tid);
          if (!el) {
            newRows.push(r);
            continue;
          }
          newRows.push({
            ...r,
            cells: r.cells.map((c) =>
              c.merged
                ? c
                : { ...c, text: ((el.querySelector(`td[data-cell-id="${c.id}"]`) as HTMLElement | null)?.innerText || "").replace(/\u00a0/g, " ").trim() },
            ),
          });
        } else {
          newRows.push(textQueue[li++] ?? r);
        }
      }
      while (li < textQueue.length) newRows.push(textQueue[li++]);
      if (JSON.stringify(newRows) === JSON.stringify(it.rows)) continue;
      await api.updateExtractItem(sel.value!.id, id, newRows).then(() => {
        it.rows = newRows;
        n++;
      });
    }
    dirty.value = false;
    toast(n ? `已保存 ${n} 个结构项` : "内容无变化");
  } catch (e) {
    toast(e instanceof Error ? e.message : "保存失败");
  } finally {
    savingDoc.value = false;
  }
}

async function doExtract(): Promise<void> {
  const d = sel.value;
  if (!d || d.status === 1 || extractingId.value) return;
  extractingId.value = d.id;
  toast(`「${d.filename}」开始拆解抽取…`);
  try {
    const r = await api.runExtract(d.id);
    toast(`拆解完成：${r.extracted} 个结构项已抽取${r.materials ? "，" + r.materials + " 份素材已入库" : ""}`);
    await load();
    await refreshCore();
    const fresh = docs.value.find((x) => x.id === d.id);
    if (fresh) await pick(fresh);
  } catch (e) {
    toast(e instanceof Error ? e.message : "拆解失败");
  } finally {
    extractingId.value = "";
  }
}

async function openBind(): Promise<void> {
  if (!sel.value) return;
  bindDoc.value = sel.value;
  bindChoice.value = "existing";
  bindNewErr.value = false;
  bindNewName.value = "";
  try {
    const r = await api.projects("active");
    projects.value = r.items.map((p) => ({ id: p.id, name: p.name }));
  } catch {
    projects.value = [];
  }
  bindSel.value = sel.value.project_id ?? "";
  bindOpen.value = true;
}
function chooseBind(b: "existing" | "new" | "none"): void {
  bindChoice.value = b;
}
async function saveBind(): Promise<void> {
  const d = bindDoc.value;
  if (!d) return;
  try {
    if (bindChoice.value === "existing") {
      if (!bindSel.value) {
        toast("请选择要绑定的项目");
        return;
      }
      await api.bind(d.id, { project_id: bindSel.value });
      toast(`已绑定到「${projects.value.find((p) => p.id === bindSel.value)?.name ?? ""}」`);
    } else if (bindChoice.value === "new") {
      const name = bindNewName.value.trim();
      if (!name) {
        bindNewErr.value = true;
        toast("请填写新项目名称");
        return;
      }
      await api.bind(d.id, { new_project_name: name, new_project_industry: bindNewInd.value });
      toast(`新项目「${name}」已创建并绑定`);
    } else {
      await api.bind(d.id, { project_id: null });
      toast("已解除绑定，文档保留在文档库");
    }
    bindOpen.value = false;
    await load();
    await refreshCore();
    const fresh = docs.value.find((x) => x.id === d.id);
    if (fresh) await pick(fresh);
  } catch (e) {
    toast(e instanceof Error ? e.message : "操作失败");
  }
}

function srcLabel(d: DocItem): string {
  return d.project_id ? "项目库上传" : "文档库上传";
}
function projChipName(d: DocItem): string {
  return store.projects.find((p) => p.id === d.project_id)?.name ?? "";
}

watch(filtered, (list) => {
  if (!list.length) {
    selId.value = "";
    extract.value = [];
    return;
  }
  if (!list.some((d) => d.id === selId.value)) pick(list[0]);
});

onMounted(async () => {
  await load();
  await refreshCore();
});
</script>

<template>
  <div class="viewscroll">
    <div class="docsub-bar">
      <div class="f-search">
        <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.2-4.2" /></svg>
        <input v-model="search" type="text" placeholder="搜索文档 / 项目">
      </div>
      <button v-for="k in ['all', 'tender', 'bid', 'notice'] as const" :key="k" class="f-chip" :class="{ on: kindF === k }" @click="kindF = k">
        {{ k === "all" ? "全部" : k === "tender" ? "招标文件" : k === "bid" ? "投标文件" : "招标公告" }}
      </button>
      <span class="bx"><select v-model="statusF">
        <option value="all">全部状态</option>
        <option value="2">已拆解</option>
        <option value="1">拆解中</option>
        <option value="0">未拆解</option>
      </select></span>
      <span class="bx"><select v-model="extF">
        <option value="all">全部格式</option>
        <option value="docx">Word</option>
        <option value="pdf">PDF</option>
        <option value="xlsx">Excel</option>
        <option value="img">图片</option>
      </select></span>
      <span class="bx"><select v-model="projF">
        <option value="all">全部项目</option>
        <option value="none">未绑定</option>
        <option v-for="p in store.projects" :key="p.id" :value="p.id">{{ p.name }}</option>
      </select></span>
      <button class="mini-btn" @click="openUpload(null, 'tender')">上传文档</button>
      <button class="mini-btn" @click="openImp">历史资料导入</button>
    </div>

    <div class="xwork">
      <div class="xlist">
        <div class="xlist-body">
          <button v-for="d in pagedDocs" :key="d.id" class="xitem" :class="{ on: d.id === selId }" @click="pick(d)">
            <span class="doc-ico" :class="d.kind">
              <svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6Z" /><path d="M14 3v4h4" /></svg>
            </span>
            <span class="nm">
              <b>{{ d.filename }}</b>
              <span>{{ fmtSize(d.size) }} · {{ (d.created_at ?? "").slice(0, 10) }}{{ d.project_id ? " · " + projChipName(d) : " · 未绑定" }}</span>
            </span>
            <span v-if="d.status === 2" class="st-x x-done">已拆解</span>
            <span v-else-if="d.status === 1" class="st-x x-run"><span class="run-dot" /> 拆解中</span>
            <span v-else-if="(d.error_msg ?? '').startsWith('OCR_UNAVAILABLE')" class="st-x x-miss">需要 OCR 扩展</span>
            <span v-else class="st-x x-none">未拆解</span>
          </button>
          <div v-if="!filtered.length" class="empty-state"><b>无匹配文档</b>调整筛选，或上传新文档</div>
        </div>
        <div v-if="totalPages > 1" class="pager">
          <button class="mini-btn" :disabled="page <= 1" @click="page -= 1">‹ 上一页</button>
          <span class="sub-note">{{ page }} / {{ totalPages }}</span>
          <button class="mini-btn" :disabled="page >= totalPages" @click="page += 1">下一页 ›</button>
        </div>
      </div>

      <div class="xpanel">
        <template v-if="sel">
          <div class="xp-head">
            <span class="t">
              <h3>{{ sel.filename }}</h3>
              <div class="m">{{ fmtSize(sel.size) }} · 上传 {{ (sel.created_at ?? "").slice(0, 10) }} · 来源：{{ srcLabel(sel) }}</div>
            </span>
            <span class="sp">
              <span class="kchip" :class="sel.kind === 'tender' ? 'k-tender' : sel.kind === 'notice' ? 'k-notice' : 'k-bid'">{{ sel.kind === "tender" ? "招标文件" : sel.kind === "notice" ? "招标公告" : "投标文件" }}</span>
              <span v-if="sel.needs_review" class="chip-miss">OCR 待复核 · 置信度 {{ ((sel.ocr_conf ?? 0) * 100).toFixed(0) }}%</span>
              <span v-else-if="sel.ocr_conf != null" class="chip-src">OCR 置信度 {{ ((sel.ocr_conf ?? 0) * 100).toFixed(0) }}%</span>
              <span v-if="sel.project_id" class="chip-proj">{{ projChipName(sel) }}</span>
              <span v-else class="chip-unbind">未绑定</span>
              <button class="act" @click="openBind()">绑定 / 换绑</button>
              <button v-if="sel.status !== 1" class="act pri" :disabled="!!extractingId" @click="doExtract()">
                {{ sel.status === 2 ? "重新拆解" : extractingId === sel.id ? "拆解中…" : "拆解抽取" }}
              </button>
            </span>
          </div>
          <div class="xp-body" style="display: flex; flex-direction: column; overflow: hidden; padding: 0">
            <div class="doctabs">
              <button :class="{ on: docTab === 'original' }" @click="docTab = 'original'">原始文档</button>
              <button :class="{ on: docTab === 'analysis' }" @click="docTab = 'analysis'">拆解与AI分析</button>
              <span v-if="docTab === 'original' && ooVersion" class="verbadge">{{ ooSaveState || ('v' + ooVersion) }}</span>
              <button v-if="docTab === 'original'" class="verbtn" @click="toggleVersions">历史版本</button>
            </div>
            <template v-if="docTab === 'original'">
              <div class="oo-stage">
                <div v-if="ooState === 'unconfigured'" class="empty-state"><b>ONLYOFFICE 未配置</b>设置 ONLYOFFICE_DOCUMENT_SERVER_PUBLIC_URL 后可在线查看与编辑原始文档</div>
                <div v-else-if="ooState === 'nosource'" class="empty-state"><b>该文档没有原始文件</b>仅拆解结果可查看（扫描件/转换件）</div>
                <div v-else-if="ooState === 'loading'" class="empty-state"><b>编辑器加载中…</b>{{ sel.filename }}</div>
                <div v-else-if="ooState === 'error'" class="empty-state"><b>编辑器加载失败</b>{{ ooError }}</div>
                <div v-if="ooState === 'ready' && viewingVid" class="oo-warn" style="display:flex;align-items:center;gap:12px">
                  <span>正在查看历史版本 v{{ viewingVer }}（只读）</span>
                  <button class="mini-btn" style="color:var(--risk)" @click="backToCurrent">返回当前版本</button>
                </div>
                <div v-else-if="ooState === 'ready' && ooMode === 'edit'" class="oo-warn">开发模式：当前修改不会保存（保存链路已接入，Ctrl+S 生成工作快照；关闭文档后生成正式版本）</div>
                <div v-show="ooState === 'ready'" id="onlyoffice-holder" ref="ooHolder" class="oo-holder"></div>
              </div>
            </template>
            <template v-else>
            <template v-if="grouped.length">
              <div class="wordbar">
                <span class="wname">{{ sel.filename }}</span>
                <span class="wcount">{{ wordCount }} 字</span>
                <button class="wbtn pri" :disabled="!dirty || savingDoc" @click="saveDoc">{{ savingDoc ? "保存中…" : "保存修改" }}</button>
              </div>
              <div class="wordtools">
                <button class="wtool" @click="exec('undo')">↶ 撤销</button>
                <button class="wtool" @click="exec('redo')">↷ 恢复</button>
                <span class="tool-sep" />
                <button class="wtool" @click="saveDoc()">保存</button>
              </div>
              <div class="wordstage">
                <div ref="docPage" class="wordpage" contenteditable="true" spellcheck="false" @input="onDocInput">
                  <template v-for="g in grouped" :key="g.label">
                    <div class="wp-sec" contenteditable="false">{{ g.label }}</div>
                    <template v-for="grp in g.groups" :key="grp.name">
                      <div class="wp-h" contenteditable="false">{{ grp.name }}</div>
                      <template v-for="it in grp.items" :key="it.id">
                        <template v-for="(blk, bi) in rowBlocks(it.rows)" :key="it.id + '-' + bi">
                          <table v-if="blk.type === 'table'" class="realtable" :data-item="it.id" :data-block="bi">
                            <tr v-for="(r, ri) in blk.rows" :key="ri">
                              <td v-for="(c, ci) in r" :key="ci" contenteditable="true" @input="onDocInput">{{ c }}</td>
                            </tr>
                          </table>
                          <table v-else-if="blk.type === 'tableV2'" class="realtable" :data-item="it.id" :data-block="bi" :data-tid="blk.v2!.tid">
                            <colgroup>
                              <col v-for="(w, wi) in blk.v2!.columnWidthsTwips" :key="wi" :style="w > 0 ? { width: twipsToPx(w) + 'px' } : undefined">
                            </colgroup>
                            <tr v-for="(row, ri) in blk.v2!.rows" :key="ri">
                              <template v-for="c in row.cells" :key="c.id">
                                <td v-if="!c.merged" :data-cell-id="c.id" :colspan="c.colSpan > 1 ? c.colSpan : undefined" :rowspan="c.rowSpan > 1 ? c.rowSpan : undefined" contenteditable="true" @input="onDocInput">{{ c.text }}</td>
                              </template>
                            </tr>
                          </table>
                          <p v-else class="wp-p" :data-item="it.id" :data-block="bi" contenteditable="true" @input="onDocInput">{{ blk.rows[0][0] }}</p>
                        </template>
                      </template>
                    </template>
                  </template>
                </div>
              </div>
              <div class="wordstatus">
                <span>字数统计：{{ wordCount }}</span>
                <span>{{ grouped.reduce((n, x) => n + x.groups.reduce((m, g2) => m + g2.items.length, 0), 0) }} 个结构项</span>
              </div>
            </template>
            <div v-else-if="sel.status === 0 && (sel.error_msg ?? '').startsWith('OCR_UNAVAILABLE')" class="xempty">
              <b>需要本地 OCR 扩展包</b>
              <p>{{ (sel.error_msg ?? "").replace("OCR_UNAVAILABLE::", "") }}</p>
              <p class="sub-note">本文档为扫描版 PDF 或图片型文件，文字识别由本地 OCR 扩展包完成（不出网）。</p>
              <button class="act pri" :disabled="!!extractingId" @click="doExtract()">安装扩展后重新拆解</button>
            </div>
            <div v-else-if="sel.status === 0" class="xempty">
              <b>这份文档还没有拆解</b>
              <p>拆解抽取将按{{ sel.kind === "tender" ? "「投标人须知前附表 / 评标办法前附表 / 投标文件格式 / 章节内容」" : "「法定代表人身份证明 / 授权委托书 / 基本情况表 / 财务状况 / 类似业绩 / 在履约项目 / 团队简历」" }}的结构输出，供评分对照与写作复用。</p>
              <button class="act pri" :disabled="!!extractingId" @click="doExtract()">开始拆解抽取</button>
            </div>
            <div v-else-if="sel.status === 1" class="xscan">
              <b>正在拆解：{{ sel.filename }}</b>
              <div class="bar"><i /></div>
              <span style="font-size:12px">{{ sel.kind === "tender" ? "解析前附表 / 评标办法 / 格式清单 / 章节目录…" : "解析资格文件 / 财务 / 业绩 / 团队简历…" }}</span>
            </div>
            <div v-else class="xempty"><b>未抽取到结构</b><p>本文档已拆解，但没有匹配到结构项。可尝试重新拆解或人工核对原文。</p></div>
            </template>
          </div>
        </template>
        <div v-else class="xempty"><b>选择左侧文档</b></div>
      </div>
    </div>

    <!-- 历史版本 -->
    <div v-if="verOpen" class="modal-wrap" @click.self="verOpen = false">
      <div class="modal" role="dialog" aria-modal="true" aria-label="历史版本">
        <h3>历史版本</h3>
        <div class="verlist">
          <div v-for="v in verList" :key="v.id" class="verrow" :class="{ cur: v.is_current }">
            <div class="vl">
              <b>v{{ v.version }}</b>
              <span class="chip-tag">{{ { upload: "上传", init: "上传", onlyoffice: "编辑保存", restore: "恢复" }[v.source] || v.source }}<template v-if="v.restored_from_version_id"> · 源自 v{{ verById(v.restored_from_version_id) }}</template></span>
              <span class="vm">{{ v.created_at ? v.created_at.slice(0, 19).replace("T", " ") : "" }}{{ v.size != null ? " · " + fmtSize(v.size) : "" }}{{ v.editor_user ? " · " + v.editor_user : "" }}</span>
              <span v-if="v.comment" class="vm">“{{ v.comment }}”</span>
            </div>
            <div class="vr">
              <span v-if="v.is_current" class="chip-role r-admin">当前版本</span>
              <template v-else>
                <button class="mini-btn" @click="viewVersion(v)">查看</button>
                <button class="mini-btn" @click="openVersionFile(v)">下载</button>
                <button class="mini-btn pri" :disabled="restoring" @click="doRestore(v)">{{ restoring ? "恢复中…" : "恢复" }}</button>
              </template>
            </div>
          </div>
          <div v-if="!verList.length" class="empty-state"><b>暂无历史版本</b></div>
        </div>
        <div class="m-foot">
          <button class="btn-ghost" @click="verOpen = false">关闭</button>
        </div>
      </div>
    </div>

    <!-- 绑定项目 -->
    <div v-if="bindOpen" class="modal-wrap" @click.self="bindOpen = false">
      <div class="modal" role="dialog" aria-modal="true" aria-label="绑定项目">
        <h3>绑定项目</h3>
        <p class="m-sub">{{ bindDoc?.filename }}</p>
        <div class="frm">
          <div class="bind-opt">
            <div class="bopt" :class="{ on: bindChoice === 'existing' }" @click="chooseBind('existing')">
              <span class="rd" /><span class="bt"><b>绑定到现有项目</b></span>
              <span class="bx" @click.stop><select v-model="bindSel"><option value="" disabled>选择项目…</option><option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option></select></span>
            </div>
            <div class="bopt" :class="{ on: bindChoice === 'new' }" @click="chooseBind('new')">
              <span class="rd" /><span class="bt"><b>新建项目并绑定</b></span>
              <span class="bx" @click.stop>
                <input v-model="bindNewName" type="text" placeholder="新项目名称 *" :class="{ err: bindNewErr }">
                <select v-model="bindNewInd"><option v-for="i in INDUSTRIES" :key="i">{{ i }}</option></select>
              </span>
            </div>
            <div class="bopt" :class="{ on: bindChoice === 'none' }" @click="chooseBind('none')">
              <span class="rd" /><span class="bt"><b>解除绑定</b></span>
            </div>
          </div>
        </div>
        <div class="m-foot">
          <button class="btn-ghost" @click="bindOpen = false">取消</button>
          <button class="btn-primary" @click="saveBind()">确认</button>
        </div>
      </div>
    </div>

    <!-- E-1C：历史资料导入 · 分类确认工作台 -->
    <div v-if="impOpen" class="modal-mask" @click.self="impOpen = false">
      <div class="modal" style="width:860px;max-width:94vw;max-height:86vh;display:flex;flex-direction:column">
        <div class="m-head" style="display:flex;justify-content:space-between;align-items:center">
          <h3 style="margin:0">历史资料导入 · 分类确认</h3>
          <button class="mini-btn" @click="impOpen = false">关闭</button>
        </div>
        <div style="padding:10px 14px;overflow:auto;flex:1">
          <template v-if="!impBatch">
            <div v-if="impBatches.length" style="margin-bottom:10px">
              <div class="sub-note" style="margin-bottom:6px">待处理 / 历史批次</div>
              <button v-for="b in impBatches" :key="b.batch_id"
                      class="mini-btn" style="display:block;width:100%;text-align:left;margin-bottom:6px"
                      @click="loadImpBatch(b.batch_id)">
                {{ b.label }} · {{ b.status }} · 共 {{ b.total }} 个
                <span v-if="b.counts.pending_confirm">（待确认 {{ b.counts.pending_confirm }}）</span>
              </button>
            </div>
            <label class="upzone" style="display:block;text-align:center;padding:26px;cursor:pointer">
              <input type="file" multiple accept=".zip,.docx,.doc,.pdf,.xlsx,.xls,.csv,.txt,.jpg,.jpeg,.png,.bmp"
                     @change="onImpFiles" />
              <b>选择文件或压缩包开始导入</b>
              <p class="sub-note">支持 zip / Word / PDF / Excel / 图片；系统仅给出分类建议，归档以您确认为准。</p>
            </label>
          </template>
          <template v-else>
            <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
              <span class="sub-note">归档项目名：</span>
              <input v-model="impProject" class="ipt" style="width:180px" />
              <button class="act pri" :disabled="impBusy || !impPendingCount" @click="confirmImpAll">
                {{ impBusy ? "执行中…" : `全部确认（${impPendingCount}）` }}
              </button>
              <button class="mini-btn" :disabled="impBusy" @click="discardImp">放弃导入</button>
              <button class="mini-btn" @click="impBatch = null; impDetail = null">返回列表</button>
            </div>
            <table style="width:100%;border-collapse:collapse;font-size:12.5px">
              <thead>
                <tr style="text-align:left;color:var(--dim)">
                  <th style="padding:4px 6px">文件</th>
                  <th style="padding:4px 6px">系统建议</th>
                  <th style="padding:4px 6px">确认分类</th>
                  <th style="padding:4px 6px">状态</th>
                  <th style="padding:4px 6px">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="i in impDetail?.items ?? []" :key="i.id" style="border-top:1px solid var(--line)">
                  <td style="padding:4px 6px">
                    {{ i.file_name }}
                    <div class="sub-note">{{ (i.size / 1024).toFixed(0) }} KB<span v-if="i.dup_note"> · {{ i.dup_note }}</span></div>
                  </td>
                  <td style="padding:4px 6px">
                    {{ kindLabel(i.kind) }} {{ i.confidence }}%
                    <div class="sub-note">{{ i.reasons.join("；") }}</div>
                  </td>
                  <td style="padding:4px 6px">
                    <template v-if="i.status === 'pending_confirm'">
                      <select v-model="impEdit[i.id].kind" style="width:110px">
                        <option value="tender">招标文件</option>
                        <option value="bid">投标文件</option>
                        <option value="material">素材</option>
                        <option value="unknown">待人工</option>
                      </select>
                      <select v-if="impEdit[i.id].kind === 'material'" v-model="impEdit[i.id].material_cat" style="width:110px;margin-left:4px">
                        <option>企业资质</option><option>人员简历</option><option>项目案例</option><option>技术方案</option>
                      </select>
                      <label v-if="i.dup_note" class="sub-note" style="display:block;margin-top:2px">
                        <input type="radio" :value="'skip'" v-model="impEdit[i.id].duplicate_action" /> 跳过
                        <input type="radio" :value="'keep_copy'" v-model="impEdit[i.id].duplicate_action" /> 保留副本
                      </label>
                    </template>
                    <template v-else>{{ kindLabel(i.decided_kind || i.kind) }}</template>
                  </td>
                  <td style="padding:4px 6px">{{ statusLabel(i.status) }}<div v-if="i.error" class="sub-note">{{ i.error }}</div></td>
                  <td style="padding:4px 6px">
                    <button v-if="i.status === 'archived'" class="mini-btn" @click="undoImp(i.id)">撤销归档</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </template>
        </div>
      </div>
    </div>
    <!-- E-2B：结构保真阅读器（只读；章节导航/搜索/条款引用/风险标记/素材推荐入口） -->
    <div v-if="rdOpen" style="position:fixed;inset:0;z-index:70;background:var(--bg);display:flex;flex-direction:column">
      <div style="display:flex;gap:10px;align-items:center;padding:8px 14px;border-bottom:1px solid var(--line)">
        <b>{{ rdTitle }} · 结构阅读</b>
        <span class="search"><input v-model="rdQuery" placeholder="文内搜索…" /><span v-if="rdQuery">{{ rdHitCount }} 处</span></span>
        <span style="flex:1" />
        <button class="mini-btn" @click="rdOpen = false">关闭阅读器</button>
      </div>
      <div style="flex:1;display:flex;overflow:hidden">
        <!-- 章节导航 -->
        <div style="width:250px;border-right:1px solid var(--line);overflow:auto;padding:8px 4px">
          <div class="sub-note" style="padding:0 8px 4px">目录（{{ rdNodes.length }}）</div>
          <button v-for="n in rdNodes" :key="n.id"
                  style="display:block;width:100%;text-align:left;background:none;border:none;padding:3px 8px;cursor:pointer;color:var(--ink)"
                  :style="rdLevelStyle(n)"
                  :class="{ 'rd-on': rdActive === n.id }"
                  @click="rdHit(n)">{{ n.title }}</button>
        </div>
        <!-- 正文块 -->
        <div ref="rdBodyEl" style="flex:1;overflow:auto;padding:18px 30px" class="rd-body">
          <div v-if="rdBusy" class="empty-state"><b>解析中…</b></div>
          <template v-else>
            <div v-for="b in rdBlocks" :key="b.i" :id="`rd-b-${b.i}`"
                 :class="{ 'rd-hit': rdMatchesQ(b) }"
                 style="margin-bottom:10px">
              <div v-if="b.type === 'heading'" style="display:flex;align-items:center;gap:8px">
                <component :is="'h' + Math.min(b.level + 1, 6)"
                           :style="{ margin: '10px 0 4px', fontSize: 20 - b.level * 1.5 + 'px' }">
                  {{ b.text }}
                </component>
                <button v-if="rdNodeByPara(b.para_no)" class="mini-btn"
                        @click="rdCopyCitation(rdNodeByPara(b.para_no) as OutlineNodeT)">复制引用</button>
                <span v-if="rdRiskFor(b.text)" class="chip-miss" :title="rdRiskFor(b.text)?.title">★ 废标风险</span>
              </div>
              <p v-else-if="b.type === 'para'" style="margin:4px 0;line-height:1.8">{{ b.text }}</p>
              <div v-else-if="b.type === 'image'" class="sub-note" style="border:1px dashed var(--line);padding:12px;text-align:center">🖼 {{ b.text }}</div>
              <table v-else-if="b.type === 'table'" style="border-collapse:collapse;width:100%;font-size:12.5px">
                <tbody>
                  <tr v-for="(row, ri) in b.rows" :key="ri">
                    <td v-for="(c, ci) in row.cells" :key="ci"
                        :colspan="c.gridspan > 1 ? c.gridspan : undefined"
                        :rowspan="c.vmerge === 'restart' ? undefined : undefined"
                        :style="{ border: '1px solid var(--line)', padding: '4px 6px', background: c.vmerge === 'continue' ? 'var(--hint-bg)' : undefined }">
                      {{ c.vmerge === 'continue' ? "" : c.text }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </div>
        <!-- 右侧：素材推荐入口 + 废标风险 -->
        <div style="width:260px;border-left:1px solid var(--line);overflow:auto;padding:10px">
          <div class="sub-note" style="margin-bottom:4px">引用用途</div>
          <select v-model="rdPurpose" style="width:100%;margin-bottom:8px" class="ipt">
            <option>项目负责人</option><option>技术人员</option><option>售后服务</option>
            <option>业绩案例</option><option>企业资质</option>
          </select>
          <div class="sub-note" style="margin-bottom:6px">素材推荐（当前章节：{{ rdActiveNode?.title ?? "未选择" }}）</div>
          <template v-if="rdMats.length">
            <div v-for="m in rdMats" :key="m.id" style="border:1px solid var(--line);border-radius:8px;padding:8px;margin-bottom:6px">
              <b style="font-size:12.5px">{{ m.title }}</b>
              <div style="margin-top:4px">
                <button class="mini-btn" :disabled="rdInsertBusy" @click="doInsertRef(m)">引用到本章</button>
              </div>
            </div>
          </template>
          <div v-else class="sub-note">点击左侧章节标题检索推荐素材</div>
          <div class="sub-note" style="margin:12px 0 6px">废标风险（{{ rdRisks.length }}）</div>
          <div v-for="r in rdRisks.slice(0, 20)" :key="r.id" style="border:1px solid var(--risk-line);border-radius:8px;padding:6px;margin-bottom:6px;font-size:12px">
            <b>{{ r.sev === "fatal" ? "❌" : r.sev === "risk" ? "⚠" : "💡" }} {{ r.title }}</b>
            <div class="sub-note">{{ r.req ?? "" }}</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
