<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "../api/client";
import type { DocKind, ExtractPreviewT, ExtractRuleGroupT, ExtractRuleT, Health, ScanReportT, WatchPathT } from "../api/types";
import { askConfirm, toast } from "../store";

const health = ref<Health | null>(null);
const section = ref("系统信息");

const sections = ["系统信息", "模型服务", "数据安全", "拆解规则", "检测规则", "关于"] as const;
const detectRules = ref<{ id: string; name: string; sev: string; type: string; desc: string; enabled: boolean }[]>([]);
const ruleHistory = ref<{ rule_id: string; rule_name: string; note: string; created_at: string | null }[]>([]);

/* ---------- 拆解规则中心（契约《标库-拆解规则契约》v1.0） ---------- */
/* 展示约束：不展示 scope、不展示/不可编辑 match_mode（契约 §2.3/§2.4） */

interface ExAnchorDraft {
  text: string;
  role: "start" | "end";
}

interface ExDraft {
  name: string;
  group_name: string;
  summary: string;
  anchors: ExAnchorDraft[];
  focus: string[];
}

const exGroups = ref<ExtractRuleGroupT[]>([]);
/** 空分组不渲染（项目负责人 09-29：空类型区块属不需要的展示）；首条规则经工具栏「新增规则」创建 */
const exVisibleGroups = computed(() => exGroups.value.filter((g) => g.items.length));
const exVersion = ref(0);
const exStale = ref(0);
const exHistory = ref<{ rule_id: string; rule_name: string; note: string; created_at: string | null }[]>([]);
const exOpen = ref<Record<string, boolean>>({});
const exDraft = ref<Record<string, ExDraft>>({});
const exNewAnchor = ref<Record<string, string>>({});
const exNewFocus = ref<Record<string, string>>({});
const exBusy = ref(false);
const exHistOpen = ref(false);
const exCreate = ref<{ kind: DocKind; name: string; group_name: string; summary: string; anchors: string; focus: string } | null>(null);

async function loadExtractRules(): Promise<void> {
  try {
    const r = await api.extractRules();
    exGroups.value = r.groups;
    exVersion.value = r.ruleset_version;
    exStale.value = r.stale_docs;
  } catch (e) {
    toast(e instanceof Error ? e.message : "规则加载失败");
  }
}

async function loadExHistory(): Promise<void> {
  try {
    exHistory.value = (await api.extractRuleHistory()).items;
  } catch {
    exHistory.value = [];
  }
}

/** 保存后统一刷新：规则清单 + 历史（历史仅在弹层打开时拉取） */
async function reloadEx(): Promise<void> {
  await loadExtractRules();
  if (exHistOpen.value) await loadExHistory();
}

function exAnchorDrafts(r: ExtractRuleT): ExAnchorDraft[] {
  return r.anchors
    .slice()
    .sort((a, b) => a.priority - b.priority)
    .map((a) => ({ text: a.text, role: a.role === "end" ? "end" : "start" }));
}

function toggleExEdit(r: ExtractRuleT): void {
  if (exOpen.value[r.id]) {
    delete exOpen.value[r.id];
    delete exDraft.value[r.id];
    return;
  }
  exOpen.value[r.id] = true;
  exDraft.value[r.id] = {
    name: r.name,
    group_name: r.group_name,
    summary: r.summary ?? "",
    anchors: exAnchorDrafts(r),
    focus: r.focus_fields.slice(),
  };
  exNewAnchor.value[r.id] = "";
  exNewFocus.value[r.id] = "";
}

function exAddAnchor(r: ExtractRuleT): void {
  const d = exDraft.value[r.id];
  const v = (exNewAnchor.value[r.id] ?? "").trim();
  if (!d || !v) return;
  if (d.anchors.some((a) => a.text === v)) {
    toast("该锚点已在列表中");
    return;
  }
  d.anchors.push({ text: v, role: "start" });
  exNewAnchor.value[r.id] = "";
}

/** 起始/结束角色切换（契约 v1.2）：结束锚点 = 区间到该标题为止（不含该行） */
function exToggleRole(r: ExtractRuleT, idx: number): void {
  const a = exDraft.value[r.id]?.anchors[idx];
  if (!a) return;
  a.role = a.role === "end" ? "start" : "end";
  if (!exDraft.value[r.id]!.anchors.some((x) => x.role === "start")) {
    toast("至少保留 1 个起始锚点");
    a.role = "start";
  }
}

function exDelAnchor(r: ExtractRuleT, idx: number): void {
  const d = exDraft.value[r.id];
  if (!d) return;
  if (d.anchors.length <= 1) {
    toast("至少保留 1 个锚点");
    return;
  }
  d.anchors.splice(idx, 1);
}

/** 锚点顺序即匹配优先顺序（契约 §2.3） */
function exMoveAnchor(r: ExtractRuleT, idx: number, dir: -1 | 1): void {
  const d = exDraft.value[r.id];
  if (!d) return;
  const to = idx + dir;
  if (to < 0 || to >= d.anchors.length) return;
  const [x] = d.anchors.splice(idx, 1);
  d.anchors.splice(to, 0, x);
}

function exAddFocus(r: ExtractRuleT): void {
  const d = exDraft.value[r.id];
  const v = (exNewFocus.value[r.id] ?? "").trim();
  if (!d || !v) return;
  if (!d.focus.includes(v)) d.focus.push(v);
  exNewFocus.value[r.id] = "";
}

function exDelFocus(r: ExtractRuleT, idx: number): void {
  exDraft.value[r.id]?.focus.splice(idx, 1);
}

