<script setup lang="ts">
// 选股研判：一页看全「策略 → 漏斗候选池 → 多 Agent 最终精选(3~5只，含理由/投票/辩论)」，
// 并提供一键选股、策略切换、AI 研判、重点研究清单管理、跳转行情/交易的快捷入口。
import { onMounted, ref, reactive, watch, computed } from "vue";
import { useRouter } from "vue-router";
import api from "@/api";
import { useApp } from "@/store";
import { pushToast, tryReq } from "@/toast";

const app = useApp();
const router = useRouter();

// ---------- 最终精选（多 Agent 投票 + 辩论） ----------
const finals = ref<any[]>([]);
const finalAsOf = ref<string | null>(null);
const finalNote = ref("");
const finalDate = ref(""); // 空=最近一次；填日期=查历史
const checked = reactive<Record<string, boolean>>({}); // 重点研究勾选态

// ---------- 候选池（选股漏斗 Top N） ----------
const picks = ref<any[]>([]);
const picksAsOf = ref<string | null>(null);
const picksRegime = ref("");
const picksNote = ref("");
const pickSearch = ref("");
const funnel = ref<any[]>([]); // L0 漏斗阶梯
const funnelNIn = ref(0);

// ---------- 策略预设 ----------
const strategies = ref<any[]>([]);
const selectedStrategy = ref(""); // 空=默认均衡

// ---------- 重点研究清单 ----------
const watchlist = ref<string[]>([]);

// ---------- 操作状态 ----------
const runningSel = ref(false);
const selProgress = ref("");
const researching = ref(false);
const researchProgress = ref("");

async function loadFinal() {
  const r = await tryReq(() => api.selectionFinal(app.mode, finalDate.value || undefined));
  finals.value = r?.picks || [];
  finalAsOf.value = r?.asof || null;
  finalNote.value = r?.note || "";
  // 勾选态：已入选重点研究清单的默认勾上
  for (const p of finals.value) if (watchlist.value.includes(p.symbol)) checked[p.symbol] = true;
}

async function loadPicks() {
  const r = await tryReq(() => api.selectionPicks(app.mode));
  picks.value = r?.picks || [];
  picksAsOf.value = r?.asof || null;
  picksRegime.value = r?.regime || "";
  picksNote.value = r?.note || "";
  funnel.value = Array.isArray(r?.funnel) ? r.funnel : [];
  funnelNIn.value = funnel.value[0]?.before || funnel.value.reduce((m: number, s: any) => Math.max(m, s.before || 0), 0) || 0;
}

async function loadStrategies() {
  const r = await tryReq(() => api.selectionStrategies(app.mode));
  strategies.value = r?.strategies || [];
  // 后端清单已含默认策略（balanced，default=true）；若尚未选中则默认选中它，
  // 避免前端再另加一个同名“默认”项导致下拉出现两个“均衡多因子（默认）”。
  if (!selectedStrategy.value && strategies.value.length) {
    selectedStrategy.value = (strategies.value.find((s: any) => s.default) || strategies.value[0]).id;
  }
}

async function loadWatchlist() {
  const r = await tryReq(() => api.selectionWatchlist(app.mode));
  watchlist.value = r?.symbols || [];
  for (const p of finals.value) if (watchlist.value.includes(p.symbol)) checked[p.symbol] = true;
}

async function runSelection() {
  if (runningSel.value) return;
  runningSel.value = true;
  selProgress.value = "选股中（拉取行情/因子，可能数分钟）…";
  const body: any = {};
  if (selectedStrategy.value) body.strategy = selectedStrategy.value;
  const r = await tryReq(() => api.selectionRun(body, app.mode));
  if (!r?.job_id) { runningSel.value = false; selProgress.value = ""; return; }
  for (let i = 0; i < 80; i++) {
    await new Promise((res) => setTimeout(res, 3000));
    const j = await tryReq(() => api.job(r.job_id));
    if (j === undefined) {
      // 404：后端重启后内存 Job 丢失，停止轮询避免反复弹错
      runningSel.value = false; selProgress.value = "";
      pushToast("选股任务记录已失效（后端可能重启过），请刷新查看结果", "info");
      return;
    }
    if (j?.status === "done") {
      runningSel.value = false; selProgress.value = "";
      await loadPicks();
      pushToast(`选股完成，候选池 ${picks.value.length} 只（${r.regime || picksRegime.value}）`, "ok");
      return;
    }
    if (j?.status === "error") {
      runningSel.value = false; selProgress.value = "";
      pushToast("选股失败：" + (j.error || "未知错误"), "err");
      return;
    }
    selProgress.value = `选股中…（${i * 3 + 3}s）`;
  }
  runningSel.value = false; selProgress.value = "";
  pushToast("选股超时，请稍后刷新查看", "info");
}

