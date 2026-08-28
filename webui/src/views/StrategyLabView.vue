<script setup lang="ts">
// 策略实验室：独立策略（打板/二板/尾盘低吸/趋势买点 + 尾盘选股）切换与独立回测。
import { onMounted, ref } from "vue";
import api from "@/api";
import { pushToast, tryReq } from "@/toast";

interface StrategyItem {
  id: string;
  name: string;
  summary: string;
  enabled: boolean;
  key_params: Record<string, any>;
  last_backtest: any | null;
  versions: { value: string; label: string; params: Record<string, any> }[];}

const mode = ref("paper");
const strategies = ref<StrategyItem[]>([]);
const running = ref<Record<string, boolean>>({});
const scanning = ref<Record<string, boolean>>({});
const reports = ref<Record<string, any>>({});
const scanGrid = ref<Record<string, string>>({});
const jobs = ref<Record<string, any>>({});
const jobsTimes = ref<Record<string, string>>({});
// 日期辅助函数必须声明在 btForm 初始化之前：const 箭头函数存在暂时性死区，
// 若在下方先执行 defaultForm() 会触发 "Cannot access before initialization"，导致组件挂载失败页面空白。
const today = () => new Date().toISOString().slice(0, 10);
const yearAgo = () => {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 1);
  return d.toISOString().slice(0, 10);
};

function defaultForm() {
  return { start: yearAgo(), end: today(), cash: 1_000_000, version_id: "" };
}

// 预填充全部策略的回测表单（v-model 直接索引，避免可选链赋值被 esbuild 拒绝）
const _ALL_IDS = ["tail_pick", "limit_up", "second_board", "dip_buy", "trend_buy", "etf_t0"];
const btForm = ref<Record<string, { start: string; end: string; cash: number; version_id: string }>>(
  Object.fromEntries(_ALL_IDS.map((id) => [id, defaultForm()])),
);
const timers: Record<string, any> = {};

async function load() {
  const st = await tryReq(() => api.strategylabStatus(mode.value));
  if (st?.strategies) {
    strategies.value = st.strategies;
    for (const s of st.strategies) {
      if (!btForm.value[s.id]) btForm.value[s.id] = defaultForm();
    }
  }
  if (st?.jobs) jobs.value = st.jobs;
  if (st?.jobs_times) jobsTimes.value = st.jobs_times;
}

async function toggle(s: StrategyItem) {
  const r = await tryReq(() => api.strategylabSetEnabled(s.id, !s.enabled));
  if (r?.ok) s.enabled = r.enabled;
  await load();
}

function fmtPct(v: any): string {
  if (v === null || v === undefined) return "n/a";
  const n = Number(v);
  return isFinite(n) ? (n * 100).toFixed(2) + "%" : "n/a";
}

function fmtNum(v: any): string {
  if (v === null || v === undefined) return "n/a";
  const n = Number(v);
  return isFinite(n) ? n.toLocaleString() : "n/a";
}

async function runBt(s: StrategyItem) {
  const f = btForm.value[s.id] || defaultForm();
  if (!f.start) { pushToast("请填写开始日期", "err"); return; }
  running.value[s.id] = true;
  try {
    const r = await tryReq(() => api.strategylabBacktest(
      { strategy: s.id, start: f.start, end: f.end || null, cash: Number(f.cash) || 1_000_000,
        version_id: f.version_id || null },
      mode.value,
    ));
    if (!r?.job_id) { pushToast("回测提交失败", "err"); return; }
    pushToast(`${s.name} 回测已提交（${r.job_id}），后台运行中`, "ok");
    if (timers[s.id]) clearInterval(timers[s.id]);
    timers[s.id] = setInterval(async () => {
      const j = await tryReq(() => api.job(r.job_id));
      if (j?.status === "done" || j?.status === "error" || j?.status === "failed") {
        clearInterval(timers[s.id]);
        running.value[s.id] = false;
        if (j.status === "done") pushToast(`${s.name} 回测完成`, "ok");
        else pushToast(`${s.name} 回测失败：${j?.error || j?.result?.error || j?.status}`, "err");
        await load();
      }
    }, 4000);
  } catch (e) {
    running.value[s.id] = false;
    pushToast(String(e), "err");
  }
}

