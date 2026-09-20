<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { api } from "../api/client";
import type { Material } from "../api/types";
import { CATS, navTo, refreshCore, store, toast } from "../store";
import PagePreview from "../components/PagePreview.vue";

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
    root.querySelectorAll<HTMLTableElement>("table.realtable tr").forEach((tr) => {
      const cells = Array.from(tr.querySelectorAll("td")).map((td) => (td.innerText || "").replace(/ /g, " ").trim());
      if (cells.some((c) => c)) rows.push(cells);
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
      body.rows = rows;
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

function matBlocks(rows: string[][]): { type: "table"; rows: string[][] }[] {
  return [{ type: "table", rows }];
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
  if (!m.doc_name) {
    toast("上传记录在后续版本开放");
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
  editOpen.value = true;
}
async function saveCreate(): Promise<void> {
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
                  <template v-for="(blk, bi) in matBlocks(sel.rows)" :key="'b' + bi">
                    <table v-if="blk.type === 'table'" class="realtable" contenteditable="true">
                      <tr v-for="(r, ri) in blk.rows" :key="ri">
                        <td v-for="(c, ci) in r" :key="ci" contenteditable="true">{{ c }}</td>
                      </tr>
                    </table>
                    <p v-else class="wp-p" contenteditable="true" style="text-indent:2em">{{ blk.rows[0][0] }}</p>
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
  </div>
</template>