async function runResearch() {
  if (researching.value) return;
  researching.value = true;
  researchProgress.value = "AI 研判启动中（多 Agent 推理，可能需半小时以上）…";
  const r = await tryReq(() => api.selectionResearch(app.mode));
  if (!r?.job_id) { researching.value = false; researchProgress.value = ""; return; }
  for (let i = 0; i < 720; i++) {
    await new Promise((res) => setTimeout(res, 5000));
    const j = await tryReq(() => api.job(r.job_id));
    if (j === undefined) {
      // 404：后端重启后内存 Job 丢失，停止轮询避免反复弹错
      researching.value = false; researchProgress.value = "";
      pushToast("研判任务记录已失效（后端可能重启过），请刷新查看精选", "info");
      return;
    }
    if (j?.status === "done") {
      researching.value = false; researchProgress.value = "";
      if (j.result?.skipped) {
        pushToast("研判未执行：" + (j.result.reason || "未知原因"), "info");
        return;
      }
      await loadFinal();
      await loadWatchlist();
      pushToast(finals.value.length
        ? `研判完成，精选 ${finals.value.length} 只（LLM 调用 ${j.result?.llm_calls ?? "-"} 次）`
        : "研判完成，未产出精选（宁缺毋滥）", finals.value.length ? "ok" : "info");
      return;
    }
    if (j?.status === "error") {
      researching.value = false; researchProgress.value = "";
      pushToast("研判失败：" + (j.error || "未知错误"), "err");
      return;
    }
    const sec = i * 5 + 5;
    researchProgress.value = `多 Agent 分析投票中…（${Math.floor(sec / 60)}分${sec % 60}秒，深度推理模型较慢）`;
  }
  researching.value = false; researchProgress.value = "";
  pushToast("研判超时，请稍后刷新查看精选", "info");
}

function goMarket(sym: string) {
  router.push({ path: "/market", query: { sym } });
}

// ---------------- 重点研究清单 ----------------
function isWatched(sym: string) { return watchlist.value.includes(sym); }
function checkedCount() { return Object.values(checked).filter(Boolean).length; }

async function addCheckedToWatch() {
  const add = Object.keys(checked).filter((s) => checked[s]);
  if (!add.length) { pushToast("请先勾选要加入重点研究的标的", "info"); return; }
  const r = await tryReq(() => api.selectionWatchlistAdd(add, app.mode));
  if (!r) return; // tryReq 已弹错误提示，失败时不再报“加入成功”
  watchlist.value = r.symbols || [];
  pushToast(`已加入重点研究 ${add.length} 只（共 ${watchlist.value.length} 只）`, "ok");
}
async function toggleWatch(sym: string) {
  if (isWatched(sym)) {
    const r = await tryReq(() => api.selectionWatchlistDel([sym], app.mode));
    watchlist.value = r?.symbols || watchlist.value;
  } else {
    const r = await tryReq(() => api.selectionWatchlistAdd([sym], app.mode));
    watchlist.value = r?.symbols || watchlist.value;
  }
}
async function removeWatch(sym: string) {
  const r = await tryReq(() => api.selectionWatchlistDel([sym], app.mode));
  watchlist.value = r?.symbols || watchlist.value;
  checked[sym] = false;
}

// ---------------- 漏斗阶梯 ----------------
function funnelWidth(after: number) {
  if (!funnelNIn.value) return 0;
  // 立方缩放：硬过滤每级淘汰比例小，线性宽度几乎无收窄、看不出漏斗形；
  // 用 p^3 放大层级差异（条内数字仍为真实留存量，悬浮可见原始 before→after）。
  const p = after / funnelNIn.value;
  return Math.max(8, Math.round(p ** 3 * 100));
}
function funnelRemovedPct(stage: any) {
  if (!stage.before) return 0;
  return Math.round((stage.removed / stage.before) * 100);
}
// 漏斗末端：候选池规模（最后一级 L0 硬过滤的留存量），随后进入 L1 打分选 Top N
const poolSize = computed(() => (funnel.value.length ? funnel.value[funnel.value.length - 1]?.after || 0 : 0));

const filteredPicks = ref<any[]>([]);
watch([picks, pickSearch], () => {
  const kw = pickSearch.value.trim().toLowerCase();
  filteredPicks.value = kw
    ? picks.value.filter((p: any) =>
        String(p.symbol).toLowerCase().includes(kw) ||
        String(p.industry || "").toLowerCase().includes(kw))
    : picks.value;
}, { immediate: true });