async function exSave(r: ExtractRuleT): Promise<void> {
  const d = exDraft.value[r.id];
  if (!d || exBusy.value) return;
  if (!d.name.trim()) {
    toast("规则名称不能为空");
    return;
  }
  if (!d.anchors.length) {
    toast("锚点至少 1 个");
    return;
  }
  if (!d.anchors.some((a) => a.role === "start")) {
    toast("至少保留 1 个起始锚点");
    return;
  }
  exBusy.value = true;
  try {
    const res = await api.patchExtractRule(r.id, {
      name: d.name.trim(),
      group_name: d.group_name.trim(),
      summary: d.summary.trim(),
      anchors: d.anchors.map((a) => ({ text: a.text, role: a.role })),
      focus_fields: d.focus,
    });
    toast(res.changed === false ? "内容无变化" : "规则已保存");
    delete exOpen.value[r.id];
    delete exDraft.value[r.id];
    await reloadEx();
  } catch (e) {
    toast(e instanceof Error ? e.message : "保存失败");
  } finally {
    exBusy.value = false;
  }
}

async function exToggle(r: ExtractRuleT): Promise<void> {
  if (exBusy.value) return;
  exBusy.value = true;
  try {
    await api.patchExtractRule(r.id, { enabled: !r.enabled });
    toast(r.enabled ? `已停用「${r.name}」` : `已启用「${r.name}」`);
    await reloadEx();
  } catch (e) {
    toast(e instanceof Error ? e.message : "操作失败");
  } finally {
    exBusy.value = false;
  }
}

async function exDelete(r: ExtractRuleT): Promise<void> {
  if (exBusy.value) return;
  if (r.builtin) {
    toast("内置规则不可删除，可用「恢复默认」还原");
    return;
  }
  const yes = await askConfirm("删除规则", `<p>确认删除规则「<b>${r.name}</b>」？</p><p style="margin-top:6px">删除后已拆解文档会标记为「规则已变更」，需重新拆解才会按新规则生效。</p>`, "确认删除");
  if (!yes) return;
  exBusy.value = true;
  try {
    await api.deleteExtractRule(r.id);
    toast(`规则「${r.name}」已删除`);
    await reloadEx();
  } catch (e) {
    toast(e instanceof Error ? e.message : "删除失败");
  } finally {
    exBusy.value = false;
  }
}

async function exMove(kind: DocKind, items: ExtractRuleT[], idx: number, dir: -1 | 1): Promise<void> {
  const to = idx + dir;
  if (to < 0 || to >= items.length || exBusy.value) return;
  const ids = items.map((x) => x.id);
  const [x] = ids.splice(idx, 1);
  ids.splice(to, 0, x);
  exBusy.value = true;
  try {
    await api.reorderExtractRules(kind, ids);
    await reloadEx();
  } catch (e) {
    toast(e instanceof Error ? e.message : "排序失败");
  } finally {
    exBusy.value = false;
  }
}

async function exReset(kind?: DocKind): Promise<void> {
  if (exBusy.value) return;
  const scopeText = kind ? `「${exGroups.value.find((g) => g.kind === kind)?.label ?? kind}」` : "全部类型";
  const yes = await askConfirm(
    "恢复默认规则",
    `<p>确认将 ${scopeText} 的规则恢复为内置默认？</p><p style="margin-top:6px">自定义规则将被删除，内置规则的名称/锚点/关注字段还原为出厂值。已拆解文档会标记为「规则已变更」。</p>`,
    "恢复默认",
  );
  if (!yes) return;
  exBusy.value = true;
  try {
    const r = await api.resetExtractRules(kind);
    toast(`已恢复默认（还原 ${r.restored} 项，移除 ${r.removed} 项自定义规则）`);
    await reloadEx();
  } catch (e) {
    toast(e instanceof Error ? e.message : "恢复失败");
  } finally {
    exBusy.value = false;
  }
}

function exOpenCreate(kind: DocKind = "tender"): void {
  exCreate.value = { kind, name: "", group_name: "", summary: "", anchors: "", focus: "" };
}

async function exSubmitCreate(): Promise<void> {
  const c = exCreate.value;
  if (!c || exBusy.value) return;
  const name = c.name.trim();
  const anchors = c.anchors.split(/[\n,，]/).map((x) => x.trim()).filter(Boolean);
  if (!name) {
    toast("请填写规则名称");
    return;
  }
  if (!anchors.length) {
    toast("请至少填写 1 个锚点");
    return;
  }
  exBusy.value = true;
  try {
    await api.createExtractRule({
      kind: c.kind,
      name,
      anchors: anchors.map((t) => ({ text: t })),
      group_name: c.group_name.trim(),
      summary: c.summary.trim(),
      focus_fields: c.focus.split(/[\n,，]/).map((x) => x.trim()).filter(Boolean),
    });
    toast(`规则「${name}」已新增`);
    exCreate.value = null;
    await reloadEx();
  } catch (e) {
    toast(e instanceof Error ? e.message : "新增失败");
  } finally {
    exBusy.value = false;
  }
}

async function openExHistory(): Promise<void> {
  exHistOpen.value = true;
  await loadExHistory();
}

/* ---------- 测试规则（契约 §十一：dry-run 命中证据，不落库） ---------- */
const exTest = ref<null | { docId: string; ruleIds: string[]; running: boolean }>(null);
const exTestDocs = ref<{ id: string; filename: string; kind: DocKind }[]>([]);
const exTestResult = ref<ExtractPreviewT | null>(null);

const exTestCandidateRules = computed<ExtractRuleT[]>(() => {
  const t = exTest.value;
  if (!t) return [];
  const doc = exTestDocs.value.find((d) => d.id === t.docId);
  const kind = doc?.kind ?? "tender";
  const g = exGroups.value.find((x) => x.kind === kind);
  return (g?.items ?? []).filter((x) => x.enabled);
});

