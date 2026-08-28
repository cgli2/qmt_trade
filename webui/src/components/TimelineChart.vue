<script setup lang="ts">
// 分时图组件（纯 Canvas 实现，与 KlineChart 同一技术方案：无依赖、rAF 合并重绘、dpr 适配）。
// 能力：价格折线(涨红跌绿) + 均价线(VWAP) + 昨收虚线 + 底部成交量柱 + 左侧涨跌幅/右侧价格双轴。
// 交互：滚轮缩放（锚定光标）、拖拽平移、双击复位、悬停十字线提示，与 K 线图体验一致。
// 横轴固定 240 个分钟槽位（09:31-11:30 / 13:01-15:00），无数据槽位自然留白。
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useApp } from "@/store";

const props = defineProps<{
  points: any[];          // 分时点：t=HH:MM / s=槽位0..239 / p=价 / a=均价 / v=分钟成交量
  prevClose: number | null; // 昨收：涨跌幅与 Y 轴对称基准
}>();

const app = useApp();

// ---------------- 画布留白（左侧涨跌幅轴，右侧价格轴，底部时间轴）
const PAD_L = 50, PAD_R = 60, PAD_T = 14, PAD_B = 22;
const VOL_H = 58;          // 底部成交量区高度
const GAP = 8;             // 价格区与成交量区间隔
const SLOTS = 240;
const MIN_BARS = 12;       // 放大极限：最少可见槽位数
// 整点/半点时间标签所在槽位（09:31 为槽位 0，每 30 分钟 30 槽）
const TICKS = [0, 29, 59, 89, 119, 149, 179, 209, 239];
const AVG_COLOR = "#f59e0b";

const wrap = ref<HTMLDivElement>();
const cvs = ref<HTMLCanvasElement>();

// ---------------- 视窗状态（槽位区间，浮点支持平滑缩放平移）
const view = reactive({ start: 0, count: SLOTS });
const hoverSlot = ref<number | null>(null);
const hoverPos = ref<{ x: number; y: number } | null>(null);

function clamp(v: number, lo: number, hi: number) {
  return Math.min(Math.max(v, lo), hi);
}
function plotW() {
  return Math.max(10, (wrap.value?.clientWidth || 0) - PAD_L - PAD_R);
}
function totalH() {
  return Math.max(10, (wrap.value?.clientHeight || 0) - PAD_T - PAD_B);
}

function resetView() {
  view.start = 0;
  view.count = SLOTS;
}

