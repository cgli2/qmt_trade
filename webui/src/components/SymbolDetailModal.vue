<script setup lang="ts">
// 个股详情弹窗：点击持仓/订单中的标的打开，查看实时行情（分时+报价）与K线图。
// 复用行情页的 KlineChart / TimelineChart 组件与 /market/* 同一条 DataHub 取数路径。
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import api from "@/api";
import { tryReq } from "@/toast";
import Modal from "@/components/Modal.vue";
import KlineChart from "@/components/KlineChart.vue";
import TimelineChart from "@/components/TimelineChart.vue";

const props = defineProps<{ symbol: string; name?: string; mode: string }>();
const emit = defineEmits<{ (e: "close"): void }>();

const tab = ref<"quote" | "kline">("quote");

// ---------------- 实时行情：分时 + 报价摘要，打开期间 5s 自动刷新（直调 api，失败静默保帧）
const timeline = ref<any>(null);
const quote = ref<any>(null);
const updatedAt = ref("");
let pollTimer: any = null;

async function loadTimeline() {
  try {
    const r = await api.timeline(props.symbol, props.mode);
    timeline.value = r;
    updatedAt.value = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch { /* 网络抖动时保持上一帧 */ }
}

async function loadQuote() {
  try {
    const r = await api.quote(props.symbol, props.mode);
    quote.value = r?.quotes?.[props.symbol] || null;
  } catch { /* 网络抖动时保持上一帧 */ }
}

function refresh() {
  loadTimeline();
  loadQuote();
}

const quoteStats = computed(() => {
  const q = timeline.value?.quote || quote.value;
  if (!q || q.last == null) return null;
  const pc = timeline.value?.prev_close ?? q.prev_close;
  const chg = pc ? ((q.last - pc) / pc) * 100 : null;
  return { q, pc, chg };
});

// ---------------- K线：日/周/月/年四周期，切入 K线 tab 时懒加载
const period = ref<"D1" | "W1" | "M1" | "Y1">("D1");
const PERIODS = [
  { v: "D1", label: "日线" },
  { v: "W1", label: "周线" },
  { v: "M1", label: "月线" },
  { v: "Y1", label: "年线" },
] as const;
const bars = ref<any[]>([]);
const klineLoading = ref(false);
// 均线显隐与配色（与 KlineChart 内部绘制颜色一致）
const maVisible = ref<Record<string, boolean>>({ ma5: true, ma10: true, ma20: true, ma60: true });
const MA_META = [
  { k: "ma5", label: "MA5", color: "#f59e0b" },
  { k: "ma10", label: "MA10", color: "#a855f7" },
  { k: "ma20", label: "MA20", color: "#38bdf8" },
  { k: "ma60", label: "MA60", color: "#ec4899" },
];

function defaultStartFor(p: string) {
  // 各周期默认回看区间：日线半年、周线两年、月线八年、年线三十年
  const months: Record<string, number> = { D1: 6, W1: 24, M1: 96, Y1: 360 };
  const d = new Date();
  d.setMonth(d.getMonth() - (months[p] ?? 6));
  return d.toISOString().slice(0, 10);
}
const start = ref(defaultStartFor("D1"));

async function loadKline() {
  klineLoading.value = true;
  const r = await tryReq(() => api.kline(props.symbol, period.value, start.value, props.mode, undefined, 400, "QFQ"));
  bars.value = r?.rows || [];
  klineLoading.value = false;
}

function switchPeriod(p: "D1" | "W1" | "M1" | "Y1") {
  if (period.value === p) return;
  period.value = p;
  start.value = defaultStartFor(p);
  loadKline();
}

function switchTab(t: "quote" | "kline") {
  tab.value = t;
  if (t === "kline" && !bars.value.length) loadKline();
}

function fmtNum(v: any) {
  return v == null ? "-" : Number(v).toFixed(2);
}
function fmtVol(v: any) {
  if (v == null) return "-";
  if (v >= 1e8) return (v / 1e8).toFixed(2) + "亿";
  if (v >= 1e4) return (v / 1e4).toFixed(2) + "万";
  return String(Math.round(v));
}

onMounted(() => {
  refresh();
  pollTimer = setInterval(() => {
    if (tab.value === "quote" && !document.hidden) refresh();
  }, 5000);
});
onBeforeUnmount(() => {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
});
</script>

<template>
  <Modal :title="`${symbol}${name ? ' · ' + name : ''} 行情详情`" @close="emit('close')">
    <div class="sd-head">
      <button class="btn sm" :class="tab === 'quote' ? '' : 'ghost'" @click="switchTab('quote')">实时行情</button>
      <button class="btn sm" :class="tab === 'kline' ? '' : 'ghost'" @click="switchTab('kline')">K线图</button>
      <span v-if="tab === 'quote'" class="tiny muted" style="margin-left:8px">
        <template v-if="timeline?.live"><span class="badge ok" style="margin-right:6px">● 实时</span></template>
        <template v-else-if="timeline?.stale"><span class="badge warn" style="margin-right:6px">⚠ 非今日 · {{ timeline.date }}</span></template>
        <template v-else-if="timeline?.date"><span class="badge muted" style="margin-right:6px">收盘 · {{ timeline.date }}</span></template>
        <span v-if="updatedAt">{{ updatedAt }} 更新 · 5s 自动刷新</span>
      </span>
    </div>

    <!-- 实时行情：报价摘要 + 分时图 -->
    <template v-if="tab === 'quote'">
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
      <div v-else class="muted" style="padding:8px 0">暂无实时行情（数据源暂不支持实时报价，可稍后重试）</div>

      <TimelineChart :points="timeline?.points || []" :prev-close="timeline?.prev_close ?? null" style="height:300px; margin-top:8px" />
      <div class="tiny muted" style="margin-top:4px">
        滚轮缩放 · 拖拽平移 · 双击复位 · 悬停查看明细<span v-if="timeline?.note"> · {{ timeline.note }}</span>
      </div>
    </template>

    <!-- K线图：周期切换 + 均线显隐 -->
    <template v-else>
      <div class="sd-head">
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
          ><i class="dot"></i>{{ m.label }}</button>
        </div>
      </div>
      <div v-if="klineLoading && !bars.length" class="muted" style="padding:20px 0">加载中…</div>
      <div v-else-if="!bars.length" class="muted" style="padding:20px 0">该区间无K线数据</div>
      <KlineChart v-else :rows="bars" :mas="maVisible" :period="period" style="height:400px" />
      <div class="tiny muted" style="margin-top:4px">滚轮缩放 · 拖拽平移 · 双击复位 · 悬停查看明细 · 前复权</div>
    </template>

    <template #actions>
      <button class="btn ghost" @click="emit('close')">关闭</button>
    </template>
  </Modal>
</template>

<style scoped>
/* 弹窗加宽以容纳图表（默认 .modal 只有 640px） */
:deep(.modal) { width: min(1040px, 96vw); }
.sd-head {
  display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 10px;
}
.period-group { display: flex; gap: 4px; }
.ma-legend { display: flex; gap: 6px; flex-wrap: wrap; margin-left: auto; }
.ma-chip {
  display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px;
  border: 1px solid var(--border); border-radius: 999px; background: var(--bg-elev);
  color: var(--text-2); font-size: 12px; cursor: pointer; transition: all .15s;
}
.ma-chip .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ma-color); }
.ma-chip:hover { border-color: var(--ma-color); }
.ma-chip.off { opacity: .45; }
.ma-chip.off .dot { background: var(--text-2); }
.rt-summary { display: flex; align-items: flex-start; gap: 20px; flex-wrap: wrap; padding: 4px 0; }
.rt-last { font-size: 30px; font-weight: 800; line-height: 1.1; font-variant-numeric: tabular-nums; }
.rt-chg { align-self: center; font-size: 17px; font-weight: 700; font-variant-numeric: tabular-nums; }
.rt-grid {
  flex: 1; min-width: 280px; display: flex; flex-wrap: wrap; gap: 4px 24px;
  align-content: center; font-size: 13px;
}
.rt-item { display: inline-flex; align-items: baseline; white-space: nowrap; }
.rt-grid .k { color: var(--text-2); margin-right: 8px; }
.rt-grid .v { font-weight: 600; font-variant-numeric: tabular-nums; }
</style>
