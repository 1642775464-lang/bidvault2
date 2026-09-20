<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api } from "../api/client";
import type { Health, ScanReportT, WatchPathT } from "../api/types";
import { toast } from "../store";

const health = ref<Health | null>(null);
const section = ref("系统信息");

const sections = ["系统信息", "模型服务", "数据安全", "拆解规则", "检测规则", "关于"] as const;
const detectRules = ref<{ id: string; name: string; sev: string; type: string; desc: string; enabled: boolean }[]>([]);
const ruleHistory = ref<{ rule_id: string; rule_name: string; note: string; created_at: string | null }[]>([]);

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

onMounted(() => {
  void loadSecData();
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
            <div class="pcards">
              <div class="pcard" :class="{ on: health?.glm_configured }">
                <div class="ph">
                  <b>GLM-4-Flash</b>
                  <span class="chip-role" :class="health?.glm_configured ? 'r-admin' : 'r-viewer'">{{ health?.glm_configured ? "已连接" : "未连接" }}</span>
                </div>
                <div class="pd">文本结构化抽取（拆解增强）· 免费模型</div>
                <div class="pm">zhipu api · server-side</div>
              </div>
              <div class="pcard" :class="{ on: health?.glm_configured }">
                <div class="ph"><b>GLM-4V-Flash</b><span class="chip-role" :class="health?.glm_configured ? 'r-admin' : 'r-viewer'">{{ health?.glm_configured ? "已接入" : "未配置" }}</span></div>
                <div class="pd">图片证据字段识别 · 素材库「AI 识别字段」按钮触发</div>
                <div class="pm">zhipu api · server-side</div>
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

          <!-- 拆解规则 -->
          <template v-else-if="section === '拆解规则'">
            <div class="sec-lab">招标文件 · 三项（不分组）</div>
            <div class="rulecard">
              <div class="rh2"><b>投标人须知前附表</b><span class="n">锚点定位 · 整表抽取</span></div>
              <div class="rf"><span class="fchip hit">递交截止时间</span><span class="fchip hit">投标有效期</span><span class="fchip hit">投标保证金</span><span class="fchip hit">正副本份数</span><span class="fchip">资质/财务/业绩/信誉要求</span><span class="fchip">招标人/代理机构/交货期</span></div>
            </div>
            <div class="rulecard">
              <div class="rh2"><b>评标办法前附表</b><span class="n">锚点定位 · 整表抽取</span></div>
              <div class="rf"><span class="fchip hit">评标方法与排序规则</span><span class="fchip hit">形式评审标准</span><span class="fchip hit">资格评审标准</span><span class="fchip">评分项及分值</span></div>
            </div>
            <div class="rulecard">
              <div class="rh2"><b>投标文件格式</b><span class="n">格式章节内全部表格</span></div>
              <div class="rf"><span class="fchip">开标一览表</span><span class="fchip">产品明细报价</span><span class="fchip">商务/技术偏差表</span><span class="fchip">资格文件</span><span class="fchip">分项报价表</span></div>
            </div>
            <div class="sec-lab" style="margin-top:14px">投标文件 · 两项（不分组）</div>
            <div class="rulecard">
              <div class="rh2"><b>近年完成的类似项目情况表</b><span class="n">区间法 · 业绩与履约情况</span></div>
              <div class="rf"><span class="fchip hit">项目名称</span><span class="fchip hit">合同金额</span><span class="fchip hit">实施时间</span><span class="fchip hit">验收情况</span><span class="fchip">设备/货物配置明细</span><span class="fchip">业绩描述段落</span></div>
            </div>
            <div class="rulecard">
              <div class="rh2"><b>项目团队成员及相关人员简历</b><span class="n">区间法 · 项目团队</span></div>
              <div class="rf"><span class="fchip hit">姓名</span><span class="fchip hit">职务/层级</span><span class="fchip hit">职称</span><span class="fchip hit">执业资格及证书编号</span><span class="fchip">简历正文</span></div>
            </div>
            <p class="sub-note">抽取 = docx 表格结构化解析 + GLM-4-Flash 分段抽取合并；规则由项目负责人 2026-09-16 标注定稿。</p>
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
                  <tr><td>版本</td><td style="font-family:var(--mono);font-size:12px">v2.0.0</td></tr>
                  <tr><td>当前里程碑</td><td>M1-M5 全部交付</td></tr>
                  <tr><td>技术栈</td><td>Vue 3 + FastAPI + SQLite（WAL / FTS5 trigram）</td></tr>
                </tbody>
              </table>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>
