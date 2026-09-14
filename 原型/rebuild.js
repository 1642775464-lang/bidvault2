// 重建干净的 4.1 框架原型（全量替换，修复结构损坏）
const fs = require('fs');
const dir = 'D:/项目文件/招投标工作台/bidvault2/原型';
const icons = fs.readFileSync(dir + '/4.1-icons.json', 'utf8');

const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>标库 BidVault v2</title>
<style>
  :root {
    --bg:#F4F7FB; --surface:#fff; --ink:#1E293B; --muted:#64748B; --line:#E2E8F0;
    --accent:#2563EB; --accent-soft:#EFF4FF; --ok:#16A34A; --warn:#D97706; --danger:#DC2626;
    --radius:8px; --radius-lg:12px; --shadow:0 1px 2px rgba(30,41,59,.04); --shadow-pop:0 4px 16px rgba(30,41,59,.12);
    --side-w:220px; --side-w-min:64px;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--ink); font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif; }
  .layout { display:flex; min-height:100vh; }
  .side { width:var(--side-w); background:var(--surface); border-right:1px solid var(--line); padding:16px 12px; display:flex; flex-direction:column; gap:4px; transition:width .2s ease; overflow:hidden; flex-shrink:0; cursor:pointer; }
  .side .logo { font-weight:700; font-size:16px; color:var(--accent); padding:4px 10px 16px; white-space:nowrap; pointer-events:none; }
  .side .logo span { font-size:10.5px; color:var(--muted); font-weight:400; }
  .side a { display:flex; align-items:center; gap:12px; padding:10px 12px; border-radius:var(--radius); color:var(--muted); text-decoration:none; cursor:pointer; white-space:nowrap; pointer-events:auto; }
  .side a .ico { width:20px; height:20px; flex-shrink:0; display:grid; place-items:center; }
  .side a .ico svg { width:19px; height:19px; }
  .side a .txt { overflow:hidden; }
  .side a.on { background:var(--accent-soft); color:var(--accent); font-weight:600; }
  .side a:hover:not(.on) { background:var(--bg); color:var(--accent); }
  .side .foot { margin-top:auto; font-size:10.5px; color:var(--muted); padding:8px 10px; white-space:nowrap; pointer-events:none; }
  .layout.min .side { width:var(--side-w-min); }
  .layout.min .side a .txt, .layout.min .side .logo span, .layout.min .side .foot { display:none; }
  .layout.min .side a { justify-content:center; padding:10px 6px; }
  .layout.min .side .logo { padding:4px 0 16px; text-align:center; font-size:14px; }
  .main { flex:1; min-width:0; }
  .topbar { background:var(--surface); border-bottom:1px solid var(--line); padding:12px 28px; display:flex; align-items:center; gap:16px; box-shadow:var(--shadow); }
  .topbar .crumb { font-size:13px; color:var(--muted); }
  .topbar .crumb b { color:var(--ink); }
  .topbar .safe { margin-left:auto; font-size:11px; color:var(--muted); }
  .wrap { max-width:1080px; margin:0 auto; padding:28px 32px 100px; }
  .page { display:none; }
  .page.on { display:block; }
  h2.sec { font-size:15px; font-weight:600; margin:26px 0 12px; display:flex; align-items:center; gap:8px; }
  h2.sec::before { content:""; width:4px; height:16px; background:var(--accent); border-radius:2px; }
  h2.sec:first-child { margin-top:0; }
  h2.sec a { margin-left:auto; font-size:12px; color:var(--accent); font-weight:500; }
  .hint { color:var(--muted); font-size:12.5px; padding:8px 0; }
  .hint.err { color:var(--danger); }
  .btn { display:inline-flex; align-items:center; gap:6px; padding:8px 18px; border-radius:var(--radius-lg); font-weight:600; cursor:pointer; border:1px solid var(--line); background:var(--surface); color:var(--ink); }
  .btn-accent { background:var(--accent); border-color:var(--accent); color:#fff; }
  .btn:disabled { opacity:.5; cursor:wait; }
  .badge { display:inline-block; font-size:11px; padding:2px 9px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-weight:500; }
  .badge.ok { background:#DCFCE7; color:var(--ok); }
  .badge.warn { background:#FEF3C7; color:var(--warn); }
  .badge.secret { background:#FEF2F2; color:var(--danger); font-weight:600; }
  .mono { font-family:Consolas,monospace; }
  .stat-band { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:16px 0; }
  .stat { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius-lg); padding:16px 18px; box-shadow:var(--shadow); }
  .stat .n { font-size:26px; font-weight:700; color:var(--accent); font-variant-numeric:tabular-nums; }
  .stat .n small { font-size:13px; color:var(--muted); font-weight:400; }
  .stat .l { font-size:12px; color:var(--muted); margin-top:2px; }
  .stat.hot .n { color:var(--warn); }
  .panel { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius-lg); padding:16px 24px; box-shadow:var(--shadow); margin-bottom:14px; }
  .chips { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }
  .chip { font-size:12px; padding:4px 13px; border:1px solid var(--line); border-radius:999px; color:var(--muted); background:var(--surface); cursor:pointer; }
  .chip.on { background:var(--accent); border-color:var(--accent); color:#fff; }
  .ws-bar { display:flex; align-items:center; gap:12px; margin-bottom:12px; }
  .proj-pick { position:relative; }
  .pp-label { font-size:11px; color:var(--muted); margin-bottom:3px; }
  .pp-box { width:280px; background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); padding:8px 12px; cursor:pointer; display:flex; align-items:center; gap:8px; }
  .pp-box:hover { border-color:var(--accent); }
  .pp-cur { flex:1; font-weight:600; font-size:13px; }
  .pp-caret { color:var(--muted); font-size:11px; }
  .pp-menu { display:none; position:absolute; top:calc(100% + 6px); left:0; width:320px; background:var(--surface); border:1px solid var(--line); border-radius:var(--radius-lg); box-shadow:var(--shadow-pop); z-index:20; padding:8px; }
  .pp-menu.open { display:block; }
  .pp-search { width:100%; border:1px solid var(--line); border-radius:var(--radius); padding:7px 10px; font:inherit; font-size:12.5px; margin-bottom:6px; outline:none; }
  .pp-search:focus { border-color:var(--accent); }
  .pp-group { padding:8px 10px; border-radius:var(--radius); cursor:pointer; font-size:12.5px; font-weight:600; display:flex; justify-content:space-between; align-items:baseline; }
  .pp-group:hover { background:var(--accent-soft); color:var(--accent); }
  .pp-group i { font-style:normal; font-size:10.5px; color:var(--muted); font-weight:400; font-family:Consolas,monospace; }
  .pp-new { padding:8px 10px; color:var(--accent); font-size:12px; cursor:pointer; border-top:1px dashed var(--line); margin-top:4px; font-weight:600; }
  .imp-area { flex:1; border:1.5px dashed var(--accent); background:var(--accent-soft); border-radius:var(--radius-lg); padding:10px 18px; display:flex; align-items:center; gap:12px; cursor:pointer; }
  .imp-area:hover { border-color:var(--accent); }
  .imp-area.done { border-style:solid; background:var(--surface); }
  .imp-ico { width:30px; height:30px; border-radius:8px; background:var(--accent); color:#fff; display:grid; place-items:center; font-size:14px; flex-shrink:0; }
  .imp-txt { font-weight:600; font-size:13px; }
  .imp-sub { font-size:11px; color:var(--muted); }
  .ws-export { margin-left:auto; padding:7px 20px; }
  .ws-split { display:flex; gap:12px; height:calc(100vh - 260px); min-height:420px; }
  .ws-doc { flex:1.35; background:var(--surface); border:1px solid var(--line); border-radius:var(--radius-lg); display:flex; flex-direction:column; overflow:hidden; }
  .doc-page { flex:1; overflow-y:auto; padding:26px 34px; }
  .doc-page h1 { font-size:20px; margin-bottom:20px; text-align:center; }
  .doc-page { line-height:2; color:#334155; }
  .doc-page h3 { font-size:14.5px; margin:16px 0 8px; color:#0F172A; }
  .doc-page p { margin-bottom:10px; color:#334155; }
  .mk-bad { background:#FEE2E2; border-bottom:2px solid var(--danger); padding:0 2px; border-radius:3px 3px 0 0; cursor:pointer; }
  .mk-warn { background:#FEF3C7; border-bottom:2px solid var(--warn); padding:0 2px; border-radius:3px 3px 0 0; cursor:pointer; }
  .ws-right { flex:1; background:var(--surface); border:1px solid var(--line); border-radius:var(--radius-lg); display:flex; flex-direction:column; overflow:hidden; }
  .ws-tabs { display:flex; border-bottom:1px solid var(--line); }
  .ws-tabs span { flex:1; text-align:center; padding:11px 4px; cursor:pointer; color:var(--muted); font-size:13px; border-bottom:2.5px solid transparent; }
  .ws-tabs span.on { color:var(--accent); border-color:var(--accent); font-weight:600; }
  .ws-list { flex:1; overflow-y:auto; padding:12px; }
  .it { border:1px solid var(--line); border-radius:var(--radius); padding:10px 12px; margin-bottom:9px; font-size:12.5px; }
  .it .h { display:flex; align-items:center; gap:8px; margin-bottom:4px; }
  .tg { font-size:10.5px; padding:1px 8px; border-radius:4px; font-weight:600; }
  .tg.bad { background:#FEE2E2; color:var(--danger); }
  .tg.warn { background:#FEF3C7; color:var(--warn); }
  .tg.blue { background:var(--accent-soft); color:var(--accent); }
  .tg.ok { background:#DCFCE7; color:var(--ok); }
  .it .a { margin-left:auto; color:var(--accent); cursor:pointer; font-size:11.5px; font-weight:500; }
  .it .m { font-size:11px; color:var(--muted); margin-top:4px; }
  .pj-toolbar { display:flex; gap:14px; align-items:center; margin-bottom:16px; }
  .pj-search { flex:1; max-width:360px; border:1px solid var(--line); border-radius:var(--radius-lg); padding:10px 16px; font-size:13.5px; }
  .pj-search:focus { outline:2px solid var(--accent-soft); border-color:var(--accent); }
  .pj-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }
  .pj-card { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius-lg); padding:18px 20px; cursor:pointer; box-shadow:var(--shadow); transition:border-color .15s; }
  .pj-card:hover { border-color:var(--accent); }
  .pj-card .nm { font-weight:700; font-size:14.5px; margin-bottom:6px; }
  .pj-card .in { display:flex; gap:6px; margin-bottom:10px; }
  .pj-card .nums { display:flex; gap:16px; font-size:11.5px; color:var(--muted); font-family:Consolas,monospace; }
  .pj-card .nums b { color:var(--ink); }
  .pj-back { display:flex; align-items:baseline; gap:14px; margin-bottom:14px; }
  .pj-back a { color:var(--accent); cursor:pointer; font-size:12.5px; }
  .pj-back b { font-size:18px; }
  .pj-meta { font-size:11.5px; color:var(--muted); font-family:Consolas,monospace; }
  .pj-sum { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:6px; }
  .doc-row { display:flex; align-items:center; gap:12px; padding:11px 4px; border-bottom:1px solid var(--line); flex-wrap:wrap; }
  .doc-t { font-weight:600; font-size:13.5px; }
  .doc-t .badge { margin-left:6px; }
  .doc-m { font-size:11.5px; color:var(--muted); margin-top:2px; display:flex; gap:12px; flex-wrap:wrap; }
  .doc-side { margin-left:auto; text-align:right; font-size:12px; color:var(--muted); white-space:nowrap; }
  .doc-side a { color:var(--accent); margin-left:8px; cursor:pointer; }
  .mat-card { background:var(--surface); border:1px solid var(--line); border-left:4px solid var(--accent); border-radius:var(--radius); padding:12px 18px; margin-bottom:10px; box-shadow:var(--shadow); }
  .mat-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .mat-title { font-size:14px; font-weight:600; }
  .mat-meta { font-size:11px; color:var(--muted); margin-top:4px; font-family:Consolas,monospace; }
</style>
</head>
<body>
<div class="layout">
  <aside class="side" id="side" onclick="sideClick(event)">
    <div class="logo">标库<span> BidVault</span></div>
    <a class="on" onclick="go('p1', this)"><span class="ico" data-i="0"></span><span class="txt">工作台</span></a>
    <a onclick="go('p2', this)"><span class="ico" data-i="1"></span><span class="txt">项目库</span></a>
    <a onclick="go('p3', this)"><span class="ico" data-i="2"></span><span class="txt">文档库</span></a>
    <a onclick="go('p4', this)"><span class="ico" data-i="3"></span><span class="txt">素材库</span></a>
    <a onclick="go('p5', this)"><span class="ico" data-i="4"></span><span class="txt">系统设置</span></a>
  </aside>

  <main class="main">
    <div class="topbar"><span class="crumb">标库 / <b id="crumb">工作台</b></span></div>
    <div class="wrap">

      <div class="page on" id="p1">
        <div class="ws-bar">
          <div class="proj-pick">
            <div class="pp-label">项目</div>
            <div class="pp-box" onclick="ppToggle(event)">
              <span class="pp-cur" id="ppCur">选择项目</span><span class="pp-caret">▾</span>
              <div class="pp-menu" id="ppMenu">
                <input class="pp-search" placeholder="搜索项目…" onclick="event.stopPropagation()" oninput="ppFilterMenu(this.value)" />
                <div id="ppItems"></div>
                <div class="pp-new" onclick="event.stopPropagation()">＋ 新建项目</div>
              </div>
            </div>
          </div>
          <button class="btn" onclick="impGo()">导入初版投标文件</button>
          <button class="btn">导出</button>
        </div>
        <div class="ws-split">
          <div class="ws-doc">
            <div class="doc-page">
              <h1>智慧园区数据中心项目投标文件</h1>
              <h3>第一章 投标函</h3>
              <p>致：南京云问网络技术有限公司（招标人）</p>
              <p>根据贵方招标文件（编号 HTC-HW-2026-0891）正式授权，我方参加本次投标，并完全接受并遵守招标文件中的各项规定和要求。</p>
              <h3>第二章 法定代表人身份证明</h3>
              <p>兹证明张建国为本公司法定代表人，单位性质为有限责任公司，注册资本伍仟万元整。</p>
              <h3>第三章 授权委托书</h3>
              <p>本人张建国系法定代表人，现委托代理人王志强参加本项目投标活动，并代表我方处理投标过程中的全部事宜。</p>
              <h3>第四章 技术方案</h3>
              <p>本项目采用模块化机房建设方案，数据中心按国家 B 级机房标准建设，配置精密空调 2 台、UPS 不间断电源系统 1 套，动环监控全覆盖。</p>
              <p>施工工期计划 180 日历天，质量标准符合国家现行规范，验收合格率 100%。</p>
              <p>项目团队配置项目经理 1 名（一级建造师·机电），高级工程师 2 名，专业施工人员 18 名。</p>
              <h3>第五章 类似项目业绩</h3>
              <p>公司近年完成多个同类项目，其中××银行机房监控系统项目（合同金额 486 万元）已于 2023 年 8 月通过验收，运行稳定。</p>
              <h3>第六章 售后服务承诺</h3>
              <p>质保期三年，提供 7×24 小时响应服务，故障 4 小时内到场处理，重大故障 2 小时内给出应急方案。</p>
            </div>
          </div>
          <div class="ws-right">
            <div class="ws-tabs">
              <span class="on" onclick="wsTab('bad', this)">废标检测</span>
              <span onclick="wsTab('blue', this)">案例推荐</span>
              <span onclick="wsTab('ok', this)">人员介绍</span>
            </div>
            <div class="ws-list" id="wsList"></div>
          </div>
        </div>
      </div>

      <div class="page" id="p2">
        <div id="pjListView">
          <div class="pj-toolbar">
            <input class="pj-search" placeholder="搜索项目名称…" oninput="pjFilter(this.value)" />
            <div class="chips">
              <span class="chip on" onclick="pjChip('', this)">全部</span>
              <span class="chip" onclick="pjChip('金融', this)">金融</span>
              <span class="chip" onclick="pjChip('知产', this)">知产</span>
              <span class="chip" onclick="pjChip('市政', this)">市政</span>
              <span class="chip" onclick="pjChip('通信', this)">通信</span>
            </div>
          </div>
          <div class="pj-grid" id="pjGrid"></div>
        </div>
        <div id="pjDetail" style="display:none;">
          <div class="pj-back"><a onclick="pjBack()">‹ 返回项目列表</a><b id="pjTitle"></b><span class="pj-meta" id="pjMeta"></span></div>
          <div class="pj-sum">
            <div class="stat"><div class="n" id="pjNDocs">0<small> 份</small></div><div class="l">项目文档</div></div>
            <div class="stat"><div class="n">0<small> 条</small></div><div class="l">可复用素材</div></div>
            <div class="stat"><div class="n">0<small> 人</small></div><div class="l">参与人员</div></div>
            <div class="stat hot"><div class="n" id="pjNRisk">0<small> 项</small></div><div class="l">废标风险</div></div>
          </div>
          <h2 class="sec">项目文档</h2>
          <div class="doc-row"><div style="min-width:0;"><div class="doc-t">投标文件 v0.3<span class="badge secret">涉密</span></div><div class="doc-m mono">编辑中 · 最近修改 2 分钟前</div></div><div class="doc-side"><span class="badge warn">废标 1 项</span></div></div>
          <div class="doc-row"><div style="min-width:0;"><div class="doc-t">招标文件.pdf</div><div class="doc-m mono">1.2 MB · 前附表已提取</div></div><div class="doc-side"><span class="badge ok">已索引</span></div></div>
          <div class="doc-row"><div style="min-width:0;"><div class="doc-t">中标通知书.pdf</div><div class="doc-m mono">860 KB</div></div><div class="doc-side"><span class="badge ok">已索引</span></div></div>
          <h2 class="sec">可复用素材</h2>
          <div class="mat-card"><div class="mat-head"><span class="badge">类似项目</span><b class="mat-title">××银行机房监控系统项目</b><span class="badge warn">金融</span></div><div class="mat-meta">486 万 · 2023 竣工 · 证据：合同 P.3 · 验收报告 P.14</div></div>
          <div class="mat-card"><div class="mat-head"><span class="badge">人员</span><b class="mat-title">张建国</b><span class="badge ok">一级建造师（机电）</span></div><div class="mat-meta">法定代表人 · 参与项目 12 个</div></div>
        </div>
      </div>

      <div class="page" id="p3">
        <h2 class="sec">文档列表</h2>
        <div class="doc-row"><div style="min-width:0;"><div class="doc-t">投标文件定稿8月.docx</div></div><div class="doc-side"><span class="badge ok">已索引</span></div></div>
        <div class="doc-row"><div style="min-width:0;"><div class="doc-t">招标文件.pdf</div></div><div class="doc-side"><span class="badge ok">已索引</span></div></div>
      </div>

      <div class="page" id="p4">
        <h2 class="sec">素材库</h2>
        <div class="mat-card"><div class="mat-head"><span class="badge">类似项目</span><b class="mat-title">××银行机房监控系统项目</b><span class="badge warn">金融</span></div><div class="mat-meta">486 万 · 2023 · 证据：合同 P.3</div></div>
      </div>

      <div class="page" id="p5">
        <h2 class="sec">通用</h2>
        <div class="panel"><div class="hint">开机自动启动 · 数据目录 · 磁盘水位告警</div></div>
        <h2 class="sec">AI 服务</h2>
        <div class="panel"><div class="hint">GLM API Key · 调用审计</div></div>
        <h2 class="sec">自动归档</h2>
        <div class="panel"><div class="hint">扫描周期 · 收藏路径管理</div></div>
        <h2 class="sec">备份</h2>
        <div class="panel"><div class="hint">每日自动备份 · 7 天滚动 · 手动还原</div></div>
        <h2 class="sec">规则设置</h2>
        <div class="panel"><div class="hint">通用废标清单 · 历次检查标准（版本留痕）</div></div>
      </div>

    </div>
  </main>
</div>

<script>
const ICONS = __ICONS_DATA__;

function renderIcons() {
  document.querySelectorAll('.side a .ico').forEach(el => {
    const it = ICONS.feather[+el.dataset.i];
    el.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + it.inner + '</svg>';
  });
}
function sideClick(e) {
  if (e.target.closest('a')) return;
  document.getElementById('layout').classList.toggle('min');
}
function go(id, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('on'));
  document.getElementById(id).classList.add('on');
  document.querySelectorAll('.side a').forEach(a => a.classList.remove('on'));
  el.classList.add('on');
  document.getElementById('crumb').textContent = el.querySelector('.txt').textContent;
  window.scrollTo(0, 0);
}

const PROJECTS = [
  '智慧园区数据中心项目', '××银行机房监控系统项目',
  '××知识产权服务平台运维', '××市政道路改造工程',
];
function ppToggle(e) {
  e.stopPropagation();
  document.getElementById('ppMenu').classList.toggle('open');
}
function ppFilterMenu(q) {
  q = q.trim();
  document.getElementById('ppItems').innerHTML = PROJECTS
    .filter(p => p.includes(q))
    .map(p => '<div class="pp-group" onclick="ppPick(\\'' + p + '\\', event)">' + p + '</div>')
    .join('');
}
function ppPick(name, e) {
  e.stopPropagation();
  document.getElementById('ppCur').textContent = name;
  document.getElementById('ppMenu').classList.remove('open');
  impDone(name);
}
function impGo() { impDone('智慧园区数据中心项目'); }
function impDone(name) {
  const a = document.getElementById('impArea');
  a.classList.add('done');
  document.getElementById('impTxt').textContent = '已选项目：' + name + ' —— 导入初版投标文件';
  document.getElementById('impSub').textContent = '编辑区已加载';
}
document.addEventListener('click', () => document.getElementById('ppMenu')?.classList.remove('open'));

const WS_DATA = {
  bad: [
    ['bad', '未响应', '修复建议 ›', '精密空调未注明一级能效', '对应正文高亮 · 招标 3.2 条'],
    ['bad', '偏差', '详情 ›', '工期 180 天 vs 要求 150 天', '对应正文高亮 · 招标 2.1 条'],
    ['bad', '缺项', '插入模板 ›', '开标一览表未编写', '格式要求第 4 项'],
    ['bad', '缺证据', '去补挂 ›', '王志强社保证明未挂接', '资格审查必备'],
  ],
  blue: [
    ['blue', '机房类', '插入 ›', '××银行机房监控项目', '486 万 · 2023 · 含合同+验收证据'],
    ['blue', '园区类', '插入 ›', '××智慧园区一期信息化', '890 万 · 2024 · 与本项目最相似'],
    ['blue', '信誉', '插入 ›', '市级优秀企业奖', '近三年有效'],
  ],
  ok: [
    ['ok', '材料齐', '插入介绍 ›', '张建国 · 法定代表人', '一级建造师（机电）· 12 个项目'],
    ['bad', '缺社保证明', '去补挂 ›', '王志强 · 授权代表', '高级工程师 · 金融行业 ×3'],
  ],
};
function wsTab(tg, el) {
  document.querySelectorAll('.ws-tabs span').forEach(x => x.classList.remove('on'));
  el.classList.add('on');
  document.getElementById('wsList').innerHTML = WS_DATA[tg].map(([c, tag, act, title, meta]) =>
    '<div class="it"><div class="h"><span class="tg ' + c + '">' + tag + '</span><span class="a">' + act + '</span></div>' + title + '<div class="m">' + meta + '</div></div>'
  ).join('');
}

const PJ_DATA = [
  ['智慧园区数据中心项目', '金融', 12, 5, 2, 1],
  ['××银行机房监控系统项目', '金融', 5, 3, 1, 0],
  ['××知识产权服务平台运维', '知产', 3, 2, 1, 0],
  ['××市政道路改造工程', '市政', 8, 4, 3, 2],
  ['××通信枢纽楼智能化', '通信', 6, 2, 2, 0],
  ['××数据中心二期机电', '通信', 9, 6, 4, 1],
];
function pjRender(list) {
  document.getElementById('pjGrid').innerHTML = list.map(([nm, ind, docs, mats, ppl, risk]) =>
    '<div class="pj-card" onclick="pjOpen(\\'' + nm + '\\',' + docs + ',' + risk + ')">' +
    '<div class="nm">' + nm + '</div>' +
    '<div class="in"><span class="badge warn">' + ind + '</span>' + (risk ? '<span class="badge secret">废标风险 ' + risk + '</span>' : '') + '</div>' +
    '<div class="nums"><span>文档 <b>' + docs + '</b></span><span>素材 <b>' + mats + '</b></span><span>人员 <b>' + ppl + '</b></span></div></div>'
  ).join('');
}
function pjFilter(q) {
  pjRender(PJ_DATA.filter(p => p[0].toLowerCase().includes(q.toLowerCase())));
}
function pjChip(ind, el) {
  document.querySelectorAll('#p2 .chip').forEach(c => c.classList.remove('on'));
  el.classList.add('on');
  pjRender(ind ? PJ_DATA.filter(p => p[1] === ind) : PJ_DATA);
}
function pjOpen(nm, docs, risk) {
  document.getElementById('pjListView').style.display = 'none';
  const d = document.getElementById('pjDetail');
  d.style.display = 'block';
  document.getElementById('pjTitle').textContent = nm;
  document.getElementById('pjMeta').textContent = '共 ' + docs + ' 份文档 · 最近活动 2 分钟前';
  document.getElementById('pjNRisk').innerHTML = risk + '<small> 项</small>';
}
function pjBack() {
  document.getElementById('pjDetail').style.display = 'none';
  document.getElementById('pjListView').style.display = 'block';
}

renderIcons();
wsTab('bad', document.querySelector('.ws-tabs span.on'));
pjRender(PJ_DATA);
</script>
</body>
</html>
`;
fs.writeFileSync(dir + '/4.1-整体框架.html', html.replace('__ICONS_DATA__', icons));
console.log('rebuilt clean, size:', html.length + icons.length);