// ---------------- 槽位 → 点 的映射（points 稀疏，按 s 建表）
const slotMap = computed(() => {
  const m: Record<number, any> = {};
  for (const pt of props.points || []) if (pt && pt.s != null) m[pt.s] = pt;
  return m;
});

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

  const map = slotMap.value;
  const pc = props.prevClose;
  // 主题色从 CSS 变量读取，明暗主题自动适配（涨红跌绿，A股习惯）
  const st = getComputedStyle(document.documentElement);
  const colGrid = st.getPropertyValue("--border").trim() || "#e2e5ee";
  const colText = st.getPropertyValue("--text-2").trim() || "#6b7180";
  const colUp = st.getPropertyValue("--danger").trim() || "#e0483b";
  const colDown = st.getPropertyValue("--ok").trim() || "#1f9d57";
  const pts = props.points || [];
  if (!pts.length) return;

  const pw = plotW(), th = totalH();
  const ph = Math.max(40, th - VOL_H - GAP);   // 价格区高度
  const s0 = Math.max(0, Math.floor(view.start));
  const s1 = Math.min(SLOTS, Math.ceil(view.start + view.count));

  // 可视区价格范围（含均价与昨收），围绕昨收对称展开（分时图惯例）
  let lo = Infinity, hi = -Infinity;
  for (let s = s0; s < s1; s++) {
    const p = map[s];
    if (!p) continue;
    if (p.p < lo) lo = p.p;
    if (p.p > hi) hi = p.p;
    if (p.a != null) { if (p.a < lo) lo = p.a; if (p.a > hi) hi = p.a; }
  }
  if (!isFinite(lo) || !isFinite(hi)) return;
  if (pc != null && pc > 0) {
    const dev = Math.max(Math.abs(hi - pc), Math.abs(pc - lo), pc * 0.002);
    lo = pc - dev * 1.08; hi = pc + dev * 1.08;
  } else {
    const padY = (hi - lo) * 0.08 || Math.abs(hi) * 0.02 || 1;
    lo -= padY; hi += padY;
  }

  const step = pw / view.count;
  const xOf = (s: number) => PAD_L + (s - view.start + 0.5) * step;
  const yOf = (p: number) => PAD_T + ((hi - p) / (hi - lo)) * ph;
  const volTop = PAD_T + ph + GAP;
  let maxV = 0;
  for (let s = s0; s < s1; s++) {
    const v = map[s]?.v;
    if (v != null && v > maxV) maxV = v;
  }

  ctx.font = "11px -apple-system, 'Segoe UI', sans-serif";
  ctx.textBaseline = "middle";

  // ---- 网格：右轴价格 + 左轴涨跌幅（围绕昨收对称，0% 即昨收位）
  for (let g = 0; g <= 4; g++) {
    const p = lo + ((hi - lo) * g) / 4;
    const y = yOf(p);
    ctx.strokeStyle = colGrid;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(PAD_L + pw, y); ctx.stroke();
    ctx.fillStyle = colText;
    ctx.textAlign = "left";
    ctx.fillText(p.toFixed(2), PAD_L + pw + 8, y);
    if (pc != null && pc > 0) {
      const pct = ((p - pc) / pc) * 100;
      ctx.textAlign = "right";
      ctx.fillStyle = pct >= 0 ? colUp : colDown;
      ctx.fillText(pct.toFixed(2) + "%", PAD_L - 6, y);
    }
  }
  // ---- 时间轴（整点/半点固定刻度，只画可视区内）
  ctx.textAlign = "center";
  ctx.fillStyle = colText;
  for (const s of TICKS) {
    if (s < s0 - 1 || s > s1) continue;
    const x = xOf(s);
    if (x < PAD_L - 4 || x > PAD_L + pw + 4) continue;
    const m = s < 120 ? 9 * 60 + 31 + s : 13 * 60 + 1 + (s - 120);
    ctx.fillText(`${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`, x, H - PAD_B / 2);
  }

  // ---- 昨收虚线（基准线）
  if (pc != null && pc > 0 && pc >= lo && pc <= hi) {
    ctx.strokeStyle = colText;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(PAD_L, yOf(pc)); ctx.lineTo(PAD_L + pw, yOf(pc)); ctx.stroke();
    ctx.setLineDash([]);
  }

  // ---- 成交量柱（对比昨收着色：红涨绿跌）
  ctx.save();
  ctx.beginPath(); ctx.rect(PAD_L, volTop, pw, VOL_H); ctx.clip();
  const bw = Math.max(1, Math.min(step * 0.7, 9));
  for (let s = s0; s < s1; s++) {
    const p = map[s];
    if (!p || p.v == null || p.v <= 0) continue;
    ctx.fillStyle = pc != null && p.p < pc ? colDown : colUp;
    const h = maxV > 0 ? (p.v / maxV) * (VOL_H - 2) : 0;
    ctx.fillRect(xOf(s) - bw / 2, volTop + VOL_H - h, bw, h);
  }
  ctx.restore();

  // ---- 价格线 + 渐变填充 + 均价线（裁剪到价格区）
  const lastPt = pts[pts.length - 1];
  const lineCol = pc != null && pc > 0
    ? (lastPt.p >= pc ? colUp : colDown)
    : (st.getPropertyValue("--primary").trim() || "#3478f6");
  ctx.save();
  ctx.beginPath(); ctx.rect(PAD_L, PAD_T, pw, ph); ctx.clip();
  const e0 = Math.max(s0 - 1, 0), e1 = Math.min(s1 + 1, SLOTS);
  // 按槽位连续性切段：缺失槽位处断开，避免跨缺口连成一条直线造成分时失真
  const segs: { x: number; y: number }[][] = [];
  let cur: { x: number; y: number }[] = [];
  let lastS = -2;
  for (let s = e0; s < e1; s++) {
    const p = map[s];
    if (!p) continue;
    if (cur.length && s - lastS > 1) { segs.push(cur); cur = []; }
    cur.push({ x: xOf(s), y: yOf(p.p) });
    lastS = s;
  }
  if (cur.length) segs.push(cur);
  if (segs.length) {
    const grad = ctx.createLinearGradient(0, PAD_T, 0, PAD_T + ph);
    grad.addColorStop(0, lineCol + "2e");
    grad.addColorStop(1, lineCol + "00");
    for (const seg of segs) {
      // 渐变填充
      ctx.save();
      ctx.beginPath();
      seg.forEach((pt, i) => (i ? ctx.lineTo(pt.x, pt.y) : ctx.moveTo(pt.x, pt.y)));
      ctx.lineTo(seg[seg.length - 1].x, PAD_T + ph); ctx.lineTo(seg[0].x, PAD_T + ph); ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.restore();
      // 价格折线
      ctx.beginPath();
      seg.forEach((pt, i) => (i ? ctx.lineTo(pt.x, pt.y) : ctx.moveTo(pt.x, pt.y)));
      ctx.strokeStyle = lineCol;
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    // 最新点标记（盘中实时点）
    const lx = xOf(lastPt.s), ly = yOf(lastPt.p);
    if (lx >= PAD_L && lx <= PAD_L + pw) {
      ctx.beginPath();
      ctx.arc(lx, ly, 3, 0, Math.PI * 2);
      ctx.fillStyle = lineCol;
      ctx.fill();
    }
  }
  // 均价线（VWAP，橙色；同样在槽位缺口处断开）
  ctx.beginPath();
  let avgStarted = false, lastAS = -2;
  for (let s = e0; s < e1; s++) {
    const p = map[s];
    if (!p || p.a == null) { avgStarted = false; continue; }
    const x = xOf(s), y = yOf(p.a);
    if (!avgStarted || s - lastAS > 1) { ctx.moveTo(x, y); avgStarted = true; } else ctx.lineTo(x, y);
    lastAS = s;
  }
  ctx.strokeStyle = AVG_COLOR;
  ctx.lineWidth = 1.2;
  ctx.stroke();
  ctx.restore();

  // ---- 悬停十字线 + 右轴价格气泡 + 左轴涨跌幅气泡
  const hv = hoverSlot.value;
  const hp = hv != null ? map[hv] : null;
  if (hv != null && hp && hv >= s0 && hv < s1) {
    const x = xOf(hv), y = yOf(hp.p);
    ctx.strokeStyle = colText;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(x, PAD_T); ctx.lineTo(x, PAD_T + ph); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(PAD_L + pw, y); ctx.stroke();
    ctx.setLineDash([]);
    const col = pc != null && hp.p < pc ? colDown : colUp;
    ctx.fillStyle = col;
    ctx.fillRect(PAD_L + pw + 3, y - 9, PAD_R - 6, 18);
    ctx.fillStyle = "#fff";
    ctx.textAlign = "left";
    ctx.fillText(Number(hp.p).toFixed(2), PAD_L + pw + 8, y);
    if (pc != null && pc > 0) {
      const pct = ((hp.p - pc) / pc) * 100;
      ctx.fillStyle = col;
      ctx.fillRect(2, y - 9, PAD_L - 5, 18);
      ctx.fillStyle = "#fff";
      ctx.textAlign = "right";
      ctx.fillText(pct.toFixed(2) + "%", PAD_L - 8, y);
    }
  }
}

