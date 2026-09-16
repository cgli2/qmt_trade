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
// K线明细表分页：默认只渲染最近 N 行。原先 400 行 × 8 列一次性同步渲染，且模板里每行调
// rowChg() 三次（:style 一次、文本插值两次）+ 每次渲染新建 bars.slice().reverse() 数组，
// 是切股瞬间主线程被长时间占住、图表与页面"卡一下"的另一半原因。
const DETAIL_PAGE = 60;
const detailLimit = ref(DETAIL_PAGE);
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

// 代码 -> 标的画像（含真实名称），供推荐列表补充名称
const symbolMap = computed(() => {
  const m: Record<string, any> = {};
  for (const s of symbols.value) m[s.symbol] = s;
  return m;
});

// 推荐项展示名：优先真实名称；拿不到名称时不回退到"未知"，宁可留空
function pickLabel(p: any): string {
  const nm = String(symbolMap.value[p.symbol]?.name || "").trim();
  if (nm && nm !== "未知") return nm;
  const ind = String(p.industry || "").trim();
  return ind === "未知" ? "" : ind;
}

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

// ---------------- K线取数：结果缓存 + 并发去重 + 竞态守卫（卡顿修复 2026-09-16）
// 原实现三缺：① 每次切股都重新请求，回看刚看过的标的也要重等一轮；② 同一 key 连点会
// 并发发出多份完全相同的请求；③ 响应不带序号，快速切股时先发的请求可能后到，把 bars
// 覆盖成**别的标的**的数据（图表与明细表随之整体重绘，观感就是"卡一下还显示错了"）。
// 另外 loading 原先绑在根 div，而全局 .loading{opacity:.55;pointer-events:none} 会让
// **整页变暗且不可点击** —— 加载期间连点「策略推荐」卡片毫无反应，这是"明显卡顿"的直接来源。
const KLINE_CACHE_MS = 60_000;   // 与后端 minute_bar_ttl 同量级：盘中当日 bar 会变，不宜久存
const KLINE_CACHE_MAX = 40;      // 上限防长期驻留膨胀（一条 400 行日线约 100KB）
const klineCache = new Map<string, { at: number; rows: any[] }>();
const klineInflight = new Map<string, Promise<any[] | undefined>>();
let klineSeq = 0;                // 单调递增请求序号：只接受最后一次点击的结果
const klineLoading = ref(false); // 局部 loading：只罩 K线区，绝不锁左栏与整页

function klineKey(sym: string) {
  return [sym, period.value, start.value, end.value || "", adjust.value, app.mode].join("|");
}

function klineCachePut(key: string, rows: any[]) {
  if (klineCache.size >= KLINE_CACHE_MAX) {
    // Map 的迭代序即插入序；命中时 loadKline 会 delete+set 把该项移到末尾，
    // 因此首键就是最久未使用的一条 —— 删它即标准 LRU，零依赖。
    const oldest = klineCache.keys().next().value;
    if (oldest !== undefined) klineCache.delete(oldest);
  }
  klineCache.set(key, { at: Date.now(), rows });
}

async function fetchKline(key: string, sym: string): Promise<any[] | undefined> {
  // 并发去重：同 key 的重复点击共享同一个在途 Promise，后端只挨一次
  const pending = klineInflight.get(key);
  if (pending) return pending;
  const p = (async () => {
    const r = await tryReq(() => api.kline(
      sym, period.value, start.value, app.mode, end.value || undefined, 400, adjust.value));
    if (!r) return undefined;              // 失败不入缓存，下次点击可立即重试
    const rows = r.rows || [];
    klineCachePut(key, rows);
    return rows;
  })();
  klineInflight.set(key, p);
  try {
    return await p;
  } finally {
    klineInflight.delete(key);
  }
}

