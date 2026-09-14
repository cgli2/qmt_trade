<script setup lang="ts">
// 交易执行：模拟盘 / 实盘 双 Tab，各自独立账本、互不干扰。
// - 模拟盘(paper)：真实行情 + 模拟撮合，用于验证策略有效性；账本可随时重置。
// - 实盘(live)：直连券商(QMT)；下单/结算/对账与模拟盘同链路，均须穿过三道风控闸门。
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import api from "@/api";
import { useApp } from "@/store";
import { pushToast, tryReq } from "@/toast";
import Modal from "@/components/Modal.vue";
import SymbolSelect from "@/components/SymbolSelect.vue";
import SymbolDetailModal from "@/components/SymbolDetailModal.vue";

defineProps<{ mode: "paper" | "live" }>();
const app = useApp();
// 模式以全局 store 为准（响应式），切换时无需重建组件，避免整页卸载/重挂导致的卡顿
const props = reactive({ get mode(): "paper" | "live" { return app.mode; } });
const isLive = computed(() => props.mode === "live");

let requestGeneration = 0;
onUnmounted(() => { requestGeneration++; });
const loading = ref(false);
const positions = ref<any[]>([]);
const orders = ref<any[]>([]);
const intents = ref<any[]>([]);
const broker = ref<any>(null);
const recon = ref<any>(null);
const symbols = ref<any[]>([]);
const symbolsLoading = ref(false);
let symbolsInflight: Promise<void> | null = null;
const dateFilter = ref("");
const orderDlg = ref<any>(null);
const orderResult = ref<any>(null);
const planResult = ref<any>(null);
const showPlan = ref(false);
const symbolDlg = ref<{ symbol: string; name?: string } | null>(null);

// 点击持仓/订单/意图中的标的 → 弹窗查看实时行情与K线
function openSymbol(symbol?: string, name?: string) {
  if (!symbol) return;
  symbolDlg.value = { symbol, name };
}

async function load() {
  if (isLive.value) return loadLive();
  return loadPaper();
}

async function loadPaper() {
  const generation = ++requestGeneration;
  loading.value = true;
  // 注意：不在这里拉全量标的(symbols)——它只被「手动下单」弹窗用到，
  // 放进 Promise.all 会让每次切 tab 都阻塞在全市场标的请求上（1~5s）。
  // 标的改为弹窗打开时懒加载(ensureSymbols)，tab 内容即刻渲染。
  const [p, o, i] = await Promise.all([
    tryReq(() => api.positions("paper")),
    tryReq(() => api.orders("paper", dateFilter.value || undefined)),
    tryReq(() => api.intents("paper", dateFilter.value || undefined)),
  ]);
  if (generation !== requestGeneration) return;
  positions.value = p?.positions || [];
  orders.value = sortOrders(o?.orders || []);
  intents.value = i?.intents || [];
  loading.value = false;
}

async function loadLive() {
  const generation = ++requestGeneration;
  loading.value = true;
  // 同 loadPaper：全量标的(symbols)移出关键路径，改为下单弹窗懒加载，
  // 避免切到实盘 tab 时既要等券商探测、又要等全市场标的。
  const [b, o] = await Promise.all([
    // QMT 未连接时网关会重试连接（约 30s+），超时兜底避免页面一直转圈
    withTimeout(tryReq(() => api.broker("live")), 40000,
      { available: false, message: "券商查询超时：请确认 QMT 客户端已登录（连接重试中，可稍后刷新）" }),
    tryReq(() => api.orders("live", dateFilter.value || undefined)),
  ]);
  if (generation !== requestGeneration) return;
  broker.value = b || { available: false, message: "券商信息加载失败" };
  orders.value = sortOrders(o?.orders || []);
  loading.value = false;
}

function withTimeout<T>(p: Promise<T>, ms: number, fallback: T): Promise<T> {
  return Promise.race([
    p,
    new Promise<T>((resolve) => setTimeout(() => resolve(fallback), ms)),
  ]);
}

// 统一按委托时间倒序（最新在上），无论后端按日期正序还是按最近倒序返回
function sortOrders(rows: any[]) {
  return [...rows].sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
}

