<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api } from "../api/client";
import type { Health } from "../api/types";
import { toast } from "../store";

const health = ref<Health | null>(null);
const section = ref("系统信息");

const sections = ["系统信息", "模型服务", "数据安全", "拆解规则", "检测规则", "关于"] as const;
const detectRules = ref<{ id: string; name: string; sev: string; type: string; desc: string }[]>([]);

onMounted(async () => {
  try {
    health.value = await api.health();
    detectRules.value = (await api.detectRules()).rules;
  } catch (e) {
    toast(e instanceof Error ? e.message : "加载失败");
  }
});
</script>

<template>
  <div class="viewscroll">
    <div class="setwrap" style="flex:1;min-height:0">
      <aside class="stree">
        <div class="tt">设置中心</div>
        <button v-for="s in sections" :key="s" class="sitem" :class="{ on: section === s }" @click="section = s">
          {{ s }}
          <span v-if="s === '模型服务'" class="dot" :class="{ warn: health && !health.glm_configured }" />
          <span v-else-if="s === '数据安全'" class="dot" />
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
                  <tr><td>审计记录</td><td>{{ health?.counts.audit_logs ?? "—" }} 条 · 全部在线调用留痕</td></tr>
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
            <div class="sec-lab" style="margin-top:14px">投标文件 · 七项</div>
            <div class="rulecard">
              <div class="rh2"><b>资格与身份文件</b><span class="n">2 项</span></div>
              <div class="rf"><span class="fchip">法定代表人身份证明</span><span class="fchip">授权委托书</span></div>
            </div>
            <div class="rulecard">
              <div class="rh2"><b>单位与财务状况</b><span class="n">2 项</span></div>
              <div class="rf"><span class="fchip">基本情况表</span><span class="fchip">近年财务状况</span></div>
            </div>
            <div class="rulecard">
              <div class="rh2"><b>业绩与履约情况</b><span class="n">2 项</span></div>
              <div class="rf"><span class="fchip">类似项目情况表</span><span class="fchip">在履约项目情况表</span></div>
            </div>
            <div class="rulecard">
              <div class="rh2"><b>项目团队</b><span class="n">1 项</span></div>
              <div class="rf"><span class="fchip">团队成员及简历</span></div>
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
              <div class="rf"><span class="fchip">{{ r.desc }}</span></div>
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
