<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api } from "../api/client";

const props = defineProps<{ docId: string; docName?: string; page: number }>();
const emit = defineEmits<{ close: [] }>();

const pages = ref(1);
const cur = ref(props.page);
const loading = ref(true);

const src = computed(() => `/api/documents/${props.docId}/pages?page_no=${cur.value}&t=${Date.now()}`);

watch(
  () => props.docId,
  async () => {
    try {
      const r = await api.pageCount(props.docId);
      pages.value = r.pages;
    } catch {
      pages.value = 1;
    }
    cur.value = Math.min(Math.max(1, props.page), pages.value);
  },
  { immediate: true },
);

watch(
  () => props.page,
  (p) => {
    cur.value = Math.min(Math.max(1, p), pages.value);
  },
);

function prev(): void {
  if (cur.value > 1) cur.value -= 1;
}
function next(): void {
  if (cur.value < pages.value) cur.value += 1;
}
function onKey(ev: KeyboardEvent): void {
  if (ev.key === "ArrowLeft") prev();
  else if (ev.key === "ArrowRight") next();
  else if (ev.key === "Escape") emit("close");
}
</script>

<template>
  <div class="modal-wrap" style="z-index: 95" @click.self="emit('close')">
    <div class="modal wide" style="width: min(920px, 96vw); padding: 14px 16px" @keydown="onKey">
      <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px">
        <h3 style="font-size: 14.5px; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">
          {{ docName || "原文预览" }}<span class="chip-src" style="margin-left: 8px">第 {{ cur }} / {{ pages }} 页</span>
        </h3>
        <button class="mini-btn" :disabled="cur <= 1" @click="prev">‹ 上一页</button>
        <button class="mini-btn" :disabled="cur >= pages" @click="next()">下一页 ›</button>
        <button class="dr-close" @click="emit('close')">✕</button>
      </div>
      <div style="background: #f0f3f7; border-radius: 10px; min-height: 300px; display: grid; place-items: center; overflow: auto; max-height: 76vh">
        <img v-if="pages" :key="src" :src="src" style="max-width: 100%; max-height: 76vh" @load="loading = false">
      </div>
    </div>
  </div>
</template>
