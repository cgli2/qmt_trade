<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import api from "@/api";
import { useApp } from "@/store";
import { pushToast, tryReq } from "@/toast";
import Modal from "@/components/Modal.vue";

const app = useApp();
const loading = ref(false);
const form = ref({ start: defaultStart(), end: "", cash: 1000000, top_n: 10, warmup: 250, llm: false });
const jobs = ref<any[]>([]);
const current = ref<any>(null);
const detail = ref<any>(null);
let timer: any = null;

function defaultStart() {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 1);
  return d.toISOString().slice(0, 10);
}

const METRIC_LABEL: Record<string, string> = {
  total_return: "总收益率", annual_return: "年化收益", sharpe: "夏普比率",
  max_drawdown: "最大回撤", win_rate: "胜率", profit_factor: "盈亏比",
  calmar: "卡玛比率", volatility: "年化波动", trade_count: "交易次数",
  avg_holding_days: "平均持有天数", turnover: "换手率",
};

const metrics = computed(() => {
  const m = current.value?.result?.metrics || {};
  return Object.entries(m).map(([k, v]) => ({
    key: k, label: METRIC_LABEL[k] || k,
    value: typeof v === "number" ? (Math.abs(v as number) < 1 && k !== "trade_count" ? ((v as number) * 100).toFixed(2) + "%" : (v as number).toFixed(3)) : v,
  }));
});

const curve = computed(() => {
  const ec = current.value?.result?.equity_curve || [];
  if (ec.length < 2) return { points: "", first: 0, last: 0 };
  const vals = ec.map((p: any) => Number(p.equity ?? p.value ?? p[1] ?? p));
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  const w = 900, h = 220;
  return {
    points: vals.map((v: number, i: number) =>
      `${(i / (vals.length - 1)) * w},${h - ((v - min) / span) * (h - 16) - 8}`).join(" "),
    first: vals[0], last: vals[vals.length - 1],
  };
});

async function run() {
  if (!form.value.start) { pushToast("请选择开始日期", "err"); return; }
  const r = await tryReq(() => api.backtestRun({
    start: form.value.start,
    end: form.value.end || null,
    cash: Number(form.value.cash),
    top_n: Number(form.value.top_n),
    warmup: Number(form.value.warmup),
    llm: !!form.value.llm,
  }, app.mode));
  if (r?.job_id) {
    pushToast(`回测已提交（${r.job_id}），后台运行中…`, "ok");
    startPoll(r.job_id);
  }
}

async function refreshJobs() {
  const r = await tryReq(() => api.jobs(20));
  jobs.value = (Array.isArray(r) ? r : r?.jobs || []).filter((j: any) => j.kind === "backtest");
}

function startPoll(jid: string) {
  stopPoll();
  loading.value = true;
  timer = setInterval(async () => {
    const j = await tryReq(() => api.job(jid));
    await refreshJobs();
    if (!j) { stopPoll(); loading.value = false; return; }
    if (j.status === "done" || j.status === "error") {
      stopPoll();
      loading.value = false;
      current.value = j;
      if (j.status === "error") pushToast(`回测失败：${j.error}`, "err");
      else if (!j.result?.has_metrics) pushToast(`回测无结果：${j.result?.error || "样本不足"}`, "err");
      else pushToast("回测完成", "ok");
    }
  }, 1500);
}
function stopPoll() { if (timer) { clearInterval(timer); timer = null; } }

async function loadJob(j: any) {
  if (j.status === "running" || j.status === "pending") { startPoll(j.id); return; }
  current.value = await tryReq(() => api.job(j.id));
}

function statusBadge(s: string) {
  return s === "done" ? "ok" : s === "error" ? "danger" : s === "running" ? "warn" : "muted";
}
function ts(t: number) { return t ? new Date(t * 1000).toLocaleString() : "-"; }

onMounted(refreshJobs);
onUnmounted(stopPoll);
</script>

