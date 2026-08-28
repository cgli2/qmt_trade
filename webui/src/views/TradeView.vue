<script setup lang="ts">
// 交易执行：模拟盘 / 实盘 双 Tab，各自独立账本、互不干扰。
// - 模拟盘(paper)：真实行情 + 模拟撮合，用于验证策略有效性；账本可随时重置。
// - 实盘(live)：直连券商(QMT)；下单/结算/对账与模拟盘同链路，均须穿过三道风控闸门。
import { computed, onMounted, ref, watch } from "vue";
import api from "@/api";
import { pushToast, tryReq } from "@/toast";
import Modal from "@/components/Modal.vue";
import SymbolSelect from "@/components/SymbolSelect.vue";
import SymbolDetailModal from "@/components/SymbolDetailModal.vue";

const props = defineProps<{ mode: "paper" | "live" }>();
const isLive = computed(() => props.mode === "live");

const loading = ref(false);
const positions = ref<any[]>([]);
const orders = ref<any[]>([]);
const intents = ref<any[]>([]);
const broker = ref<any>(null);
const recon = ref<any>(null);
const symbols = ref<any[]>([]);
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
  loading.value = true;
  const [p, o, i, s] = await Promise.all([
    tryReq(() => api.positions("paper")),
    tryReq(() => api.orders("paper", dateFilter.value || undefined)),
    tryReq(() => api.intents("paper", dateFilter.value || undefined)),
    tryReq(() => api.symbols("paper")),
  ]);
  positions.value = p?.positions || [];
  orders.value = sortOrders(o?.orders || []);
  intents.value = i?.intents || [];
  symbols.value = s?.symbols || [];
  loading.value = false;
}