async function loadRecon() {
  loading.value = true;
  recon.value = await tryReq(() => api.reconcile(props.mode, dateFilter.value || undefined));
  loading.value = false;
}

async function ackRecon() {
  if (!confirm("确认人工签核本次对账差异？签核后系统将恢复正常交易权限。")) return;
  const r = await tryReq(
    () => api.reconcileAck({ trade_date: dateFilter.value || null, operator: "webui", note: "Web 控制台签核" }, props.mode),
    "对账已签核"
  );
  if (r) loadRecon();
}

// 全量标的仅「手动下单」弹窗的 SymbolSelect 需要，故懒加载 + 组件级缓存：
// 单实例组件跨 paper/live tab 复用同一份标的（universe 与模式无关），整个会话只拉一次。
async function ensureSymbols(): Promise<void> {
  if (symbols.value.length) return;              // 已加载
  if (symbolsInflight) return symbolsInflight;   // 进行中：复用同一 Promise，避免并发重复请求
  symbolsLoading.value = true;
  symbolsInflight = (async () => {
    try {
      const s = await tryReq(() => api.symbols(props.mode));
      symbols.value = s?.symbols || [];
    } finally {
      symbolsLoading.value = false;
      symbolsInflight = null;
    }
  })();
  return symbolsInflight;
}

async function openOrder() {
  orderDlg.value = { symbol: "", action: "BUY", shares: 0, price: null, confidence: 0.6, conviction: "MEDIUM", stop_loss_type: "percent", stop_loss_value: 0.05, reason: "" };
  orderResult.value = null;
  await ensureSymbols();   // 弹窗已即时打开，标的列表随后填充（后端 TTL 缓存，通常已就绪）
}

async function submitOrder() {
  const d = orderDlg.value;
  if (!d?.symbol) return pushToast("请选择标的", "err");
  // 手动下单走 POST /trade/intent：Intent 原样穿过三道风控闸门（仓位/风控/KillSwitch），
  // 与策略产出的 Intent 同链路；api.orders 是 GET 列表读接口，不能复用。
  const r = await tryReq(() => api.submitIntent({
    symbol: d.symbol, action: d.action, shares: Number(d.shares) || 0,
    price: d.price != null && d.price !== "" ? Number(d.price) : null,
    confidence: Number(d.confidence) || 0.6, conviction: d.conviction,
    stop_loss_type: d.stop_loss_type, stop_loss_value: Number(d.stop_loss_value) || 0.05,
    reason: d.reason || "手动下单",
  }, props.mode));
  if (r) { orderResult.value = r; load(); }
}

async function runPlan() {
  const r = await tryReq(() => api.runPlan(props.mode));
  if (r) { planResult.value = r; showPlan.value = true; load(); }
}

async function settle() {
  // 后端尚未提供 /trade/settle，api.settle 也不在客户端中；保留 typeof 守卫形态，
  // 未来在 api.ts 里补上 settle 方法后此处自动生效（否则总是提示“暂未开放”）。
  const maybe = (api as any).settle;
  if (typeof maybe !== 'function') {
    pushToast('盘后结算功能暂未开放', 'info');
    return;
  }
  const r = await tryReq(() => maybe(props.mode), "结算完成");
  if (r) load();
}

async function resetLedger() {
  if (!confirm("确认重置模拟账本？将清空所有模拟持仓与订单记录。")) return;
  // 对应后端 DELETE /trade/positions（仅 paper 允许，live 会直接 403）
  const r = await tryReq(() => api.positionsReset(props.mode), "模拟账本已重置");
  if (r) load();
}

function pnlColor(v: any) {
  const n = Number(v);
  if (Number.isNaN(n) || n === 0) return {};
  return { color: n > 0 ? "var(--danger)" : "var(--ok)" };
}

