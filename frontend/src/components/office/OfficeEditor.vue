<script setup lang="ts">
import { nextTick, onUnmounted, ref, watch } from "vue";
import { api } from "../../api/client";

const props = defineProps<{
  officeDocumentId: string;
  mode?: "view" | "edit";
  versionId?: string | null;
}>();

const loading = ref(true);
const error = ref("");
const holderId = `office-editor-${Math.random().toString(36).slice(2, 10)}`;
const holder = ref<HTMLElement | null>(null);
const currentVersion = ref<number | null>(null);

let editor: { destroyEditor: () => void } | null = null;
let scriptPromise: Promise<void> | null = null;
let seq = 0; // 防过期异步：只让最后一次初始化生效

function loadScript(scriptUrl: string): Promise<void> {
  if (!scriptPromise) {
    scriptPromise = new Promise((resolve, reject) => {
      const el = document.createElement("script");
      el.src = scriptUrl;
      el.onload = () => resolve();
      el.onerror = () => reject(new Error("api.js 加载失败"));
      document.head.appendChild(el);
    });
  }
  return scriptPromise;
}

function destroy(): void {
  try {
    editor?.destroyEditor();
  } catch {
    /* 忽略重复销毁 */
  }
  editor = null;
}

async function init(vid?: string): Promise<void> {
  const mySeq = ++seq;
  loading.value = true;
  error.value = "";
  try {
    const cfg = await api.editorConfigOffice(props.officeDocumentId, vid ?? props.versionId ?? undefined);
    if (mySeq !== seq) return; // 过期响应丢弃
    await loadScript(cfg.scriptUrl);
    if (mySeq !== seq) return;
    const DocsAPI = (
      window as unknown as {
        DocsAPI: { DocEditor: new (id: string, c: unknown) => { destroyEditor: () => void } };
      }
    ).DocsAPI;
    await nextTick();
    if (mySeq !== seq) return;
    const target = document.getElementById(holderId);
    if (!target) return;
    destroy();
    editor = new DocsAPI.DocEditor(holderId, cfg.config);
    currentVersion.value = cfg.version ?? null;
    loading.value = false;
  } catch (e) {
    if (mySeq !== seq) return;
    error.value = e instanceof Error ? e.message : "编辑器加载失败";
    loading.value = false;
  }
}

function reload(versionId?: string | null): void {
  void init(versionId ?? undefined);
}

watch(
  () => [props.officeDocumentId, props.versionId, props.mode] as const,
  () => {
    void init();
  },
);

void init();

onUnmounted(() => {
  seq++; // 使在途初始化失效
  destroy();
});

defineExpose({ reload, currentVersion });
</script>

<template>
  <div class="office-editor">
    <div v-if="loading" class="empty-state"><b>编辑器加载中…</b></div>
    <div v-else-if="error" class="empty-state"><b>编辑器加载失败</b>{{ error }}</div>
    <div v-show="!loading && !error" :id="holderId" ref="holder" class="office-holder"></div>
  </div>
</template>

<style scoped>
.office-editor { width: 100%; height: 100%; display: flex; flex-direction: column; min-height: 0; }
.office-holder { flex: 1; min-height: 0; }
</style>