async function loadLive() {
  loading.value = true;
  const [b, o, s] = await Promise.all([
    // QMT 未连接时网关会重试连接（约 30s+），超时兜底避免页面一直转圈
    withTimeout(tryReq(() => api.broker("live")), 40000,
      { available: false, message: "券商查询超时：请确认 QMT 客户端已登录（连接重试中，可稍后刷新）" }),
    tryReq(() => api.orders("live", dateFilter.value || undefined)),
    tryReq(() => api.symbols("live")),
  ]);
  broker.value = b || { available: false, message: "券商信息加载失败" };
  orders.value = sortOrders(o?.orders || []);
  symbols.value = s?.symbols || [];
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

async function resetLedger() {
  if (!confirm("确认重置模拟盘账本？\n将清空全部模拟持仓，账户现金回到初始资金；订单/意图记录保留作历史留痕。")) return;
  const r = await tryReq(() => api.positionsReset("paper"), "模拟账本已重置");
  if (r) { recon.value = null; loadPaper(); }
}

function openOrder() {
  orderResult.value = null;
  orderDlg.value = {
    symbol: symbols.value[0]?.symbol || "", action: "BUY", shares: 0, price: null,
    confidence: 0.6, conviction: "MEDIUM",
    reason: isLive.value ? "Web 控制台手动实盘下单" : "Web 控制台手动模拟下单",
    stop_loss_type: "percent", stop_loss_value: 0.08,
  };
}

async function submitOrder() {
  const o = orderDlg.value;
  if (!o.symbol) { pushToast("请选择标的", "err"); return; }
  if (isLive.value && !confirm(
    `⚠️ 实盘真实下单确认\n\n${o.action === "BUY" ? "买入" : "卖出"} ${o.symbol} ${Number(o.shares) || "（由仓位管理器计算）"} 股\n\n订单将经 QMT 发送到券商账户（仍需通过三道风控闸门）。确认执行？`
  )) return;
  loading.value = true;
  const r = await tryReq(() => api.submitIntent({
    ...o,
    shares: Number(o.shares) || 0,
    price: o.price ? Number(o.price) : null,
    confidence: Number(o.confidence),
    stop_loss_value: Number(o.stop_loss_value),
  }, props.mode));
  loading.value = false;
  if (r) {
    orderResult.value = r;
    pushToast(r.ok ? "订单已通过风控并成交" : `被拦截：${r.rejected_by || r.reason}`, r.ok ? "ok" : "err");
    load();
  }
}

// 盘后结算 = review 任务：收盘权益入账(record_equity) + 账本落库 + 复盘。
// 后端为异步任务，这里提交后轮询 /jobs/{id} 直到终态。
async function settle() {
  const tag = isLive.value ? "实盘" : "模拟盘";
  if (!confirm(`确认对${tag}执行盘后结算？\n将记录当日收盘权益、刷新账本并生成复盘。`)) return;
  loading.value = true;
  const r = await tryReq(() => api.strategyReview(props.mode, dateFilter.value || undefined));
  loading.value = false;
  if (!r?.job_id) return;
  pushToast(`结算任务已提交（${r.job_id}），后台运行中…`, "ok");
  let tries = 0;
  const timer = setInterval(async () => {
    tries += 1;
    const j = await tryReq(() => api.job(r.job_id));
    if (!j || tries > 150) { clearInterval(timer); return; }
    if (j.status === "done") {
      clearInterval(timer);
      pushToast("盘后结算完成", "ok");
      load();
    } else if (j.status === "error") {
      clearInterval(timer);
      pushToast(`结算失败：${j.error}`, "err");
    }
  }, 2000);
}

async function runPlan() {
  loading.value = true;
  const r = await tryReq(() => api.runPlan("paper", dateFilter.value || undefined));
  loading.value = false;
  if (r) { planResult.value = r; showPlan.value = true; loadPaper(); }
}

function pnlColor(v: any) {
  const n = Number(v);
  if (!n) return "";
  return n > 0 ? "color:var(--danger)" : "color:var(--ok)";
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

onMounted(load);
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
              <thead><tr><th>标的</th><th>方向</th><th>数量</th><th>价格</th><th>状态</th><th>时间</th></tr></thead>
              <tbody>
                <tr v-for="(o, i) in orders" :key="i">
                  <td><a class="sym-link" @click="openSymbol(o.symbol)">{{ o.symbol }}</a></td>
                  <td><span class="badge" :class="String(o.side).includes('BUY') ? 'danger' : 'ok'">{{ o.side }}</span></td>
                  <td class="pill">{{ o.shares ?? o.volume }}</td>
                  <td class="pill">{{ o.price != null ? Number(o.price).toFixed(3) : "-" }}</td>
                  <td><span class="badge muted">{{ o.status }}</span></td>
                  <td class="tiny muted">{{ fmtTs(o.created_at || o.trade_date) }}</td>
                </tr>
                <tr v-if="!orders.length"><td colspan="6" class="muted">无订单</td></tr>
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
            <thead><tr><th>标的</th><th>方向</th><th>数量</th><th>价格</th><th>状态</th><th>时间</th></tr></thead>
            <tbody>
              <tr v-for="(o, i) in orders" :key="i">
                <td><a class="sym-link" @click="openSymbol(o.symbol)">{{ o.symbol }}</a></td>
                <td><span class="badge" :class="String(o.side).includes('BUY') ? 'danger' : 'ok'">{{ o.side }}</span></td>
                <td class="pill">{{ o.shares ?? o.volume }}</td>
                <td class="pill">{{ o.price != null ? Number(o.price).toFixed(3) : "-" }}</td>
                <td><span class="badge muted">{{ o.status }}</span></td>
                <td class="tiny muted">{{ fmtTs(o.created_at || o.trade_date) }}</td>
              </tr>
              <tr v-if="!orders.length"><td colspan="6" class="muted">无订单</td></tr>
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
          <SymbolSelect v-model="orderDlg.symbol" :options="symbols" placeholder="搜索 5500+ 标的" />
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
            <option value="percent">固定百分比</option><option value="structure">结构止损</option>
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
.tv-tabs { display: flex; gap: 12px; margin-bottom: 16px; }
.tv-tab {
  flex: 1; padding: 12px 16px; border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--bg-elev); font-weight: 600; font-size: 14px; transition: all .15s;
}
.tv-tab small { display: block; color: var(--text-2); font-weight: 400; font-size: 12px; margin-top: 3px; }
.tv-tab:hover { border-color: var(--primary); }
.tv-tab.active { border-color: var(--primary); background: color-mix(in srgb, var(--primary) 9%, var(--bg-elev)); }
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