function fmtNum(v: any, digits = 2) {
  const n = Number(v);
  return v == null || Number.isNaN(n) ? "-" : n.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtTs(v: any) {
  const n = Number(v);
  if (!n) return String(v || "-").slice(0, 19);
  const d = new Date(n * 1000);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function killBadge(mode?: string) {
  if (mode === "NORMAL") return "ok";
  if (mode === "REDUCE_ONLY") return "warn";
  return "danger";
}

onMounted(() => { load(); ensureSymbols(); });   // ensureSymbols 后台预热标的，不阻塞 tab 渲染
watch(() => props.mode, () => { recon.value = null; symbolDlg.value = null; load(); });
</script>

<template>
  <div :class="{ loading }">
    <div class="tv-tabs">
      <router-link class="tv-tab" :class="{ active: !isLive }" to="/trade/paper">
        🧪 模拟盘<small>真实行情 · 模拟撮合 · 验证策略有效性</small>
      </router-link>
      <router-link class="tv-tab" :class="{ active: isLive }" to="/trade/live">
        🏦 实盘<small>真实券商账户 · QMT · 下单/结算/对账全链路</small>
      </router-link>
    </div>

    <!-- ============================ 模拟盘 ============================ -->
    <template v-if="!isLive">
      <div class="card">
        <h3>💹 模拟交易操作
          <span class="sub">LLM 只产出 TradeIntent（P1），实际下单必须经三道风控闸门；账本与实盘完全隔离</span>
        </h3>
        <div class="row tv-toolbar">
          <div class="tv-date"><label>交易日（留空=最近 50 条）</label><input v-model="dateFilter" type="date" /></div>
          <div><label>&nbsp;</label><button class="btn ghost" @click="loadPaper">刷新</button></div>
          <div><label>&nbsp;</label><button @click="runPlan">运行交易计划</button></div>
          <div><label>&nbsp;</label><button class="btn ghost" @click="openOrder">手动下单</button></div>
          <div><label>&nbsp;</label><button class="btn ghost" @click="settle">盘后结算</button></div>
          <div><label>&nbsp;</label><button class="btn ghost" @click="loadRecon">盘后对账</button></div>
          <div><label>&nbsp;</label><button class="btn ghost" style="color:var(--danger)" @click="resetLedger">重置模拟账本</button></div>
        </div>
      </div>

      <div class="card">
        <h3>📦 模拟持仓 <span class="sub">{{ positions.length }} 只 · 本地模拟账本</span></h3>
        <table>
          <thead><tr><th>标的</th><th>数量</th><th>可用</th><th>成本价</th><th>现价</th><th>市值</th><th>浮动盈亏</th><th>持有天数</th></tr></thead>
          <tbody>
            <tr v-for="(p, i) in positions" :key="i">
              <td><a class="sym-link" @click="openSymbol(p.symbol, p.name)"><b>{{ p.symbol }}</b></a> <span class="tiny muted">{{ p.name || "" }}</span></td>
              <td class="pill">{{ p.shares ?? p.volume ?? "-" }}</td>
              <td class="pill">{{ p.available ?? "-" }}</td>
              <td class="pill">{{ p.avg_cost != null ? Number(p.avg_cost).toFixed(3) : "-" }}</td>
              <td class="pill">{{ p.last_price != null ? Number(p.last_price).toFixed(2) : "-" }}</td>
              <td class="pill">{{ fmtNum(p.market_value, 0) }}</td>
              <td class="pill" :style="pnlColor(p.unrealized_pnl)">{{ fmtNum(p.unrealized_pnl) }}</td>
              <td class="pill tiny">{{ p.holding_days ?? "-" }}</td>
            </tr>
            <tr v-if="!positions.length"><td colspan="8" class="muted">空仓</td></tr>
          </tbody>
        </table>
      </div>

      <div class="grid cols-2">
        <div class="card">
          <h3>📝 订单 <span class="sub">{{ orders.length }} 条</span></h3>
          <div style="max-height:340px; overflow:auto">
            <table>
              <thead><tr><th>标的</th><th>名称</th><th>方向</th><th>数量</th><th>价格</th><th>状态</th><th>时间</th></tr></thead>
              <tbody>
                <tr v-for="(o, i) in orders" :key="i">
                  <td><a class="sym-link" @click="openSymbol(o.symbol)">{{ o.symbol }}</a></td>
                  <td class="tiny">{{ o.name || "-" }}</td>
                  <td><span class="badge" :class="String(o.side).includes('BUY') ? 'danger' : 'ok'">{{ o.side }}</span></td>
                  <td class="pill">{{ o.shares ?? o.volume }}</td>
                  <td class="pill">{{ o.price != null ? Number(o.price).toFixed(3) : "-" }}</td>
                  <td><span class="badge muted">{{ o.status }}</span></td>
                  <td class="tiny muted">{{ fmtTs(o.created_at || o.trade_date) }}</td>
                </tr>
                <tr v-if="!orders.length"><td colspan="7" class="muted">无订单</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="card">
          <h3>🧭 交易意图 TradeIntent <span class="sub">{{ intents.length }} 条 · LLM/因子产出</span></h3>
          <div style="max-height:340px; overflow:auto">
            <table>
              <thead><tr><th>标的</th><th>动作</th><th>置信</th><th>信念</th><th>理由</th></tr></thead>
              <tbody>
                <tr v-for="(t, i) in intents" :key="i">
                  <td><a class="sym-link" @click="openSymbol(t.symbol)">{{ t.symbol }}</a></td>
                  <td><span class="badge" :class="t.action === 'BUY' ? 'danger' : (t.action === 'SELL' ? 'ok' : 'muted')">{{ t.action }}</span></td>
                  <td class="pill">{{ t.confidence != null ? Number(t.confidence).toFixed(2) : "-" }}</td>
                  <td class="tiny">{{ t.conviction }}</td>
                  <td class="tiny muted" style="max-width:280px">{{ (t.reasoning || "").slice(0, 90) }}</td>
                </tr>
                <tr v-if="!intents.length"><td colspan="5" class="muted">无意图记录</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </template>

    <!-- ============================ 实盘 ============================ -->
    <template v-else>
      <div class="card">
        <h3>🏦 实盘交易操作
          <span class="sub">下单经 QMT 直达券商，仍需穿过三道风控闸门；结算/对账与模拟盘同链路，账本独立（trade_live.db）</span>
        </h3>
        <div class="row tv-toolbar">
          <div class="tv-date"><label>交易日（留空=最近 50 条）</label><input v-model="dateFilter" type="date" /></div>
          <div><label>&nbsp;</label><button class="btn ghost" @click="loadLive">刷新</button></div>
          <div><label>&nbsp;</label><button @click="openOrder">手动下单</button></div>
          <div><label>&nbsp;</label><button class="btn ghost" @click="settle">盘后结算</button></div>
          <div><label>&nbsp;</label><button class="btn ghost" @click="loadRecon">盘后对账</button></div>
        </div>
      </div>

      <div class="card">
        <h3>💰 实盘账户 <span class="sub">数据直连券商（QMT）</span>
          <span v-if="broker?.killswitch" class="badge" :class="killBadge(broker.killswitch)" style="margin-left:8px">
            KillSwitch: {{ broker.killswitch }}
          </span>
        </h3>

        <div v-if="!broker?.available" class="muted" style="padding:14px 0">
          ⚠️ {{ broker?.message || "券商信息加载中…" }}
        </div>
        <template v-else>
          <div class="grid cols-4" style="margin-bottom:14px">
            <div class="asset-tile"><div class="k">总资产</div><div class="v">{{ fmtNum(broker.asset?.total_asset) }}</div></div>
            <div class="asset-tile"><div class="k">可用现金</div><div class="v">{{ fmtNum(broker.asset?.cash) }}</div></div>
            <div class="asset-tile"><div class="k">冻结资金</div><div class="v">{{ fmtNum(broker.asset?.frozen_cash) }}</div></div>
            <div class="asset-tile"><div class="k">持仓市值</div><div class="v">{{ fmtNum(broker.asset?.market_value) }}</div></div>
          </div>

          <h3 style="margin-top:6px">📦 券商实时持仓 <span class="sub">{{ (broker.positions || []).length }} 只</span></h3>
          <table>
            <thead><tr><th>标的</th><th>数量</th><th>可用</th><th>成本价</th><th>现价</th><th>市值</th><th>浮动盈亏</th></tr></thead>
            <tbody>
              <tr v-for="(p, i) in broker.positions" :key="i">
                <td><a class="sym-link" @click="openSymbol(p.symbol, p.name)"><b>{{ p.symbol }}</b></a> <span class="tiny muted">{{ p.name || "" }}</span></td>
                <td class="pill">{{ p.volume }}</td>
                <td class="pill">{{ p.available ?? "-" }}</td>
                <td class="pill">{{ p.avg_cost != null ? Number(p.avg_cost).toFixed(3) : "-" }}</td>
                <td class="pill">{{ p.last_price != null ? Number(p.last_price).toFixed(2) : "-" }}</td>
                <td class="pill">{{ fmtNum(p.market_value, 0) }}</td>
                <td class="pill" :style="pnlColor(p.unrealized_pnl)">{{ fmtNum(p.unrealized_pnl) }}</td>
              </tr>
              <tr v-if="!(broker.positions || []).length"><td colspan="7" class="muted">空仓</td></tr>
            </tbody>
          </table>
        </template>
      </div>

      <div class="card">
        <h3>📝 实盘订单（本地账本） <span class="sub">{{ orders.length }} 条{{ dateFilter ? ` · ${dateFilter}` : " · 最近 50 条" }}</span></h3>
        <div style="max-height:340px; overflow:auto">
          <table>
            <thead><tr><th>标的</th><th>名称</th><th>方向</th><th>数量</th><th>价格</th><th>状态</th><th>时间</th></tr></thead>
            <tbody>
              <tr v-for="(o, i) in orders" :key="i">
                <td><a class="sym-link" @click="openSymbol(o.symbol)">{{ o.symbol }}</a></td>
                <td class="tiny">{{ o.name || "-" }}</td>
                <td><span class="badge" :class="String(o.side).includes('BUY') ? 'danger' : 'ok'">{{ o.side }}</span></td>
                <td class="pill">{{ o.shares ?? o.volume }}</td>
                <td class="pill">{{ o.price != null ? Number(o.price).toFixed(3) : "-" }}</td>
                <td><span class="badge muted">{{ o.status }}</span></td>
                <td class="tiny muted">{{ fmtTs(o.created_at || o.trade_date) }}</td>
              </tr>
              <tr v-if="!orders.length"><td colspan="7" class="muted">无订单</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <!-- ============================ 对账（两 Tab 共用） ============================ -->
    <div class="card" v-if="recon">
      <h3>🔍 盘后对账
        <span class="sub">本地账本 vs 券商持仓，差异未签核则强制 REDUCE_ONLY</span>
        <div class="spacer"></div>
        <button v-if="recon.available && !recon.passed" class="btn sm warn" @click="ackRecon">人工签核</button>
      </h3>
      <div v-if="!recon.available" class="muted">{{ recon.message }}</div>
      <template v-else>
        <div class="row" style="margin-bottom:10px">
          <div style="flex:0 0 auto">
            <span class="badge" :class="recon.passed ? 'ok' : 'danger'">{{ recon.passed ? "对账通过" : "存在差异" }}</span>
            <span class="tiny muted" style="margin-left:8px">检查 {{ recon.checked }} 项，差异 {{ (recon.discrepancies || []).length }} 项</span>
          </div>
        </div>
        <pre class="json">{{ recon.render }}</pre>
      </template>
    </div>

    <Modal v-if="orderDlg" :title="isLive ? '手动实盘下单（真实资金 · 走完整风控链路）' : '手动模拟下单（走完整风控链路）'" @close="orderDlg = null">
      <div class="row">
        <div class="field" style="flex:2"><label>标的 *</label>
          <SymbolSelect v-model="orderDlg.symbol" :options="symbols" :placeholder="symbolsLoading ? '标的列表加载中…' : '搜索 5500+ 标的'" />
        </div>
        <div class="field"><label>动作</label>
          <select v-model="orderDlg.action"><option>BUY</option><option>SELL</option></select>
        </div>
      </div>
      <div class="row">
        <div class="field"><label>股数（0=由仓位管理器计算）</label><input v-model="orderDlg.shares" type="number" step="100" /></div>
        <div class="field"><label>参考限价（留空=最新收盘）</label><input v-model="orderDlg.price" type="number" step="0.01" /></div>
      </div>
      <div class="row">
        <div class="field"><label>置信度 0~1</label><input v-model="orderDlg.confidence" type="number" step="0.05" min="0" max="1" /></div>
        <div class="field"><label>信念强度</label>
          <select v-model="orderDlg.conviction"><option>LOW</option><option>MEDIUM</option><option>HIGH</option></select>
        </div>
        <div class="field"><label>止损方式</label>
          <select v-model="orderDlg.stop_loss_type">
            <option value="percent">固定百分比</option>
            <option value="structure">结构止损</option>
          </select>
        </div>
        <div class="field"><label>止损值</label><input v-model="orderDlg.stop_loss_value" type="number" step="0.01" /></div>
      </div>
      <div class="field"><label>理由</label><input v-model="orderDlg.reason" /></div>

      <div v-if="orderResult" style="margin-top:10px">
        <span class="badge" :class="orderResult.ok ? 'ok' : 'danger'">{{ orderResult.ok ? "已成交" : "被拦截" }}</span>
        <span class="tiny muted" style="margin-left:8px">
          {{ orderResult.symbol }} {{ orderResult.action }} {{ orderResult.shares }} 股
          <template v-if="orderResult.rejected_by"> · 拦截层 {{ orderResult.rejected_by }}</template>
        </span>
        <pre class="json" style="margin-top:8px">{{ JSON.stringify(orderResult, null, 2) }}</pre>
      </div>

      <template #actions>
        <button class="btn ghost" @click="orderDlg = null">关闭</button>
        <button @click="submitOrder">提交</button>
      </template>
    </Modal>

    <Modal v-if="showPlan" title="交易计划执行结果" @close="showPlan = false">
      <pre class="json">{{ planResult?.rendered || JSON.stringify(planResult, null, 2) }}</pre>
      <template #actions><button class="btn ghost" @click="showPlan = false">关闭</button></template>
    </Modal>

    <SymbolDetailModal
      v-if="symbolDlg"
      :symbol="symbolDlg.symbol"
      :name="symbolDlg.name"
      :mode="props.mode"
      @close="symbolDlg = null"
    />
  </div>
</template>

<style scoped>
.tv-tabs { display: flex; gap: 0; margin-bottom: 20px; border-bottom: 2px solid var(--border); }
.tv-tab {
  flex: 0 0 auto; padding: 12px 24px; border: none; border-bottom: 3px solid transparent;
  background: transparent; font-weight: 600; font-size: 15px; transition: all .15s;
  position: relative; margin-bottom: -2px;
}
.tv-tab small { display: block; color: var(--text-2); font-weight: 400; font-size: 12px; margin-top: 4px; }
.tv-tab:hover { color: var(--primary); }
.tv-tab.active { 
  color: var(--primary); 
  border-bottom-color: var(--primary); 
  background: linear-gradient(to bottom, transparent, color-mix(in srgb, var(--primary) 5%, transparent));
}
.asset-tile { background: var(--bg-2); border-radius: 10px; padding: 12px 14px; }
.asset-tile .k { color: var(--text-2); font-size: 12px; margin-bottom: 4px; }
.asset-tile .v { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }
/* 工具栏：所有项按内容自适应，不拉伸占满；日期控件定宽，避免撑满整行 */
.tv-toolbar > * { flex: 0 0 auto; min-width: 0; }
.tv-toolbar .tv-date { flex: 0 0 200px; }
.tv-toolbar button { white-space: nowrap; }
/* 表格中的标的代码：可点击打开行情详情弹窗 */
.sym-link {
  color: var(--primary); cursor: pointer; text-decoration: none;
  font-family: ui-monospace, monospace;
}
.sym-link:hover { text-decoration: underline; }
</style>