<template>
  <div>
    <div class="card">
      <h3>⏳ 回测配置
        <span class="sub">与实盘同一套 DataHub / 因子 / 风控代码路径（P7），PIT 严格防未来函数</span>
      </h3>
      <div class="row">
        <div><label>开始日期 *</label><input v-model="form.start" type="date" /></div>
        <div><label>结束日期（留空=今天）</label><input v-model="form.end" type="date" /></div>
        <div><label>初始资金</label><input v-model.number="form.cash" type="number" step="100000" /></div>
        <div><label>持仓数 top_n</label><input v-model.number="form.top_n" type="number" /></div>
        <div><label>预热天数</label><input v-model.number="form.warmup" type="number" /></div>
        <div style="flex:0 0 auto">
          <label>LLM 参与</label>
          <input type="checkbox" v-model="form.llm" style="width:auto" />
        </div>
        <div style="flex:0 0 auto"><label>&nbsp;</label>
          <button :disabled="loading" @click="run">{{ loading ? "运行中…" : "开始回测" }}</button>
        </div>
      </div>
      <div class="tiny muted" style="margin-top:8px">
        预热天数用于因子计算窗口（默认 250 交易日≈1年）。开启 LLM 会显著变慢并产生调用成本，默认关闭走纯因子确定性路径。
      </div>
    </div>

    <template v-if="current?.result?.has_metrics">
      <div class="grid cols-4" style="margin-bottom:16px">
        <div class="stat" v-for="m in metrics.slice(0, 8)" :key="m.key">
          <div class="label">{{ m.label }}</div>
          <div class="value sm pill">{{ m.value }}</div>
        </div>
      </div>

      <div class="card" v-if="curve.points">
        <h3>净值曲线
          <span class="sub">
            {{ Number(curve.first).toFixed(0) }} → {{ Number(curve.last).toFixed(0) }}
            （{{ (((curve.last - curve.first) / curve.first) * 100).toFixed(2) }}%）
          </span>
        </h3>
        <svg viewBox="0 0 900 220" style="width:100%;height:220px">
          <polyline :points="curve.points" fill="none" stroke="var(--primary)" stroke-width="2" />
        </svg>
      </div>

      <div class="grid cols-2">
        <div class="card">
          <h3>全部指标</h3>
          <table>
            <tbody>
              <tr v-for="m in metrics" :key="m.key">
                <th style="width:50%">{{ m.label }} <span class="tiny muted">{{ m.key }}</span></th>
                <td class="pill">{{ m.value }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="card">
          <h3>成交统计</h3>
          <table>
            <tbody>
              <tr><th>总成交笔数</th><td class="pill">{{ current.result.trades }}</td></tr>
              <tr><th>已平仓笔数</th><td class="pill">{{ current.result.closed_trades }}</td></tr>
              <tr><th>净值点数</th><td class="pill">{{ (current.result.equity_curve || []).length }}</td></tr>
            </tbody>
          </table>
          <button class="btn ghost sm" style="margin-top:10px"
                  @click="detail = { title: '回测执行日志', body: (current.result.details || []).join('\n') }">
            查看执行日志（{{ (current.result.details || []).length }} 条）
          </button>
        </div>
      </div>
    </template>

    <div class="card" v-else-if="current">
      <h3>回测结果</h3>
      <div class="muted">
        {{ current.error || current.result?.error || "本次回测未产出指标（可能样本不足或全部标的被过滤）" }}
      </div>
      <pre class="json" v-if="current.result?.details?.length" style="margin-top:10px">{{ (current.result.details || []).slice(0, 40).join("\n") }}</pre>
    </div>

    <div class="card">
      <h3>📜 历史回测任务
        <div class="spacer"></div><button class="btn sm ghost" @click="refreshJobs">刷新</button>
      </h3>
      <table>
        <thead><tr><th>任务 ID</th><th>状态</th><th>提交时间</th><th>完成时间</th><th style="width:90px">操作</th></tr></thead>
        <tbody>
          <tr v-for="j in jobs" :key="j.id">
            <td class="pill tiny">{{ j.id }}</td>
            <td><span class="badge" :class="statusBadge(j.status)">{{ j.status }}</span></td>
            <td class="tiny muted">{{ ts(j.created) }}</td>
            <td class="tiny muted">{{ ts(j.finished) }}</td>
            <td><button class="btn sm ghost" @click="loadJob(j)">载入</button></td>
          </tr>
          <tr v-if="!jobs.length"><td colspan="5" class="muted">暂无回测记录</td></tr>
        </tbody>
      </table>
    </div>

    <Modal v-if="detail" :title="detail.title" @close="detail = null">
      <pre class="json">{{ detail.body }}</pre>
      <template #actions><button class="btn ghost" @click="detail = null">关闭</button></template>
    </Modal>
  </div>
</template>
