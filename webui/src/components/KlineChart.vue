<script setup lang="ts">
// K线蜡烛图组件（纯 Canvas 实现，无第三方依赖，与项目轻量前端风格一致）。
// 能力：蜡烛图(OHLC) + MA5/10/20/60 均线叠加 + 滚轮缩放/拖拽平移/双击复位/悬停十字线提示。
// 渲染性能：只绘制可视窗口内的K线，rAF 合并重绘，devicePixelRatio 适配高清屏。
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useApp } from "@/store";

const props = defineProps<{
  rows: any[];                 // K线行：date/open/high/low/close/volume/amount/ma5..ma60
  mas: Record<string, boolean>; // 均线显隐开关：ma5/ma10/ma20/ma60
  period: string;              // D1/W1/M1/Y1，决定横轴日期格式
}>();

const app = useApp();

// ---------------- 画布留白（右侧给价格轴，底部给日期轴）
const PAD_L = 10, PAD_R = 66, PAD_T = 14, PAD_B = 26;
const MIN_BARS = 8;            // 放大极限：最少可见K线数
const DEFAULT_SHOWN = 150;     // 打开/切换标的时默认展示最近根数

// 均线颜色（与 MarketView 图例保持一致）
const MA_KEYS = ["ma5", "ma10", "ma20", "ma60"];
const MA_COLORS: Record<string, string> = {
  ma5: "#f59e0b", ma10: "#a855f7", ma20: "#38bdf8", ma60: "#ec4899",
};

const wrap = ref<HTMLDivElement>();
const cvs = ref<HTMLCanvasElement>();

// ---------------- 视窗状态（浮点索引，支持平滑缩放平移）
const view = reactive({ start: 0, count: 0 });
const hoverIdx = ref<number | null>(null);
const hoverPos = ref<{ x: number; y: number } | null>(null);

function clamp(v: number, lo: number, hi: number) {
  return Math.min(Math.max(v, lo), hi);
}
function plotW() {
  return Math.max(10, (wrap.value?.clientWidth || 0) - PAD_L - PAD_R);
}
function plotH() {
  return Math.max(10, (wrap.value?.clientHeight || 0) - PAD_T - PAD_B);
}
// ---- 窗格几何：上方价格区 + 下方成交量区（共享同一视窗与横轴，缩放/平移/周期切换自动同步）
const VOL_FRAC = 0.22, VOL_GAP = 8;
function paneVolH() {
  return Math.max(46, Math.min(120, Math.round(plotH() * VOL_FRAC)));
}
function panePriceH() {
  return Math.max(10, plotH() - VOL_GAP - paneVolH());
}
function paneVolTop() {
  return PAD_T + panePriceH() + VOL_GAP;
}

function resetView() {
  const n = (props.rows || []).length;
  view.count = Math.min(n || 1, DEFAULT_SHOWN);
  view.start = Math.max(0, n - view.count);
}

// ---------------- rAF 合并重绘：交互高频事件只调度一次绘制
let raf = 0;
function requestDraw() {
  if (raf) return;
  raf = requestAnimationFrame(() => { raf = 0; draw(); });
}