// ---------------- 悬停明细提示框
const hoverInfo = computed(() => {
  const hv = hoverSlot.value;
  if (hv == null) return null;
  const p = slotMap.value[hv];
  if (!p) return null;
  const pc = props.prevClose;
  const chg = pc != null && pc > 0 ? ((p.p - pc) / pc) * 100 : null;
  return { p, chg };
});
const tipStyle = computed(() => {
  const hp = hoverPos.value;
  if (!hp || hoverSlot.value == null) return { display: "none" };
  const W = wrap.value?.clientWidth || 0;
  const H = wrap.value?.clientHeight || 0;
  const left = hp.x > W - 210 ? hp.x - 196 : hp.x + 14;
  const top = Math.max(4, Math.min(hp.y + 14, H - 140));
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
  const pts = props.points || [];
  if (!pts.length || mx < PAD_L || mx > PAD_L + plotW()) {
    hoverSlot.value = null;
    return;
  }
  const step = plotW() / view.count;
  hoverSlot.value = clamp(Math.floor(view.start + (mx - PAD_L) / step), 0, SLOTS - 1);
  hoverPos.value = { x: mx, y: my };
}

function onWheel(e: WheelEvent) {
  e.preventDefault();
  const pts = props.points || [];
  if (!pts.length || !cvs.value) return;
  const rect = cvs.value.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const frac = clamp((mx - PAD_L) / plotW(), 0, 1);
  const anchor = view.start + frac * view.count;       // 光标指向的槽位保持不动
  const factor = e.deltaY > 0 ? 1.18 : 1 / 1.18;
  const nc = clamp(view.count * factor, MIN_BARS, SLOTS);
  view.count = nc;
  view.start = clamp(anchor - frac * nc, 0, Math.max(0, SLOTS - nc));
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
    const step = plotW() / view.count;
    view.start = clamp(dragStart0 + (dragX - e.clientX) / step, 0, Math.max(0, SLOTS - view.count));
  }
  updateHover(mx, my);
  requestDraw();
}
function onMouseUp() { dragging = false; }
function onMouseLeave() {
  dragging = false;
  hoverSlot.value = null;
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

// 轮询只更新数据不复位视窗，避免用户缩放状态被刷新打断；切换标的(points 长度骤变)才复位
watch(() => props.points, (n, o) => {
  if (!o || !o.length || (n || []).length === 0) resetView();
  hoverSlot.value = null;
  requestDraw();
});
watch([() => props.prevClose, () => app.theme], () => requestDraw());
</script>

<template>
  <div ref="wrap" class="tl-wrap">
    <canvas ref="cvs" class="tl-cvs"></canvas>
    <div v-if="hoverInfo" class="tl-tip" :style="tipStyle">
      <div class="tt-time">{{ hoverInfo.p.t }}</div>
      <div class="tt-grid">
        <span class="k">价格</span>
        <span class="v" :class="(props.prevClose != null && hoverInfo.p.p < props.prevClose) ? 'down' : 'up'">
          <b>{{ Number(hoverInfo.p.p).toFixed(2) }}</b>
        </span>
        <span class="k">涨跌幅</span>
        <span class="v" :class="(hoverInfo.chg ?? 0) >= 0 ? 'up' : 'down'">
          {{ hoverInfo.chg == null ? "-" : hoverInfo.chg.toFixed(2) + "%" }}
        </span>
        <span class="k">均价</span>
        <span class="v avg">{{ hoverInfo.p.a == null ? "-" : Number(hoverInfo.p.a).toFixed(2) }}</span>
        <span class="k">分钟量</span><span class="v">{{ fmtVol(hoverInfo.p.v) }}</span>
      </div>
    </div>
    <div class="tl-legend">
      <span class="lg-price">价格</span>
      <span class="lg-avg">均价</span>
      <span class="lg-pc" v-if="prevClose != null">昨收 {{ Number(prevClose).toFixed(2) }}</span>
    </div>
    <div v-if="!(points || []).length" class="tl-empty">暂无分时数据</div>
  </div>
</template>

<style scoped>
.tl-wrap {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 300px;
}
.tl-cvs {
  display: block;
  width: 100%;
  height: 100%;
  cursor: crosshair;
  user-select: none;
}
.tl-tip {
  position: absolute;
  z-index: 10;
  pointer-events: none;
  min-width: 160px;
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow);
  padding: 8px 10px;
  font-size: 12px;
}
.tt-time {
  font-weight: 700;
  margin-bottom: 6px;
  font-variant-numeric: tabular-nums;
}
.tt-grid {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 2px 12px;
}
.tt-grid .k { color: var(--text-2); }
.tt-grid .v { text-align: right; font-variant-numeric: tabular-nums; }
.v.up { color: var(--danger); }
.v.down { color: var(--ok); }
.v.avg { color: #f59e0b; }
.tl-legend {
  position: absolute;
  top: 4px;
  left: 56px;
  display: flex;
  gap: 12px;
  font-size: 11px;
  color: var(--text-2);
  pointer-events: none;
}
.lg-price::before, .lg-avg::before {
  content: "";
  display: inline-block;
  width: 14px;
  height: 2px;
  margin-right: 4px;
  vertical-align: middle;
}
.lg-price::before { background: var(--primary); }
.lg-avg::before { background: #f59e0b; }
.tl-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-2);
  font-size: 13px;
}
</style>