async function openExTest(ruleId?: string): Promise<void> {
  if (!exTestDocs.value.length) {
    try {
      exTestDocs.value = (await api.documents()).items.map((d) => ({
        id: d.id, filename: d.filename, kind: d.kind,
      }));
    } catch (e) {
      toast(e instanceof Error ? e.message : "文档列表加载失败");
      return;
    }
  }
  if (!exTestDocs.value.length) {
    toast("文档库暂无文档，请先上传");
    return;
  }
  const rule = ruleId
    ? exGroups.value.flatMap((g) => g.items).find((x) => x.id === ruleId)
    : undefined;
  const kind = rule?.kind ?? exTestDocs.value[0].kind;
  const g = exGroups.value.find((x) => x.kind === kind);
  exTest.value = {
    docId: rule
      ? exTestDocs.value.find((d) => d.kind === kind)?.id ?? exTestDocs.value[0].id
      : exTestDocs.value[0].id,
    ruleIds: rule ? [rule.id] : (g?.items ?? []).filter((x) => x.enabled).map((x) => x.id),
    running: false,
  };
  exTestResult.value = null;
}

function exTestToggleRule(id: string): void {
  const t = exTest.value;
  if (!t) return;
  t.ruleIds = t.ruleIds.includes(id) ? t.ruleIds.filter((x) => x !== id) : [...t.ruleIds, id];
}

function exTestOnDocChange(): void {
  const t = exTest.value;
  if (!t) return;
  t.ruleIds = exTestCandidateRules.value.map((x) => x.id);
  exTestResult.value = null;
}

async function exTestRun(): Promise<void> {
  const t = exTest.value;
  if (!t || t.running || !t.docId || !t.ruleIds.length) return;
  t.running = true;
  exTestResult.value = null;
  try {
    exTestResult.value = await api.previewExtractRules({
      document_id: t.docId,
      rule_set_version: exVersion.value,
      rules: t.ruleIds,
    });
  } catch (e) {
    toast(e instanceof Error ? e.message : "试运行失败");
  } finally {
    t.running = false;
  }
}

function exRuleMethod(r: ExtractRuleT): string {
  return r.kind === "tender" ? "锚点定位 · 整表抽取" : r.group_name ? `区间法 · ${r.group_name}` : "区间法";
}

async function toggleRule(r: { id: string; enabled: boolean }): Promise<void> {
  try {
    await api.patchDetectRule(r.id, { enabled: !r.enabled });
    await loadSecData();
  } catch (e) {
    toast(e instanceof Error ? e.message : "操作失败");
  }
}

/* 数据安全：自动归档 + 备份 */
const pathsList = ref<WatchPathT[]>([]);
const reports = ref<ScanReportT[]>([]);
const newPath = ref("");
const backups = ref<{ name: string; label: string; size_mb: number }[]>([]);
const backupKeep = ref(7);
const backupSchedule = ref("");

async function loadSecData(): Promise<void> {
  try {
    health.value = await api.health();
    detectRules.value = (await api.detectRules()).rules;
    ruleHistory.value = (await api.detectRuleHistory()).items;
    if (section.value === "数据安全") {
      const [p, rp, bk] = await Promise.all([api.paths(), api.scanReports(8), api.backups()]);
      pathsList.value = p.items;
      reports.value = rp.items;
      backups.value = bk.items;
      backupKeep.value = bk.keep;
      backupSchedule.value = bk.schedule;
    }
  } catch (e) {
    toast(e instanceof Error ? e.message : "加载失败");
  }
}

function switchSection(s: typeof sections[number]): void {
  section.value = s;
  if (s === "数据安全") void loadSecData();
  if (s === "拆解规则") void loadExtractRules();
}

async function addWatchPath(): Promise<void> {
  const path = newPath.value.trim();
  if (!path) return;
  try {
    await api.addPath(path);
    newPath.value = "";
    toast("监视目录已添加");
    await loadSecData();
  } catch (e) {
    toast(e instanceof Error ? e.message : "添加失败");
  }
}

async function delWatchPath(id: string): Promise<void> {
  try {
    await api.removePath(id);
    await loadSecData();
  } catch (e) {
    toast(e instanceof Error ? e.message : "删除失败");
  }
}

async function scanPathNow(id: string): Promise<void> {
  try {
    await api.scanNow(id);
    toast("扫描完成");
    await loadSecData();
  } catch (e) {
    toast(e instanceof Error ? e.message : "扫描失败");
  }
}

async function backupNow(): Promise<void> {
  try {
    const r = await api.backupNow();
    toast(`备份完成：${r.name}（${r.size_mb} MB）`);
    await loadSecData();
  } catch (e) {
    toast(e instanceof Error ? e.message : "备份失败");
  }
}

async function doRestore(name: string): Promise<void> {
  if (!window.confirm(`确认还原备份「${name}」？当前数据将被覆盖，重启服务后生效。`)) return;
  try {
    await api.backupRestore(name);
    toast("还原已就绪，请重启服务（双击启动脚本）后生效");
  } catch (e) {
    toast(e instanceof Error ? e.message : "还原失败");
  }
}

/* ---------- 模型服务：AI 网关全局配置（用户可自配其他模型） ---------- */
const gw = ref<{
  provider: string; base_url: string; model: string; timeout_s: number;
  api_key_ref: string; has_key: boolean; secret_source: string;
} | null>(null);
const gwForm = ref({ provider: "", base_url: "", model: "", timeout_s: 60, api_key_ref: "", api_key: "" });
const gwBusy = ref(false);
const gwTestMsg = ref("");
const gwExpanded = ref(false);

