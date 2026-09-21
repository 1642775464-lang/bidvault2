<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { api } from "../api/client";
import type { Material } from "../api/types";
import { CATS, navTo, refreshCore, store, toast } from "../store";
import PagePreview from "../components/PagePreview.vue";
import { rowBlocks, isTableBlockV2, twipsToPx } from "../utils/documentBlocks";
import type { ExtractRowV2 } from "../api/types";

const search = ref("");
const cat = ref("all");
const selId = ref("");
const mats = ref<Material[]>([]);

const useOpen = ref(false);
const useId = ref<string | null>(null);
const useProj = ref("");
const delOpen = ref(false);
const delId = ref<string | null>(null);

const editOpen = ref(false);
const eTitle = ref("");
const eCat = ref(CATS[0]);
const eType = ref("表格");
const eTags = ref("");
const eContent = ref("");
const eFile = ref<File | null>(null);
/* B-2：已 Word 化素材双视图 */
const matTab = ref<"word" | "ai">("word");
const matVerOpen = ref(false);
const matVerState = ref<"loading" | "error" | "empty" | "ready">("loading");
const matVerError = ref("");
const matVerList = ref<{ id: string; version: number; source: string; created_at: string | null; is_current: boolean; size: number | null; comment: string | null; restored_from_version_id: string | null }[]>([]);
const matViewingVid = ref<string | null>(null);
const matViewingVer = ref<number | null>(null);
const ooState = ref<"idle" | "loading" | "ready" | "error">("idle");
const ooError = ref("");
const matOoHolder = ref<HTMLElement | null>(null);
let matOoEditor: { destroyEditor: () => void } | null = null;
let matScriptPromise: Promise<void> | null = null;
const eTitleErr = ref(false);

const typeIconPath: Record<string, string> = {
  表格: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10"/>',
  文本: '<path d="M6 3h8l4 4v14H6Z"/><path d="M9 12h6M9 16h6"/>',
  图片: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m5 18 5-5 3 3 3-3 3 3"/>',
  附件: '<path d="M12 3v12"/><path d="m8 11 4 4 4-4"/><path d="M5 21h14"/>',
};
const typeCls: Record<string, string> = { 表格: "tp-tbl", 文本: "tp-txt", 图片: "tp-img", 附件: "tp-file" };

async function load(): Promise<void> {
  try {
    const r = await api.materials();
    mats.value = r.items;
  } catch (e) {
    toast(e instanceof Error ? e.message : "加载失败");
  }
}

const filtered = computed(() =>
  mats.value
    .slice()
    .sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""))
    .filter((m) => {
      if (cat.value !== "all" && m.cat !== cat.value) return false;
      const q = search.value.trim().toLowerCase();
      if (!q) return true;
      return (m.title + " " + m.tags.join(" ") + " " + (m.project_name ?? "")).toLowerCase().includes(q);
    }),
);

const groups = computed(() => {
  const out: { cat: string; items: Material[] }[] = [];
  for (const m of filtered.value) {
    const last = out[out.length - 1];
    if (last && last.cat === m.cat) {
      last.items.push(m);
    } else {
      out.push({ cat: m.cat, items: [m] });
    }
  }
  return out;
});

const page = ref(1);
const MAT_PAGE_SIZE = 10;
const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / MAT_PAGE_SIZE)));
const pagedGroups = computed(() => {
  const startIdx = (page.value - 1) * MAT_PAGE_SIZE;
  const slice = filtered.value.slice(startIdx, startIdx + MAT_PAGE_SIZE);
  const out: { cat: string; items: Material[] }[] = [];
  for (const m of slice) {
    const last = out[out.length - 1];
    if (last && last.cat === m.cat) {
      last.items.push(m);
    } else {
      out.push({ cat: m.cat, items: [m] });
    }
  }
  return out;
});
watch([cat, search], () => {
  page.value = 1;
});

const sel = computed(() => mats.value.find((m) => m.id === selId.value) ?? null);
const imgDocId = computed(() =>
  (sel.value?.file_name ?? "").startsWith("img/") ? sel.value!.file_name!.split("/")[1] : "",
);
const previewOpen = ref(false);

function pick(m: Material): void {
  selId.value = m.id;
}

/* ---------- Word 画布编辑 ---------- */
const matPage = ref<HTMLElement | null>(null);
const wordCount = ref(0);
const dirty = ref(false);
const saving = ref(false);
const isWordMat = computed(() => !!sel.value?.office_document_id);
/* B-2：正式版本变化 → AI数据同步中 → processor 完成信号 → 自动重载（无猜测延迟） */
const matSyncing = ref(false);
const matKnownVer = ref<number | null>(null);
let matPollTimer: ReturnType<typeof setInterval> | null = null;

