<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import api from "@/api";
import { useApp } from "@/store";
import { pushToast, tryReq } from "@/toast";
import SymbolSelect from "@/components/SymbolSelect.vue";
import KlineChart from "@/components/KlineChart.vue";
import TimelineChart from "@/components/TimelineChart.vue";
import EventView from "@/views/EventView.vue";

const app = useApp();
const route = useRoute();
// 页级 tab：行情查询 / 事件驱动（融合）
const pageTab = ref<"market" | "event">("market");
const loading = ref(false);
const symbols = ref<any[]>([]);
const sources = ref<string[]>([]);
const real = ref(true);
const picked = ref<string>("");
const start = ref(defaultStartFor("D1"));
const end = ref("");
const adjust = ref<"QFQ" | "HFQ" | "NONE">("QFQ"); // 前复权(默认,最新价≈真实):后复权:不复权
const bars = ref<any[]>([]);
const quote = ref<any>(null);
const news = ref<any[]>([]);
const tab = ref<"bars" | "quote" | "news">("bars");

// K线图：时间周期与均线显隐
const period = ref<"D1" | "W1" | "M1" | "Y1">("D1");
const PERIODS = [
  { v: "D1", label: "日线" },
  { v: "W1", label: "周线" },
  { v: "M1", label: "月线" },
  { v: "Y1", label: "年线" },
] as const;
const periodLabel = computed(() => PERIODS.find((p) => p.v === period.value)?.label || period.value);
const maVisible = ref<Record<string, boolean>>({ ma5: true, ma10: true, ma20: true, ma60: true });
// 均线图例配色（与 KlineChart 内部绘制颜色一致）
const MA_META = [
  { k: "ma5", label: "MA5", color: "#f59e0b" },
  { k: "ma10", label: "MA10", color: "#a855f7" },
  { k: "ma20", label: "MA20", color: "#38bdf8" },
  { k: "ma60", label: "MA60", color: "#ec4899" },
];

// 分时图：数据 + 自动轮询（5s 一次，报价永不缓存；分时端点自带 60s 分钟缓存）
const timeline = ref<any>(null);
const tlUpdatedAt = ref("");
let pollTimer: any = null;
const POLL_MS = 5000;

// 策略推荐（选股漏斗）
const picks = ref<any[]>([]);
const picksAsOf = ref<string | null>(null);
const picksRegime = ref<string | null>(null);
const picksNote = ref<string>("");
const running = ref(false);
const runProgress = ref("");

function defaultStartFor(p: string) {
  // 各周期默认回看区间：日线半年、周线两年、月线八年、年线三十年（约覆盖 A 股全部历史）
  const months: Record<string, number> = { D1: 6, W1: 24, M1: 96, Y1: 360 };
  const d = new Date();
  d.setMonth(d.getMonth() - (months[p] ?? 6));
  return d.toISOString().slice(0, 10);
}

const pickedInfo = computed(() => symbols.value.find((s) => s.symbol === picked.value));

async function loadSymbols() {
  const r = await tryReq(() => api.symbols(app.mode));
  symbols.value = r?.symbols || [];
  sources.value = r?.sources || [];
  real.value = !!r?.real;
}

async function loadPicks() {
  const r = await tryReq(() => api.selectionPicks(app.mode));
  picks.value = r?.picks || [];
  picksAsOf.value = r?.asof || null;
  picksRegime.value = r?.regime || null;
  picksNote.value = r?.note || "";
}

async function loadKline() {
  if (!picked.value) { pushToast("请先选择标的", "err"); return; }
  loading.value = true;
  const r = await tryReq(() => api.kline(picked.value, period.value, start.value, app.mode, end.value || undefined, 400, adjust.value));
  bars.value = r?.rows || [];
  loading.value = false;
  if (r && !bars.value.length) pushToast("该区间无数据，试试放宽日期", "info");
}

function switchPeriod(p: "D1" | "W1" | "M1" | "Y1") {
  if (period.value === p) return;
  period.value = p;
  // 切周期时给出匹配的回看区间，避免周/月/年视图只有寥寥几根K线
  start.value = defaultStartFor(p);
  loadKline();
}

async function loadQuote(soft = false) {
  // soft：轮询刷新时不触发整页 loading，避免图表随轮询闪烁
  if (!picked.value) return;
  if (!soft) loading.value = true;
  const r = await tryReq(() => api.quote(picked.value, app.mode));
  quote.value = r?.quotes?.[picked.value] || null;
  if (!soft) loading.value = false;
}

