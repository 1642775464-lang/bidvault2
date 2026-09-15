<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api } from "../api/client";
import type { SearchHit } from "../api/types";
import { toast } from "../store";
import PagePreview from "./PagePreview.vue";

const emit = defineEmits<{ close: [] }>();

const q = ref("");
const mode = ref("fts");
const hits = ref<SearchHit[]>([]);
const done = ref(false);
const inputEl = ref<HTMLInputElement | null>(null);
const preview = ref<{ docId: string; name: string; page: number } | null>(null);

onMounted(() => inputEl.value?.focus());

async function run(): Promise<void> {
  const query = q.value.trim();
  if (!query) return;
  try {
    const r = await api.search(query, "", mode.value);
    hits.value = r.hits;
    done.value = true;
  } catch (e) {
    toast(e instanceof Error ? e.message : "检索失败");
  }
}

function openPreview(h: SearchHit): void {
  if (!h.page_no) return;
  preview.value = { docId: h.document_id, name: h.filename, page: h.page_no };
}
</script>

<template>
  <div class="modal-wrap" @click.self="emit('close')">
    <div class="modal" role="dialog" aria-modal="true" aria-label="全局搜索">
      <h3>全局检索</h3>
      <p class="m-sub">跨文档库全文检索（FTS5 trigram + 3-gram 兜底）</p>
      <div class="frm">
        <div style="display: flex; gap: 8px">
          <input
            ref="inputEl"
            v-model="q"
            class="g-input"
            type="text"
            placeholder="关键词…"
            @keyup.enter="run"
          >
          <select v-model="mode" class="f-select">
            <option value="fts">关键词</option>
            <option value="nl">自然语言</option>
          </select>
          <button class="btn-primary" @click="run">检索</button>
        </div>

        <div v-if="hits.length" class="hits">
          <div v-for="h in hits" :key="h.chunk_id" class="hitbox">
            <div class="hb-head">
              <b>{{ h.filename }}</b>
              <span class="kchip" :class="h.kind === 'tender' ? 'k-tender' : 'k-bid'">{{ h.kind === "tender" ? "招标" : "投标" }}</span>
              <span v-if="h.is_secret" class="chip-unbind">涉密</span>
              <button v-if="h.page_no" class="chip-src" style="cursor: pointer" @click="openPreview(h)">P{{ h.page_no }} 原文</button>
            </div>
            <!-- eslint-disable-next-line vue/no-v-html -->
            <div class="hb-snip" v-html="h.snippet_html" />
          </div>
        </div>
        <div v-else-if="done" class="empty-state"><b>无匹配结果</b>换个关键词试试</div>
      </div>
      <div class="m-foot">
        <button class="btn-ghost" @click="emit('close')">关闭</button>
      </div>
    </div>

    <PagePreview
      v-if="preview"
      :doc-id="preview.docId"
      :doc-name="preview.name"
      :page="preview.page"
      @close="preview = null"
    />
  </div>
</template>

<style scoped>
.g-input {
  flex: 1;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 13px;
  color: var(--ink);
}
.hits {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 46vh;
  overflow-y: auto;
}
.hitbox {
  border: 1px solid var(--line);
  border-radius: 9px;
  padding: 10px 12px;
}
.hb-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.hb-head b {
  color: var(--ink);
}
.hb-snip {
  font-size: 12.5px;
  color: var(--sub);
  margin-top: 6px;
}
.hb-snip :deep(mark) {
  background: #b9d7ff;
  border-radius: 2px;
  padding: 0 1px;
}
</style>