async function pollMatVersion(): Promise<void> {
  const m = sel.value;
  if (!m?.office_document_id || matSyncing.value) return;
  try {
    const items = (await api.officeVersions(m.office_document_id)).items;
    const cur = items.find((v) => v.is_current)?.version ?? null;
    if (matKnownVer.value === null) {
      matKnownVer.value = cur;
      return;
    }
    if (cur == null || cur <= matKnownVer.value) return;
    matSyncing.value = true;
    matKnownVer.value = cur;
    // 完成信号：callback-state 出现 processor 结果（事件信号，非猜测延迟）
    for (let i = 0; i < 90; i++) {
      try {
        const st = (await api.officeCallbackState(m.office_document_id)).state;
        if (st.includes("processor=") || st.includes("保存处理失败")) break;
      } catch {
        /* 轮询失败继续 */
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    await load();
    matSyncing.value = false;
  } catch {
    /* 轮询失败下轮重试 */
  }
}

function startMatPoll(): void {
  if (matPollTimer) clearInterval(matPollTimer);
  void pollMatVersion();
  matPollTimer = setInterval(() => void pollMatVersion(), 15000);
}

function stopMatPoll(): void {
  if (matPollTimer) clearInterval(matPollTimer);
  matPollTimer = null;
}

const sourceInfo = computed(() => {
  const m = sel.value;
  if (!m?.source_locator_json) return null;
  try {
    const loc = JSON.parse(m.source_locator_json);
    return { range: loc.child_range, fingerprint: String(loc.fingerprint || "").slice(0, 19), source_document_id: m.source_document_id, source_document_version_id: m.source_document_version_id };
  } catch {
    return null;
  }
});
const sourceStale = ref(false);

async function loadMatVersions(): Promise<void> {
  const m = sel.value;
  if (!m?.office_document_id) {
    matVerState.value = "error";
    matVerError.value = "素材未关联 Word 文件";
    return;
  }
  matVerState.value = "loading";
  matVerError.value = "";
  try {
    matVerList.value = (await api.officeVersions(m.office_document_id)).items;
    matVerState.value = matVerList.value.length ? "ready" : "empty";
  } catch (e) {
    matVerState.value = "error";
    matVerError.value = e instanceof Error ? e.message : "加载失败";
    matVerList.value = [];
  }
  // 来源新版本提示：与素材自身版本列表互不影响（source 尚未迁通用层，走 source 端点）
  try {
    if (!m.source_document_id || !m.source_document_version) return;
    const cur = await api.onlyofficeVersion(m.source_document_id);
    sourceStale.value = !!cur.version && cur.version > m.source_document_version;
  } catch {
    sourceStale.value = false;
  }
}

function loadOnlyofficeScriptOnce(scriptUrl: string): Promise<void> {
  if (matScriptPromise) return matScriptPromise;
  matScriptPromise = new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = scriptUrl;
    el.onload = () => resolve();
    el.onerror = () => reject(new Error("api.js 加载失败"));
    document.head.appendChild(el);
  });
  return matScriptPromise;
}

async function initMatOffice(vid?: string): Promise<void> {
  const m = sel.value;
  if (!m?.office_document_id) return;
  ooState.value = "loading";
  ooError.value = "";
  try {
    const cfg = await api.editorConfigOffice(m.office_document_id, vid);
    if (!matScriptPromise) matScriptPromise = loadOnlyofficeScriptOnce(cfg.scriptUrl);
    await matScriptPromise;
    const DocsAPI = (window as unknown as { DocsAPI: { DocEditor: new (id: string, c: unknown) => { destroyEditor: () => void } } }).DocsAPI;
    await nextTick();
    if (!document.getElementById("material-office-holder")) return;
    matOoEditor?.destroyEditor();
    matOoEditor = new DocsAPI.DocEditor("material-office-holder", cfg.config);
    ooState.value = "ready";
  } catch (e) {
    ooState.value = "error";
    ooError.value = e instanceof Error ? e.message : "加载失败";
  }
}

function switchMatTab(t: "word" | "ai"): void {
  matTab.value = t;
  if (t === "ai") void pollMatVersion();
  if (t === "word") void initMatOffice(matViewingVid.value ?? undefined);
  else {
    matOoEditor?.destroyEditor();
    matOoEditor = null;
    ooState.value = "idle";
  }
}