async function runScan(s: StrategyItem) {
  const f = btForm.value[s.id] || defaultForm();
  let grid: Record<string, any[]>;
  try {
    grid = JSON.parse(scanGrid.value[s.id] || "");
    if (!grid || Array.isArray(grid) || !Object.keys(grid).length) throw new Error();
  } catch {
    pushToast('参数网格须为 JSON 对象，例如 {"stop_pct":[0.03,0.05]}', 'err'); return;
  }
  scanning.value[s.id] = true;
  const r = await tryReq(() => api.strategylabScan({ strategy: s.id, start: f.start, end: f.end || null,
    cash: Number(f.cash) || 1_000_000, version_id: f.version_id || null, grid }, mode.value));
  if (!r?.job_id) { scanning.value[s.id] = false; return; }
  pushToast(`${s.name} 参数扫描已提交（${r.combinations} 组）`, 'ok');
  const timer = setInterval(async () => {
    const j = await tryReq(() => api.job(r.job_id));
    if (j?.status === 'done' || j?.status === 'error' || j?.status === 'failed') {
      clearInterval(timer); scanning.value[s.id] = false;
      pushToast(j.status === 'done' ? `${s.name} 参数扫描完成` : `参数扫描失败：${j?.error || j.status}`, j.status === 'done' ? 'ok' : 'err');
      await load();
    }
  }, 4000);
}

async function loadReport(s: StrategyItem) {
  const report = await tryReq(() => api.strategylabReport(s.id, mode.value));
  if (report) { reports.value[s.id] = report; pushToast(`${s.name} 实验报告已加载`, 'ok'); }
}

async function makeCandidate(s: StrategyItem) {
  const result = await tryReq(() => api.strategylabPaperCandidate(s.id, mode.value));
  if (result?.ok) pushToast(`${s.name} 已生成模拟盘候选（不会自动启用或下单）`, 'ok');
}

onMounted(load);
</script>

