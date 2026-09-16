<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { api } from "../api/client";
import type { Material } from "../api/types";
import { CATS, navTo, refreshCore, store, toast } from "../store";
import PagePreview from "../components/PagePreview.vue";

const search = ref("");
const cat = ref("all");
const selId = ref("");
const mats = ref<Material[]>([]);

/* 编辑弹窗 */
const editOpen = ref(false);
const editId = ref<string | null>(null);
const eTitle = ref("");
const eCat = ref(CATS[0]);
const eType = ref("表格");
const eTags = ref("");
const eContent = ref("");
const eTitleErr = ref(false);

/* 引用 / 删除弹窗 */
const useOpen = ref(false);
const useId = ref<string | null>(null);
const useProj = ref("");
const delOpen = ref(false);
const delId = ref<string | null>(null);

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
const pagedGroups = computed(() => {
  const out: { cat: string; items: Material[] }[] = [];
  for (const m of pagedMats.value) {
    const last = out[out.length - 1];
    if (last && last.cat === m.cat) {
      last.items.push(m);
    } else {
      out.push({ cat: m.cat, items: [m] });
    }
  }
  return out;
});

const sel = computed(() => mats.value.find((m) => m.id === selId.value) ?? null);
const imgDocId = computed(() => (sel.value?.file_name ?? "").startsWith("img/") ? sel.value!.file_name!.split("/")[1] : "");
const previewOpen = ref(false);

/* ---------- Word 式页内编辑 ---------- */
const paper = reactive({ title: "", tags: "", type: "表格", rows: [] as string[][], text: "" });
const dirty = ref(false);
const saving = ref(false);

function loadPaper(): void {
  const m = sel.value;
  if (!m) return;
  paper.title = m.title;
  paper.tags = m.tags.join("，");
  paper.type = m.type;
  paper.rows = (m.rows ?? []).map((r) => [r[0] ?? "", r[1] ?? ""]);
  paper.text = m.text ?? "";
  dirty.value = false;
}
watch(() => sel.value?.id, loadPaper, { immediate: true });
watch(paper, () => {
  dirty.value = dirty.value || sel.value != null;
}, { deep: true });

function markDirty(): void {
  dirty.value = true;
}