async function viewMatVersion(v: { id: string; version: number }): Promise<void> {
  matViewingVid.value = v.id;
  matViewingVer.value = v.version;
  matTab.value = "word";
  await initMatOffice(v.id);
}

async function backToMatCurrent(): Promise<void> {
  matViewingVid.value = null;
  matViewingVer.value = null;
  await initMatOffice();
}

async function restoreMatVersion(v: { id: string; version: number }): Promise<void> {
  const m = sel.value;
  if (!m?.office_document_id) return;
  if (!window.confirm(`确认恢复素材 v${v.version}？将以新建版本方式回滚，并同步刷新 AI/结构化数据。`)) return;
  try {
    await api.restoreOfficeVersion(m.office_document_id, v.id);
    toast(`素材已恢复 v${v.version}（AI/结构化数据同步更新）`);
    matViewingVid.value = null;
    matViewingVer.value = null;
    await initMatOffice();
    void loadMatVersions();
    await load();
  } catch (e) {
    toast(e instanceof Error ? e.message : "恢复失败");
  }
}

function openSourceDoc(): void {
  navTo("docs");
  toast("来源文档已在文档库定位（可在原始文档页签查看）");
}

function downloadMatVersion(v: { id: string }): void {
  if (sel.value?.office_document_id) window.open(`/api/office-documents/${sel.value.office_document_id}/versions/${v.id}/file`);
}

watch(() => sel.value?.id, () => {
  matOoEditor?.destroyEditor();
  matOoEditor = null;
  ooState.value = "idle";
  matTab.value = "word";
  matViewingVid.value = null;
  matViewingVer.value = null;
  matVerOpen.value = false;
  matKnownVer.value = null;
  if (isWordMat.value) {
    void nextTick(() => initMatOffice());
    startMatPoll();
  } else {
    stopMatPoll();
  }
});

onUnmounted(() => {
  stopMatPoll();
  matOoEditor?.destroyEditor();
});

function recount(): void {
  if (matPage.value) {
    wordCount.value = (matPage.value.innerText || "").replace(/\s/g, "").length;
  }
}
function onMatInput(): void {
  dirty.value = true;
  recount();
}
function exec(cmd: string): void {
  matPage.value?.focus();
  document.execCommand(cmd);
}

async function saveInline(): Promise<void> {
  if (!sel.value || !dirty.value || !matPage.value) return;
  saving.value = true;
  try {
    const root = matPage.value;
    const title = (root.querySelector(".wp-hl")?.textContent || "").trim() || sel.value.title;
    const tags = (root.querySelector(".wp-tags")?.textContent || "")
      .split(/[,，]/)
      .map((x) => x.trim())
      .filter(Boolean);
    const rows: string[][] = [];
    const v2Map = new Map<number, HTMLTableElement>();
    root.querySelectorAll("table.realtable, .wp-p[data-kind=body]").forEach((el) => {
      const tid = (el as HTMLElement).getAttribute?.("data-tid");
      if (el.tagName === "TABLE" && tid) {
        v2Map.set(Number(tid), el as HTMLTableElement);
        return;
      }
      if (el.tagName === "TABLE") {
        el.querySelectorAll("tr").forEach((tr) => {
          const cells = Array.from(tr.querySelectorAll("td")).map((td) =>
            (td.innerText || "").replace(/\u00a0/g, " ").trim(),
          );
          if (cells.some((c) => c)) rows.push(cells);
        });
      } else {
        const line = ((el as HTMLElement).innerText || "").replace(/\u00a0/g, " ").trim();
        if (line) rows.push([line]);
      }
    });
    const textLines: string[] = [];
    root.querySelectorAll<HTMLElement>(".wp-p[data-kind=text]").forEach((el) => {
      const line = (el.innerText || "").trim();
      if (line) textLines.push(line);
    });
    const body: Record<string, unknown> = {
      title,
      cat: sel.value.cat,
      type: sel.value.type,
      tags,
    };
    if (sel.value.type === "表格") {
      if (!rows.length) {
        toast("表格素材至少保留一行内容");
        saving.value = false;
        return;
      }
      // 以素材骨架为基准：V2 表格行按 cell.id 回填，legacy 行按 DOM 顺序取回
      const skeleton = (sel.value.rows ?? []) as ExtractRowV2[];
      const newRows: ExtractRowV2[] = [];
      let li = 0;
      for (const r of skeleton) {
        if (isTableBlockV2(r)) {
          const el = v2Map.get(r.tid);
          newRows.push(
            !el
              ? r
              : {
                  ...r,
                  cells: r.cells.map((c) =>
                    c.merged
                      ? c
                      : { ...c, text: ((el.querySelector(`td[data-cell-id="${c.id}"]`) as HTMLElement | null)?.innerText || "").replace(/\u00a0/g, " ").trim() },
                  ),
                },
          );
        } else {
          newRows.push(rows[li++] ?? r);
        }
      }
      while (li < rows.length) newRows.push(rows[li++]);
      if (!newRows.length) {
        toast("表格素材至少保留一行内容");
        saving.value = false;
        return;
      }
      body.rows = newRows;
      body.text = null;
    } else if (sel.value.type === "文本") {
      body.text = textLines.length ? textLines.join("\n") : null;
      body.rows = null;
    } else if (sel.value.type === "图片") {
      body.rows = rows.length ? rows : null;
      body.text = textLines.length ? textLines.join("\n") : null;
    }
    await api.updateMaterial(sel.value.id, body);
    dirty.value = false;
    toast("已保存");
    await load();
  } catch (e) {
    toast(e instanceof Error ? e.message : "保存失败");
  } finally {
    saving.value = false;
  }
}

