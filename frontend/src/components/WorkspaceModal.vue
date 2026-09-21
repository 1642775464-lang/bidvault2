<script setup lang="ts">
import { nextTick, onUnmounted, ref, watch } from "vue";
import { api } from "../api/client";
import { twipsToPx } from "../utils/documentBlocks";
import { toast } from "../store";

const props = defineProps<{ projectId: string; projectName: string }>();
const emit = defineEmits<{ (e: "close"): void }>();

const loading = ref(true);
const error = ref("");
const oid = ref<string | null>(null);
const version = ref<number | null>(null);
const verOpen = ref(false);
const verList = ref<{ id: string; version: number; source: string; created_at: string | null; is_current: boolean; size: number | null; comment: string | null; restored_from_version_id: string | null }[]>([]);
const viewingVid = ref<string | null>(null);
const viewingVer = ref<number | null>(null);
const holder = ref<HTMLElement | null>(null);
let editor: { destroyEditor: () => void } | null = null;
let scriptPromise: Promise<void> | null = null;

function loadScript(scriptUrl: string): Promise<void> {
  const existing = scriptPromise;
  if (existing) return existing;
  const created: Promise<void> = new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = scriptUrl;
    el.onload = () => resolve();
    el.onerror = () => reject(new Error("api.js 加载失败"));
    document.head.appendChild(el);
  });
  scriptPromise = created;
  return created;
}

async function init(vid?: string): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const cfg = await api.editorConfigOffice(oid.value ?? "", vid);
    await loadScript(cfg.scriptUrl);
    const DocsAPI = (window as unknown as {
      DocsAPI: { DocEditor: new (id: string, c: unknown) => { destroyEditor: () => void } };
    }).DocsAPI;
    await nextTick();
    const elId = vid ? `ws-hist-${vid}` : "ws-holder";
    const target = vid ? document.getElementById(elId) : holder.value;
    if (!target) return;
    editor?.destroyEditor();
    editor = new DocsAPI.DocEditor(elId, cfg.config);
    version.value = cfg.version ?? null;
  } catch (e) {
    error.value = e instanceof Error ? e.message : "加载失败";
  } finally {
    loading.value = false;
  }
}

function downloadVersion(v: { id: string }): void {
  if (oid.value) window.open(`/api/office-documents/${oid.value}/versions/${v.id}/file`);
}

async function loadVersions(): Promise<void> {
  if (!oid.value) return;
  try {
    verList.value = (await api.officeVersions(oid.value)).items;
  } catch {
    verList.value = [];
  }
}

async function restore(v: { id: string; version: number }): Promise<void> {
  if (!oid.value) return;
  if (!window.confirm(`确认恢复工作稿 v${v.version}？将以新建版本方式回滚。`)) return;
  try {
    await api.restoreOfficeVersion(oid.value, v.id);
    toast(`工作稿已恢复 v${v.version}`);
    viewingVid.value = null;
    viewingVer.value = null;
    await init();
    await loadVersions();
  } catch (e) {
    toast(e instanceof Error ? e.message : "恢复失败");
  }
}

function viewVersion(v: { id: string; version: number }): void {
  viewingVid.value = v.id;
  viewingVer.value = v.version;
  init(v.id);
}

async function backToCurrent(): Promise<void> {
  viewingVid.value = null;
  viewingVer.value = null;
  await init();
}

watch(
  () => props.projectId,
  async () => {
    try {
      const r = await api.ensureWorkspace(props.projectId);
      oid.value = r.office_document_id;
      await init();
      void loadVersions();
    } catch (e) {
      error.value = e instanceof Error ? e.message : "工作稿初始化失败";
      loading.value = false;
    }
  },
  { immediate: true },
);

onUnmounted(() => {
  editor?.destroyEditor();
});
</script>

<template>
  <div class="modal-wrap" @click.self="emit('close')">
    <div class="modal ws-modal" role="dialog" aria-modal="true" :aria-label="`${projectName} 投标工作稿`">
      <h3>{{ projectName }} · 投标工作稿</h3>
      <div class="doctabs" style="padding: 6px 0 0">
        <button :class="{ on: !viewingVid }" @click="backToCurrent">当前工作稿{{ version ? ` v${version}` : "" }}</button>
        <button @click="verOpen = !verOpen; loadVersions()">历史版本</button>
        <span class="verbadge" v-if="version">v{{ version }}</span>
      </div>
      <div v-if="error" class="empty-state"><b>加载失败</b>{{ error }}</div>
      <div v-else-if="loading" class="empty-state"><b>工作稿加载中…</b></div>
      <div v-show="!loading && !error" class="oo-stage">
        <div id="ws-holder" ref="holder" class="oo-holder"></div>
      </div>
      <!-- 历史版本面板（复用通用 versions API） -->
      <template v-if="verOpen">
        <div class="sec-title">历史版本</div>
        <div class="verlist">
          <div v-for="v in verList" :key="v.id" class="verrow" :class="{ cur: v.is_current && !viewingVid }">
            <div class="vl">
              <b>v{{ v.version }}</b>
              <span class="chip-tag">{{ { init: "初始化", onlyoffice: "编辑保存", restore: "恢复", extract: "提取", upload: "上传" }[v.source] || v.source }}</span>
              <span class="vm">{{ v.created_at ? v.created_at.slice(0, 19).replace("T", " ") : "" }}{{ v.size ? " · " + Math.round(v.size / 1024) + " KB" : "" }}<template v-if="v.comment"> · “{{ v.comment }}”</template></span>
            </div>
            <div class="vr">
              <span v-if="v.is_current && !viewingVid" class="chip-role r-admin">当前版本</span>
              <template v-else>
                <button class="mini-btn" @click="viewVersion(v)">查看</button>
                <button class="mini-btn" @click="downloadVersion(v)">下载</button>
                <button class="mini-btn pri" @click="restore(v)">恢复</button>
              </template>
            </div>
          </div>
          <div v-if="!verList.length" class="empty-state"><b>暂无历史版本</b></div>
        </div>
      </template>
      <div class="m-foot">
        <span v-if="viewingVid" style="color:var(--risk);font-size:12px">正在查看历史版本 v{{ viewingVer }}（只读）</span>
        <button class="btn-ghost" @click="emit('close')">关闭</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ws-modal { width: min(1280px, 94vw); height: min(860px, 92vh); display: flex; flex-direction: column; overflow: hidden; }
.ws-modal .oo-stage { flex: 1; min-height: 0; display: flex; flex-direction: column; background: #f4f6fa; }
.ws-modal .oo-holder { flex: 1; min-height: 0; }
.ws-modal .verlist { max-height: 40vh; overflow: auto; }
.ws-modal .m-foot { display: flex; justify-content: space-between; align-items: center; margin-top: 10px; }
</style>