/** 预设：常见 OpenAI 兼容服务，选中后自动填 base_url 与模型名（仍可手改） */
const GW_PRESETS: { label: string; provider: string; base_url: string; model: string; key_ref: string; hint: string }[] = [
  { label: "智谱 GLM", provider: "openai_compatible", base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash", key_ref: "ZHIPU_API_KEY", hint: "默认 · 免费" },
  { label: "DeepSeek", provider: "openai_compatible", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat", key_ref: "DEEPSEEK_API_KEY", hint: "文本质量高" },
  { label: "通义千问", provider: "openai_compatible", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus", key_ref: "DASHSCOPE_API_KEY", hint: "阿里云" },
  { label: "Kimi", provider: "openai_compatible", base_url: "https://api.moonshot.cn/v1", model: "moonshot-v1-8k", key_ref: "MOONSHOT_API_KEY", hint: "长上下文" },
  { label: "OpenAI", provider: "openai_compatible", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini", key_ref: "OPENAI_API_KEY", hint: "需可访问" },
  { label: "自定义", provider: "openai_compatible", base_url: "", model: "", key_ref: "BIDVAULT_AI_KEY", hint: "任意 OpenAI 兼容服务" },
];

async function loadGlobalGateway(): Promise<void> {
  try {
    const r = await api.globalGateway();
    gw.value = r;
    gwForm.value = {
      provider: r.provider || "openai_compatible",
      base_url: r.base_url,
      model: r.model,
      timeout_s: r.timeout_s || 60,
      api_key_ref: r.api_key_ref || "BIDVAULT_AI_KEY",
      api_key: "",
    };
  } catch {
    gw.value = null;
  }
}

function applyPreset(p: (typeof GW_PRESETS)[number]): void {
  gwForm.value.provider = p.provider;
  gwForm.value.base_url = p.base_url;
  gwForm.value.model = p.model;
  gwForm.value.api_key_ref = p.key_ref;
  gwTestMsg.value = "";
}

async function saveGlobalGateway(): Promise<void> {
  if (gwBusy.value) return;
  gwBusy.value = true;
  gwTestMsg.value = "";
  try {
    const body: Record<string, unknown> = {
      provider: gwForm.value.provider,
      base_url: gwForm.value.base_url.trim(),
      model: gwForm.value.model.trim(),
      timeout_s: gwForm.value.timeout_s,
      api_key_ref: gwForm.value.api_key_ref.trim(),
    };
    // 仅在用户实际输入时提交密钥（留空＝保留原密钥）
    if (gwForm.value.api_key.trim()) body.api_key = gwForm.value.api_key.trim();
    const r = await api.saveGlobalGateway(body as Parameters<typeof api.saveGlobalGateway>[0]);
    gw.value = r;
    gwForm.value.api_key = "";
    toast("模型服务配置已保存");
  } catch (e) {
    toast(e instanceof Error ? e.message : "保存失败");
  } finally {
    gwBusy.value = false;
  }
}

async function clearGatewayKey(): Promise<void> {
  if (!window.confirm("确认清除已保存的 API 密钥？（清除后需重新填写才能调用模型）")) return;
  gwBusy.value = true;
  try {
    const r = await api.saveGlobalGateway({ api_key: "__clear__" });
    gw.value = r;
    toast("已清除密钥");
  } catch (e) {
    toast(e instanceof Error ? e.message : "清除失败");
  } finally {
    gwBusy.value = false;
  }
}

async function testGlobalGateway(): Promise<void> {
  if (gwBusy.value) return;
  gwBusy.value = true;
  gwTestMsg.value = "测试中…";
  try {
    const r = await api.testGlobalGateway();
    gwTestMsg.value = `连接成功 · ${r.model} · ${r.latency_ms}ms`;
  } catch (e) {
    gwTestMsg.value = e instanceof Error ? e.message : "连接失败";
  } finally {
    gwBusy.value = false;
  }
}

onMounted(() => {
  void loadSecData();
  void loadGlobalGateway();
});
</script>

<template>
  <div class="viewscroll">
    <div class="setwrap" style="flex:1;min-height:0">
      <aside class="stree">
        <div class="tt">设置中心</div>
        <button v-for="s in sections" :key="s" class="sitem" :class="{ on: section === s }" @click="switchSection(s)">
          {{ s }}
          <span v-if="s === '模型服务'" class="dot" :class="{ warn: health && !health.glm_configured }" />
          <span v-else-if="s === '数据安全'" class="dot" :class="{ warn: health?.disk?.warn }" />
        </button>
      </aside>

      <div class="spanel">
        <div class="sp-head">
          <h3>{{ section }}</h3>
        </div>
        <div class="sp-body">
          <!-- 系统信息 -->
          <template v-if="section === '系统信息'">
            <div v-if="health" class="metrics">
              <div class="metric"><b>{{ health.counts.documents }}</b><span>文档总数</span></div>
              <div class="metric"><b>{{ health.counts.materials }}</b><span>素材总数</span></div>
              <div class="metric"><b>{{ health.counts.watch_paths }}</b><span>监视目录</span></div>
              <div class="metric"><b>{{ health.counts.audit_logs }}</b><span>审计记录</span></div>
              <div class="metric" :class="{ warn: health.disk?.warn }"><b>{{ health.disk?.free_gb ?? "—" }}</b><span>磁盘剩余 GB{{ health.disk?.warn ? " · 水位告警" : "" }}</span></div>
            </div>
            <div class="tblbox">
              <table>
                <tbody>
                  <tr><td>运行状态</td><td><span class="chip-role r-admin">正常</span></td></tr>
                  <tr><td>数据目录</td><td style="font-family:var(--mono);font-size:12px">{{ health?.data_dir ?? "—" }}</td></tr>
                  <tr><td>定时扫描间隔</td><td>{{ health?.scan_interval ?? "—" }} 秒</td></tr>
                  <tr><td>部署形态</td><td>单机 localhost · 单用户</td></tr>
                </tbody>
              </table>
            </div>
          </template>

          <!-- 模型服务 -->
          <template v-else-if="section === '模型服务'">
            <!-- AI 网关：当前配置 + 用户自配 -->
            <div class="gwcard" :class="{ on: gw?.has_key }">
              <div class="gw-head">
                <span class="gw-t">
                  <b>AI 服务（网关）</b>
                  <span class="chip-role" :class="gw?.has_key ? 'r-admin' : 'r-viewer'">{{ gw?.has_key ? "已连接" : "未配置" }}</span>
                </span>
                <span class="gw-actions">
                  <button class="mini-btn" :disabled="gwBusy" @click="testGlobalGateway">连接测试</button>
                  <button class="mini-btn" @click="gwExpanded = !gwExpanded">{{ gwExpanded ? "收起" : "配置" }}</button>
                </span>
              </div>
              <div class="gw-sum">
                <span v-if="gw && gw.provider">{{ gw.model || "（未填模型名）" }} · {{ gw.base_url || "（未填地址）" }}</span>
                <span v-else>尚未配置模型服务（AI 助手与拆解增强不可用）</span>
                <span v-if="gwTestMsg" class="gw-test" :class="{ ok: gwTestMsg.includes('连接成功') }">{{ gwTestMsg }}</span>
              </div>

              <div v-if="gwExpanded" class="gw-form">
                <div class="gw-presets">
                  <button v-for="p in GW_PRESETS" :key="p.label" type="button" class="gw-chip"
                          :class="{ on: gwForm.base_url === p.base_url && gwForm.model === p.model }"
                          @click="applyPreset(p)">{{ p.label }}<span class="h">{{ p.hint }}</span></button>
                </div>
                <div class="frow">
                  <span class="fl">服务地址</span>
                  <input v-model="gwForm.base_url" type="text" class="gw-in"
                         placeholder="https://api.example.com/v1" />
                </div>
                <div class="frow">
                  <span class="fl">模型名称</span>
                  <input v-model="gwForm.model" type="text" class="gw-in" placeholder="如 deepseek-chat" />
                </div>
                <div class="frow">
                  <span class="fl">API 密钥</span>
                  <input v-model="gwForm.api_key" type="password" class="gw-in"
                         :placeholder="gw?.has_key ? '已保存（留空则不变）' : '粘贴你的密钥'" autocomplete="new-password" />
                </div>
                <div class="frow">
                  <span class="fl">超时（秒）</span>
                  <input v-model.number="gwForm.timeout_s" type="number" min="5" max="600" class="gw-in gw-num" />
                </div>
                <div class="gw-foot">
                  <button v-if="gw?.has_key" class="mini-btn" :disabled="gwBusy" @click="clearGatewayKey">清除密钥</button>
                  <span class="gw-note">密钥经本机系统凭据保存，不写入配置文件</span>
                  <button class="btn-primary" :disabled="gwBusy" @click="saveGlobalGateway">保存配置</button>
                </div>
              </div>
            </div>

            <div class="pcards">
              <div class="pcard" :class="{ on: health?.glm_configured }">
                <div class="ph">
                  <b>GLM-4V-Flash</b>
                  <span class="chip-role" :class="health?.glm_configured ? 'r-admin' : 'r-viewer'">{{ health?.glm_configured ? "已接入" : "未配置" }}</span>
                </div>
                <div class="pd">图片证据字段识别 · 素材库「AI 识别字段」按钮触发</div>
                <div class="pm">需 ZHIPU_API_KEY 环境变量</div>
              </div>
              <div class="pcard on">
                <div class="ph"><b>PaddleOCR</b><span class="chip-role r-admin">已接入</span></div>
                <div class="pd">扫描件自动 OCR · 约 4 秒/页 · 全程本机运行</div>
                <div class="pm">local · cpu</div>
              </div>
              <div class="pcard on">
                <div class="ph"><b>向量检索</b><span class="chip-role r-admin">已接入</span></div>
                <div class="pd">bge-small-zh 本地语义向量 + FTS5 关键词混合检索</div>
                <div class="pm">local · 全局检索「自然语言」模式</div>
              </div>
            </div>
          </template>

          <!-- 数据安全 -->
          <template v-else-if="section === '数据安全'">
            <div class="tblbox">
              <table>
                <tbody>
                  <tr><td>数据存储</td><td>全程本机磁盘（原件 / 抽取结果 / 页图缓存 / 备份）</td></tr>
                  <tr><td>在线调用审计</td><td>{{ health?.counts.audit_logs ?? "—" }} 条留痕</td></tr>
                  <tr><td>自动备份</td><td>{{ backupSchedule || "每日 02:00 自动备份" }} · 滚动保留 {{ backupKeep }} 份</td></tr>
                  <tr><td>磁盘空间</td><td :class="health?.disk?.warn ? 'disk-warn' : ''">剩余 {{ health?.disk?.free_gb ?? "—" }} GB / 共 {{ health?.disk?.total_gb ?? "—" }} GB · 已用 {{ health?.disk?.used_pct ?? "—" }}%{{ health?.disk?.warn ? "（低于安全水位，请清理或迁移）" : "" }}</td></tr>
                </tbody>
              </table>
            </div>

            <div class="sec-title">自动归档监视目录</div>
            <div class="arcrow">
              <input v-model="newPath" type="text" placeholder="D:\标书资料\归档目录">
              <button class="btn-primary" @click="addWatchPath">添加</button>
            </div>
            <div class="tblbox">
              <table>
                <thead><tr><th>目录</th><th>最近扫描</th><th style="width:150px"></th></tr></thead>
                <tbody>
                  <tr v-for="p in pathsList" :key="p.id">
                    <td style="font-family:var(--mono);font-size:12px">{{ p.path }}</td>
                    <td>{{ p.last_scan_at ? p.last_scan_at.slice(0, 19).replace("T", " ") : "—" }}</td>
                    <td>
                      <button class="mini-btn" @click="scanPathNow(p.id)">立即扫描</button>
                      <button class="mini-btn danger" style="margin-left:6px" @click="delWatchPath(p.id)">删除</button>
                    </td>
                  </tr>
                  <tr v-if="!pathsList.length"><td colspan="3" style="color:var(--dim)">尚未添加监视目录</td></tr>
                </tbody>
              </table>
            </div>

            <div class="sec-title">最近扫描报告</div>
            <div class="tblbox">
              <table>
                <thead><tr><th>目录</th><th>触发</th><th>新增</th><th>更新</th><th>跳过</th><th>失败</th><th>时间</th></tr></thead>
                <tbody>
                  <tr v-for="r in reports" :key="r.id">
                    <td style="font-family:var(--mono);font-size:11px">{{ r.path }}</td>
                    <td>{{ r.trigger === "manual" ? "手动" : r.trigger === "startup" ? "启动" : "定时" }}</td>
                    <td>{{ r.added }}</td><td>{{ r.updated }}</td><td>{{ r.skipped }}</td><td>{{ r.failed }}</td>
                    <td>{{ r.created_at ? r.created_at.slice(5, 16).replace("T", " ") : "—" }}</td>
                  </tr>
                  <tr v-if="!reports.length"><td colspan="7" style="color:var(--dim)">暂无扫描记录</td></tr>
                </tbody>
              </table>
            </div>

            <div class="sec-title">备份与还原</div>
            <div class="arcrow">
              <button class="btn-primary" @click="backupNow">立即备份</button>
            </div>
            <div class="tblbox">
              <table>
                <thead><tr><th>备份</th><th>方式</th><th>大小</th><th style="width:100px"></th></tr></thead>
                <tbody>
                  <tr v-for="b in backups" :key="b.name">
                    <td style="font-family:var(--mono);font-size:12px">{{ b.name }}</td>
                    <td>{{ b.label === "auto" ? "自动" : "手动" }}</td>
                    <td>{{ b.size_mb }} MB</td>
                    <td><button class="mini-btn" @click="doRestore(b.name)">还原</button></td>
                  </tr>
                  <tr v-if="!backups.length"><td colspan="4" style="color:var(--dim)">暂无备份，点击「立即备份」创建</td></tr>
                </tbody>
              </table>
            </div>
          </template>

          <!-- 拆解规则（规则中心：可配置，契约 v1.0） -->
          <template v-else-if="section === '拆解规则'">
            <div class="ex-toolbar">
              <span class="sub-note">规则集 v{{ exVersion }}</span>
              <span v-if="exStale" class="chip-miss">有 {{ exStale }} 份已拆解文档需重新拆解</span>
              <span style="margin-left:auto" />
              <button class="mini-btn" :disabled="exBusy" @click="exOpenCreate()">新增规则</button>
              <button class="mini-btn" :disabled="exBusy" @click="openExTest()">测试规则</button>
              <button class="mini-btn" :disabled="exBusy" @click="openExHistory">规则历史</button>
              <button class="mini-btn" :disabled="exBusy" @click="exReset()">恢复默认</button>
            </div>

            <template v-for="g in exVisibleGroups" :key="g.kind">
              <div class="sec-lab" :style="g.kind === 'tender' ? '' : 'margin-top:14px'">
                {{ g.label }} · {{ g.items.length }} 项（启用 {{ g.enabled_count }}）
                <button class="mini-btn" style="float:right;margin-top:-2px" :disabled="exBusy" @click="exOpenCreate(g.kind)">新增规则</button>
              </div>

              <div v-for="(r, idx) in g.items" :key="r.id" class="rulecard" :class="{ off: !r.enabled }">
                <div class="rh2">
                  <b>{{ r.name }}</b>
                  <span v-if="r.group_name" class="chip-tag">{{ r.group_name }}</span>
                  <span v-if="r.builtin" class="chip-role r-viewer">内置</span>
                  <span v-if="!r.enabled" class="chip-miss">已停用</span>
                  <span class="n">{{ exRuleMethod(r) }}</span>
                </div>
                <div class="rf">
                  <span v-for="a in r.anchors" :key="a.text" class="fchip hit">{{ a.text }}</span>
                  <span v-for="f in r.focus_fields" :key="f" class="fchip">{{ f }}</span>
                </div>
                <div class="rh2" style="margin-top:8px">
                  <button class="mini-btn" :disabled="exBusy" @click="toggleExEdit(r)">{{ exOpen[r.id] ? "收起" : "编辑" }}</button>
                  <button class="mini-btn" :disabled="exBusy" @click="exToggle(r)">{{ r.enabled ? "停用" : "启用" }}</button>
                  <button class="mini-btn" :disabled="exBusy" @click="openExTest(r.id)">测试</button>
                  <button class="mini-btn" :disabled="exBusy || idx === 0" @click="exMove(g.kind, g.items, idx, -1)">↑</button>
                  <button class="mini-btn" :disabled="exBusy || idx === g.items.length - 1" @click="exMove(g.kind, g.items, idx, 1)">↓</button>
                  <button class="mini-btn danger" :disabled="exBusy || r.builtin" :title="r.builtin ? '内置规则不可删除，可用恢复默认还原' : '删除规则'" @click="exDelete(r)">删除</button>
                </div>

                <!-- 展开编辑 -->
                <div v-if="exOpen[r.id] && exDraft[r.id]" class="ex-edit">
                  <div class="frm">
                    <div class="frow">
                      <div class="f">
                        <label class="fl">规则名称<span style="color:var(--fatal)">*</span></label>
                        <input v-model="exDraft[r.id].name" type="text" placeholder="如：合同条款">
                      </div>
                      <div class="f">
                        <label class="fl">分组（投标侧用于归类，可留空）</label>
                        <input v-model="exDraft[r.id].group_name" type="text" placeholder="如：业绩与履约情况">
                      </div>
                    </div>
                    <div class="f">
                      <label class="fl">说明</label>
                      <input v-model="exDraft[r.id].summary" type="text" placeholder="如：关键条款表">
                    </div>
                  </div>

                  <div class="sec-title" style="margin-top:10px">章节锚点（顺序即匹配优先；请勿带「第X章」等章节号；点「起/结」切换角色）</div>
                  <div class="rf">
                    <span v-for="(a, ai) in exDraft[r.id].anchors" :key="a.text + ai" class="fchip hit ex-chip" :class="{ 'ex-end': a.role === 'end' }">
                      <i class="ex-pri">{{ ai + 1 }}</i>
                      <i v-if="a.role === 'end'" class="ex-pri ex-end-tag">结</i>{{ a.text }}
                      <button class="ex-x" :title="a.role === 'end' ? '点击改为起始锚点' : '点击改为结束锚点（区间到该标题为止）'" @click="exToggleRole(r, ai)">{{ a.role === "end" ? "起" : "结" }}</button>
                      <button class="ex-x" :disabled="ai === 0" @click="exMoveAnchor(r, ai, -1)">‹</button>
                      <button class="ex-x" :disabled="ai === exDraft[r.id].anchors.length - 1" @click="exMoveAnchor(r, ai, 1)">›</button>
                      <button class="ex-x" @click="exDelAnchor(r, ai)">×</button>
                    </span>
                  </div>
                  <div class="arcrow" style="margin-top:6px">
                    <input v-model="exNewAnchor[r.id]" type="text" placeholder="新增锚点，如：评标办法（回车添加）" @keyup.enter="exAddAnchor(r)">
                    <button class="mini-btn" @click="exAddAnchor(r)">添加锚点</button>
                  </div>

                  <div class="sec-title" style="margin-top:10px">关注字段（仅作说明展示，不参与抽取过滤）</div>
                  <div class="rf">
                    <span v-for="(f, fi) in exDraft[r.id].focus" :key="f + fi" class="fchip ex-chip">
                      {{ f }}<button class="ex-x" @click="exDelFocus(r, fi)">×</button>
                    </span>
                    <span v-if="!exDraft[r.id].focus.length" class="sub-note">暂无</span>
                  </div>
                  <div class="arcrow" style="margin-top:6px">
                    <input v-model="exNewFocus[r.id]" type="text" placeholder="新增关注字段（回车添加）" @keyup.enter="exAddFocus(r)">
                    <button class="mini-btn" @click="exAddFocus(r)">添加字段</button>
                  </div>

                  <div class="m-foot" style="margin-top:10px">
                    <button class="btn-ghost" :disabled="exBusy" @click="toggleExEdit(r)">取消</button>
                    <button class="btn-primary" :disabled="exBusy" @click="exSave(r)">{{ exBusy ? "保存中…" : "保存规则" }}</button>
                  </div>
                </div>
              </div>
            </template>
          </template>

          <!-- 检测规则 -->
          <template v-else-if="section === '检测规则'">
            <div v-for="r in detectRules" :key="r.id" class="rulecard">
              <div class="rh2">
                <b>{{ r.name }}</b>
                <span class="chip-role" :class="r.sev === '致命' ? 'r-admin' : r.sev === '风险' ? 'r-agent' : 'r-viewer'">{{ r.sev }} · {{ r.type }}</span>
                <span class="n">{{ r.id }}</span>
              </div>
              <div class="rh2" style="margin-top:8px">
                <button class="mini-btn" :class="{ danger: r.enabled }" @click="toggleRule(r)">
                  {{ r.enabled ? "停用" : "启用" }}
                </button>
              </div>
              <div class="rf"><span class="fchip">{{ r.desc }}</span></div>
            </div>
            <div class="sec-title">变更留痕</div>
            <div class="tblbox">
              <table>
                <thead><tr><th>时间</th><th>规则</th><th>变更</th></tr></thead>
                <tbody>
                  <tr v-for="(h, i) in ruleHistory" :key="i">
                    <td>{{ h.created_at ? h.created_at.slice(0, 19).replace("T", " ") : "—" }}</td>
                    <td>{{ h.rule_name }}</td>
                    <td>{{ h.note }}</td>
                  </tr>
                  <tr v-if="!ruleHistory.length"><td colspan="3" style="color:var(--dim)">暂无变更记录</td></tr>
                </tbody>
              </table>
            </div>
          </template>

          <!-- 关于 -->
          <template v-else>
            <div class="tblbox">
              <table>
                <tbody>
                  <tr><td>产品</td><td>标库 BidVault v2（单机版招投标文件智能库）</td></tr>
                  <tr><td>版本</td><td style="font-family:var(--mono);font-size:12px">v2.2.0（图片对位与工作稿底稿）</td></tr>
                  <tr><td>当前里程碑</td><td>M1-M5 全部交付</td></tr>
                  <tr><td>技术栈</td><td>Vue 3 + FastAPI + SQLite（WAL / FTS5 trigram）</td></tr>
                </tbody>
              </table>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 新增规则 -->
    <div v-if="exCreate" class="modal-wrap" @click.self="exCreate = null">
      <div class="modal" role="dialog" aria-modal="true" aria-label="新增拆解规则">
        <h3>新增拆解规则</h3>
        <div class="frm">
          <div class="frow">
            <div class="f">
              <label class="fl">适用文档类型</label>
              <select v-model="exCreate.kind">
                <option v-for="g in exGroups" :key="g.kind" :value="g.kind">{{ g.label }}</option>
              </select>
            </div>
            <div class="f">
              <label class="fl">规则名称<span style="color:var(--fatal)">*</span></label>
              <input v-model="exCreate.name" type="text" placeholder="如：合同条款">
            </div>
          </div>
          <div class="frow">
            <div class="f">
              <label class="fl">分组（可留空）</label>
              <input v-model="exCreate.group_name" type="text" placeholder="如：业绩与履约情况">
            </div>
          </div>
          <div class="f">
            <label class="fl">说明（可留空）</label>
            <input v-model="exCreate.summary" type="text" placeholder="如：关键条款表">
          </div>
          <div class="f">
            <label class="fl">章节锚点<span style="color:var(--fatal)">*</span>（每行一个，顺序即匹配优先）</label>
            <textarea v-model="exCreate.anchors" rows="3" placeholder="合同条款&#10;合同主要条款" />
            <p class="sub-note">锚点为章节标题文本，勿带「第X章 / 1.2 / 一、」等章节号——系统匹配时会自动剥离正文标题的章节号。</p>
          </div>
          <div class="f">
            <label class="fl">关注字段（可留空，逗号或换行分隔，仅作说明展示）</label>
            <textarea v-model="exCreate.focus" rows="2" placeholder="工期, 质保期, 违约责任" />
          </div>
        </div>
        <div class="m-foot">
          <button class="btn-ghost" :disabled="exBusy" @click="exCreate = null">取消</button>
          <button class="btn-primary" :disabled="exBusy" @click="exSubmitCreate">{{ exBusy ? "保存中…" : "创建规则" }}</button>
        </div>
      </div>
    </div>

    <!-- 测试规则（试运行 · dry-run 命中证据） -->
    <div v-if="exTest" class="modal-wrap" @click.self="exTest = null">
      <div class="modal" role="dialog" aria-modal="true" aria-label="测试规则" style="min-width:720px">
        <h3>测试规则 · 试运行</h3>
        <p class="m-sub">只读试运行：返回规则命中证据（锚点 / 行号 / 行数 / 预览），不落任何数据，也不产生拆解结果。</p>
        <div class="frow">
          <div class="f">
            <label class="fl">选择文档</label>
            <select :value="exTest.docId" @change="(exTest!.docId = ($event.target as HTMLSelectElement).value); exTestOnDocChange()">
              <option v-for="d in exTestDocs" :key="d.id" :value="d.id">{{ d.filename }}（{{ { tender: "招标文件", bid: "投标文件", notice: "招标公告" }[d.kind] }}）</option>
            </select>
          </div>
        </div>
        <div class="sec-title" style="margin-top:8px">参与测试的规则（{{ exTestCandidateRules.length }} 条启用规则）</div>
        <div class="rf">
          <button v-for="r in exTestCandidateRules" :key="r.id" class="mini-btn"
                  :class="{ on: exTest.ruleIds.includes(r.id) }" :disabled="exTest.running"
                  @click="exTestToggleRule(r.id)">
            {{ exTest.ruleIds.includes(r.id) ? "✓ " : "" }}{{ r.name }}
          </button>
          <span v-if="!exTestCandidateRules.length" class="sub-note">该文档类型暂无启用规则</span>
        </div>
        <div class="m-foot" style="margin-top:10px">
          <button class="btn-ghost" @click="exTest = null">关闭</button>
          <button class="btn-primary" :disabled="exTest.running || !exTest.docId || !exTest.ruleIds.length" @click="exTestRun">
            {{ exTest.running ? "试运行中…" : "执行试运行" }}
          </button>
        </div>

        <template v-if="exTestResult">
          <div class="sec-title" style="margin-top:12px">
            命中证据 · 规则集 v{{ exTestResult.rule_set_version }}
            <span class="sub-note" style="margin-left:8px">
              文档 {{ exTestResult.guard.file_size_mb ?? "—" }} MB · {{ exTestResult.guard.pages ?? "—" }} 页 · 试运行不落库
            </span>
          </div>
          <div style="max-height:46vh;overflow:auto">
            <div v-for="m in exTestResult.matches" :key="m.rule_id" class="rulecard" :class="{ off: !m.matched }">
              <div class="rh2">
                <b>{{ m.name }}</b>
                <span v-if="m.matched" class="chip-role r-admin">命中</span>
                <span v-else class="chip-miss">未命中</span>
                <span v-if="m.matched" class="sub-note">
                  锚点「{{ m.anchor }}」 · 行 {{ m.location!.line_start }}–{{ m.location!.line_end }}<template v-if="m.location!.page_start"> · 第 {{ m.location!.page_start }}–{{ m.location!.page_end }} 页</template> · {{ m.rows_count }} 行<template v-if="m.truncated"> · 预览已截断</template>
                </span>
              </div>
              <div v-if="m.matched && m.preview.length" class="ex-preview">
                <div v-for="(ln, i) in m.preview" :key="i">{{ ln }}</div>
              </div>
              <div v-if="!m.matched" class="rf">
                <span class="chip-miss">{{ m.reason }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>
    </div>

    <!-- 规则变更留痕 -->
    <div v-if="exHistOpen" class="modal-wrap" @click.self="exHistOpen = false">
      <div class="modal" role="dialog" aria-modal="true" aria-label="规则变更留痕">
        <h3>规则变更留痕（当前规则集 v{{ exVersion }}）</h3>
        <div class="tblbox" style="max-height:52vh;overflow:auto">
          <table>
            <thead><tr><th style="width:150px">时间</th><th style="width:200px">规则</th><th>变更</th></tr></thead>
            <tbody>
              <tr v-for="(h, i) in exHistory" :key="i">
                <td>{{ h.created_at ? h.created_at.slice(0, 19).replace("T", " ") : "—" }}</td>
                <td>{{ h.rule_name }}</td>
                <td>{{ h.note }}</td>
              </tr>
              <tr v-if="!exHistory.length"><td colspan="3" style="color:var(--dim)">暂无变更记录</td></tr>
            </tbody>
          </table>
        </div>
        <div class="m-foot">
          <button class="btn-ghost" @click="exHistOpen = false">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>