async function analyze(): Promise<void> {
  if (!imgDocId.value) return;
  try {
    const r = await api.analyzeImages(imgDocId.value);
    toast("AI 识别完成：" + r.analyzed + " 张成功" + (r.failed ? " · " + r.failed + " 张失败" : ""));
    await load();
  } catch (e) {
    toast(e instanceof Error ? e.message : "识别失败");
  }
}

function goSource(m: Material): void {
  if (!m.doc_name && m.file_name) {
    window.open(`/api/files/${m.file_name}`);
    return;
  }
  if (!m.doc_name) {
    toast("该素材没有来源文件");
    return;
  }
  navTo("docs");
  toast("源文档《" + m.doc_name + "》已在文档库中定位");
}

function openUse(m: Material): void {
  useId.value = m.id;
  useProj.value = m.project_id ?? "";
  useOpen.value = true;
}
async function saveUse(): Promise<void> {
  const m = mats.value.find((x) => x.id === useId.value);
  if (!m) return;
  if (!useProj.value) {
    toast("请选择目标项目");
    return;
  }
  try {
    await api.useMaterial(m.id, useProj.value);
    toast("「" + m.title + "」已引用到「" + (store.projects.find((p) => p.id === useProj.value)?.name ?? "") + "」");
    useOpen.value = false;
    await load();
  } catch (e) {
    toast(e instanceof Error ? e.message : "操作失败");
  }
}

function openDel(m: Material): void {
  delId.value = m.id;
  delOpen.value = true;
}
async function saveDel(): Promise<void> {
  const m = mats.value.find((x) => x.id === delId.value);
  if (!m) return;
  try {
    await api.deleteMaterial(m.id);
    toast("素材「" + m.title + "」已删除");
    delOpen.value = false;
    if (selId.value === m.id) selId.value = "";
    await load();
    await refreshCore();
  } catch (e) {
    toast(e instanceof Error ? e.message : "删除失败");
  }
}

function openCreate(): void {
  eTitleErr.value = false;
  eTitle.value = "";
  eCat.value = cat.value === "all" ? CATS[0] : cat.value;
  eType.value = "表格";
  eTags.value = "";
  eContent.value = "";
  eFile.value = null;
  editOpen.value = true;
}
async function saveCreate(): Promise<void> {
  if (eFile.value) {
    try {
      const r = await api.uploadMaterial(eFile.value, eCat.value);
      toast(`素材「${eFile.value.name}」已上传（${r.type}）`);
      editOpen.value = false;
      await load();
    } catch (e) {
      toast(e instanceof Error ? e.message : "上传失败");
    }
    return;
  }
  const title = eTitle.value.trim();
  if (!title) {
    eTitleErr.value = true;
    return;
  }
  const tags = eTags.value.split(/[,，]/).map((x) => x.trim()).filter(Boolean);
  const body: Record<string, unknown> = { title, cat: eCat.value, type: eType.value, tags };
  if (eType.value === "表格") {
    const rows = eContent.value
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean)
      .map((l) => {
        const i = l.indexOf("：") >= 0 ? l.indexOf("：") : l.indexOf(":");
        return i < 0 ? [l, ""] : [l.slice(0, i).trim(), l.slice(i + 1).trim()];
      });
    if (!rows.length) {
      toast("表格素材请至少填写一行「字段：值」");
      return;
    }
    body.rows = rows;
  } else if (eType.value === "文本") {
    const text = eContent.value.trim();
    if (!text) {
      toast("请填写文本内容");
      return;
    }
    body.text = text;
  }
  try {
    await api.createMaterial(body);
    toast("素材「" + title + "」已创建");
    editOpen.value = false;
    await load();
  } catch (e) {
    toast(e instanceof Error ? e.message : "保存失败");
  }
}

