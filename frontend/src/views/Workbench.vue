<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { api } from "../api/client";
import type { ExtractGroup, Material, Project } from "../api/types";
import {
  RISK_TYPES,
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
const detecting = ref(false);

function pickProject(p: Project): void {
  if (p.id === store.activeProjectId) {
    ddOpen.value = false;
    return;
  }
  store.activeProjectId = p.id;
  ddOpen.value = false;
  clearSearch();
  toast(`已切换到「${p.name}」`);
}

const myDocs = computed(() => (cur.value ? store.docsByProject[cur.value.id] ?? [] : []));
const bidDocs = computed(() => myDocs.value.filter((d) => d.kind === "bid"));
const tenderDoc = computed(() => myDocs.value.find((d) => d.kind === "tender") ?? null);

/* ---------------- 文档预览 ---------------- */
const docHtml = ref("");
const docTitle = ref("");
const docMeta = ref("");
const fs = ref(15);
const searchInput = ref<HTMLInputElement | null>(null);
const hitInfo = ref("—");
const outline = ref<{ id: string; label: string }[]>([]);
const progress = ref(0);
const curChapter = ref(-1);
const docScroll = ref<HTMLElement | null>(null);
const docBody = ref<HTMLElement | null>(null);
let hits: HTMLElement[] = [];
let hitIdx = -1;

const hasDoc = computed(() => !!cur.value && bidDocs.value.length > 0);

const chapters = computed(() => {
  if (!docHtml.value) return [] as { id: string; label: string }[];
  const box = document.createElement("div");
  box.innerHTML = docHtml.value;
  return Array.from(box.querySelectorAll("h2")).map((h, i) => ({ id: `ch-${i}`, label: h.textContent ?? "" }));
});

async function loadDoc(): Promise<void> {
  hits = [];
  hitIdx = -1;
  hitInfo.value = "—";
  docHtml.value = "";
  docTitle.value = "";
  outline.value = [];
  progress.value = 0;
  curChapter.value = -1;
  if (searchInput.value) searchInput.value.value = "";
  const d = bidDocs.value[0];
  if (!d) return;
  try {
    const r = await api.content(d.id);
    docTitle.value = r.filename;
    docMeta.value = `${d.kind === "tender" ? "招标文件" : "投标文件"} · ${Math.max(1, Math.round(d.size / 1024))} KB`;
    const paras = r.text.split(/\n+/).map((s) => s.trim()).filter(Boolean);
    const parts: string[] = [];
    let chN = 0;
    for (const para of paras) {
      const m = para.match(/^(第[一二三四五六七八九十百]+章[^，。;；]*)$/);
      if (m) {
        parts.push(`<h2 id="ch-${chN}">${m[1]}</h2>`);
        chN++;
      } else {
        parts.push(`<p>${para}</p>`);
      }
    }
    docHtml.value = parts.join("");
    await nextTick();
    outline.value = Array.from(docBody.value?.querySelectorAll("h2") ?? []).map((h) => ({
      id: h.id,
      label: h.textContent ?? "",
    }));
    if (!outline.value.length && docBody.value?.querySelector("h1")) {
      outline.value = [{ id: "", label: docTitle.value }];
    }
    onDocScroll();
  } catch {
    docHtml.value = "";
  }
}

watch(hasDoc, loadDoc, { immediate: false });
watch(() => store.activeProjectId, () => {
  clearSearch();
  loadDoc();
});

function gotoChapter(id: string): void {
  const h = docBody.value?.querySelector(`#${id}`);
  if (h && docScroll.value) {
    const top = h.getBoundingClientRect().top - docScroll.value.getBoundingClientRect().top + docScroll.value.scrollTop - 14;
    docScroll.value.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
  }
}

function onDocScroll(): void {
  const ds = docScroll.value;
  if (!ds) return;
  const max = ds.scrollHeight - ds.clientHeight;
  progress.value = max > 0 ? (ds.scrollTop / max) * 100 : 0;
  const hs = Array.from(docBody.value?.querySelectorAll("h2") ?? []);
  let c = -1;
  hs.forEach((h, i) => {
    if (h.getBoundingClientRect().top - ds.getBoundingClientRect().top < 130) c = i;
  });
  curChapter.value = c;
}

/* 文内搜索 */
function clearSearch(): void {
  if (searchInput.value) searchInput.value.value = "";
  docBody.value?.querySelectorAll("mark.hit").forEach((m) => {
    const parent = m.parentNode;
    if (parent) {
      parent.replaceChild(document.createTextNode(m.textContent ?? ""), m);
      parent.normalize();
    }
  });
  hits = [];
  hitIdx = -1;
  hitInfo.value = hasDoc.value ? "—" : "";
}
function doSearch(q: string): void {
  clearSearch();
  if (!q || !docBody.value) return;
  const walker = document.createTreeWalker(docBody.value, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.nodeValue?.trim()) return NodeFilter.FILTER_REJECT;
      const p = n.parentNode as HTMLElement | null;
      if (p && p.closest && p.closest("mark.hit")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes: Text[] = [];
  while (walker.nextNode()) nodes.push(walker.currentNode as Text);
  const low = q.toLowerCase();
  const ql = q.length;
  nodes.forEach((n) => {
    const text = n.nodeValue ?? "";
    let idx = text.toLowerCase().indexOf(low);
    if (idx < 0) return;
    const frag = document.createDocumentFragment();
    let pos = 0;
    while (idx >= 0) {
      frag.appendChild(document.createTextNode(text.slice(pos, idx)));
      const mk = document.createElement("mark");
      mk.className = "hit";
      mk.textContent = text.substr(idx, ql);
      frag.appendChild(mk);
      pos = idx + ql;
      idx = text.toLowerCase().indexOf(low, pos);
    }
    frag.appendChild(document.createTextNode(text.slice(pos)));
    n.parentNode?.replaceChild(frag, n);
  });
  hits = Array.from(docBody.value?.querySelectorAll("mark.hit") ?? []);
  if (hits.length) {
    hitIdx = 0;
    flashHit();
  }
  updateHitInfo();
}
function flashHit(): void {
  const el = hits[hitIdx];
  el?.scrollIntoView({ block: "center", behavior: "smooth" });
  updateHitInfo();
}
function updateHitInfo(): void {
  hitInfo.value = hits.length ? `${hitIdx + 1}/${hits.length}` : "无命中";
}
let searchTimer = 0;
function onSearchInput(ev: Event): void {
  window.clearTimeout(searchTimer);
  const v = (ev.target as HTMLInputElement).value.trim();
  searchTimer = window.setTimeout(() => doSearch(v), 180);
}
function hitNext(): void {
  if (hits.length) {
    hitIdx = (hitIdx + 1) % hits.length;
    flashHit();
  }
}
function hitPrev(): void {
  if (hits.length) {
    hitIdx = (hitIdx - 1 + hits.length) % hits.length;
    flashHit();
  }
}
function setFs(v: number): void {
  fs.value = Math.max(13, Math.min(19, v));
}
const docNote = computed(() =>
  hasDoc.value ? `共 ${outline.value.length} 章 · 正文 ${fs.value}px` : "暂无投标文件",
);

/* ---------------- 检测面板 ---------------- */
const tab = ref<"scan" | "score" | "cases" | "people">("scan");
const filter = ref<string>("all");
const onlyOpen = ref(false);
const doneOpen = ref(false);
const openDetail = ref(new Set<string>());
const casesSort = ref<"match" | "time">("match");

interface RiskLike {
  id: string;
  sev: "fatal" | "risk" | "hint";
  type: string;
  title: string;
  meta: string;
  req: string;
  quote: string;
  advice: string;
  fix: string;
}
const risks = ref<RiskLike[]>([]);
const detectedAt = ref<string | null>(null);
const doneIds = ref(new Set<string>());

const pending = computed(() =>
  risks.value.filter((r) => !doneIds.value.has(r.id) && (filter.value === "all" || r.type === filter.value)),
);
const pendingFatal = computed(() => risks.value.filter((r) => r.sev === "fatal" && !doneIds.value.has(r.id)).length);
const doneList = computed(() => risks.value.filter((r) => doneIds.value.has(r.id) && (filter.value === "all" || r.type === filter.value)));
const riskIndex = computed(() => {
  let s = 0;
  risks.value.forEach((r) => {
    if (!doneIds.value.has(r.id)) s += r.sev === "fatal" ? 6 : 3;
  });
  return Math.max(0, 100 - s);
});

function rescan(): void {
  if (detecting.value) return;
  if (!hasDoc.value || !tenderDoc.value) {
    toast("无法开始检测");
    return;
  }
  toast("废标检测引擎在 M4 里程碑接入，当前版本暂以拆解结果供评分对照");
}

/* ---------------- 评分对照 / 案例 / 人员 ---------------- */
const tenderExtract = ref<ExtractGroup[]>([]);
const scoreRows = computed<string[][]>(() => {
  const g = tenderExtract.value.find((x) => x.name.includes("评标办法"));
  if (!g) return [];
  return g.items.flatMap((it) => it.rows.map((r) => [it.name + " · " + r[0], r[1], "—"] as string[]));
});
const caseMats = ref<Material[]>([]);
const peopleMats = ref<Material[]>([]);

async function loadPanels(): Promise<void> {
  tenderExtract.value = [];
  caseMats.value = [];
  peopleMats.value = [];
  if (!cur.value) return;
  try {
    if (tenderDoc.value && tenderDoc.value.status === 2) {
      const r = await api.extract(tenderDoc.value.id);
      tenderExtract.value = r.groups;
    }
    const mats = await api.materials();
    const mine = mats.items.filter((m) => m.project_id === cur.value?.id);
    caseMats.value = mine.filter((m) => m.cat === "业绩案例");
    peopleMats.value = mine.filter((m) => m.cat === "人员团队");
  } catch {
    /* 空态 */
  }
}

watch(() => store.activeProjectId, loadPanels);
watch(tenderDoc, loadPanels);

function matMatch(m: Material): number {
  let n = 60;
  if (m.project_id === store.activeProjectId) n += 20;
  n += Math.min(18, (m.rows?.length ?? 0) * 4 + (m.tags?.length ?? 0) * 3);
  return Math.min(97, n);
}

function goLib(): void {
  navTo("lib");
}
function goDocsUpload(): void {
  navTo("docs");
  openUpload(cur.value?.id ?? null, "bid");
}
function doExport(fmt?: string): void {
  const f = fmt ?? "Word";
  if (pendingFatal.value > 0) {
    toast(`仍有 ${pendingFatal.value} 项致命风险未处理，导出前请先处理`);
    return;
  }
  toast(`导出${f}在 M4 / M5 里程碑开放`);
}

function onOvIdxClass(): string {
  if (!risks.value.length) return "lv-na";
  const v = riskIndex.value;
  return v >= 80 ? "lv-red" : v >= 50 ? "lv-amber" : "lv-green";
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

onMounted(async () => {
  await loadDoc();
  await loadPanels();
  await refreshCore();
});
</script>

<template>
  <div class="viewscroll">
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
        <div class="doc-toolbar">
          <div class="search">
            <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.2-4.2" /></svg>
            <input ref="searchInput" type="text" placeholder="在文档内搜索…" :disabled="!hasDoc" @input="onSearchInput">
          </div>
          <span class="hit-info">{{ hitInfo }}</span>
          <button class="mini-btn" :disabled="!hits.length" @click="hitPrev">↑</button>
          <button class="mini-btn" :disabled="!hits.length" @click="hitNext">↓</button>
          <span class="tool-sep" />
          <button class="mini-btn" :disabled="!hasDoc" @click="setFs(fs - 1)">A−</button>
          <button class="mini-btn" :disabled="!hasDoc" @click="setFs(fs + 1)">A+</button>
          <span class="tool-note">{{ docNote }}</span>
        </div>
        <div ref="docScroll" class="doc-scroll" @scroll="onDocScroll">
          <div v-if="hasDoc" class="doc-wrap">
            <aside v-if="outline.length" class="doc-outline">
              <div class="ol-title">章节大纲</div>
              <button v-for="(o, i) in outline" :key="o.id" class="ol-item" :class="{ on: i === curChapter }" @click="o.id && gotoChapter(o.id)">{{ o.label }}</button>
            </aside>
            <article ref="docBody" class="doc-page" :style="{ fontSize: fs + 'px' }">
              <h1>{{ docTitle }}</h1>
              <p class="doc-meta">{{ docMeta }}</p>
              <!-- eslint-disable-next-line vue/no-v-html -->
              <div v-html="docHtml" />
            </article>
          </div>
          <div v-else class="doc-empty">
            <div class="es-icon">
              <svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6Z" /><path d="M14 3v4h4" /><path d="M9 13h6M9 17h4" /></svg>
            </div>
            <b>该项目的投标文件还没有上传</b>
            <p>投标文件在「技术标制作」阶段由文档编写产生；可在文档库上传管理，进度跟踪在项目库。</p>
            <button class="act pri" style="margin-right:8px" @click="goLib()">前往项目库</button>
            <button class="act" @click="goDocsUpload()">在文档库上传投标文件</button>
          </div>
        </div>
        <div class="doc-progress"><i :style="{ width: progress + '%' }" /></div>
      </section>

      <!-- 右：检测面板 -->
      <section class="col col-panel">
        <div class="tabs">
          <button v-for="t in [['scan', '废标检测'], ['score', '评分办法对照'], ['cases', '案例推荐'], ['people', '人员介绍']] as const"
                  :key="t[0]" class="tab" :class="{ on: tab === t[0] }" @click="tab = t[0]">{{ t[1] }}</button>
        </div>
        <div class="panel-body">
          <!-- 废标检测 -->
          <div v-show="tab === 'scan'" class="tabpane">
            <div class="ov">
              <div class="ov-top">
                <span class="ov-idx" :class="onOvIdxClass()">{{ risks.length ? riskIndex : "—" }}</span>
                <span class="ov-label">废标风险指数<br><span style="font-size:10.5px">0 安全 · 100 极高</span></span>
                <span class="ov-lv" :class="onOvIdxClass()">{{ risks.length ? (riskIndex >= 80 ? "高风险" : riskIndex >= 50 ? "中风险" : "低风险") : "尚未检测" }}</span>
              </div>
              <div class="ov-counts">
                <span v-if="!risks.length" style="color:var(--dim)">运行检测后，这里将显示风险分级计数</span>
              </div>
              <div class="ov-sub">
                <span class="ov-time">{{ detectedAt ? "最近检测：" + detectedAt : "尚未运行检测" }}</span>
                <button class="mini-btn" @click="rescan">
                  <svg viewBox="0 0 24 24"><path d="M20 11a8 8 0 1 0-2.3 6.3" /><path d="M20 5v6h-6" /></svg>重新检测
                </button>
              </div>
              <div class="chips">
                <button v-for="t in ['all', ...RISK_TYPES]" :key="t" class="chip" :class="{ on: filter === t }" :disabled="!risks.length" @click="filter = t">
                  {{ t === "all" ? "全部" : t }}<span class="n">0</span>
                </button>
                <span class="switch-wrap">只看未处理<button class="sw" role="switch" :aria-checked="onlyOpen" @click="onlyOpen = !onlyOpen" /></span>
              </div>
            </div>
            <div class="emptystate es-undet">
              <div class="es-icon"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.2-4.2" /></svg></div>
              <div class="es-title">该项目尚未运行检测</div>
              <p class="es-text">点击「开始检测」后，这里将显示风险分级与处理进度</p>
            </div>
            <div class="risklist" />
            <div class="done-head" style="display:none" />
          </div>

          <!-- 评分办法对照 -->
          <div v-show="tab === 'score'" class="tabpane">
            <p class="demo-note">
              {{ tenderDoc && tenderDoc.status === 2 ? "解析自文档库拆解结果：《" + tenderDoc.filename + "》" : "对照招标评分办法逐项核对技术分响应情况" }}
            </p>
            <div class="mini-table">
              <table>
                <thead><tr><th>评分项</th><th>招标要求</th><th>投标响应</th><th>状态</th></tr></thead>
                <tbody>
                  <template v-if="scoreRows.length">
                    <tr v-for="(r, i) in scoreRows" :key="i">
                      <td>{{ r[0] }}</td><td>{{ r[1] }}</td><td>—</td>
                      <td><span class="chip-unbind">待 M4 检测</span></td>
                    </tr>
                  </template>
                  <tr v-else>
                    <td colspan="4" style="text-align:center;color:var(--dim);padding:18px 12px">
                      在文档库上传并拆解招标文件后，自动解析评分办法 · <span class="chip-unbind" style="margin-left:4px">招标文件缺件或未拆解</span>
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
              <article v-for="m in caseMats" :key="m.id" class="case-card">
                <div class="case-head">
                  <span class="case-match" :class="matMatch(m) >= 90 ? 'm-hi' : matMatch(m) >= 80 ? 'm-mid' : 'm-lo'">{{ matMatch(m) }}%</span>
                  <h4>{{ m.title }}</h4>
                </div>
                <p v-if="m.rows && m.rows.length" class="case-meta">
                  {{ m.rows.slice(0, 2).map((r) => r[0] + " " + r[1]).join(" · ") }}
                </p>
                <p v-else-if="m.text" class="case-meta">{{ m.text.slice(0, 60) }}…</p>
                <div class="case-tags"><span v-for="t in m.tags" :key="t">{{ t }}</span></div>
                <p class="case-why">来自{{ m.doc_name ? "《" + m.doc_name + "》" : "手动上传素材" }} · 引用 ×{{ m.used }}</p>
                <div class="rc-actions">
                  <button class="act pri" @click="navTo('mats')">查看素材</button>
                </div>
              </article>
              <div v-if="!caseMats.length" class="empty-state"><b>暂无业绩案例素材</b>在文档库拆解投标文件或手动上传后，这里将推荐同类案例</div>
            </div>
          </div>

          <!-- 人员介绍 -->
          <div v-show="tab === 'people'" class="tabpane">
            <div class="person">
              <div v-for="m in peopleMats" :key="m.id" class="p-row">
                <span class="p-ava">{{ (m.title || "？").slice(0, 1) }}</span>
                <span class="p-info">
                  <b>{{ m.title }}</b>
                  <span>{{ m.rows && m.rows.length ? m.rows.slice(0, 2).map((r) => r[1]).join(" · ") : m.text?.slice(0, 40) || "—" }}</span>
                </span>
                <span class="p-tag ok">材料入库</span>
              </div>
              <div v-if="!peopleMats.length" class="empty-state"><b>暂无人员团队素材</b>拆解投标文件后，团队成员与简历将汇总在这里</div>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>