async function loadTimeline() {
  if (!picked.value) return;
  // 直调 api（不经 tryReq）：轮询失败静默跳过，避免错误 toast 刷屏
  try {
    const r = await api.timeline(picked.value, app.mode);
    timeline.value = r;
    tlUpdatedAt.value = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch { /* 网络抖动时保持上一帧 */ }
}

// 实时行情报价摘要（供卡片大字展示）
const quoteStats = computed(() => {
  const q = timeline.value?.quote || quote.value;
  if (!q || q.last == null) return null;
  const pc = timeline.value?.prev_close ?? q.prev_close;
  const chg = pc ? ((q.last - pc) / pc) * 100 : null;
  return { q, pc, chg };
});
function fmtNum(v: any) {
  return v == null ? "-" : Number(v).toFixed(2);
}
function fmtVol(v: any) {
  if (v == null) return "-";
  if (v >= 1e8) return (v / 1e8).toFixed(2) + "亿";
  if (v >= 1e4) return (v / 1e4).toFixed(2) + "万";
  return String(Math.round(v));
}

// ---------------- 实时轮询：仅在「行情页·实时行情 tab·已选标的」时运行；切走即停、页面隐藏跳过
function pollActive() {
  return pageTab.value === "market" && tab.value === "quote" && !!picked.value;
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}
function startPolling() {
  stopPolling();
  if (!pollActive()) return;
  pollTimer = setInterval(() => {
    if (document.hidden || !pollActive()) return;
    loadQuote(true);
    loadTimeline();
  }, POLL_MS);
}
async function refreshQuoteTab() {
  await Promise.all([loadQuote(), loadTimeline()]);
}
onBeforeUnmount(stopPolling);

async function loadNews() {
  loading.value = true;
  const r = await tryReq(() => api.news(app.mode, picked.value || undefined, start.value, end.value || undefined));
  news.value = r?.news || [];
  loading.value = false;
}

// 情绪分 → 中文标签 + 徽章配色（>0 利好 / <0 利空 / 0 中性；悬停可见原始分值）
function sentLabel(v: any) {
  const n = Number(v);
  if (v == null || isNaN(n)) return "-";
  if (n > 0) return "利好";
  if (n < 0) return "利空";
  return "中性";
}
function sentClass(v: any) {
  const n = Number(v);
  if (n > 0) return "ok";
  if (n < 0) return "danger";
  return "muted";
}

function fmtTime(t: any) {
  const v = t ?? t?.time;
  if (v == null || v === "") return "-";
  const d = new Date(Number(v) * 1000);
  if (isNaN(d.getTime())) return String(v).slice(0, 16);
  const off = d.getTimezoneOffset() * 60000;
  return new Date(d.getTime() - off).toISOString().slice(0, 16).replace("T", " ");
}

function onPick(s: string) {
  picked.value = s;
  if (tab.value === "bars") loadKline();
  else if (tab.value === "quote") refreshQuoteTab();
  else loadNews();
}

async function runSelection() {
  if (running.value) return;
  running.value = true;
  runProgress.value = "选股中（拉取行情/因子，可能数分钟）…";
  const r = await tryReq(() => api.selectionRun({}, app.mode));
  if (!r?.job_id) { running.value = false; runProgress.value = ""; return; }
  pollJob(r.job_id);
}

async function pollJob(id: string) {
  for (let i = 0; i < 80; i++) {
    await new Promise((res) => setTimeout(res, 3000));
    const j = await tryReq(() => api.job(id));
    if (j === undefined) {
      // 404：后端重启后内存 Job 丢失，停止轮询避免反复弹错
      running.value = false; runProgress.value = "";
      pushToast("选股任务记录已失效（后端可能重启过），请重试", "info");
      return;
    }
    if (j?.status === "done") {
      running.value = false; runProgress.value = "";
      await loadPicks();
      pushToast(`选股完成，推荐 ${picks.value.length} 只`, "ok");
      if (picks.value[0]) onPick(picks.value[0].symbol);
      return;
    }
    if (j?.status === "error") {
      running.value = false; runProgress.value = "";
      pushToast("选股失败：" + (j.error || "未知错误"), "err");
      return;
    }
    runProgress.value = `选股中…（${i * 3 + 3}s）`;
  }
  running.value = false; runProgress.value = "";
  pushToast("选股超时，请稍后在「策略」页查看或重试", "info");
}

function switchTab(t: any) {
  tab.value = t;
  if (t === "bars" && !bars.value.length) loadKline();
  if (t === "quote") refreshQuoteTab();
  if (t === "news" && !news.value.length) loadNews();
}

// K线明细表：相对前一根的涨跌幅（%）
function rowChg(idx: number) {
  const rows = bars.value;
  if (idx <= 0 || !rows[idx - 1]) return null;
  const prev = Number(rows[idx - 1].close), cur = Number(rows[idx].close);
  if (!prev) return null;
  return ((cur - prev) / prev) * 100;
}

const stats = computed(() => {
  if (!bars.value.length) return null;
  const closes = bars.value.map((r: any) => Number(r.close));
  const first = closes[0], last = closes[closes.length - 1];
  return {
    count: closes.length,
    last: last.toFixed(2),
    chg: (((last - first) / first) * 100).toFixed(2),
    high: Math.max(...bars.value.map((r: any) => Number(r.high))).toFixed(2),
    low: Math.min(...bars.value.map((r: any) => Number(r.low))).toFixed(2),
  };
});

async function reload() {
  await loadSymbols();
  await loadPicks();
  // 深链 ?sym= 直达（如从选股研判页点击个股跳转）
  const qs = String(route.query.sym || "");
  if (qs && qs !== picked.value) picked.value = qs;
  if (!picked.value) {
    // 预选优先：策略推荐第一只 > 全市场第一只
    const pref = picks.value[0]?.symbol || symbols.value[0]?.symbol;
    if (pref) { picked.value = pref; loadKline(); }
  } else {
    loadKline();
  }
}

onMounted(reload);
watch(() => app.mode, reload);
watch(() => route.query.sym, (v) => {
  const s = String(v || "");
  if (s && s !== picked.value) { picked.value = s; loadKline(); }
});
// 轮询跟随视图状态：进入实时行情 tab 启动，离开（换 tab/换页/卸载）立即停止
watch([pageTab, tab, picked], () => {
  if (pollActive()) startPolling();
  else stopPolling();
});
</script>

<template>
  <div :class="{ loading }">
    <!-- 页级 tab：行情与事件融合 -->
    <div class="page-tabs">
      <button class="page-tab" :class="{ on: pageTab === 'market' }" @click="pageTab = 'market'">📈 行情查询</button>
      <button class="page-tab" :class="{ on: pageTab === 'event' }" @click="pageTab = 'event'">📰 事件驱动</button>
    </div>

    <div v-if="pageTab === 'market'" class="market-layout">
      <!-- 左栏：策略推荐 + 全部标的搜索 -->
      <aside class="m-side">
        <section class="card picks-card">
          <div class="picks-head">
            <h3>🎯 策略推荐</h3>
            <button class="btn sm ghost" :disabled="running" @click="runSelection">
              {{ running ? "选股中…" : "重新选股" }}
            </button>
          </div>
          <div v-if="picksRegime" class="badge ok" style="margin-bottom:8px">
            Regime: {{ picksRegime }}<span v-if="picksAsOf"> · {{ picksAsOf }}</span>
          </div>
          <div v-if="runProgress" class="tiny muted" style="margin-bottom:8px">{{ runProgress }}</div>

          <div v-if="picks.length" class="picks-list">
            <button
              v-for="p in picks"
              :key="p.symbol"
              class="pick"
              :class="{ on: p.symbol === picked }"
              @click="onPick(p.symbol)"
            >
              <span class="rk">#{{ p.rank }}</span>
              <span class="ps">{{ p.symbol }}</span>
              <span class="pn">{{ p.industry || "" }}</span>
              <span class="psc">{{ Number(p.score).toFixed(3) }}</span>
            </button>
          </div>
          <div v-else class="muted" style="font-size:13px">
            {{ picksNote || "暂无推荐，点击「重新选股」生成。" }}
          </div>
        </section>

        <section class="card all-card">
          <h3>🔍 全部标的 <span class="sub">{{ symbols.length }} 只 · 可搜索</span></h3>
          <SymbolSelect
            v-model="picked"
            :options="symbols"
            placeholder="搜索 5500+ 标的（代码/名称/行业）"
            @select="onPick"
          />
          <div v-if="pickedInfo" class="cur-pick">
            已选：<b>{{ pickedInfo.symbol }}</b> {{ pickedInfo.name }}
            <span class="tiny muted" v-if="pickedInfo.industry">· {{ pickedInfo.industry }}</span>
          </div>
        </section>
      </aside>

      <!-- 右栏：行情详情 -->
      <main class="m-main">
        <div class="card">
          <h3>📈 行情查询
            <span class="sub">与回测/实盘同一条 DataHub 取数路径（PIT 保证，P7）</span>
            <span class="badge" :class="real ? 'ok' : 'danger'" style="margin-left:8px">
              数据源: {{ sources.join(", ") || "—" }} · {{ real ? "真实" : "模拟" }}
            </span>
          </h3>
          <div class="row">
            <div><label>当前标的</label>
              <div class="cur-sym">{{ picked || "—" }} <span class="tiny muted" v-if="pickedInfo">{{ pickedInfo.name }}</span></div>
            </div>
            <div><label>开始日期</label><input v-model="start" type="date" /></div>
            <div><label>结束日期</label><input v-model="end" type="date" /></div>
            <div>
              <label>复权方式</label>
              <select v-model="adjust" @change="loadKline">
                <option value="QFQ">前复权（最新价≈真实）</option>
                <option value="HFQ">后复权</option>
                <option value="NONE">不复权</option>
              </select>
            </div>
            <div style="flex:0 0 auto"><label>&nbsp;</label><button @click="loadKline">查询</button></div>
          </div>
          <div style="margin-top:12px; display:flex; gap:6px">
            <button class="btn sm" :class="tab === 'bars' ? '' : 'ghost'" @click="switchTab('bars')">K线</button>
            <button class="btn sm" :class="tab === 'quote' ? '' : 'ghost'" @click="switchTab('quote')">实时行情</button>
            <button class="btn sm" :class="tab === 'news' ? '' : 'ghost'" @click="switchTab('news')">相关新闻</button>
          </div>
        </div>

        <template v-if="tab === 'bars'">
          <div class="grid cols-4" v-if="stats" style="margin-bottom:16px">
            <div class="stat"><div class="label">最新收盘</div><div class="value pill">{{ stats.last }}</div></div>
            <div class="stat"><div class="label">区间涨跌</div>
              <div class="value pill" :style="{ color: Number(stats.chg) >= 0 ? 'var(--danger)' : 'var(--ok)' }">
                {{ stats.chg }}%
              </div></div>
            <div class="stat"><div class="label">区间最高/最低</div><div class="value sm pill">{{ stats.high }} / {{ stats.low }}</div></div>
            <div class="stat"><div class="label">K线根数</div><div class="value pill">{{ stats.count }}</div></div>
          </div>

          <div class="card">
            <div class="kline-head">
              <h3>K线图 <span class="sub">{{ picked }} · {{ periodLabel }}</span></h3>
              <div class="period-group">
                <button
                  v-for="p in PERIODS"
                  :key="p.v"
                  class="btn sm"
                  :class="period === p.v ? '' : 'ghost'"
                  @click="switchPeriod(p.v)"
                >{{ p.label }}</button>
              </div>
              <div class="ma-legend">
                <button
                  v-for="m in MA_META"
                  :key="m.k"
                  class="ma-chip"
                  :class="{ off: !maVisible[m.k] }"
                  :style="{ '--ma-color': m.color }"
                  @click="maVisible[m.k] = !maVisible[m.k]"
                >
                  <i class="dot"></i>{{ m.label }}
                </button>
              </div>
            </div>
            <KlineChart :rows="bars" :mas="maVisible" :period="period" style="height:440px" />
            <div class="tiny muted" style="margin-top:6px">滚轮缩放 · 拖拽平移 · 双击复位 · 悬停查看明细</div>
          </div>

          <div class="card">
            <h3>K线明细 <span class="sub">{{ bars.length }} 行</span></h3>
            <div style="max-height:460px; overflow:auto">
              <table>
                <thead><tr><th>日期</th><th>开</th><th>高</th><th>低</th><th>收</th><th>涨跌幅</th><th>成交量</th><th>成交额</th></tr></thead>
                <tbody>
                  <tr v-for="(b, i) in bars.slice().reverse()" :key="i">
                    <td class="pill">{{ String(b.date).slice(0, 10) }}</td>
                    <td class="pill">{{ Number(b.open).toFixed(2) }}</td>
                    <td class="pill">{{ Number(b.high).toFixed(2) }}</td>
                    <td class="pill">{{ Number(b.low).toFixed(2) }}</td>
                    <td class="pill"><b>{{ Number(b.close).toFixed(2) }}</b></td>
                    <td class="pill" :style="{ color: (rowChg(bars.length - 1 - i) ?? 0) >= 0 ? 'var(--danger)' : 'var(--ok)' }">
                      {{ rowChg(bars.length - 1 - i) == null ? "-" : rowChg(bars.length - 1 - i)!.toFixed(2) + "%" }}
                    </td>
                    <td class="pill tiny">{{ Number(b.volume).toLocaleString() }}</td>
                    <td class="pill tiny">{{ b.amount ? Number(b.amount).toLocaleString() : "-" }}</td>
                  </tr>
                  <tr v-if="!bars.length"><td colspan="8" class="muted">无数据</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </template>

        <template v-else-if="tab === 'quote'">
          <div class="card">
            <h3>实时行情 <span class="sub">{{ picked }} <span v-if="pickedInfo">· {{ pickedInfo.name }}</span></span>
              <span v-if="timeline?.live" class="badge ok" style="margin-left:8px">● 实时</span>
              <span v-else-if="timeline?.stale" class="badge warn" style="margin-left:8px">⚠ 非今日 · {{ timeline.date }}</span>
              <span v-else-if="timeline?.date" class="badge muted" style="margin-left:8px">收盘 · {{ timeline.date }}</span>
              <span v-if="tlUpdatedAt" class="tiny muted" style="margin-left:8px">{{ tlUpdatedAt }} 更新 · 5s 自动刷新</span>
              <div class="spacer"></div><button class="btn sm ghost" @click="refreshQuoteTab()">刷新</button>
            </h3>
            <div v-if="quoteStats" class="rt-summary">
              <div class="rt-last" :style="{ color: (quoteStats.chg ?? 0) >= 0 ? 'var(--danger)' : 'var(--ok)' }">
                {{ fmtNum(quoteStats.q.last) }}
              </div>
              <div class="rt-chg" :style="{ color: (quoteStats.chg ?? 0) >= 0 ? 'var(--danger)' : 'var(--ok)' }">
                {{ quoteStats.chg == null ? "-" : (quoteStats.chg >= 0 ? "+" : "") + quoteStats.chg.toFixed(2) + "%" }}
              </div>
              <div class="rt-grid">
                <span class="rt-item"><span class="k">今开</span><span class="v">{{ fmtNum(quoteStats.q.open) }}</span></span>
                <span class="rt-item"><span class="k">昨收</span><span class="v">{{ fmtNum(quoteStats.pc ?? quoteStats.q.prev_close) }}</span></span>
                <span class="rt-item"><span class="k">最高</span><span class="v" style="color:var(--danger)">{{ fmtNum(quoteStats.q.high) }}</span></span>
                <span class="rt-item"><span class="k">最低</span><span class="v" style="color:var(--ok)">{{ fmtNum(quoteStats.q.low) }}</span></span>
                <span class="rt-item"><span class="k">成交量</span><span class="v">{{ fmtVol(quoteStats.q.volume) }}</span></span>
                <span class="rt-item"><span class="k">成交额</span><span class="v">{{ fmtVol(quoteStats.q.amount) }}</span></span>
                <span class="rt-item"><span class="k">买一/卖一</span><span class="v">{{ fmtNum(quoteStats.q.bid1) }} / {{ fmtNum(quoteStats.q.ask1) }}</span></span>
              </div>
            </div>
            <div v-else class="muted">暂无实时行情（当前数据源不支持实时行情，可切换至 paper/live 或配置 QMT）</div>
          </div>

          <div class="card">
            <div class="kline-head">
              <h3>分时走势 <span class="sub">{{ picked }} · {{ timeline?.date || "—" }}</span>
                <span v-if="timeline?.stale" class="badge warn" style="margin-left:8px">⚠ 非今日数据</span></h3>
            </div>
            <TimelineChart :points="timeline?.points || []" :prev-close="timeline?.prev_close ?? null" style="height:400px" />
            <div class="tiny muted" style="margin-top:6px">
              滚轮缩放 · 拖拽平移 · 双击复位 · 悬停查看明细<span v-if="timeline?.note"> · {{ timeline.note }}</span>
            </div>
          </div>
        </template>

        <div class="card" v-else>
          <h3>相关新闻 <span class="sub">{{ news.length }} 条</span>
            <div class="spacer"></div><button class="btn sm ghost" @click="loadNews">刷新</button>
          </h3>
          <table>
            <thead><tr><th style="width:110px">时间</th><th style="width:100px">标的</th><th>标题</th><th style="width:90px">情绪</th></tr></thead>
            <tbody>
              <tr v-for="(n, i) in news" :key="i">
                <td class="tiny pill">{{ fmtTime(n.publish_time || n.time) }}</td>
                <td class="tiny">{{ n.symbol || "-" }}</td>
                <td>{{ n.title }}<div class="tiny muted">{{ n.source || "" }}</div></td>
                <td>
                  <span class="badge" :class="sentClass(n.sentiment)" :title="n.sentiment != null ? `情绪分 ${Number(n.sentiment).toFixed(2)}（-1 最利空 / +1 最利好）` : '暂无情绪分'">
                    {{ sentLabel(n.sentiment) }}
                  </span>
                </td>
              </tr>
              <tr v-if="!news.length"><td colspan="4" class="muted">无新闻</td></tr>
            </tbody>
          </table>
        </div>
      </main>
    </div>

    <EventView v-else />
  </div>
</template>

<style scoped>
.page-tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 14px;
}
.page-tab {
  padding: 7px 15px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg-elev);
  color: var(--text-2);
  cursor: pointer;
  font-size: 14px;
  transition: all 0.15s;
}
.page-tab:hover { border-color: var(--primary); }
.page-tab.on {
  background: var(--primary);
  border-color: var(--primary);
  color: #fff;
  font-weight: 700;
}
.market-layout {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.m-side {
  width: 320px;
  flex: 0 0 320px;
}
.m-main {
  flex: 1;
  min-width: 0;
}
.picks-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}
.picks-head h3 {
  margin: 0;
}
.picks-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 420px;
  overflow: auto;
}
.pick {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  text-align: left;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg-elev);
  color: var(--text);
  cursor: pointer;
  transition: all 0.15s;
}
.pick:hover {
  border-color: var(--primary);
  background: var(--bg-2);
}
.pick.on {
  border-color: var(--primary);
  background: var(--primary);
  color: #fff;
}
.pick .rk {
  font-weight: 800;
  color: var(--primary);
  font-family: ui-monospace, monospace;
}
.pick.on .rk { color: #fff; }
.pick .ps {
  font-weight: 700;
  font-family: ui-monospace, monospace;
}
.pick .pn {
  flex: 1;
  font-size: 12px;
  color: var(--text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.pick.on .pn { color: rgba(255, 255, 255, 0.85); }
.pick .psc {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--ok);
}
.pick.on .psc { color: #fff; }
.cur-pick {
  margin-top: 10px;
  font-size: 13px;
  color: var(--text-2);
}
.cur-sym {
  font-weight: 700;
  font-family: ui-monospace, monospace;
  font-size: 15px;
  padding: 9px 0;
}
.kline-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 10px;
}
.kline-head h3 { margin: 0; }
.period-group {
  display: flex;
  gap: 4px;
  margin-left: auto;
}
.ma-legend {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.ma-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-elev);
  color: var(--text-2);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}
.ma-chip .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--ma-color);
}
.ma-chip:hover { border-color: var(--ma-color); }
.ma-chip.off {
  opacity: 0.45;
}
.ma-chip.off .dot { background: var(--text-2); }
.rt-summary {
  display: flex;
  align-items: flex-start;
  gap: 24px;
  flex-wrap: wrap;
  padding: 6px 0 2px;
}
.rt-last {
  font-size: 34px;
  font-weight: 800;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}
.rt-chg {
  align-self: center;
  font-size: 18px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.rt-grid {
  flex: 1;
  min-width: 300px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 28px;             /* 行间距 6px，每对之间 28px */
  align-content: center;
  font-size: 13px;
}
.rt-item { display: inline-flex; align-items: baseline; white-space: nowrap; }
.rt-grid .k { color: var(--text-2); margin-right: 8px; }
.rt-grid .v {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
@media (max-width: 980px) {
  .market-layout { flex-direction: column; }
  .m-side { width: 100%; flex: none; }
}
</style>