watch(filtered, (list) => {
  if (!list.length) {
    selId.value = "";
  } else if (!list.some((m) => m.id === selId.value)) {
    selId.value = list[0].id;
  }
});
watch([selId, mats], async () => {
  dirty.value = false;
  await nextTick();
  recount();
});

onMounted(async () => {
  await load();
  await refreshCore();
});
</script>

<template>
  <div class="viewscroll">
    <div class="sub-bar mats-sub">
      <div class="f-search">
        <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.2-4.2" /></svg>
        <input v-model="search" type="text" placeholder="搜索素材 / 标签 / 项目">
      </div>
      <select v-model="cat" class="f-select">
        <option value="all">全部分类</option>
        <option v-for="c in CATS" :key="c">{{ c }}</option>
      </select>
      <button class="mini-btn" @click="openCreate()">上传 / 新建素材</button>
    </div>

    <div class="v2wrap">
      <div class="mlist">
        <div class="mlist-body">
          <template v-for="g in pagedGroups" :key="g.cat">
            <div class="mgroup"><div class="gl">{{ g.cat }} · {{ mats.filter((x) => x.cat === g.cat).length }}</div></div>
            <button v-for="m in g.items" :key="m.id" class="mitem" :class="{ on: m.id === selId }" @click="pick(m)">
              <span class="m-ico" :class="'c-' + m.cat">
                <svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6Z" /><path d="M14 3v4h4" /></svg>
              </span>
              <span class="nm">
                <b>{{ m.title }}</b>
                <span>{{ m.src }} · ×{{ m.used }} 引用</span>
              </span>
              <span class="tchip" :class="typeCls[m.type]">
                <svg viewBox="0 0 24 24" v-html="typeIconPath[m.type]" />
              </span>
            </button>
          </template>
          <div v-if="!filtered.length" class="empty-state"><b>无匹配素材</b>调整筛选，或上传 / 新建素材</div>
        </div>
        <div v-if="totalPages > 1" class="pager">
          <button class="mini-btn" :disabled="page <= 1" @click="page -= 1">‹ 上一页</button>
          <span class="sub-note">{{ page }} / {{ totalPages }}</span>
          <button class="mini-btn" :disabled="page >= totalPages" @click="page += 1">下一页 ›</button>
        </div>
      </div>

      <div class="editor">
        <template v-if="sel">
          <div class="ed-head">
            <span class="t">
              <h3>{{ sel.cat }}素材</h3>
              <div class="m">
                {{ sel.src }}{{ sel.doc_name ? " · 来自《" + sel.doc_name + "》" : "" }}
                {{ sel.project_name ? " · 已关联 " + sel.project_name : "" }}
                · 更新 {{ (sel.updated_at ?? "").slice(0, 10) }} ·
                <span class="chip-use" style="vertical-align:1px">×{{ sel.used }} 引用</span>
              </div>
            </span>
            <span class="sp">
              <button v-if="dirty" class="act pri" :disabled="saving" @click="saveInline">{{ saving ? "保存中…" : "保存修改" }}</button>
              <button class="act" @click="openUse(sel)">引用到项目</button>
              <button class="act danger" @click="openDel(sel)">删除</button>
            </span>
          </div>
          <div class="ed-body" style="display: flex; flex-direction: column; overflow: hidden; padding: 0">
            <template v-if="isWordMat">
              <div class="doctabs" style="padding:10px 16px 0">
                <button :class="{ on: matTab === 'word' }" @click="switchMatTab('word')">Word正文</button>
                <button :class="{ on: matTab === 'ai' }" @click="switchMatTab('ai')">AI / 结构化数据</button>
                <span v-if="matSyncing" class="syncbadge">AI数据同步中…</span>
                <span class="verbadge">{{ matViewingVid ? `查看历史 v${matViewingVer}` : "" }}</span>
                <button class="verbtn" @click="matVerOpen = true; loadMatVersions()">历史版本</button>
                <button class="verbtn" @click="openSourceDoc()">查看来源</button>
              </div>
              <template v-if="matTab === 'word'">
                <div v-if="matViewingVid" class="oo-warn" style="display:flex;align-items:center;gap:12px">
                  <span>正在查看素材历史版本 v{{ matViewingVer }}（只读）</span>
                  <button class="mini-btn" style="color:var(--risk)" @click="backToMatCurrent">返回当前版本</button>
                </div>
                <div v-if="sourceStale" class="oo-warn" style="display:flex;align-items:center;gap:12px;background:var(--accent-bg);color:var(--accent-deep);border-bottom:1px solid var(--line)">
                  <span>来源文档已有新版本（素材不会自动覆盖）</span>
                </div>
                <div class="oo-stage">
                  <div v-if="ooState === 'loading'" class="empty-state"><b>编辑器加载中…</b></div>
                  <div v-else-if="ooState === 'error'" class="empty-state"><b>编辑器加载失败</b>{{ ooError }}</div>
                  <div v-show="ooState === 'ready'" id="material-office-holder" ref="matOoHolder" class="oo-holder"></div>
                </div>
              </template>
              <template v-else>
                <div class="wordstage">
                  <div class="wordpage" contenteditable="false" style="cursor:default">
                    <div class="wp-sec" contenteditable="false">来源信息</div>
                    <div class="wp-p" contenteditable="false">
                      来源文档：{{ sourceInfo?.source_document_id ? "已关联（" + sourceInfo.source_document_id.slice(0, 8) + "…）" : "—" }}<br>
                      来源版本：{{ sourceInfo ? "提取自 v" + (matVerList.find((x) => x.id === sel?.source_document_version_id)?.version ?? "?") : "—" }}<br>
                      来源位置：{{ sourceInfo ? "body 区块 " + sourceInfo.range[0] + " – " + sourceInfo.range[1] : "—" }}<br>
                      内容指纹：{{ sourceInfo?.fingerprint ?? "—" }}<br>
                      创建时间：{{ sel.updated_at?.slice(0, 19).replace("T", " ") || "—" }}
                    </div>
                    <div class="wp-sec" contenteditable="false">AI / 结构化数据（派生自 Word 正文，只读）</div>
                    <template v-if="sel.rows && sel.rows.length">
                      <table class="realtable" contenteditable="false">
                        <tr v-for="(r, ri) in sel.rows" :key="'m' + ri">
                          <td v-for="(c, ci) in r" :key="ci">{{ c }}</td>
                        </tr>
                      </table>
                    </template>
                    <div v-if="sel.text" class="wp-p" contenteditable="false" style="white-space:pre-wrap">{{ sel.text }}</div>
                    <div class="wp-p" contenteditable="false" style="text-align:center;text-indent:0;font-size:12px;color:var(--dim)">以上数据由 Word 正文解析派生，随保存自动更新</div>
                  </div>
                </div>
              </template>
            </template>
            <template v-else>
            <div class="wordbar">
              <span class="wname">{{ sel.cat }}素材 · {{ sel.type }}</span>
              <span class="wcount">{{ wordCount }} 字</span>
              <button class="wbtn pri" :disabled="!dirty || saving" @click="saveInline">{{ saving ? "保存中…" : "保存修改" }}</button>
            </div>
            <div class="wordtools">
              <button class="wtool" @click="exec('undo')">↶ 撤销</button>
              <button class="wtool" @click="exec('redo')">↷ 恢复</button>
              <span class="tool-sep" />
              <button class="wtool" @click="saveInline()">保存</button>
            </div>
            <div class="wordstage">
              <div :key="sel.id" ref="matPage" class="wordpage" contenteditable="true" spellcheck="false" @input="onMatInput">
                <div class="wp-hl" contenteditable="true">{{ sel.title }}</div>
                <div class="wp-p wp-tags" contenteditable="true" style="text-align:center;text-indent:0;font-size:12px;color:var(--dim)">{{ sel.tags.join("，") }}</div>
                <div class="wp-p" contenteditable="false" style="text-align:center;text-indent:0;font-size:12px;color:#98a6b8">
                  {{ sel.src }}{{ sel.doc_name ? " · 来自《" + sel.doc_name + "》" : "" }}{{ sel.project_name ? " · 已关联 " + sel.project_name : "" }} · 更新 {{ (sel.updated_at ?? "").slice(0, 10) }}
                </div>

                <template v-if="sel.type === '表格' && sel.rows">
                  <div class="wp-sec" contenteditable="false">结构化内容</div>
                  <template v-for="(blk, bi) in rowBlocks(sel.rows)" :key="'b' + bi">
                    <table v-if="blk.type === 'table'" class="realtable" contenteditable="true">
                      <tr v-for="(r, ri) in blk.rows" :key="ri">
                        <td v-for="(c, ci) in r" :key="ci" contenteditable="true">{{ c }}</td>
                      </tr>
                    </table>
                    <table v-else-if="blk.type === 'tableV2'" class="realtable" :data-tid="blk.v2!.tid" contenteditable="true">
                      <colgroup>
                        <col v-for="(w, wi) in blk.v2!.columnWidthsTwips" :key="wi" :style="w > 0 ? { width: twipsToPx(w) + 'px' } : undefined">
                      </colgroup>
                      <tr v-for="(row, ri) in blk.v2!.rows" :key="ri">
                        <template v-for="c in row.cells" :key="c.id">
                          <td v-if="!c.merged" :data-cell-id="c.id" :colspan="c.colSpan > 1 ? c.colSpan : undefined" :rowspan="c.rowSpan > 1 ? c.rowSpan : undefined" contenteditable="true">{{ c.text }}</td>
                        </template>
                      </tr>
                    </table>
                    <p v-else class="wp-p" data-kind="body" contenteditable="true" style="text-indent:2em">{{ blk.rows[0][0] }}</p>
                  </template>
                </template>

                <template v-else-if="sel.type === '文本' && sel.text">
                  <div class="wp-sec" contenteditable="false">文本内容</div>
                  <div v-for="(para, i) in sel.text.split('\n')" :key="'t' + i" class="wp-p" contenteditable="true" data-kind="text">{{ para }}</div>
                </template>

                <template v-else-if="sel.type === '图片' && sel.file_name">
                  <div class="wp-sec" contenteditable="false">图片证据{{ sel.source_page ? " · 来源第 " + sel.source_page + " 页" : "" }}</div>
                  <div class="wp-img" contenteditable="false">
                    <img :src="'/api/files/' + sel.file_name">
                  </div>
                  <div v-if="imgDocId" class="wp-p" contenteditable="false" style="text-align:center;text-indent:0">
                    <button class="act pri" @click="previewOpen = true" style="margin-right:8px">查看原文{{ sel.source_page ? " P" + sel.source_page : "" }}</button>
                    <button class="act" @click="analyze()">AI 识别字段</button>
                  </div>
                  <template v-if="sel.rows && sel.rows.length">
                    <div class="wp-sec" contenteditable="false">识别字段</div>
                    <table class="realtable" contenteditable="true">
                      <tr v-for="(r, i) in sel.rows" :key="'ar' + i">
                        <td v-for="(c, ci) in r" :key="ci" contenteditable="true">{{ c }}</td>
                      </tr>
                    </table>
                  </template>
                </template>

                <template v-else>
                  <div class="wp-sec" contenteditable="false">文件</div>
                  <div class="wp-p" contenteditable="false" style="text-align:center;text-indent:0">{{ sel.file_name || "—" }} {{ sel.file_size || "" }}</div>
                </template>
              </div>
            </div>
            <div class="wordstatus">
              <span>字数统计：{{ wordCount }}</span>
              <span v-if="dirty" style="color:var(--risk)">有未保存修改</span>
            </div>
            </template>
          </div>
          <div class="ed-foot">
            <button class="act" @click="goSource(sel)">{{ sel.doc_name ? "查看来源文档" : "查看上传记录" }}</button>
          </div>
        </template>
        <div v-else class="xempty"><b>选择左侧素材</b></div>
      </div>
    </div>

    <!-- 新建素材 -->
    <div v-if="editOpen" class="modal-wrap" @click.self="editOpen = false">
      <div class="modal" role="dialog" aria-modal="true" aria-label="新建素材">
        <h3>上传 / 新建素材</h3>
        <div class="frm">
          <div class="frow">
            <div class="f">
              <label class="fl">素材标题<span style="color:var(--fatal)">*</span></label>
              <input v-model="eTitle" type="text" placeholder="如：营业执照副本扫描件" :class="{ err: eTitleErr }">
              <p class="ferr" :class="{ show: eTitleErr }">请填写素材标题</p>
            </div>
            <div class="f">
              <label class="fl">素材分类</label>
              <select v-model="eCat"><option v-for="c in CATS" :key="c">{{ c }}</option></select>
            </div>
          </div>
          <div class="frow">
            <div class="f">
              <label class="fl">附件文件</label>
              <input type="file" accept=".jpg,.jpeg,.png,.pdf,.docx,.xlsx,.xls,.csv,.txt,.zip" @change="eFile = ($event.target as HTMLInputElement).files?.[0] ?? null">
            </div>
            <div class="f"><label class="fl">&nbsp;</label><span v-if="eFile" class="chip-tag">{{ eFile.name }}</span></div>
          </div>
          <div class="frow">
            <div class="f">
              <label class="fl">素材类型</label>
              <select v-model="eType">
                <option value="表格">表格（键值行）</option>
                <option value="文本">文本段落</option>
              </select>
            </div>
            <div class="f">
              <label class="fl">标签（逗号分隔）</label>
              <input v-model="eTags" type="text" placeholder="如：资质, 营业执照">
            </div>
          </div>
          <div class="f">
            <label class="fl">内容（每行一条：字段：值 / 文本段落）</label>
            <textarea v-model="eContent" rows="6" placeholder="法定代表人：张建国" />
          </div>
        </div>
        <div class="m-foot">
          <button class="btn-ghost" @click="editOpen = false">取消</button>
          <button class="btn-primary" @click="saveCreate()">保存</button>
        </div>
      </div>
    </div>

    <!-- 引用 -->
    <div v-if="useOpen" class="modal-wrap" @click.self="useOpen = false">
      <div class="modal" role="dialog" aria-modal="true" aria-label="引用到项目">
        <h3>引用到项目写作</h3>
        <p class="m-sub">{{ mats.find((x) => x.id === useId)?.title }}</p>
        <div class="frm">
          <div class="f">
            <label class="fl">目标项目</label>
            <select v-model="useProj">
              <option value="" disabled>选择项目…</option>
              <option v-for="p in store.projects.filter((x) => !x.is_archived)" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
          </div>
        </div>
        <div class="m-foot">
          <button class="btn-ghost" @click="useOpen = false">取消</button>
          <button class="btn-primary" @click="saveUse()">确认引用</button>
        </div>
      </div>
    </div>

    <!-- 删除 -->
    <div v-if="delOpen" class="modal-wrap" @click.self="delOpen = false">
      <div class="modal" role="dialog" aria-modal="true">
        <h3>删除素材</h3>
        <div class="m-body">
          <p>确认删除素材「<b>{{ mats.find((x) => x.id === delId)?.title }}</b>」？</p>
          <p style="margin-top:6px">该素材已被引用 {{ mats.find((x) => x.id === delId)?.used ?? 0 }} 次，删除后已引用的项目不受影响，但无法再新增引用。此操作不可撤销。</p>
        </div>
        <div class="m-foot">
          <button class="btn-ghost" @click="delOpen = false">取消</button>
          <button class="btn-primary" style="background:var(--fatal);border-color:var(--fatal)" @click="saveDel()">确认删除</button>
        </div>
      </div>
    </div>

    <!-- 原文预览 -->
    <PagePreview
      v-if="previewOpen && imgDocId && sel"
      :doc-id="imgDocId"
      :doc-name="sel.doc_name ?? '原文'"
      :page="sel.source_page ?? 1"
      @close="previewOpen = false"
    />
    <!-- 素材历史版本 -->
    <div v-if="matVerOpen" class="modal-wrap" @click.self="matVerOpen = false">
      <div class="modal" role="dialog" aria-modal="true" aria-label="素材历史版本">
        <h3>素材历史版本</h3>
        <div class="verlist">
          <div v-for="v in matVerList" :key="v.id" class="verrow" :class="{ cur: v.is_current }">
            <div class="vl">
              <b>v{{ v.version }}</b>
              <span class="chip-tag">{{ { extract: "提取", onlyoffice: "编辑保存", restore: "恢复", upload: "上传", init: "上传" }[v.source] || v.source }}<template v-if="v.restored_from_version_id"> · 源自 v{{ matVerList.find((x) => x.id === v.restored_from_version_id)?.version ?? "?" }}</template></span>
              <span class="vm">{{ v.created_at ? v.created_at.slice(0, 19).replace("T", " ") : "" }}{{ v.size != null ? " · " + Math.round((v.size || 0) / 1024) + " KB" : "" }}<template v-if="v.comment"> · “{{ v.comment }}”</template></span>
            </div>
            <div class="vr">
              <span v-if="v.is_current" class="chip-role r-admin">当前版本</span>
              <template v-else>
                <button class="mini-btn" @click="viewMatVersion(v)">查看</button>
                <button class="mini-btn" @click="downloadMatVersion(v)">下载</button>
                <button class="mini-btn pri" @click="restoreMatVersion(v)">恢复</button>
              </template>
            </div>
          </div>
          <div v-if="matVerState === 'loading'" class="empty-state"><b>正在加载历史版本…</b></div>
          <div v-else-if="matVerState === 'error'" class="empty-state"><b>历史版本加载失败：{{ matVerError }}</b><button class="mini-btn" @click="loadMatVersions()">重试</button></div>
          <div v-else-if="matVerState === 'empty'" class="empty-state"><b>暂无历史版本</b></div>
        </div>
        <div class="m-foot">
          <button class="btn-ghost" @click="matVerOpen = false">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>