/** @param force 跳过结果缓存（「查询」按钮 / 改复权方式时用，保证拿到最新数据） */
async function loadKline(force = false) {
  if (!picked.value) { pushToast("请先选择标的", "err"); return; }
  const sym = picked.value;
  const key = klineKey(sym);
  const seq = ++klineSeq;
  if (!force) {
    const hit = klineCache.get(key);
    if (hit && Date.now() - hit.at < KLINE_CACHE_MS) {
      // 只调整**淘汰顺序**（删后重插 → 移到 Map 末尾），保留原始取数时刻 hit.at。
      // 若走 klineCachePut 会用 Date.now() 重置 at，反复点击同一标的等于无限续期 TTL，
      // 盘中当日 bar 将永远刷不出来。
      klineCache.delete(key);
      klineCache.set(key, hit);
      bars.value = hit.rows;
      detailLimit.value = DETAIL_PAGE;     // 换标的后明细表回到第一页
      return;                              // 秒开：零请求、零等待、零 loading 闪烁
    }
  }
  klineLoading.value = true;
  try {
    const rows = await fetchKline(key, sym);
    // 竞态守卫：期间用户已切到别的标的 → 丢弃本次结果，绝不覆盖当前 bars
    if (seq !== klineSeq || picked.value !== sym) return;
    if (rows === undefined) return;        // tryReq 已弹过错误 toast
    bars.value = rows;
    detailLimit.value = DETAIL_PAGE;
    if (!rows.length) pushToast("该区间无数据，试试放宽日期", "info");
  } finally {
    if (seq === klineSeq) klineLoading.value = false;
  }
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

// K线明细表：倒序 + 涨跌幅 + 格式化**一次算好**并缓存为 computed。
// 模板里因此变成纯字段读取：零函数调用、零数组新建、零重复 Number() 转换，
// 且只有 bars / detailLimit 变化时才重算（原先每次任意重渲染都会全表重算）。
const detailRows = computed(() => {
  const rows = bars.value;
  const from = Math.max(rows.length - detailLimit.value, 0);
  const out: any[] = [];
  for (let i = rows.length - 1; i >= from; i--) {
    const b = rows[i];
    const prevClose = i > 0 ? Number(rows[i - 1].close) : 0;
    const chg = prevClose ? ((Number(b.close) - prevClose) / prevClose) * 100 : null;
    out.push({
      key: String(b.date).slice(0, 10),
      open: Number(b.open).toFixed(2),
      high: Number(b.high).toFixed(2),
      low: Number(b.low).toFixed(2),
      close: Number(b.close).toFixed(2),
      chgText: chg == null ? "-" : chg.toFixed(2) + "%",
      up: (chg ?? 0) >= 0,
      vol: Number(b.volume).toLocaleString(),
      amount: b.amount ? Number(b.amount).toLocaleString() : "-",
    });
  }
  return out;
});
const detailHidden = computed(() => bars.value.length - detailRows.value.length);
function showAllDetail() { detailLimit.value = bars.value.length; }

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
  // 深链 ?sym= 直达（如从选股研判页点击个股跳转）
  const qs = String(route.query.sym || "");
  if (qs && qs !== picked.value) picked.value = qs;
  // 标的已知就立刻发 K线请求，不必等 /market/symbols（实测 ~1.5s）返回才开始画图；
  // symbols 与 picks 之间也互不依赖，原先串行 await 纯属白等
  const klineTask = picked.value ? loadKline() : Promise.resolve();
  await Promise.all([loadSymbols(), loadPicks()]);
  await klineTask;
  if (!picked.value) {
    // 预选优先：策略推荐第一只 > 全市场第一只
    const pref = picks.value[0]?.symbol || symbols.value[0]?.symbol;
    if (pref) { picked.value = pref; loadKline(); }
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
  <div>
    <!-- 页级 tab：行情与事件融合 -->
    <div class="page-tabs">
      <button class="page-tab" :class="{ on: pageTab === 'market' }" @click="pageTab = 'market'">📈 行情查询</button>
      <button class="page-tab" :class="{ on: pageTab === 'event' }" @click="pageTab = 'event'">📰 事件驱动</button>
    </div>

    <div v-if="pageTab === 'market'" class="market-layout">
      <!-- 左栏：策略推荐 + 全部标的搜索。
           ★ 刻意**不**受 loading 影响：全局 .loading 带 pointer-events:none，
             若绑在根节点会让加载期间整页（含本栏）不可点击，用户连点卡片毫无反应。 -->
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
              <span class="pn">{{ pickLabel(p) }}</span>
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

      <!-- 右栏：行情详情。loading（新闻/实时行情）只罩本栏，不锁左栏与页级 tab -->
      <main class="m-main" :class="{ loading }">
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
              <!-- 显式传 true：Vue 会把 Event 对象当首个实参传入，
                   依赖"Event 恰好真值"来跳过缓存太脆弱 -->
              <select v-model="adjust" @change="loadKline(true)">
                <option value="QFQ">前复权（最新价≈真实）</option>
                <option value="HFQ">后复权</option>
                <option value="NONE">不复权</option>
              </select>
            </div>
            <div style="flex:0 0 auto"><label>&nbsp;</label>
              <button :disabled="klineLoading" @click="loadKline(true)">
                {{ klineLoading ? "加载中…" : "查询" }}
              </button>
            </div>
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

          <div class="card kline-card">
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
            <!-- 局部加载指示：只浮在 K线卡片上，且自身 pointer-events:none，
                 加载期间用户仍能继续点左栏切股、也能缩放图表（原整页 loading 做不到） -->
            <div v-if="klineLoading" class="kline-busy">
              <span class="spin"></span>正在加载 {{ picked }} 的{{ periodLabel }}…
            </div>
          </div>

          <div class="card">
            <h3>K线明细 <span class="sub">
              共 {{ bars.length }} 行<template v-if="detailHidden > 0"> · 已显示最近 {{ detailRows.length }} 行</template>
            </span></h3>
            <div style="max-height:460px; overflow:auto">
              <table>
                <thead><tr><th>日期</th><th>开</th><th>高</th><th>低</th><th>收</th><th>涨跌幅</th><th>成交量</th><th>成交额</th></tr></thead>
                <tbody>
                  <!-- 纯字段读取：涨跌幅/格式化已在 detailRows computed 里一次算好 -->
                  <tr v-for="r in detailRows" :key="r.key">
                    <td class="pill">{{ r.key }}</td>
                    <td class="pill">{{ r.open }}</td>
                    <td class="pill">{{ r.high }}</td>
                    <td class="pill">{{ r.low }}</td>
                    <td class="pill"><b>{{ r.close }}</b></td>
                    <td class="pill" :style="{ color: r.up ? 'var(--danger)' : 'var(--ok)' }">{{ r.chgText }}</td>
                    <td class="pill tiny">{{ r.vol }}</td>
                    <td class="pill tiny">{{ r.amount }}</td>
                  </tr>
                  <tr v-if="!bars.length"><td colspan="8" class="muted">无数据</td></tr>
                </tbody>
              </table>
            </div>
            <div v-if="detailHidden > 0" style="padding-top:10px; text-align:center">
              <button class="btn sm ghost" @click="showAllDetail">显示全部 {{ bars.length }} 行</button>
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
/* 局部加载指示：浮在图表上方，自身不拦截鼠标 → 加载中仍可切股/缩放/平移 */
.kline-card { position: relative; }
.kline-busy {
  position: absolute;
  top: 54px;
  left: 50%;
  transform: translateX(-50%);
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-elev);
  color: var(--text-2);
  font-size: 12px;
  white-space: nowrap;
  box-shadow: var(--shadow-sm);
  pointer-events: none;
  z-index: 3;
}
.spin {
  width: 12px;
  height: 12px;
  flex: 0 0 12px;
  border: 2px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: kline-spin 0.7s linear infinite;
}
@keyframes kline-spin { to { transform: rotate(360deg); } }
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