async function saveInline(): Promise<void> {
  if (!sel.value || !dirty.value) return;
  saving.value = true;
  try {
    const body: Record<string, unknown> = {
      title: paper.title.trim() || sel.value.title,
      cat: sel.value.cat,
      type: paper.type,
      tags: paper.tags.split(/[,，]/).map((x) => x.trim()).filter(Boolean),
    };
    if (paper.type === "表格") {
      const rows = paper.rows.filter((r) => r[0].trim() || (r[1] ?? "").trim());
      if (!rows.length) {
        toast("表格素材至少保留一行内容");
        saving.value = false;
        return;
      }
      body.rows = rows;
      body.text = null;
    } else if (paper.type === "文本") {
      body.text = paper.text?.trim() || null;
      body.rows = null;
    } else if (paper.type === "图片") {
      const rows = paper.rows.filter((r) => r[0].trim() || (r[1] ?? "").trim());
      body.rows = rows.length ? rows : null;
      body.text = paper.text;
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

function autoGrow(ev: Event): void {
  const el = ev.target as HTMLTextAreaElement;
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight}px`;
}

async function analyze(): Promise<void> {
  if (!imgDocId.value) return;
  try {
    const r = await api.analyzeImages(imgDocId.value);
    toast(`AI 识别完成：${r.analyzed} 张成功${r.failed ? ` · ${r.failed} 张失败` : ""}`);
    await load();
  } catch (e) {
    toast(e instanceof Error ? e.message : "识别失败");
  }
}
const page = ref(1);
const MAT_PAGE_SIZE = 10;
const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / MAT_PAGE_SIZE)));
const pagedMats = computed(() => filtered.value.slice((page.value - 1) * MAT_PAGE_SIZE, page.value * MAT_PAGE_SIZE));
watch([cat, search], () => {
  page.value = 1;
});

function pick(m: Material): void {
  selId.value = m.id;
}

function openEdit(id: string | null): void {
  editId.value = id;
  const m = id ? mats.value.find((x) => x.id === id) ?? null : null;
  eTitleErr.value = false;
  if (m) {
    eTitle.value = m.title;
    eCat.value = m.cat;
    eType.value = m.type;
    eTags.value = m.tags.join(", ");
    eContent.value =
      m.type === "表格" ? (m.rows ?? []).map((r) => r.join("：")).join("\n") : m.type === "文本" ? m.text ?? "" : "";
  } else {
    eTitle.value = "";
    eCat.value = CATS[0];
    eType.value = "表格";
    eTags.value = "";
    eContent.value = "";
  }
  editOpen.value = true;
}

function syncContentLabel(): string {
  return eType.value === "表格" ? "内容（每行一条：字段：值）" : "内容（文本段落）";
}

async function saveEdit(): Promise<void> {
  const title = eTitle.value.trim();
  if (!title) {
    eTitleErr.value = true;
    return;
  }
  const tags = eTags.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
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
    if (editId.value) {
      await api.updateMaterial(editId.value, body);
      toast(`素材「${title}」已更新`);
    } else {
      await api.createMaterial(body);
      toast(`素材「${title}」已创建`);
    }
    editOpen.value = false;
    await load();
  } catch (e) {
    toast(e instanceof Error ? e.message : "保存失败");
  }
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
    await api.useMaterial(m.id, "素材库引用");
    toast(`「${m.title}」已引用到「${store.projects.find((p) => p.id === useProj.value)?.name ?? ""}」`);
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
    toast(`素材「${m.title}」已删除`);
    delOpen.value = false;
    if (selId.value === m.id) selId.value = "";
    await load();
    await refreshCore();
  } catch (e) {
    toast(e instanceof Error ? e.message : "删除失败");
  }
}

function goSource(m: Material): void {
  if (!m.doc_name) {
    toast("上传记录在后续版本开放");
    return;
  }
  navTo("docs");
  toast(`源文档《${m.doc_name}》已在文档库中定位`);
}

watch(filtered, (list) => {
  if (!list.length) {
    selId.value = "";
  } else if (!list.some((m) => m.id === selId.value)) {
    selId.value = list[0].id;
  }
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
      <button class="mini-btn" @click="openEdit(null)">上传 / 新建素材</button>
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
              <button v-if="dirty" class="act pri" :disabled="saving" @click="saveInline()">{{ saving ? "保存中…" : "保存修改" }}</button>
              <button class="act" @click="openUse(sel)">引用到项目</button>
              <button class="act danger" @click="openDel(sel)">删除</button>
            </span>
          </div>
          <div class="ed-body">
            <div class="paper" :key="sel.id">
              <input v-model="paper.title" class="paper-title-edit" placeholder="素材标题">
              <div class="paper-tags-edit">
                <span class="chip-tag" style="flex:none">{{ sel.cat }}</span>
                <input v-model="paper.tags" class="paper-tags-input" placeholder="标签，逗号分隔">
              </div>

              <template v-if="paper.type === '表格'">
                <div class="paper-item-head" style="margin-top:14px">
                  <span class="paper-item-name">结构化内容</span>
                  <button class="row-add" style="opacity:1" @click="paper.rows.push(['', ''])">＋ 添加行</button>
                </div>
                <div v-for="(r, ri) in paper.rows" :key="ri" class="p-row-edit">
                  <input class="fk" v-model="r[0]" placeholder="字段">
                  <textarea class="fv" v-model="r[1]" rows="1" placeholder="内容" @input="autoGrow($event)" />
                  <button class="row-del" @click="paper.rows.splice(ri, 1)">✕</button>
                </div>
              </template>

              <template v-else-if="paper.type === '文本'">
                <div class="paper-item-head" style="margin-top:14px">
                  <span class="paper-item-name">文本内容</span>
                </div>
                <textarea v-model="paper.text" class="paper-prose-edit" rows="10" />
              </template>

              <template v-else-if="paper.type === '图片' && sel.file_name">
                <div class="paper-item-head" style="margin-top:14px">
                  <span class="paper-item-name">图片证据{{ sel.source_page ? ` · 来源第 ${sel.source_page} 页` : "" }}</span>
                </div>
                <div style="display: flex; gap: 8px; margin: 8px 0" v-if="imgDocId">
                  <button class="act pri" @click="previewOpen = true">查看原文{{ sel.source_page ? ` P${sel.source_page}` : "" }}</button>
                  <button class="act" @click="analyze()">AI 识别字段</button>
                </div>
                <img :src="`/api/files/${sel.file_name}`" style="max-width: 100%; max-height: 460px; border-radius: 6px; box-shadow: 0 1px 8px rgba(15,30,55,.15)">
                <template v-if="paper.rows.length">
                  <div class="paper-item-head" style="margin-top:16px"><span class="paper-item-name">识别字段</span></div>
                  <div v-for="(r, ri) in paper.rows" :key="ri" class="p-row-edit">
                    <input class="fk" v-model="r[0]" placeholder="字段">
                    <textarea class="fv" v-model="r[1]" rows="1" placeholder="内容" @input="autoGrow($event)" />
                    <button class="row-del" @click="paper.rows.splice(ri, 1)">✕</button>
                  </div>
                </template>
              </template>

              <template v-else>
                <div class="filebox">
                  <div class="fi"><svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6Z" /><path d="M14 3v4h4" /></svg></div>
                  <b>{{ sel.file_name || "—" }}</b>
                  <span>{{ sel.file_size || "" }}</span>
                </div>
              </template>
            </div>
          </div>
          <div class="ed-foot">
            <button class="act" @click="goSource(sel)">{{ sel.doc_name ? "查看来源文档" : "查看上传记录" }}</button>
          </div>
        </template>
        <div v-else class="xempty"><b>选择左侧素材</b></div>
      </div>
    </div>

    <!-- 编辑素材 -->
    <div v-if="editOpen" class="modal-wrap" @click.self="editOpen = false">
      <div class="modal" role="dialog" aria-modal="true" aria-label="素材编辑">
        <h3>{{ editId ? "编辑素材" : "上传 / 新建素材" }}</h3>
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
                <option value="图片">图片扫描件</option>
                <option value="附件">附件文档</option>
              </select>
            </div>
            <div class="f">
              <label class="fl">标签（逗号分隔）</label>
              <input v-model="eTags" type="text" placeholder="如：资质, 营业执照">
            </div>
          </div>
          <div v-if="eType === '表格' || eType === '文本'" class="f">
            <label class="fl">{{ syncContentLabel() }}</label>
            <textarea v-model="eContent" rows="6" placeholder="法定代表人：张建国&#10;单位性质：有限责任公司" />
          </div>
        </div>
        <div class="m-foot">
          <button class="btn-ghost" @click="editOpen = false">取消</button>
          <button class="btn-primary" @click="saveEdit()">保存</button>
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
