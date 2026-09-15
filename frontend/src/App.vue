<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import { navTo, refreshCore, settleConfirm, store, confirmState, toast } from "./store";
import GlobalSearch from "./components/GlobalSearch.vue";
import ProjectDrawer from "./components/ProjectDrawer.vue";
import ProjectModal from "./components/ProjectModal.vue";
import UploadModal from "./components/UploadModal.vue";
import Workbench from "./views/Workbench.vue";
import Projects from "./views/Projects.vue";
import Documents from "./views/Documents.vue";
import Materials from "./views/Materials.vue";
import Settings from "./views/Settings.vue";

const view = ref<"work" | "lib" | "docs" | "mats" | "settings">("work");
const collapsed = ref(false);
const searchOpen = ref(false);

const views = {
  work: { comp: Workbench, label: "工作台" },
  lib: { comp: Projects, label: "项目库" },
  docs: { comp: Documents, label: "文档库" },
  mats: { comp: Materials, label: "素材库" },
  settings: { comp: Settings, label: "系统设置" },
} as const;

const icons: Record<string, string> = {
  work: "M3 10.5 12 3l9 7.5|M5.5 9.5V21h13V9.5",
  lib: "M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z",
  docs: "M6 3h8l4 4v14H6Z|M14 3v4h4|M9 12h6M9 16h6",
  mats: "m12 3 9 5-9 5-9-5Z|m3 13 9 5 9-5",
  settings:
    "M12 4v2.5M12 17.5V20M4 12h2.5M17.5 12H20M6.3 6.3l1.8 1.8M15.9 15.9l1.8 1.8M17.7 6.3l-1.8 1.8M8.1 15.9l-1.8 1.8",
};

watch(
  () => store.navView,
  (v) => {
    if (v && v in views) {
      view.value = v as typeof view.value;
      navTo("");
    }
  },
);

async function onSaved(): Promise<void> {
  await refreshCore();
}

onMounted(refreshCore);
</script>

<template>
  <div class="shell" style="display:flex;width:100%">
    <aside class="nav" :class="{ mini: collapsed }" @click="collapsed = !collapsed">
      <div class="nav-logo" @click.stop>
        <span class="logo-mark">标</span>
        <span class="logo-word">标 library</span>
      </div>
      <nav @click.stop>
        <button
          v-for="(v, key) in views"
          :key="key"
          class="nav-item"
          :class="{ on: view === key }"
          @click="view = key"
        >
          <svg viewBox="0 0 24 24">
            <path v-for="(d, i) in icons[key].split('|')" :key="i" :d="d" />
          </svg>
          <span>{{ v.label }}</span>
        </button>
      </nav>
      <div class="nav-foot">标库 BidVault v2 · 五页完整版</div>
    </aside>

    <div class="main">
      <div class="crumb-bar">
        <div class="crumb">
          <b>标库</b><span class="sep">/</span><span>{{ views[view].label }}</span>
        </div>
        <div class="top-tools">
          <button class="icon-btn" title="全局搜索" @click="searchOpen = true">
            <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.2-4.2" /></svg>
          </button>
          <button class="icon-btn" title="通知" @click="toast('通知中心在后续里程碑开放')">
            <svg viewBox="0 0 24 24"><path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6" /><path d="M10 19a2 2 0 0 0 4 0" /></svg>
          </button>
          <span class="avatar">朱</span>
        </div>
      </div>

      <component :is="views[view].comp" />
    </div>

    <GlobalSearch v-if="searchOpen" @close="searchOpen = false" />
    <ProjectDrawer />
    <ProjectModal @saved="onSaved" />
    <UploadModal @uploaded="onSaved" />

    <div class="toast-wrap">
      <div v-for="t in store.toasts" :key="t.id" class="toast">{{ t.text }}</div>
    </div>

    <!-- 确认弹窗（导出拦截等） -->
    <div v-if="confirmState.open" class="modal-wrap" style="z-index: 96" @click.self="settleConfirm(false)">
      <div class="modal" style="width: min(460px, 94vw)">
        <h3>{{ confirmState.title }}</h3>
        <div class="m-body" v-html="confirmState.body" />
        <div class="m-foot">
          <button class="btn-ghost" @click="settleConfirm(false)">取消</button>
          <button class="btn-primary" @click="settleConfirm(true)">{{ confirmState.okText }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