function draw() {
  const el = wrap.value, c = cvs.value;
  if (!el || !c) return;
  const W = el.clientWidth, H = el.clientHeight;
  if (!W || !H) return;
  const dpr = window.devicePixelRatio || 1;
  if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) {
    c.width = Math.round(W * dpr);
    c.height = Math.round(H * dpr);
  }
  const ctx = c.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const rows = props.rows || [];
  // 主题色从 CSS 变量读取，明暗主题自动适配（涨红跌绿，A股习惯）
  const st = getComputedStyle(document.documentElement);
  const colGrid = st.getPropertyValue("--border").trim() || "#e2e5ee";
  const colText = st.getPropertyValue("--text-2").trim() || "#6b7180";
  const colUp = st.getPropertyValue("--danger").trim() || "#e0483b";
  const colDown = st.getPropertyValue("--ok").trim() || "#1f9d57";
  if (!rows.length) return;

  const pw = plotW(), ph = panePriceH();
  const i0 = Math.max(0, Math.floor(view.start));
  const i1 = Math.min(rows.length, Math.ceil(view.start + view.count));
  // 可视区价格范围（含启用的均线），上下留 6% 空隙
  let lo = Infinity, hi = -Infinity;
  for (let i = i0; i < i1; i++) {
    const r = rows[i];
    if (r.low < lo) lo = r.low;
    if (r.high > hi) hi = r.high;
    for (const k of MA_KEYS) {
      const v = props.mas[k] ? r[k] : null;
      if (v != null) { if (v < lo) lo = v; if (v > hi) hi = v; }
    }
  }
  if (!isFinite(lo) || !isFinite(hi)) return;
  const padY = (hi - lo) * 0.06 || Math.abs(hi) * 0.02 || 1;
  lo -= padY; hi += padY;

  const step = pw / view.count;
  const xOf = (i: number) => PAD_L + (i - view.start + 0.5) * step;
  const yOf = (p: number) => PAD_T + ((hi - p) / (hi - lo)) * ph;

  // ---- 网格与价格轴
  ctx.font = "11px -apple-system, 'Segoe UI', sans-serif";
  ctx.textBaseline = "middle";
  for (let g = 0; g <= 4; g++) {
    const p = lo + ((hi - lo) * g) / 4;
    const y = yOf(p);
    ctx.strokeStyle = colGrid;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(PAD_L + pw, y); ctx.stroke();
    ctx.fillStyle = colText;
    ctx.textAlign = "left";
    ctx.fillText(p.toFixed(2), PAD_L + pw + 8, y);
  }
  // ---- 日期轴（间隔按可视宽度自适应，跨多年时显示 YYYY-MM）
  const nLabels = Math.max(2, Math.floor(pw / 90));
  const stride = Math.max(1, Math.round(view.count / nLabels));
  ctx.textAlign = "center";
  for (let i = Math.ceil(i0 / stride) * stride; i < i1; i += stride) {
    ctx.fillStyle = colText;
    ctx.fillText(dateLabel(rows, i0, i1, i), xOf(i), H - PAD_B / 2);
  }

  // ---- 蜡烛与均线（裁剪到绘图区，防止越界）
  ctx.save();
  ctx.beginPath(); ctx.rect(PAD_L, PAD_T, pw, ph); ctx.clip();
  const bw = Math.max(1, Math.min(step * 0.62, 24));   // 蜡烛实体宽度
  for (let i = i0; i < i1; i++) {
    const r = rows[i];
    const col = r.close >= r.open ? colUp : colDown;
    const x = xOf(i);
    ctx.strokeStyle = col;
    ctx.fillStyle = col;
    ctx.lineWidth = Math.max(1, bw * 0.14);
    ctx.beginPath(); ctx.moveTo(x, yOf(r.high)); ctx.lineTo(x, yOf(r.low)); ctx.stroke();
    const yO = yOf(r.open), yC = yOf(r.close);
    ctx.fillRect(x - bw / 2, Math.min(yO, yC), bw, Math.max(Math.abs(yO - yC), 1));
  }
  ctx.lineWidth = 1.4;
  for (const k of MA_KEYS) {
    if (!props.mas[k]) continue;
    ctx.strokeStyle = MA_COLORS[k];
    ctx.beginPath();
    let started = false;
    const s = Math.max(0, i0 - 1), e = Math.min(rows.length, i1 + 1);
    for (let i = s; i < e; i++) {
      const v = rows[i][k];
      if (v == null) { started = false; continue; }   // 均线预热不足处断线
      const x = xOf(i), y = yOf(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
  ctx.restore();

  // ---- 成交量窗格：与K线同视窗同横轴（rows 自带 volume，周/月/年为周期内求和），
  // 涨跌配色与蜡烛一致；周期切换只改 rows，绘制自动随之变化
  const vh = paneVolH(), vtop = paneVolTop();
  ctx.strokeStyle = colGrid;
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(PAD_L, vtop - VOL_GAP / 2); ctx.lineTo(PAD_L + pw, vtop - VOL_GAP / 2); ctx.stroke();
  let vmax = 0;
  for (let i = i0; i < i1; i++) {
    const v = Number(rows[i].volume);
    if (v > vmax) vmax = v;
  }
  ctx.font = "11px -apple-system, 'Segoe UI', sans-serif";
  ctx.textBaseline = "middle";
  ctx.fillStyle = colText;
  ctx.textAlign = "left";
  ctx.fillText("成交量", PAD_L + 4, vtop + 7);
  if (vmax > 0) {
    ctx.textAlign = "left";
    ctx.fillText(fmtVol(vmax), PAD_L + pw + 8, vtop + 7);   // 右侧量轴：可视区峰值
    ctx.save();
    ctx.beginPath(); ctx.rect(PAD_L, vtop, pw, vh); ctx.clip();
    for (let i = i0; i < i1; i++) {
      const r = rows[i];
      const v = Number(r.volume) || 0;
      if (v <= 0) continue;
      const h = (v / vmax) * (vh - 14);                     // 顶部留白给峰值刻度
      ctx.fillStyle = r.close >= r.open ? colUp : colDown;
      ctx.globalAlpha = 0.85;
      ctx.fillRect(xOf(i) - bw / 2, vtop + vh - h, bw, h);
    }
    ctx.globalAlpha = 1;
    ctx.restore();
  }

  // ---- 悬停十字线 + 右侧收盘价气泡
  const hv = hoverIdx.value;
  if (hv != null && hv >= i0 && hv < i1) {
    const r = rows[hv];
    const x = xOf(hv), y = yOf(r.close);
    ctx.strokeStyle = colText;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(x, PAD_T); ctx.lineTo(x, paneVolTop() + paneVolH()); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(PAD_L + pw, y); ctx.stroke();
    ctx.setLineDash([]);
    const col = r.close >= r.open ? colUp : colDown;
    ctx.fillStyle = col;
    ctx.fillRect(PAD_L + pw + 3, y - 9, PAD_R - 6, 18);
    ctx.fillStyle = "#fff";
    ctx.textAlign = "left";
    ctx.fillText(Number(r.close).toFixed(2), PAD_L + pw + 8, y);
    // 悬停根对应的成交量值显示在量轴上
    const vv = Number(r.volume);
    if (vv > 0) {
      const vy = paneVolTop() + paneVolH() - 9;
      ctx.fillStyle = r.close >= r.open ? colUp : colDown;
      ctx.fillRect(PAD_L + pw + 3, vy - 9, PAD_R - 6, 18);
      ctx.fillStyle = "#fff";
      ctx.fillText(fmtVol(vv), PAD_L + pw + 8, vy);
    }
  }
}

function dateLabel(rows: any[], i0: number, i1: number, i: number) {
  const d = String(rows[i].date).slice(0, 10);
  if (props.period === "Y1") return d.slice(0, 4);
  if (props.period === "M1") return d.slice(0, 7);
  // 周/日线：可视区跨年才带年份，否则 MM-DD 更紧凑
  const y0 = String(rows[i0].date).slice(0, 4);
  const y1 = String(rows[Math.max(i0, i1 - 1)].date).slice(0, 4);
  return y0 !== y1 ? d.slice(0, 7) : d.slice(5);
}

// ---------------- 悬停明细提示框
const hoverInfo = computed(() => {
  if (hoverIdx.value == null) return null;
  const rows = props.rows || [];
  const r = rows[hoverIdx.value];
  if (!r) return null;
  const prev = rows[hoverIdx.value - 1];
  const chg = prev && prev.close ? ((r.close - prev.close) / prev.close) * 100 : null;
  return { r, chg };
});
const tipStyle = computed(() => {
  const hp = hoverPos.value;
  if (!hp || hoverIdx.value == null) return { display: "none" };
  const W = wrap.value?.clientWidth || 0;
  const H = wrap.value?.clientHeight || 0;
  const left = hp.x > W - 235 ? hp.x - 222 : hp.x + 14;
  const top = Math.max(4, Math.min(hp.y + 14, H - 200));
  return { left: left + "px", top: top + "px" };
});
function fmtVol(v: any) {
  if (v == null) return "-";
  if (v >= 1e8) return (v / 1e8).toFixed(2) + "亿";
  if (v >= 1e4) return (v / 1e4).toFixed(2) + "万";
  return String(Math.round(v));
}

// ---------------- 交互：滚轮缩放（锚定光标位置）、拖拽平移、双击复位
function updateHover(mx: number, my: number) {
  const rows = props.rows || [];
  if (!rows.length || mx < PAD_L || mx > PAD_L + plotW()) {
    hoverIdx.value = null;
    return;
  }
  const step = plotW() / view.count;
  hoverIdx.value = clamp(Math.floor(view.start + (mx - PAD_L) / step), 0, rows.length - 1);
  hoverPos.value = { x: mx, y: my };
}

function onWheel(e: WheelEvent) {
  e.preventDefault();
  const rows = props.rows || [];
  if (!rows.length || !cvs.value) return;
  const rect = cvs.value.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const frac = clamp((mx - PAD_L) / plotW(), 0, 1);
  const anchor = view.start + frac * view.count;       // 光标指向的K线索引保持不动
  const factor = e.deltaY > 0 ? 1.18 : 1 / 1.18;
  const nc = clamp(view.count * factor, MIN_BARS, rows.length);
  view.count = nc;
  view.start = clamp(anchor - frac * nc, 0, Math.max(0, rows.length - nc));
  updateHover(mx, e.clientY - rect.top);
  requestDraw();
}

let dragging = false, dragX = 0, dragStart0 = 0;
function onMouseDown(e: MouseEvent) {
  dragging = true;
  dragX = e.clientX;
  dragStart0 = view.start;
}
function onMouseMove(e: MouseEvent) {
  if (!cvs.value) return;
  const rect = cvs.value.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  if (dragging) {
    const rows = props.rows || [];
    const step = plotW() / view.count;
    view.start = clamp(dragStart0 + (dragX - e.clientX) / step, 0, Math.max(0, rows.length - view.count));
  }
  updateHover(mx, my);
  requestDraw();
}
function onMouseUp() { dragging = false; }
function onMouseLeave() {
  dragging = false;
  hoverIdx.value = null;
  hoverPos.value = null;
  requestDraw();
}
function onDblClick() { resetView(); requestDraw(); }

// ---------------- 生命周期与响应式重绘
let ro: ResizeObserver | null = null;
onMounted(() => {
  resetView();
  requestDraw();
  const c = cvs.value;
  if (c) {
    c.addEventListener("wheel", onWheel, { passive: false });
    c.addEventListener("mousedown", onMouseDown);
    c.addEventListener("mousemove", onMouseMove);
    c.addEventListener("mouseup", onMouseUp);
    c.addEventListener("mouseleave", onMouseLeave);
    c.addEventListener("dblclick", onDblClick);
  }
  ro = new ResizeObserver(() => requestDraw());
  if (wrap.value) ro.observe(wrap.value);
});
onBeforeUnmount(() => {
  const c = cvs.value;
  if (c) {
    c.removeEventListener("wheel", onWheel);
    c.removeEventListener("mousedown", onMouseDown);
    c.removeEventListener("mousemove", onMouseMove);
    c.removeEventListener("mouseup", onMouseUp);
    c.removeEventListener("mouseleave", onMouseLeave);
    c.removeEventListener("dblclick", onDblClick);
  }
  ro?.disconnect();
});

watch(() => props.rows, () => { resetView(); hoverIdx.value = null; requestDraw(); });
watch([() => props.mas, () => props.period, () => app.theme], () => requestDraw(), { deep: true });
</script>

<template>
  <div ref="wrap" class="kline-wrap">
    <canvas ref="cvs" class="kline-cvs"></canvas>
    <div v-if="hoverInfo" class="kline-tip" :style="tipStyle">
      <div class="kt-date">{{ String(hoverInfo.r.date).slice(0, 10) }}</div>
      <div class="kt-grid">
        <span class="k">开盘</span><span class="v">{{ Number(hoverInfo.r.open).toFixed(2) }}</span>
        <span class="k">最高</span><span class="v up">{{ Number(hoverInfo.r.high).toFixed(2) }}</span>
        <span class="k">最低</span><span class="v down">{{ Number(hoverInfo.r.low).toFixed(2) }}</span>
        <span class="k">收盘</span>
        <span class="v" :class="hoverInfo.r.close >= hoverInfo.r.open ? 'up' : 'down'">
          <b>{{ Number(hoverInfo.r.close).toFixed(2) }}</b>
        </span>
        <span class="k">涨跌幅</span>
        <span class="v" :class="(hoverInfo.chg ?? 0) >= 0 ? 'up' : 'down'">
          {{ hoverInfo.chg == null ? "-" : hoverInfo.chg.toFixed(2) + "%" }}
        </span>
        <span class="k">成交量</span><span class="v">{{ fmtVol(hoverInfo.r.volume) }}</span>
        <span class="k">成交额</span><span class="v">{{ fmtVol(hoverInfo.r.amount) }}</span>
      </div>
      <div class="kt-mas">
        <span v-for="k in MA_KEYS" :key="k" v-show="mas[k]" :style="{ color: MA_COLORS[k] }">
          {{ k.toUpperCase() }}: {{ hoverInfo.r[k] == null ? "-" : Number(hoverInfo.r[k]).toFixed(2) }}
        </span>
      </div>
    </div>
    <div v-if="!(rows || []).length" class="kline-empty">暂无数据</div>
  </div>
</template>

<style scoped>
.kline-wrap {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 340px;
}
.kline-cvs {
  display: block;
  width: 100%;
  height: 100%;
  cursor: crosshair;
  user-select: none;
}
.kline-tip {
  position: absolute;
  z-index: 10;
  pointer-events: none;
  min-width: 190px;
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow);
  padding: 8px 10px;
  font-size: 12px;
}
.kt-date {
  font-weight: 700;
  margin-bottom: 6px;
  font-variant-numeric: tabular-nums;
}
.kt-grid {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 2px 12px;
}
.kt-grid .k { color: var(--text-2); }
.kt-grid .v { text-align: right; font-variant-numeric: tabular-nums; }
.v.up { color: var(--danger); }
.v.down { color: var(--ok); }
.kt-mas {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  font-variant-numeric: tabular-nums;
}
.kline-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-2);
  font-size: 13px;
}
</style>