function actionClass(a: string) {
  if (a === "buy") return "ok";
  if (a === "sell") return "danger";
  if (a === "hold" || a === "watch") return "info";
  return "muted";
}
const ACTION_LABEL: Record<string, string> = {
  buy: "买入", sell: "卖出", hold: "持有", watch: "观察",
};
function convictionClass(v: string) {
  if (v === "high") return "ok";
  if (v === "low") return "danger";
  return "info";
}
const CONVICTION_LABEL: Record<string, string> = { high: "高确信", mid: "中确信", low: "低确信" };

// ---------- 投票可读化翻译 ----------
const AGENT_LABEL: Record<string, string> = {
  technical: "技术面", fundamental: "基本面", moneyflow: "资金面", sentiment: "市场情绪",
  research_manager: "研究主管", portfolio_manager: "组合经理", risk_officer: "风控官",
};
const VERDICT_LABEL: Record<string, string> = {
  BULL: "看多", BEAR: "看空", NEUTRAL: "中性", BUY: "看多", SELL: "看空", HOLD: "持有",
};
const FACTOR_CN: Record<string, string> = {
  close_price: "最新收盘价", close: "最新收盘价", price: "最新收盘价", entry: "买入价",
  high_60d: "60日最高价", score_percentile: "综合分全市场分位",
  ma_bullish_score: "均线多头排列分", ma_align: "均线多头排列分",
  ep_ratio: "盈利收益率", earnings_yield: "盈利收益率",
  debt_safety_score: "偿债安全分", debt_safety: "偿债安全分",
  missing_fields_count: "数据缺失字段数", days_between: "间隔天数",
  regime: "市场状态", RISK_OFF: "避险状态", TREND_DOWN: "下行趋势", TREND_UP: "上行趋势",
  revenue_yoy: "营收同比", profit_yoy: "净利同比", net_profit_yoy: "净利同比",
  roe: "ROE", gross_margin: "毛利率", turnover_rate: "换手率",
  ret_20d: "20日涨跌幅", ret_60d: "60日涨跌幅", bias_20: "20日乖离率",
  atr_ratio: "ATR占价比", downside_vol: "下行波动率", max_drawdown_60: "60日最大回撤",
  breakout_60: "距60日高点比", close_ratio_60d_high: "价格相对60日高点比",
  distance_from_60d_high: "距60日高点距离",
  main_net_5d: "近5日主力净流入", main_net_10d: "近10日主力净流入",
  main_net_ratio: "主力净流入占成交比", large_order_ratio: "大单占比",
  flow_consistency: "资金流一致性", news_sentiment_5d: "近5日新闻情绪",
  news_heat_5d: "近5日新闻热度", event_sentiment_20d: "近20日事件情绪",
  industry_momentum: "行业动量",
  hard_negative_event: "严重负面事件", hard_negative_flag: "严重负面事件标记",
};
const _FACTOR_RE: [RegExp, string][] = Object.keys(FACTOR_CN)
  .sort((a, b) => b.length - a.length)
  .map((k) => [new RegExp("\\b" + k + "\\b", "g"), FACTOR_CN[k]]);

