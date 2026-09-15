<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api } from "../api/client";
import type { Project } from "../api/types";
import {
  STAGES,
  closeDrawer,
  dueInfo,
  fmtOpenDateTime,
  isBehind,
  navTo,
  openProjectModal,
  store,
  toast,
  tenderOf,
} from "../store";

const p = computed<Project | null>(() => store.projects.find((x) => x.id === store.drawerProjectId) ?? null);
const docs = ref<NonNullable<Project["documents"]>>([]);

watch(
  () => [store.drawerOpen, store.drawerProjectId] as const,
  async ([open, id]) => {
    if (!open || !id) return;
    docs.value = [];
    try {
      const full = await api.project(id);
      docs.value = full.documents ?? [];
    } catch {
      docs.value = [];
    }
  },
  { immediate: true },
);

const tenderDoc = computed(() => docs.value?.find((d) => d.kind === "tender") ?? null);

async function setStage(delta: number): Promise<void> {
  const cur = p.value;
  if (!cur) return;
  const next = Math.min(4, Math.max(0, cur.stage + delta));
  if (next === cur.stage) return;
  try {
    await api.setStage(cur.id, next);
    toast(`「${cur.name}」已${delta > 0 ? "推进到" : "回退到"}「${STAGES[next]}」`);
    const i = store.projects.findIndex((x) => x.id === cur.id);
    if (i >= 0) store.projects[i] = { ...store.projects[i], stage: next, stage_label: STAGES[next] };
  } catch (e) {
    toast(e instanceof Error ? e.message : "操作失败");
  }
}

function goDocs(): void {
  closeDrawer();
  navTo("docs");
}
function goMats(): void {
  closeDrawer();
  navTo("mats");
}
function goWork(): void {
  closeDrawer();
  store.activeProjectId = store.drawerProjectId;
  navTo("work");
}
</script>

<template>
  <template v-if="store.drawerOpen && p">
    <div class="overlay" @click="closeDrawer()" />
    <aside class="drawer open" role="dialog" aria-modal="true" aria-label="项目详情">
      <div class="dr-head">
        <div class="t">
          <h3>{{ p.name }}</h3>
          <button class="dr-close" title="关闭" @click="closeDrawer()">✕</button>
        </div>
        <div class="code">{{ p.code }} · 投标平台：{{ p.platform || "—" }}</div>
        <div class="chips">
          <span class="chip-role" :class="p.role === '主标' ? 'chip-lead' : 'chip-sup'">{{ p.role }}</span>
          <span class="chip-ind">{{ p.industry }}</span>
          <span class="badge-due" :class="dueInfo(p).cls">{{ dueInfo(p).text }}</span>
          <span v-if="!tenderDoc && !p.is_archived" class="chip-miss">招标文件缺件</span>
        </div>
      </div>
      <div class="dr-body">
        <template v-if="p.result">
          <div class="dr-sec">
            <div class="lab">投标结果</div>
            <div class="result-banner" :class="p.result === '中标' ? 'win' : 'lose'">
              <span class="chip-result" :class="p.result === '中标' ? 'res-win' : 'res-lose'">{{ p.result }}</span>
              <span>{{ p.result_note || "" }}</span>
              <span style="margin-left:auto;font-family:var(--mono);font-size:12px">{{ p.result_amount || "—" }}</span>
            </div>
          </div>
        </template>

        <div class="dr-sec">
          <div class="lab">项目信息</div>
          <div class="meta-grid">
            <div class="mg">
              <div class="k">开标时间</div>
              <div class="v"><span class="mono">{{ fmtOpenDateTime(p.open_at) }}</span>　<span class="badge-due" :class="dueInfo(p).cls">{{ dueInfo(p).text }}</span></div>
            </div>
            <div class="mg"><div class="k">主标 / 陪标</div><div class="v"><span class="chip-role" :class="p.role === '主标' ? 'chip-lead' : 'chip-sup'">{{ p.role }}</span></div></div>
            <div class="mg"><div class="k">所属行业</div><div class="v">{{ p.industry }}</div></div>
            <div class="mg"><div class="k">负责销售</div><div class="v">{{ p.sales || "—" }}</div></div>
            <div class="mg"><div class="k">投标平台</div><div class="v">{{ p.platform }}</div></div>
            <div class="mg"><div class="k">当前阶段</div><div class="v">{{ p.result ? "已归档" : STAGES[p.stage] }}</div></div>
            <div class="mg" style="grid-column:1/-1"><div class="k">客户信息</div><div class="v">{{ p.cust || "—" }}{{ p.contact ? " · " + p.contact : "" }}</div></div>
          </div>
        </div>

        <template v-if="!p.is_archived">
          <div class="dr-sec">
            <div class="lab">当前阶段</div>
            <div class="stages">
              <div v-for="(s, i) in STAGES" :key="s" class="stg" :class="{ done: i < p.stage, cur: i === p.stage }">
                <div class="bar" /><div class="sl">{{ s }}</div>
              </div>
            </div>
            <div class="stg-ctl">
              <button class="act" :disabled="p.stage === 0" @click="setStage(-1)">← 回退</button>
              <button class="act pri" :disabled="p.stage === 4" @click="setStage(1)">推进到下一阶段 →</button>
              <span style="font-size:11.5px;color:var(--dim);align-self:center">{{ isBehind(p) ? "按开标日程，阶段已落后" : "阶段与开标日程匹配" }}</span>
            </div>
          </div>
        </template>

        <div class="dr-sec">
          <div class="lab">招标资料</div>
          <div class="docs">
            <span v-if="tenderDoc" class="file-chip">📄 {{ tenderDoc.filename }}<span class="sz">{{ Math.round((tenderDoc.size || 0) / 1024) }} KB</span></span>
            <span v-else class="file-chip todo">招标文件待上传</span>
            <span class="file-chip todo">招标公告待上传</span>
          </div>
        </div>

        <div class="dr-sec">
          <div class="lab">客户历史合作项目</div>
          <div class="hist-empty">暂无该客户的历史合作记录</div>
        </div>
      </div>
      <div class="dr-foot">
        <button class="act" @click="openProjectModal(p.id)">上传资料</button>
        <button class="act" @click="goDocs()">文档库管理资料</button>
        <button class="act" @click="goMats()">素材库找写作素材</button>
        <button v-if="tenderDoc && docs.some((d) => d.kind === 'bid')" class="act" @click="goWork()">去工作台检测</button>
        <button v-else class="act" title="需先上传招标文件与投标文件" @click="toast('该项目还需补齐招标文件 / 投标文件，先「上传资料」')">去工作台检测</button>
        <button class="act pri" @click="openProjectModal(p.id)">编辑项目</button>
      </div>
    </aside>
  </template>
</template>
