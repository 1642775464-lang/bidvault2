<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api } from "../api/client";
import type { Health } from "../api/types";
import { toast } from "../store";

const health = ref<Health | null>(null);
const section = ref("系统信息");

const sections = ["系统信息", "模型服务", "数据安全", "拆解规则", "关于"] as const;

onMounted(async () => {
  try {
    health.value = await api.health();
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
                <div class="pd">文本结构化抽取 · 非涉密文档可用 · 免费模型</div>
                <div class="pm">zhipu api · server-side</div>
              </div>
              <div class="pcard">
                <div class="ph"><b>GLM-4V-Flash</b><span class="chip-role r-viewer">M3 接入</span></div>
                <div class="pd">图片证据识别 · 营业执照 / 证书扫描件</div>
                <div class="pm">zhipu api · server-side</div>
              </div>
              <div class="pcard">
                <div class="ph"><b>PaddleOCR</b><span class="chip-role r-viewer">M2 接入</span></div>
                <div class="pd">扫描件本地解析 · 涉密文档不出域</div>
                <div class="pm">local · cpu</div>
              </div>
              <div class="pcard">
                <div class="ph"><b>向量检索</b><span class="chip-role r-viewer">M2 接入</span></div>
                <div class="pd">语义检索增强 · 当前 FTS5 + 3-gram</div>
                <div class="pm">local</div>
              </div>
            </div>
            <p class="sub-note">API Key 在服务端 .env 配置（zhipu_api_key），加密存储、不下发浏览器。</p>
          </template>

          <!-- 数据安全 -->
          <template v-else-if="section === '数据安全'">
            <div class="tblbox">
              <table>
                <tbody>
                  <tr><td>涉密闸门</td><td><span class="chip-role" style="background:var(--ok-bg);color:var(--ok)">默认全涉密</span></td></tr>
                  <tr><td>在线模型调用范围</td><td>仅非涉密文档</td></tr>
                  <tr><td>涉密文档在线调用次数</td><td style="color:var(--ok);font-weight:600">0</td></tr>
                  <tr><td>数据存储</td><td>全程本机磁盘（原件 / 抽取结果 / 页图缓存 / 备份）</td></tr>
                  <tr><td>审计记录</td><td>{{ health?.counts.audit_logs ?? "—" }} 条 · 全部在线调用留痕</td></tr>
                </tbody>
              </table>
            </div>
          </template>

          <!-- 拆解规则 -->
          <template v-else-if="section === '拆解规则'">
            <div class="sec-lab">招标文件 · 固定拆解结构</div>
            <div class="rulecard">
              <div class="rh2"><b>投标人须知前附表</b><span class="n">锚点定位</span></div>
              <div class="rf"><span class="fchip hit">递交截止时间</span><span class="fchip hit">投标有效期</span><span class="fchip hit">投标保证金</span><span class="fchip hit">正副本份数</span><span class="fchip">资质要求</span></div>
            </div>
            <div class="rulecard">
              <div class="rh2"><b>评标办法前附表</b><span class="n">锚点定位</span></div>
              <div class="rf"><span class="fchip hit">评标办法</span><span class="fchip hit">技术分</span><span class="fchip hit">商务分</span><span class="fchip hit">价格分</span></div>
            </div>
            <div class="sec-lab" style="margin-top:14px">投标文件 · 固定拆解结构</div>
            <div class="rulecard">
              <div class="rh2"><b>资格与身份文件</b><span class="n">7 项</span></div>
              <div class="rf"><span class="fchip">法定代表人身份证明</span><span class="fchip">授权委托书</span><span class="fchip">基本情况表</span></div>
            </div>
            <div class="rulecard">
              <div class="rh2"><b>业绩与团队</b><span class="n">4 项</span></div>
              <div class="rf"><span class="fchip">近年财务状况</span><span class="fchip">类似项目业绩</span><span class="fchip">在履约项目</span><span class="fchip">团队简历</span></div>
            </div>
            <p class="sub-note">固定结构为 M1 版本；自定义抽取字段（锚点 / 关键词 / 正则 / LLM）在后续里程碑开放。</p>
          </template>

          <!-- 关于 -->
          <template v-else>
            <div class="tblbox">
              <table>
                <tbody>
                  <tr><td>产品</td><td>标库 BidVault v2（单机版招投标文件智能库）</td></tr>
                  <tr><td>版本</td><td style="font-family:var(--mono);font-size:12px">v2.0.0-m1</td></tr>
                  <tr><td>当前里程碑</td><td>M1 数据底座</td></tr>
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