function fmtVerdict(v: any): { text: string; cls: string; title: string } {
  const m = String(v).match(/^([A-Za-z_]+):([\d.]+)/);
  const stance = (m ? m[1] : String(v)).toUpperCase();
  const score = m ? Number(m[2]) : NaN;
  const label = VERDICT_LABEL[stance] || String(v);
  const pct = isFinite(score) ? " " + (score * 100).toFixed(0) + "%" : "";
  const cls = ["BULL", "BUY"].includes(stance) ? "ok"
    : ["BEAR", "SELL"].includes(stance) ? "danger" : "muted";
  return {
    text: label + pct, cls,
    title: isFinite(score)
      ? `看多倾向 ${(score * 100).toFixed(0)}%（≥60% 看多、≤40% 看空）`
      : String(v),
  };
}
function agentVotes(votes: any) {
  return Object.entries(votes || {})
    .filter(([k]) => !["risk_stop_pct", "invalidation", "invalidation_checks"].includes(k))
    .map(([k, v]) => ({ key: k, name: AGENT_LABEL[k] || k, ...fmtVerdict(v) }));
}
function stopLabel(votes: any): string {
  const n = parseFloat(String(votes?.risk_stop_pct ?? ""));
  return isFinite(n) ? `建议止损 -${(n * 100).toFixed(1)}%` : "";
}
function checkLines(votes: any): string[] {
  return String(votes?.invalidation_checks || "")
    .split("|").map(cnExpr).filter(Boolean);
}
function cnExpr(expr: string): string {
  let s = expr.trim();
  for (const [re, cn] of _FACTOR_RE) s = s.replace(re, cn);
  return s
    .replace(/<=/g, " 低于或等于 ").replace(/>=/g, " 高于或等于 ")
    .replace(/==?/g, " 变为 ").replace(/</g, " 低于 ").replace(/>/g, " 高于 ")
    .replace(/\//g, " ÷ ").replace(/\*/g, " × ")
    .replace(/\s+/g, " ").trim();
}
function fmtConf(c: any) {
  const n = Number(c);
  return isFinite(n) ? (n > 1 ? n.toFixed(0) + "%" : (n * 100).toFixed(0) + "%") : "-";
}
function fmtDate(s: string) {
  return s && s.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s;
}

// ---------- 辩论 / 证据 展示辅助 ----------
const STANCE_LABEL: Record<string, string> = { BULL: "看多", BEAR: "看空", NEUTRAL: "中性" };
function stanceClass(s: string) {
  const x = String(s).toUpperCase();
  return ["BULL", "BUY"].includes(x) ? "ok" : ["BEAR", "SELL"].includes(x) ? "danger" : "muted";
}
function evVerdictClass(v: string) {
  return v === "bull" ? "ok" : v === "bear" ? "danger" : "muted";
}
function fmtEvValue(ev: any) {
  if (ev.kind === "pct") {
    const n = Number(ev.value);
    return isFinite(n) ? (n * 100).toFixed(0) + "%" : String(ev.value);
  }
  const n = Number(ev.value);
  return isFinite(n) ? (Math.abs(n) >= 1 ? n.toFixed(2) : n.toFixed(4)) : String(ev.value);
}

onMounted(() => { loadStrategies(); loadWatchlist(); loadFinal(); loadPicks(); });
watch(() => app.mode, () => { loadStrategies(); loadWatchlist(); loadFinal(); loadPicks(); });
</script>

<template>
  <div>
    <!-- 顶部操作区 -->
    <div class="card sel-actions">
      <div class="sa-left">
        <button class="btn primary" :disabled="runningSel" @click="runSelection">
          {{ runningSel ? "选股中…" : "🔄 一键选股" }}
        </button>
        <button class="btn primary" :disabled="researching || !picks.length" @click="runResearch">
          {{ researching ? "研判中…" : "🧠 AI 深度研判" }}
        </button>
        <span v-if="selProgress" class="tiny muted">{{ selProgress }}</span>
        <span v-else-if="researchProgress" class="tiny muted">{{ researchProgress }}</span>
      </div>
      <div class="sa-right">
        <label class="strategy-pick" title="切换选股策略配方（影响因子权重与入选门槛）">
          策略：
          <select v-model="selectedStrategy" class="sel-select">
            <option v-for="s in strategies" :key="s.id" :value="s.id">{{ s.name }}</option>
          </select>
        </label>
        <span v-if="picksRegime" class="badge info">市场状态: {{ picksRegime }}</span>
        <span v-if="picksAsOf" class="badge">候选截止: {{ String(picksAsOf).slice(0, 10) }}</span>
        <span v-if="finalAsOf" class="badge ok">精选日期: {{ finalAsOf }}</span>
      </div>
    </div>

    <!-- 策略说明 -->
    <div v-if="selectedStrategy && strategies.find(s => s.id === selectedStrategy)"
         class="card strat-note">
      <b>{{ strategies.find(s => s.id === selectedStrategy)?.name }}</b>
      <span class="muted"> · {{ strategies.find(s => s.id === selectedStrategy)?.summary }}</span>
      <div class="tiny muted" style="margin-top:4px">
        适用：{{ strategies.find(s => s.id === selectedStrategy)?.best_for }}
      </div>
    </div>

    <!-- 漏斗阶梯：L0 硬过滤每一级的 before/after/removed -->
    <section class="card funnel-card">
      <h3>🫗 选股漏斗
        <span class="sub">L0 硬过滤 → L1 打分（全市场 → 候选池 → Top{{ picks.length || " N" }}）</span>
        <div class="spacer"></div>
        <span v-if="funnelNIn" class="badge">全市场 {{ funnelNIn }} 只</span>
      </h3>
      <div v-if="funnel.length" class="funnel">
        <div v-for="(st, i) in funnel" :key="st.rule" class="fstage"
             :title="`${st.desc}：${st.before} → ${st.after}（淘汰 ${st.removed} 只）`">
          <div class="frule-col">
            <span class="fidx">{{ i + 1 }}</span>
            <span class="frule">{{ st.desc }}</span>
          </div>
          <div class="fbar-zone">
            <div class="fbar-fill" :class="{ 'fbar-fill-warn': funnelRemovedPct(st) >= 50 }"
                 :style="{ width: funnelWidth(st.after) + '%' }">
              <span class="fbar-num">{{ st.after }}</span>
            </div>
          </div>
          <div class="fmeta">
            <b class="fremoved">-{{ st.removed }}</b>
            <span class="frem-sep">·</span>
            <span class="frem-pct">淘汰 {{ funnelRemovedPct(st) }}%</span>
          </div>
        </div>
        <!-- 漏斗末级：候选池经 L1 因子打分选出 Top N（picks） -->
        <div v-if="picks.length" class="fstage fstage-top"
             :title="`L1 因子打分：候选池 ${poolSize} 只中选出 Top ${picks.length}，进入最终精选与交易候选`">
          <div class="frule-col">
            <span class="fidx fidx-top">★</span>
            <span class="frule">L1 因子打分 · Top{{ picks.length }} 入选</span>
          </div>
          <div class="fbar-zone">
            <div class="fbar-fill fbar-fill-top" :style="{ width: funnelWidth(picks.length) + '%' }">
              <span class="fbar-num">{{ picks.length }}</span>
            </div>
          </div>
          <div class="fmeta">
            <span class="frem-pct">Top {{ picks.length }} / {{ poolSize }}</span>
          </div>
        </div>
        <div class="ftail tiny muted">
          漏斗末端即 L1 因子打分排名选出的 Top{{ picks.length }}，进入最终精选与右侧候选池列表。
        </div>
      </div>
      <div v-else class="empty">
        <p class="muted">{{ picksNote || "暂无漏斗数据，点击「一键选股」生成（全市场约 5400 只 → 候选池）。" }}</p>
      </div>
    </section>

    <div class="sel-layout">
      <!-- 左：最终精选 -->
      <main class="sel-main">
        <section class="card">
          <h3>⭐ 最终精选
            <span class="sub">多 Agent 决策投票 · 3~5 只高胜率标的 · 含看多/看空辩论</span>
            <div class="spacer"></div>
            <button class="btn sm ok" :disabled="!checkedCount()" @click="addCheckedToWatch">
              加入重点研究 ({{ checkedCount() }})
            </button>
            <input v-model="finalDate" type="date" class="date-input" title="按日期查历史精选" @change="loadFinal" />
            <button class="btn sm ghost" @click="loadFinal">刷新</button>
          </h3>

          <div v-if="finals.length" class="final-list">
            <div v-for="p in finals" :key="p.symbol" class="final-card">
              <div class="fc-head">
                <input type="checkbox" class="fc-check" v-model="checked[p.symbol]"
                       :title="'勾选后加入重点研究'" />
                <span class="fc-rank">#{{ p.rank || "-" }}</span>
                <button class="fc-symbol" @click="goMarket(p.symbol)" :title="'查看 ' + p.symbol + ' 行情'">
                  {{ p.symbol }}
                </button>
                <span v-if="p.industry" class="tiny muted">{{ p.industry }}</span>
                <span class="badge" :class="actionClass(p.action)">{{ ACTION_LABEL[p.action] || p.action }}</span>
                <span class="badge" :class="convictionClass(p.conviction)">{{ CONVICTION_LABEL[p.conviction] || p.conviction }}</span>
                <span class="tiny muted" v-if="p.confidence != null">置信 {{ fmtConf(p.confidence) }}</span>
                <span v-if="isWatched(p.symbol)" class="badge sm ok" title="已在重点研究清单">★ 重点</span>
                <span v-else class="badge sm muted" @click="toggleWatch(p.symbol)"
                      style="cursor:pointer" title="点此加入重点研究">☆ 加入</span>
              </div>
              <div v-if="p.reason" class="fc-reason">{{ p.reason }}</div>

              <!-- 看多 / 看空 核心论据 -->
              <div v-if="p.bull_case || p.bear_case" class="case-row">
                <div v-if="p.bull_case" class="case bull">
                  <div class="case-h">🟢 看多方核心论据</div>
                  <div class="case-b">{{ p.bull_case }}</div>
                </div>
                <div v-if="p.bear_case" class="case bear">
                  <div class="case-h">🔴 看空方核心论据</div>
                  <div class="case-b">{{ p.bear_case }}</div>
                </div>
              </div>

              <!-- 辩论回合（多空交锋记录） -->
              <div v-if="p.debate && p.debate.length" class="fc-debate">
                <div class="case-h">🗣 多空辩论纪要（{{ p.debate.length }} 回合）</div>
                <div v-for="(d, di) in p.debate" :key="di" class="dline">
                  <span class="dround">R{{ d.round }}</span>
                  <span class="badge sm" :class="stanceClass(d.stance)">{{ STANCE_LABEL[d.stance] || d.stance }}</span>
                  <span class="dspeaker">{{ d.speaker }}</span>
                  <span class="dclaim">{{ d.claim }}</span>
                  <span v-if="d.confidence != null" class="tiny muted dconf">{{ (d.confidence * 100).toFixed(0) }}%</span>
                </div>
              </div>

              <!-- 支撑证据（类别分位 + 关键因子原值，让理由可核查） -->
              <div v-if="p.evidence && p.evidence.length" class="fc-evidence">
                <div class="case-h">🔬 支撑证据</div>
                <div class="ev-grid">
                  <div v-for="(ev, ei) in p.evidence" :key="ei" class="ev-cell" :class="evVerdictClass(ev.verdict)">
                    <span class="ev-label">{{ ev.label }}</span>
                    <span class="ev-val">{{ fmtEvValue(ev) }}</span>
                  </div>
                </div>
              </div>

              <template v-if="p.votes && Object.keys(p.votes).length">
                <div class="fc-votes">
                  <span v-if="stopLabel(p.votes)" class="badge sm danger"
                        title="股价相对买入价跌到该幅度即触发止损">{{ stopLabel(p.votes) }}</span>
                  <span v-for="a in agentVotes(p.votes)" :key="a.key" class="vote">
                    <span class="vk">{{ a.name }}</span>
                    <span class="badge sm" :class="a.cls" :title="a.title">{{ a.text }}</span>
                  </span>
                </div>
                <div v-if="p.votes.invalidation" class="fc-invalid">
                  <b>论点失效条件：</b>{{ p.votes.invalidation }}
                </div>
                <div v-if="checkLines(p.votes).length" class="fc-checks tiny muted">
                  量化失效线（触发即止损/减仓）：{{ checkLines(p.votes).join("；") }}
                </div>
              </template>
            </div>
          </div>
          <div v-else class="empty">
            <p class="muted">{{ finalNote || "暂无精选。" }}</p>
            <p class="tiny muted">流程：选策略 → 一键选股 → AI 深度研判（看多/看空辩论）→ 产出精选；盘前 selection/research 调度任务也会自动产出。</p>
          </div>
        </section>
      </main>

      <!-- 右：候选池 + 重点研究 -->
      <aside class="sel-side">
        <section class="card">
          <h3>🎯 候选池 Top{{ picks.length }}
            <span class="sub">规则漏斗 + 因子打分</span>
          </h3>
          <input v-model="pickSearch" class="search" placeholder="过滤代码/行业…" />
          <div class="pool-list">
            <button
              v-for="p in filteredPicks"
              :key="p.symbol"
              class="pool-row"
              @click="goMarket(p.symbol)"
              :title="'查看 ' + p.symbol + ' 行情'"
            >
              <span class="rk">#{{ p.rank }}</span>
              <span class="sym">{{ p.symbol }}</span>
              <span class="ind">{{ p.industry || "" }}</span>
              <span class="sc">{{ Number(p.score).toFixed(3) }}</span>
            </button>
            <div v-if="!filteredPicks.length" class="muted" style="font-size:13px; padding:8px 4px">
              {{ picksNote || "暂无候选，点击「一键选股」生成。" }}
            </div>
          </div>
        </section>

        <section class="card">
          <h3>⭐ 重点研究清单
            <span class="sub">自定义跟踪标的（{{ watchlist.length }}）</span>
            <div class="spacer"></div>
            <button class="btn sm ghost" @click="loadWatchlist">刷新</button>
          </h3>
          <div v-if="watchlist.length" class="watch-list">
            <div v-for="s in watchlist" :key="s" class="watch-row">
              <button class="watch-sym" @click="goMarket(s)">{{ s }}</button>
              <button class="watch-del" @click="removeWatch(s)" title="移出重点研究">✕</button>
            </div>
          </div>
          <div v-else class="muted" style="font-size:13px; padding:8px 4px">
            在左侧精选卡片勾选（或点 ☆），即可把标的加入重点研究，便于盘后集中跟踪。
          </div>
        </section>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.sel-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.sa-left, .sa-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.btn.primary {
  background: var(--primary);
  border-color: var(--primary);
  color: #fff;
  font-weight: 700;
}
.btn.primary:disabled { opacity: 0.55; cursor: not-allowed; }
.btn.sm { padding: 3px 9px; font-size: 12px; }
.btn.ok { background: var(--ok); border-color: var(--ok); color: #fff; }
.btn.ok:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.ghost { background: transparent; }
.strategy-pick { font-size: 13px; color: var(--text-2); display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
.sel-select {
  font-size: 13px; padding: 4px 8px; border-radius: 7px;
  border: 1px solid var(--border); background: var(--bg-elev); color: var(--text);
}
.strat-note {
  margin-bottom: 16px; padding: 10px 14px; font-size: 13px; line-height: 1.7;
  border-left: 4px solid var(--primary);
}

/* ---- 漏斗阶梯：三栏布局（规则 | 居中漏斗条 | 淘汰统计），条形居中对称形成漏斗形 ---- */
.funnel-card { margin-bottom: 16px; }
.funnel { display: flex; flex-direction: column; gap: 7px; }
.fstage {
  display: grid; grid-template-columns: 240px minmax(0, 1fr) 116px;
  align-items: center; gap: 14px;
  padding: 2px 4px; border-radius: 8px; transition: background 0.15s;
}
.fstage:hover { background: var(--bg-2); }
.frule-col { display: flex; align-items: center; gap: 8px; min-width: 0; }
.fidx {
  width: 20px; height: 20px; border-radius: 50%;
  background: var(--bg-2); border: 1px solid var(--border); color: var(--primary);
  font-size: 11px; font-weight: 700; display: inline-flex; align-items: center; justify-content: center;
  flex: 0 0 20px;
}
.frule {
  flex: 1; font-size: 12.5px; color: var(--text-2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
/* 条区左对齐：首级条铺满可用宽度，逐级向左收窄成漏斗形（含 Top50 末级贴左） */
.fbar-zone { display: flex; justify-content: flex-start; min-width: 0; width: 100%; }
.fbar-fill {
  height: 20px; min-width: 40px; max-width: 100%; border-radius: 6px;
  background: linear-gradient(90deg,
    color-mix(in srgb, var(--primary) 34%, transparent),
    color-mix(in srgb, var(--primary) 18%, transparent));
  border: 1px solid color-mix(in srgb, var(--primary) 26%, transparent);
  display: flex; align-items: center; justify-content: center;
  transition: width 0.4s ease;
}
/* 淘汰过半：柔和琥珀色，保留警示语义但不抢眼 */
.fbar-fill-warn {
  background: linear-gradient(90deg,
    color-mix(in srgb, var(--warn) 32%, transparent),
    color-mix(in srgb, var(--warn) 16%, transparent));
  border-color: color-mix(in srgb, var(--warn) 26%, transparent);
}
.fbar-fill-warn .fbar-num { color: var(--warn); }
.fbar-num {
  color: color-mix(in srgb, var(--primary) 88%, var(--text));
  font-size: 11px; font-weight: 700;
  font-variant-numeric: tabular-nums; padding: 0 6px; white-space: nowrap;
}
.fmeta {
  display: flex; flex-direction: row; align-items: baseline; justify-content: flex-end;
  gap: 5px; line-height: 1; white-space: nowrap;
}
.fremoved { color: var(--danger); font-size: 13px; font-variant-numeric: tabular-nums; }
.frem-sep { color: var(--text-2); font-size: 11px; }
.frem-pct { color: var(--text-2); font-size: 11px; font-variant-numeric: tabular-nums; }
/* 漏斗末级：L1 打分 Top N 入选，柔和绿色凸显“最终进入候选/精选”的收口层级 */
.fstage-top { background: color-mix(in srgb, var(--ok) 5%, transparent); }
.fstage-top:hover { background: color-mix(in srgb, var(--ok) 10%, transparent); }
.fidx-top { background: var(--ok); border-color: var(--ok); color: #fff; font-size: 12px; }
.fbar-fill-top {
  background: linear-gradient(90deg,
    color-mix(in srgb, var(--ok) 32%, transparent),
    color-mix(in srgb, var(--ok) 16%, transparent));
  border-color: color-mix(in srgb, var(--ok) 28%, transparent);
}
.fbar-fill-top .fbar-num { color: var(--ok); }
.ftail { margin-top: 6px; }

.sel-layout {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.sel-main { flex: 1; min-width: 0; }
.sel-side { width: 340px; flex: 0 0 340px; }
.date-input { max-width: 150px; }

.final-list { display: flex; flex-direction: column; gap: 12px; }
.final-card {
  border: 1px solid var(--border);
  border-left: 4px solid var(--primary);
  border-radius: 10px;
  padding: 12px 14px;
  background: var(--bg-elev);
}
.fc-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.fc-check { width: 16px; height: 16px; cursor: pointer; }
.fc-rank {
  font-weight: 800;
  color: var(--primary);
  font-family: ui-monospace, monospace;
}
.fc-symbol {
  font-weight: 800;
  font-size: 16px;
  font-family: ui-monospace, monospace;
  background: none;
  border: none;
  color: var(--text);
  cursor: pointer;
  padding: 0;
}
.fc-symbol:hover { color: var(--primary); text-decoration: underline; }
.fc-reason {
  margin-top: 8px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-2);
}
.fc-votes {
  margin-top: 8px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.vote { display: inline-flex; align-items: center; gap: 4px; }
.vote .vk { font-size: 12px; color: var(--text-2); }
.badge.sm { font-size: 11px; padding: 1px 6px; }
.fc-invalid {
  margin-top: 8px;
  padding: 6px 10px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-2);
  background: var(--bg-2);
  border-left: 3px solid var(--danger);
  border-radius: 6px;
}
.fc-invalid b { color: var(--text); }
.fc-checks { margin-top: 6px; line-height: 1.6; }

/* ---- 看多/看空 / 辩论 / 证据 ---- */
.case-row { display: flex; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
.case {
  flex: 1; min-width: 220px; border-radius: 8px; padding: 8px 10px; font-size: 12.5px; line-height: 1.6;
}
.case.bull { background: rgba(40,167,69,0.08); border-left: 3px solid var(--ok); }
.case.bear { background: rgba(220,53,69,0.08); border-left: 3px solid var(--danger); }
.case-h { font-weight: 700; color: var(--text); margin-bottom: 4px; font-size: 12.5px; }
.case-b { color: var(--text-2); }
.fc-debate { margin-top: 10px; border-radius: 8px; padding: 8px 10px; background: var(--bg-2); }
.dline {
  display: flex; align-items: center; gap: 8px; font-size: 12.5px; line-height: 1.6;
  padding: 3px 0; border-bottom: 1px dashed var(--border);
}
.dline:last-child { border-bottom: none; }
.dround {
  font-family: ui-monospace, monospace; font-weight: 700; color: var(--primary);
  font-size: 11px; flex: 0 0 auto;
}
.dspeaker { color: var(--text-2); flex: 0 0 auto; }
.dclaim { flex: 1; color: var(--text); }
.dconf { flex: 0 0 auto; }
.fc-evidence { margin-top: 10px; }
.ev-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 6px; margin-top: 6px;
}
.ev-cell {
  border: 1px solid var(--border); border-radius: 7px; padding: 5px 8px;
  display: flex; flex-direction: column; gap: 1px; background: var(--bg-elev);
}
.ev-cell.bull { border-left: 3px solid var(--ok); }
.ev-cell.bear { border-left: 3px solid var(--danger); }
.ev-cell.muted { border-left: 3px solid var(--border); }
.ev-label { font-size: 11px; color: var(--text-2); }
.ev-val { font-size: 14px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--text); }

/* ---- 候选池 / 重点研究 ---- */
.search { width: 100%; margin-bottom: 8px; }
.pool-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
  max-height: 560px;
  overflow: auto;
}
.pool-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  text-align: left;
  padding: 6px 9px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-elev);
  color: var(--text);
  cursor: pointer;
  transition: all 0.15s;
}
.pool-row:hover { border-color: var(--primary); background: var(--bg-2); }
.pool-row .rk { font-weight: 700; color: var(--primary); font-family: ui-monospace, monospace; font-size: 12px; width: 30px; }
.pool-row .sym { font-weight: 700; font-family: ui-monospace, monospace; font-size: 13px; }
.pool-row .ind { flex: 1; font-size: 12px; color: var(--text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pool-row .sc { font-size: 12px; font-variant-numeric: tabular-nums; color: var(--ok); }

.watch-list { display: flex; flex-direction: column; gap: 5px; max-height: 320px; overflow: auto; }
.watch-row {
  display: flex; align-items: center; gap: 6px;
  border: 1px solid var(--border); border-radius: 8px; padding: 4px 8px; background: var(--bg-elev);
}
.watch-sym {
  flex: 1; text-align: left; font-family: ui-monospace, monospace; font-weight: 700; font-size: 13px;
  background: none; border: none; color: var(--text); cursor: pointer; padding: 2px 0;
}
.watch-sym:hover { color: var(--primary); }
.watch-del {
  flex: 0 0 auto; border: none; background: none; color: var(--text-2); cursor: pointer;
  font-size: 13px; padding: 2px 4px;
}
.watch-del:hover { color: var(--danger); }

.empty { padding: 18px 4px; }
@media (max-width: 1080px) {
  .sel-layout { flex-direction: column; }
  .sel-side { width: 100%; flex: none; }
}
@media (max-width: 900px) {
  .fstage { grid-template-columns: 1fr; gap: 4px; }
  .fmeta { flex-direction: row; align-items: baseline; justify-content: center; gap: 8px; }
}
</style>