<template>
  <div class="page">
    <div class="page-head">
      <h2>🧪 策略实验室</h2>
      <div class="sub">
        独立策略（打板 / 二板龙头 / 尾盘低吸 / 趋势买点 / ETF T+0 日内回转 + 尾盘选股法）——
        互不影响、可单独回测验证。启用开关只影响策略是否参与运行；回测结果可对比迭代。
        ETF T+0 为盘中高频任务（09:30~15:00 巡检），勾选启用后下一次触发即生效。
      </div>
      <div class="jobbar">
        <span class="jobitem" v-for="(j, name) in jobs" :key="name">
          ⏱ {{ name === "strategylab_open" ? "开盘买入" : "尾盘管理" }}
          ({{ jobsTimes[name] || "未配置" }})
          · 最近: {{ j?.last_status || "未运行" }}{{ j?.last_run ? " @ " + j.last_run : "" }}
        </span>
      </div>
    </div>

    <div class="grid">
      <div v-for="s in strategies" :key="s.id" class="card">
        <div class="card-head">
          <div>
            <strong>{{ s.name }}</strong>
            <span class="badge" :class="s.enabled ? 'ok' : 'off'">{{ s.enabled ? "已启用" : "已停用" }}</span>
          </div>
          <label class="switch">
            <input type="checkbox" :checked="s.enabled" @change="toggle(s)" />
            <span class="slider"></span>
          </label>
        </div>
        <p class="desc">{{ s.summary }}</p>

        <div v-if="Object.keys(s.key_params || {}).length" class="params">
          <span v-for="(v, k) in s.key_params" :key="k" class="pchip">
            {{ k }}={{ v }}
          </span>
        </div>

        <div class="bt-form">
          <input v-model="btForm[s.id].start" type="date" class="ipt" title="开始" />
          <input v-model="btForm[s.id].end" type="date" class="ipt" title="结束" />
          <input v-model.number="btForm[s.id].cash" type="number" class="ipt cash" title="初始资金" />
          <select v-model="btForm[s.id].version_id" class="ipt version" title="策略配置版本">
            <option value="">默认配置</option>
            <option v-for="v in s.versions" :key="v.value" :value="v.value">{{ v.label }}</option>
          </select>
          <button class="btn" :disabled="running[s.id]" @click="runBt(s)">
            {{ running[s.id] ? "回测中…" : "▶ 独立回测" }}
          </button>
          <input v-model="scanGrid[s.id]" class="ipt grid-json" placeholder='扫描网格 JSON，如 {"stop_pct":[0.03,0.05]}' title="参数扫描网格" />
          <button class="btn secondary" :disabled="scanning[s.id]" @click="runScan(s)">
            {{ scanning[s.id] ? "扫描中…" : "⌕ 参数扫描" }}
          </button>
          <button class="btn secondary" @click="loadReport(s)">▤ 实验报告</button>
          <button class="btn candidate" :disabled="!s.last_backtest" @click="makeCandidate(s)">＋ 模拟盘候选</button>
        </div>

        <div v-if="reports[s.id]" class="report">
          <strong>实验报告 · {{ reports[s.id].generated_at }}</strong>
          <span v-if="reports[s.id].backtest">回测：收益 {{ fmtPct(reports[s.id].backtest.metrics?.total_return) }}，最大回撤 {{ fmtPct(reports[s.id].backtest.metrics?.max_drawdown) }}</span>
          <span v-if="reports[s.id].scan">参数扫描：{{ reports[s.id].scan.rows?.length || 0 }} 组，最佳收益 {{ fmtPct(reports[s.id].scan.rows?.[0]?.metrics?.total_return) }}</span>
        </div>

        <div v-if="s.last_backtest" class="result">
          <div class="result-title">最近回测（{{ s.last_backtest.run_at || "?" }}）</div>
          <table class="tbl">
            <tbody>
              <tr>
                <td>总收益</td><td :class="(s.last_backtest.metrics?.total_return ?? 0) >= 0 ? 'up' : 'down'">
                  {{ fmtPct(s.last_backtest.metrics?.total_return) }}</td>
                <td>胜率</td><td>{{ fmtPct(s.last_backtest.metrics?.win_rate) }}</td>
              </tr>
              <tr>
                <td>最大回撤</td><td class="down">{{ fmtPct(s.last_backtest.metrics?.max_drawdown) }}</td>
                <td>笔数</td><td>{{ fmtNum(s.last_backtest.n_closed) }}</td>
              </tr>
              <tr>
                <td>信号毛盈亏</td><td>{{ fmtPct((s.last_backtest.cost?.gross_pnl ?? 0) / 1e6) }}</td>
                <td>成本拖累</td><td>{{ fmtPct((s.last_backtest.cost?.cost_drag ?? 0) / 1e6) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="result muted">尚未回测 —— 选好区间点「独立回测」</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { padding: 18px 22px; }
.page-head { margin-bottom: 14px; }
.sub { color: var(--text-2); font-size: 13px; margin-top: 4px; max-width: 900px; line-height: 1.6; }
.jobbar { display: flex; gap: 16px; margin-top: 8px; flex-wrap: wrap; }
.jobitem { background: var(--bg-2); border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px; font-size: 12px; color: var(--primary); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(430px, 1fr)); gap: 14px; }
.card { background: var(--bg-elev); border: 1px solid var(--border); border-radius: 10px; padding: 14px; box-shadow: var(--shadow-sm); }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; margin-left: 8px; }
.badge.ok { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }
.badge.off { background: color-mix(in srgb, var(--danger) 15%, transparent); color: var(--danger); }
.desc { color: var(--text-2); font-size: 13px; line-height: 1.6; margin: 8px 0; }
.params { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.pchip { background: var(--bg-2); border: 1px solid var(--border); border-radius: 6px; padding: 2px 8px; font-size: 12px; color: var(--primary); }
.bt-form { display: flex; gap: 6px; margin: 10px 0; flex-wrap: wrap; }
.ipt { background: var(--bg-elev); border: 1px solid var(--border-strong); color: var(--text); border-radius: 6px; padding: 6px 8px; font-size: 12px; }
.ipt.cash { width: 90px; }
.ipt.version { max-width: 180px; }
.ipt.grid-json { min-width: 205px; flex: 1; }
.btn.secondary { background: #465367; }
.btn.candidate { background: #25734c; }
.report { margin: 8px 0; padding: 8px; border: 1px solid #31516d; border-radius: 6px; color: #b9d9ef; font-size: 12px; display: flex; gap: 10px; flex-wrap: wrap; }
.btn { background: #2f6fed; border: none; color: #fff; border-radius: 6px; padding: 6px 12px; cursor: pointer; font-size: 13px; }
.btn:disabled { opacity: .5; cursor: wait; }
.result { border-top: 1px dashed #2c313a; padding-top: 8px; }
.result-title { font-size: 12px; color: #8a8f98; margin-bottom: 4px; }
.tbl { width: 100%; font-size: 12px; border-collapse: collapse; }
.tbl td { padding: 3px 4px; color: #aab0ba; }
.tbl td:nth-child(2), .tbl td:nth-child(4) { color: #e6e9ef; text-align: right; }
.up { color: #57d98a !important; }
.down { color: #e07a7a !important; }
.muted { color: #6b7280; font-size: 12px; }
.switch { position: relative; display: inline-block; width: 40px; height: 22px; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider { position: absolute; inset: 0; background: #3a3f4a; border-radius: 22px; transition: .2s; cursor: pointer; }
.slider::before { content: ""; position: absolute; width: 16px; height: 16px; left: 3px; top: 3px; background: #fff; border-radius: 50%; transition: .2s; }
.switch input:checked + .slider { background: #2f6fed; }
.switch input:checked + .slider::before { transform: translateX(18px); }
</style>
