<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api } from "../api/client";
import type { DocItem, ExtractGroup } from "../api/types";
import { CATS, INDUSTRIES, fmtSize, openUpload, refreshCore, store, toast } from "../store";

const kindF = ref<"all" | "tender" | "bid">("all");
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

async function load(): Promise<void> {
  try {
    const r = await api.documents({});
    docs.value = r.items;
  } catch (e) {
    toast(e instanceof Error ? e.message : "加载失败");
  }
}

const filtered = computed(() =>
  docs.value.slice().sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? "")).filter((d) => {
    if (kindF.value !== "all" && d.kind !== kindF.value) return false;
    const q = search.value.trim().toLowerCase();
    if (!q) return true;
    const pid = d.project_id ?? "";
    const pname = store.projects.find((p) => p.id === pid)?.name ?? "";
    return (d.filename + " " + pname).toLowerCase().includes(q);
  }),
);

const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / PAGE_SIZE)));
const pagedDocs = computed(() => filtered.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE));
watch([kindF, search], () => {
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
      <button v-for="k in ['all', 'tender', 'bid'] as const" :key="k" class="f-chip" :class="{ on: kindF === k }" @click="kindF = k">
        {{ k === "all" ? "全部" : k === "tender" ? "招标文件" : "投标文件" }}
      </button>
      <button class="mini-btn" @click="openUpload(null, 'tender')">上传文档</button>
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
              <span class="kchip" :class="sel.kind === 'tender' ? 'k-tender' : 'k-bid'">{{ sel.kind === "tender" ? "招标文件" : "投标文件" }}</span>
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
          <div class="xp-body">
            <div class="chips" style="margin-bottom: 12px">
              <select v-model="catFilter" class="f-select">
                <option value="all">全部分类</option>
                <option v-for="c in CATS" :key="c" :value="c">{{ c }}</option>
              </select>
            </div>
            <template v-if="grouped.length">
              <div v-for="g in grouped" :key="g.label" class="xg">
                <div class="lab">{{ g.label }} · {{ g.groups.reduce((s, x) => s + x.items.length, 0) }} 项</div>
                <div class="xgroups">
                  <details v-for="grp in g.groups" :key="grp.name" class="xrow" open>
                    <summary>
                      <svg class="caret" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6" /></svg>
                      <b>{{ grp.name }}</b>
                      <span class="sum">{{ grp.items.length }} 项</span>
                    </summary>
                    <div class="xdetail-wrap">
                      <details v-for="it in grp.items" :key="it.id" class="xrow" style="margin:8px">
                        <summary>
                          <svg class="caret" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6" /></svg>
                          <b>{{ it.name }}</b>
                          <span v-if="it.sum" class="sum">{{ it.sum }}</span>
                        </summary>
                        <div v-if="it.rows.length" class="xdetail">
                          <table>
                            <tbody>
                              <tr v-for="(r, i) in it.rows" :key="i"><td>{{ r[0] }}</td><td>{{ r[1] }}</td></tr>
                            </tbody>
                          </table>
                        </div>
                      </details>
                    </div>
                  </details>
                </div>
              </div>
            </template>
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
          </div>
        </template>
        <div v-else class="xempty"><b>选择左侧文档</b></div>
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
  </div>
</template>
