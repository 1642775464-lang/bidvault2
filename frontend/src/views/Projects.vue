<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api } from "../api/client";
import type { Project } from "../api/types";
import {
  STAGES,
  dueInfo,
  expectedStage,
  fmtOpenDateTime,
  isBehind,
  navTo,
  openDrawer,
  openProjectModal,
  refreshCore,
  store,
  toast,
} from "../store";

const mode = ref<"active" | "archived">("active");
const search = ref("");
const roleF = ref<"all" | "主标" | "陪标">("all");
const stageF = ref("all");

const rows = computed<Project[]>(() => {
  let list = store.projects.filter((p) => (mode.value === "archived" ? p.is_archived : !p.is_archived));
  if (roleF.value !== "all") list = list.filter((p) => p.role === roleF.value);
  if (mode.value === "active" && stageF.value !== "all") {
    list = list.filter((p) => STAGES[p.stage] === stageF.value);
  }
  const q = search.value.trim().toLowerCase();
  if (q) list = list.filter((p) => (p.name + " " + (p.cust ?? "") + " " + p.code).toLowerCase().includes(q));
  return list.slice().sort((a, b) => {
    const da = new Date((a.open_at ?? "").replace(" ", "T")).getTime();
    const db = new Date((b.open_at ?? "").replace(" ", "T")).getTime();
    return mode.value === "archived" ? db - da : da - db;
  });
});

watch(mode, () => {
  roleF.value = "all";
  stageF.value = "all";
});

async function afterChange(): Promise<void> {
  await refreshCore();
}

function stageDots(p: Project): boolean[] {
  return STAGES.map((_, i) => i <= p.stage);
}
function behind(p: Project): boolean {
  return isBehind(p);
}
async function advance(p: Project, delta: number): Promise<void> {
  const next = Math.min(4, Math.max(0, p.stage + delta));
  if (next === p.stage) return;
  try {
    await api.setStage(p.id, next);
    toast(`「${p.name}」已${delta > 0 ? "推进到" : "回退到"}「${STAGES[next]}」`);
    await afterChange();
  } catch (e) {
    toast(e instanceof Error ? e.message : "操作失败");
  }
}
function view(p: Project): void {
  openDrawer(p.id);
}
function edit(p: Project): void {
  openProjectModal(p.id);
}
function newProject(): void {
  openProjectModal(null);
}
function fmtDue(p: Project): string {
  return "2026-" + fmtOpenDateTime(p.open_at).slice(0, 11);
}

onMounted(afterChange);
</script>

<template>
  <div class="viewscroll">
    <div class="lib-bar">
      <div class="seg">
        <button :class="{ on: mode === 'active' }" @click="mode = 'active'">跟进中</button>
        <button :class="{ on: mode === 'archived' }" @click="mode = 'archived'">已归档</button>
      </div>
      <button class="btn-primary" @click="newProject()">
        <svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>新建项目
      </button>
    </div>

    <div class="sub-bar">
      <div class="f-search">
        <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.2-4.2" /></svg>
        <input v-model="search" type="text" placeholder="搜索项目 / 客户 / 编码">
      </div>
      <button v-for="r in ['all', '主标', '陪标'] as const" :key="r" class="f-chip" :class="{ on: roleF === r }" @click="roleF = r">
        {{ r === "all" ? "全部" : r }}
      </button>
      <select v-if="mode === 'active'" v-model="stageF" class="f-select">
        <option value="all">全部阶段</option>
        <option v-for="s in STAGES" :key="s">{{ s }}</option>
      </select>
      <span class="sub-note">{{ mode === "archived" ? "按开标时间倒序 · 含投标结果" : "按开标时间升序 · 点击行查看详情" }}</span>
    </div>

    <div class="tbl-wrap">
      <div class="tbl">
        <table>
          <thead>
            <tr v-if="mode === 'active'">
              <th>项目</th><th>主/陪</th><th>所属行业</th><th>开标时间</th><th>当前阶段</th><th>客户</th><th>负责销售</th><th>投标平台</th><th>操作</th>
            </tr>
            <tr v-else>
              <th>项目</th><th>主/陪</th><th>所属行业</th><th>开标时间</th><th>客户</th><th>投标结果</th><th>金额</th><th>负责销售</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in rows" :key="p.id" class="rrow" @click="view(p)">
              <td class="cell-name"><b>{{ p.name }}</b><span>{{ p.code }}</span></td>
              <td><span class="rolecell"><span class="chip-role" :class="p.role === '主标' ? 'chip-lead' : 'chip-sup'">{{ p.role }}</span></span></td>
              <td><span class="chip-ind">{{ p.industry }}</span></td>
              <td>
                <span class="cell-due">
                  <span class="d">{{ fmtDue(p) }}</span>
                  <span class="badge-due" :class="dueInfo(p).cls">{{ dueInfo(p).text }}</span>
                </span>
              </td>
              <template v-if="mode === 'active'">
                <td>
                  <span class="stp">
                    <i v-for="(on, i) in stageDots(p)" :key="i" :class="{ on }" />
                    <em>{{ p.stage_label }}</em>
                  </span>
                  <span v-if="behind(p)" class="chip-miss">落后</span>
                </td>
                <td>{{ p.cust || "—" }}</td>
                <td>{{ p.sales || "—" }}</td>
                <td style="font-size:12px">{{ p.platform }}</td>
              </template>
              <template v-else>
                <td>{{ p.cust || "—" }}</td>
                <td>
                  <span v-if="p.result" class="chip-result" :class="p.result === '中标' ? 'res-win' : 'res-lose'">{{ p.result }}</span>
                  <span v-if="p.result_note" style="font-size:11.5px;color:var(--dim);margin-left:6px">{{ p.result_note }}</span>
                  <span v-if="!p.result && !p.is_archived" style="color:var(--dim)">—</span>
                </td>
                <td style="font-family:var(--mono);font-size:12px">{{ p.result_amount || "—" }}</td>
                <td>{{ p.sales || "—" }}</td>
              </template>
              <td>
                <span class="ops" @click.stop>
                  <button class="act" @click="view(p)">查看</button>
                  <button class="act" @click="edit(p)">编辑</button>
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="!rows.length" class="empty-state">
        <b>无匹配项目</b>{{ mode === "archived" ? "暂无归档项目" : "调整筛选条件，或新建一个项目" }}
      </div>
    </div>
  </div>
</template>
