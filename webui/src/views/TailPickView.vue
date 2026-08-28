<script setup lang="ts">
// 尾盘选股法（一夜持股法）独立控制面板。
// 完全独立于现有多因子选股/Regime/风控体系：只读/写 strategies.tail_pick
// 配置段与 tail_pick_* 两个调度任务，手动触发复用通用 /scheduler/run。
// 布局：顶部概览条 + 单面板多 tab；参数配置 tab 内再按组分子 tab，重要参数前置。
import { computed, onMounted, ref, watch } from "vue";
import api from "@/api";
import { useApp } from "@/store";
import { pushToast, tryReq } from "@/toast";

const app = useApp();
const loading = ref(false);
const st = ref<any>(null);

// ---------------- 页面 tab ----------------
const TABS = [
  { key: "config", label: "⚙️ 策略参数" },
  { key: "hold", label: "🎯 候选与持仓" },
  { key: "orders", label: "📊 订单与表现" },
  { key: "bt", label: "⏳ 独立回测" },
];
const tab = ref("config");
const cfgGroup = ref(0);

// ---------------- 配置表单 ----------------
const DEFAULTS: Record<string, any> = {
  enabled: false,
  select_time: "14:30", entry_time: "14:30",
  exit_window_start: "09:30", exit_window_end: "10:00",
  min_pct_change: 0.03, max_pct_change: 0.05,
  weak_min_pct_change: -0.01, weak_max_pct_change: 0.015,
  min_volume_ratio: 1.0,
  min_turnover_rate: 0.05, max_turnover_rate: 0.10,
  min_float_market_cap: 5000000000, max_float_market_cap: 50000000000,
  volume_ladder_ratio: 1.0, volume_ladder_segments: 3, volume_ladder_seg_tolerance: 0.9,
  shrink_vol_max_ratio: 1.2, vol_spike_exclude_ratio: 2.0,
  min_intraday_outperf_vs_index: 0.0,
  chip_vwap_tolerance_pct: 0.01,
  overnight_stop_pct: 0.03, max_positions: 5, position_fraction: 0.20,
  cash_usage_ratio: 0.95, universe_top_n: 100, require_minute_bars: true,
  gap_protect_enabled: true, gap_buffer_enabled: true, gap_buffer_pct: 0.005,
  breakeven_trigger_pct: 0.010, take_profit_pct: 0.028, hard_stop_pct: 0.012,
  vwap_exit_enabled: false, market_filter_enabled: true, market_ma_days: 20,
  market_breadth_required: true, weak_market_adv_min: 2500,
  weak_market_position_ratio: 0.2, breadth_block_below: 1000,
  gapup_threshold_pct: 0.005, gapdown_threshold_pct: 0.005, low_open_check_time: "09:45",
  open_momentum_enabled: true, open_momentum_vol_mult: 2.0, open_momentum_hold_until: "10:30",
  sector_enabled: true, sector_top_n: 5, sector_bottom_n: 50, sector_boost_mult: 2.0,
  exclude_st: true, min_list_days: 60, exclude_suspended: true,
  exclude_limit_locked: true, allowed_boards: ["MAIN", "GEM"],
};

type FieldDef = [string, string, string, "number" | "bool" | "text"];
// 参数组按重要性排序：最常调的筛选阈值 / 交易纪律排前面，时刻与硬排除靠后
const GROUPS: { title: string; fields: FieldDef[] }[] = [
  {
    title: "🔍 8 层筛选阈值",
    fields: [
      ["min_pct_change", "涨幅下界", "规则①：当日涨幅 ≥ 此值（0.03 = 3%）", "number"],
      ["max_pct_change", "涨幅上界", "规则①：当日涨幅 ≤ 此值（0.05 = 5%）", "number"],
      ["weak_min_pct_change", "弱市涨幅下界(买跌)", "V4.0：指数<MA20 时改用买跌带，当日涨幅 ≥ 此值（-0.01 = -1%）", "number"],
      ["weak_max_pct_change", "弱市涨幅上界(买跌)", "V4.0：弱市当日涨幅 ≤ 此值（0.015 = +1.5%），微红微绿=主力护盘/抛压枯竭", "number"],
      ["min_volume_ratio", "量比下限", "规则②：量比 > 此值（当日量 / 近 5 日均量，默认 1.0；V4.0 弱市停用）", "number"],
      ["min_turnover_rate", "换手率下界", "规则③：换手率 ≥ 此值（0.05 = 5%）", "number"],
      ["max_turnover_rate", "换手率上界", "规则③：换手率 ≤ 此值（0.10 = 10%）", "number"],
      ["min_float_cap_yi", "流通市值下界(亿)", "规则④：流通市值 ≥ 此值（亿元）", "number"],
      ["max_float_cap_yi", "流通市值上界(亿)", "规则④：流通市值 ≤ 此值（亿元）", "number"],
      ["volume_ladder_ratio", "今/昨量倍数", "规则⑥日线层：今日量 ≥ 昨日量 × 此倍数（阶梯放量的前提）", "number"],
      ["volume_ladder_segments", "午后等分段数", "规则⑥分时层：午后连续竞价时段（13:00 起）等分为 N 段，要求各段量逐段递增", "number"],
      ["volume_ladder_seg_tolerance", "分段递增倍数", "规则⑥分时层：后段量 ≥ 前段量 × 此倍数（0.9 = 允许微降 10%）", "number"],
      ["shrink_vol_max_ratio", "弱市缩量上限", "V4.0 弱市：量比 < 此值才入选（1.2=温和放量以内），停用③量比下限/⑥阶梯放量", "number"],
      ["vol_spike_exclude_ratio", "异常放量剔除", "V4.0：量比 ≥ 此值（2.0 倍）强弱市一律剔除（疑似拉高出货）", "number"],
      ["min_intraday_outperf_vs_index", "跑赢大盘下限", "规则⑦：个股当日涨幅 − 沪深300 当日涨幅 ≥ 此值", "number"],
      ["chip_vwap_tolerance_pct", "筹码VWAP容差", "规则⑧：尾盘收 < VWAP×(1+容差) 即通过（0.01 = +1% 避免边界误杀）", "number"],
    ],
  },
  {
    title: "⚖️ 交易纪律",
    fields: [
      ["overnight_stop_pct", "隔夜硬止损", "T+1 开盘较成本跌超此比例立即砍仓（防跳空深亏）", "number"],
      ["gap_protect_enabled", "缺口保护", "T+1 平开/低开（开盘≤昨收）时触发；深低开开盘价即卖锁住跳空缺口", "bool"],
      ["gap_buffer_enabled", "缺口保护·5分钟缓冲", "平开/微低开（跌幅≤缓冲幅度）不立即卖，等 9:30-9:35：触及成本保本即走，未触及则 9:35 离场（防洗盘割肉）", "bool"],
      ["gap_buffer_pct", "缓冲幅度", "开盘在成本 ±此幅度内进入 5 分钟缓冲（默认 0.5%）", "number"],
      ["breakeven_trigger_pct", "保本触发", "开盘 5min 内触及 成本×(1+此值) 后，止损线上移至成本（保本单）", "number"],
      ["take_profit_pct", "止盈线", "触及 成本×(1+此值) 立即落袋，不等窗口结束（V4.0：1.8%→2.8%）", "number"],
      ["hard_stop_pct", "硬止损", "V4.0：触及 成本×(1−此值)（默认 -1.2%）立即市价离场，动量分支亦保底", "number"],
      ["vwap_exit_enabled", "VWAP 止损", "V4.0 起默认关闭（让位硬止损）；开启后 bar 收盘跌破当日分时均价即离场", "bool"],
      ["max_positions", "最大持仓数", "同时持有上限（一夜）", "number"],
      ["position_fraction", "单票仓位", "每只占用可用现金比例上限", "number"],
      ["cash_usage_ratio", "现金使用上限", "总现金使用上限（留缓冲）", "number"],
      ["universe_top_n", "候选池规模", "粗筛候选上限：先取日线规则合格集，超出才按成交额截断", "number"],
      ["require_minute_bars", "要求分钟线", "严格模式：无分钟线时规则⑥b/⑧'降级 best-effort 并标注", "bool"],
      ["gapup_threshold_pct", "高开阈值", "T+1 开盘较昨收高开 > 此比例（0.005 = 0.5%）→ 开盘市价卖半仓，剩余走保本/止盈/VWAP 链", "number"],
      ["gapdown_threshold_pct", "低开阈值", "T+1 开盘较昨收低开 < -此比例 → 开盘不卖，等 low_open_check_time 判定翻红", "number"],
      ["low_open_check_time", "低开判定时点", "低开分支：该 bar 收盘前最高价曾回到昨收之上（翻红）转正常离场链，否则市价砍仓（默认 09:45）", "text"],
      ["open_momentum_enabled", "隔夜动量", "高开卖半仓后：开盘价>昨收且首根 bar 放量 → 剩余半仓取消 1.8% 止盈，持有至 hold_until 市价离场", "bool"],
      ["open_momentum_vol_mult", "动量放量倍数", "隔夜动量：首根 bar 量 ≥ T 日平均每 bar 量 × 此倍数才认定放量", "number"],
      ["open_momentum_hold_until", "动量持有至", "隔夜动量分支的市价离场时刻（默认 10:30）", "text"],
    ],
  },
  {
    title: "🏭 板块效应",
    fields: [
      ["sector_enabled", "启用板块效应", "按东财行业当日涨幅排名筛选/加权（映射为当前快照，回测有幸存者偏差）", "bool"],
      ["sector_top_n", "热门行业前 N", "当日行业涨幅排名前 N 的候选标记加权，买入名义额 ×sector_boost_mult", "number"],
      ["sector_bottom_n", "弱势行业后 N", "当日行业涨幅排名后 N 的候选直接剔除", "number"],
      ["sector_boost_mult", "热门仓位倍数", "命中前 N 行业的标的买入名义额倍数（以可用现金为上限）", "number"],
    ],
  },
  {
    title: "🌡️ 大市温度计",
    fields: [
      ["market_filter_enabled", "启用大市温度计", "双模式仓位：强市满仓追涨 / 强市广度闸门空仓 / 弱市侦察兵仓位买跌 / 绝对空仓", "bool"],
      ["market_ma_days", "趋势均线天数", "条件A：沪深300 收盘站上的均线天数，站上且广度达标满仓运行（默认 20）", "number"],
      ["weak_market_adv_min", "强市广度门槛", "指数站上均线时：上涨家数 > 此值才满仓，否则空仓（强市广度闸门，默认 2500）", "number"],
      ["weak_market_position_ratio", "弱市仓位比例", "V4.0：指数跌破均线但广度≥绝对空仓线时的侦察兵仓位（0.2=2成）", "number"],
      ["breadth_block_below", "绝对空仓·上涨家数线", "上涨家数 < 此值绝对空仓（默认 1000）；介于其值与 2500 之间为震荡市/弱市侦察兵仓位试错", "number"],
      ["market_breadth_required", "启用广度分档", "关闭后弱市不做半仓分档，直接空仓", "bool"],
    ],
  },
  {
    title: "⏰ 选股 / 交易时刻",
    fields: [
      ["select_time", "选股时刻", "T 日尾盘选股时刻（HH:MM，策略定义 14:30 后）；修改将同步 tail_pick_select 调度", "text"],
      ["entry_time", "买入时刻", "T 日尾盘买入时刻（HH:MM）", "text"],
      ["exit_window_start", "离场窗口起点", "T+1 离场开始；修改将同步 tail_pick_exit 调度", "text"],
      ["exit_window_end", "离场窗口终点", "T+1 最晚离场（开盘 30 分钟内，一夜持股纪律）", "text"],
    ],
  },
  {
    title: "🚫 硬排除",
    fields: [
      ["exclude_st", "排除 ST", "", "bool"],
      ["min_list_days", "最少上市天数", "次新股过滤", "number"],
      ["exclude_suspended", "排除停牌", "", "bool"],
      ["exclude_limit_locked", "排除涨跌停封死", "买不进/卖不出的一律不碰", "bool"],
      ["allowed_boards", "允许板块", "逗号分隔，可选 MAIN / GEM / STAR / BSE", "text"],
    ],
  },
];

// 表单用「亿元」编辑市值；allowed_boards 用逗号字符串编辑
function toForm(cfg: Record<string, any>): Record<string, any> {
  const f = { ...DEFAULTS, ...(cfg || {}) };
  f.min_float_cap_yi = Number(f.min_float_market_cap) / 1e8;
  f.max_float_cap_yi = Number(f.max_float_market_cap) / 1e8;
  f.allowed_boards = (f.allowed_boards || []).join(", ");
  return f;
}
function convertOut(k: string, v: any, originV: any): any {
  if (k === "min_float_cap_yi") return Math.round(Number(v) * 1e8);
  if (k === "max_float_cap_yi") return Math.round(Number(v) * 1e8);
  if (k === "allowed_boards") {
    return String(v).split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
  }
  if (typeof originV === "number") return Number(v);
  if (typeof originV === "boolean") return !!v;
  return v;
}
const CAP_KEY_MAP: Record<string, string> = {
  min_float_cap_yi: "min_float_market_cap",
  max_float_cap_yi: "max_float_market_cap",
};

const form = ref<Record<string, any>>({ ...DEFAULTS });
const origin = ref<Record<string, any>>({ ...DEFAULTS });
const dirty = computed(() =>
  Object.keys(form.value).filter(
    (k) => JSON.stringify(form.value[k]) !== JSON.stringify(origin.value[k])
  )
);
function groupDirtyCnt(g: { fields: FieldDef[] }): number {
  return g.fields.reduce((n, f) => n + (dirty.value.includes(f[0]) ? 1 : 0), 0);
}

async function load() {
  loading.value = true;
  st.value = await tryReq(() => api.tailpickStatus(app.mode));
  loading.value = false;
  const cfg = { ...DEFAULTS, ...(st.value?.config || {}) };
  origin.value = toForm(cfg);
  form.value = { ...origin.value };
}

async function toggleEnabled() {
  const v = !!form.value.enabled;
  const r = await tryReq(
    () => api.tailpickConfig({ enabled: v }),
    v ? "尾盘选股法已启用（下次调度触发即生效）" : "尾盘选股法已停用"
  );
  if (r) {
    origin.value.enabled = v;
    load(); // 刷新调度热更新状态
  } else {
    form.value.enabled = !v;
  }
}

async function save() {
  if (!dirty.value.length) { pushToast("没有改动", "info"); return; }
  const body: Record<string, any> = {};
  for (const k of dirty.value) {
    const outKey = CAP_KEY_MAP[k] || k;
    body[outKey] = convertOut(k, form.value[k], origin.value[k]);
  }
  const r = await tryReq(() => api.tailpickConfig(body), "参数已保存并生效");
  if (r) load();
}

// ---------------- 手动触发（复用通用 /scheduler/run） ----------------
async function runOnce(name: "tail_pick_select" | "tail_pick_exit") {
  const label = name === "tail_pick_select" ? "尾盘选股" : "隔夜离场";
  const tip = name === "tail_pick_select"
    ? "将立即执行尾盘选股；若策略已启用且非 sim 模式、KillSwitch 为 NORMAL，会真实提交模拟买入。确定执行？"
    : "将立即对昨日尾盘买入的持仓提交离场卖出。确定执行？";
  if (!window.confirm(tip)) return;
  loading.value = true;
  const r = await tryReq(() => api.schedulerRun(name, app.mode));
  loading.value = false;
  if (r) {
    pushToast(`${label}：${r.rendered || r.reason || "完成"}`, r.ok ? "ok" : "err");
    load();
  }
}

// ---------------- 独立回测 ----------------
const btStart = ref("");
const btEnd = ref("");
const btCash = ref(1000000);
const btResult = ref<any>(null);
let btTimer: any = null;

async function runBacktest() {
  if (!btStart.value) { pushToast("请填写回测开始日期", "err"); return; }
  btResult.value = null;
  const r = await tryReq(() => api.tailpickBacktest(
    { start: btStart.value, end: btEnd.value || undefined, cash: Number(btCash.value) },
    app.mode
  ));
  if (!r?.job_id) return;
  pushToast(`尾盘回测已提交（${r.job_id}），后台运行中`, "ok");
  btTimer = setInterval(async () => {
    const j = await tryReq(() => api.job(r.job_id));
    if (!j) return;
    if (j.status === "done") {
      clearInterval(btTimer); btTimer = null;
      btResult.value = j.result;
      load();
    } else if (j.status === "error") {
      clearInterval(btTimer); btTimer = null;
      pushToast(`回测失败：${j.error}`, "err");
    }
  }, 2500);
}

// ---------------- 展示辅助 ----------------
function jobBadge(s?: string) {
  return s === "OK" ? "ok" : s === "FAIL" ? "danger" : s === "SKIP" ? "warn" : "muted";
}
function pct(v?: number | null, digits = 1) {
  return v === null || v === undefined ? "-" : `${(v * 100).toFixed(digits)}%`;
}
function money(v?: number | null) {
  return v === null || v === undefined ? "-" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
}
function kindOf(k: string): string {
  for (const g of GROUPS) for (const f of g.fields) if (f[0] === k) return f[3];
  return "text";
}
function schedOf(name: string) {
  return (st.value?.schedule || []).find((j: any) => j.name === name);
}
const ksBadge = computed(() => {
  const m = st.value?.killswitch?.mode;
  return m === "NORMAL" ? "ok" : m === "REDUCE_ONLY" ? "warn" : "danger";
});

onMounted(load);
watch(() => app.mode, load);
</script>

<template>
  <div :class="{ loading }">
    <!-- ================= 顶部概览条 ================= -->
    <div class="card">
      <h3>🌙 尾盘选股法 · 一夜持股 <span class="sub">独立短线策略 —— T 日 14:30 后选股买入，T+1 开盘 30 分钟内必须离场</span>
        <div class="spacer"></div>
        <label class="row" style="gap:8px;align-items:center;cursor:pointer;margin:0">
          <input type="checkbox" style="width:auto" v-model="form.enabled" @change="toggleEnabled" />
          <b>{{ form.enabled ? "已启用" : "已停用" }}</b>
        </label>
        <button class="btn sm ghost" style="margin-left:8px" @click="load">刷新</button>
      </h3>

      <div class="tp-bar">
        <div class="tp-item">
          <span class="tp-label">KillSwitch</span>
          <span class="badge" :class="ksBadge">{{ st?.killswitch?.mode || "-" }}</span>
          <span class="tiny muted">{{ st?.killswitch?.allow_open ? "允许开仓" : "只出不进" }}</span>
        </div>
        <div class="tp-item" v-for="j in (st?.schedule || [])" :key="j.name">
          <span class="tp-label">{{ j.name === "tail_pick_select" ? "选股调度" : "离场调度" }}（{{ j.time_label || "-" }}）</span>
          <span class="tiny muted">下次 {{ j.next_run || "-" }}</span>
          <span class="badge sm" :class="jobBadge(st?.jobs?.[j.name]?.last_status)">
            {{ st?.jobs?.[j.name]?.last_status || "未运行" }}
          </span>
          <button class="btn sm ghost" @click="runOnce(j.name as any)">
            {{ j.name === "tail_pick_select" ? "▶ 立即选股" : "▶ 立即离场" }}
          </button>
        </div>
        <div class="tp-item">
          <span class="tp-label">历史表现（{{ app.mode }}）</span>
          <span class="tiny">胜率 {{ pct(st?.perf?.win_rate) }}（{{ st?.perf?.wins || 0 }}/{{ st?.perf?.n_roundtrips || 0 }}）</span>
          <span class="tiny">累计盈亏 {{ money(st?.perf?.total_pnl) }}</span>
        </div>
      </div>
      <p class="tiny muted" style="margin-top:10px;margin-bottom:0;line-height:1.6">
        独立性说明：本策略自带 8 层筛选器与仓位/止损纪律，不走现有 SelectionPipeline / Regime / RiskEngine；
        执行与现有系统共用同一套撮合/成本口径。KillSwitch 非 NORMAL 时买入被自动拦截，离场不受影响。
        sim 模式只做机制验证（不真实下单），模拟盘请切到 <b>paper</b>。
      </p>
    </div>

    <!-- ================= 单面板 · 多 tab ================= -->
    <div class="card tp-panel">
      <div class="tp-tabs">
        <button v-for="t in TABS" :key="t.key"
                class="tp-tab" :class="{ on: tab === t.key }" @click="tab = t.key">
          {{ t.label }}<i v-if="t.key === 'config' && dirty.length" class="tp-dot">{{ dirty.length }}</i>
        </button>
      </div>

      <!-- ============ tab 1：策略参数（重要配置前置） ============ -->
      <div v-if="tab === 'config'">
        <div class="tp-cfgbar">
          <div class="tp-subtabs">
            <button v-for="(g, gi) in GROUPS" :key="g.title"
                    class="tp-subtab" :class="{ on: cfgGroup === gi }" @click="cfgGroup = gi">
              {{ g.title }}<i v-if="groupDirtyCnt(g)" class="tp-dot">{{ groupDirtyCnt(g) }}</i>
            </button>
          </div>
          <div class="spacer"></div>
          <span class="tiny muted" style="margin-right:10px">保存即生效（写入 settings.yaml::strategies.tail_pick）</span>
          <button class="btn sm" :disabled="!dirty.length" @click="save">保存改动（{{ dirty.length }}）</button>
        </div>
        <table>
          <thead><tr><th style="width:22%">参数</th><th style="width:18%">值</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="[k, label, hint] in GROUPS[cfgGroup].fields" :key="k">
              <td class="tiny"><b>{{ label }}</b><div class="tiny muted">{{ k }}</div></td>
              <td>
                <input v-if="kindOf(k) === 'bool'" type="checkbox" style="width:auto" v-model="form[k]" />
                <input v-else-if="kindOf(k) === 'number'" type="number" step="any" v-model.number="form[k]" />
                <input v-else v-model="form[k]" />
              </td>
              <td class="tiny muted" style="line-height:1.5">{{ hint }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ============ tab 2：候选与持仓 ============ -->
      <div v-else-if="tab === 'hold'">
        <h4 class="tp-h4">🎯 最近一次尾盘候选 <span class="sub">selection:tail_pick:latest</span></h4>
        <table>
          <thead><tr><th>代码</th><th>建议入场价</th><th>分钟级验证</th><th>通过理由</th></tr></thead>
          <tbody>
            <tr v-for="c in (st?.candidates || [])" :key="c.symbol">
              <td><b>{{ c.symbol }}</b></td>
              <td>{{ Number(c.entry_price).toFixed(2) }}</td>
              <td><span class="badge sm" :class="c.minute_verified ? 'ok' : 'warn'">
                {{ c.minute_verified ? "严格" : "best-effort" }}</span></td>
              <td class="tiny muted">{{ (c.reasons || []).join("；") }}</td>
            </tr>
            <tr v-if="!st?.candidates?.length"><td colspan="4" class="muted">尚无候选（未运行过选股或当日无标的通过 8 层筛选）</td></tr>
          </tbody>
        </table>

        <h4 class="tp-h4" style="margin-top:20px">🌃 隔夜持仓
          <span class="sub">一夜持股 —— 今日买入 {{ (st?.bought?.today || []).length }} 只 / 昨日买入 {{ (st?.bought?.yesterday || []).length }} 只</span>
        </h4>
        <table>
          <thead><tr><th>代码</th><th>数量</th><th>成本</th><th>现价</th><th>盈亏</th><th>计划</th></tr></thead>
          <tbody>
            <tr v-for="p in (st?.positions || [])" :key="p.symbol">
              <td><b>{{ p.symbol }}</b></td>
              <td>{{ p.volume }}</td>
              <td>{{ Number(p.avg_cost).toFixed(2) }}</td>
              <td>{{ Number(p.last_price).toFixed(2) }}</td>
              <td :style="{ color: (p.last_price - p.avg_cost) >= 0 ? 'var(--up, #e55)' : 'var(--down, #2a2)' }">
                {{ pct(p.avg_cost ? p.last_price / p.avg_cost - 1 : null, 2) }}
              </td>
              <td class="tiny muted">{{ p.plan_id || "-" }}</td>
            </tr>
            <tr v-if="!st?.positions?.length"><td colspan="6" class="muted">当前无尾盘策略持仓</td></tr>
          </tbody>
        </table>
        <p class="tiny muted" v-if="(st?.bought?.yesterday || []).length">
          昨日买入（今日应已离场）：{{ (st.bought.yesterday || []).join(", ") }}
        </p>
      </div>

      <!-- ============ tab 3：订单与表现 ============ -->
      <div v-else-if="tab === 'orders'">
        <div class="tp-bar" style="margin-bottom:12px">
          <div class="tp-item"><span class="tp-label">委托数</span><b>{{ st?.perf?.n_orders || 0 }}</b></div>
          <div class="tp-item"><span class="tp-label">平仓笔数</span><b>{{ st?.perf?.n_roundtrips || 0 }}</b></div>
          <div class="tp-item"><span class="tp-label">胜率</span><b>{{ pct(st?.perf?.win_rate) }}</b></div>
          <div class="tp-item"><span class="tp-label">累计盈亏</span><b>{{ money(st?.perf?.total_pnl) }}</b></div>
          <span class="tiny muted">signal = TAIL_PICK / TAIL_PICK_EXIT</span>
        </div>
        <table>
          <thead><tr><th>日期</th><th>代码</th><th>方向</th><th>价/量</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="o in (st?.orders || []).slice(0, 20)" :key="o.id">
              <td class="tiny">{{ o.trade_date }}</td>
              <td><b>{{ o.symbol }}</b></td>
              <td><span class="badge sm" :class="o.side === 'BUY' ? 'ok' : 'warn'">{{ o.side }}</span></td>
              <td class="tiny">{{ o.avg_fill_price ? Number(o.avg_fill_price).toFixed(2) : "-" }} × {{ o.filled_volume }}/{{ o.volume }}</td>
              <td class="tiny muted">{{ o.status }}</td>
            </tr>
            <tr v-if="!st?.orders?.length"><td colspan="5" class="muted">暂无尾盘策略订单</td></tr>
          </tbody>
        </table>
      </div>

      <!-- ============ tab 4：独立回测 ============ -->
      <div v-else>
        <p class="tiny muted" style="margin-top:0">TailPickBacktester —— 严禁虚拟数据，sim 结果仅验证机制</p>
        <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
          <label class="tiny">开始 <input type="date" v-model="btStart" style="width:150px" /></label>
          <label class="tiny">结束 <input type="date" v-model="btEnd" style="width:150px" /></label>
          <label class="tiny">资金 <input type="number" v-model.number="btCash" style="width:120px" /></label>
          <button class="btn sm" @click="runBacktest">运行回测</button>
        </div>
        <template v-if="btResult">
          <p class="tiny" v-if="!btResult.has_metrics" style="color:var(--danger)">
            回测未产出指标：{{ btResult.error }}</p>
          <template v-else>
            <p class="tiny" style="margin-top:12px">
              <span class="badge sm" :class="btResult.minute_available ? 'ok' : 'warn'">
                {{ btResult.minute_available ? "分钟线精确撮合" : "日线近似（非真实业绩）" }}</span>
              <span class="tiny muted" style="margin-left:6px">
                成交 {{ btResult.trades }} 笔 / 平仓 {{ btResult.closed_trades }} 笔</span>
            </p>
            <table style="max-width:480px">
              <thead><tr><th>指标</th><th>值</th></tr></thead>
              <tbody>
                <tr v-for="(v, k) in (btResult.metrics || {})" :key="k">
                  <td class="tiny">{{ k }}</td>
                  <td class="tiny">{{ typeof v === "number" ? Number(v).toFixed(4) : v }}</td>
                </tr>
              </tbody>
            </table>
          </template>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ---- 顶部概览：标签-值 inline-flex 横条 ---- */
.tp-bar { display: flex; flex-wrap: wrap; gap: 8px 28px; align-items: center; }
.tp-item { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; }
.tp-label { color: var(--text-2); font-size: 12px; }

/* ---- 单面板 tab（与系统设置页风格一致） ---- */
.tp-panel { padding-bottom: 20px; }
.tp-tabs {
  display: flex; gap: 8px; flex-wrap: wrap;
  border-bottom: 1px solid var(--border); padding-bottom: 12px; margin-bottom: 14px;
}
.tp-tab {
  padding: 8px 16px; border: 1px solid var(--border); border-radius: 9px;
  background: var(--bg-elev); color: var(--text-2); cursor: pointer;
  font-size: 14px; transition: all 0.15s;
}
.tp-tab:hover { border-color: var(--primary); }
.tp-tab.on { background: var(--primary); border-color: var(--primary); color: #fff; font-weight: 700; }

/* ---- 参数组子 tab ---- */
.tp-cfgbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.tp-subtabs { display: flex; gap: 6px; flex-wrap: wrap; }
.tp-subtab {
  padding: 5px 12px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--bg-elev); color: var(--text-2); cursor: pointer;
  font-size: 13px; font-weight: 500; transition: all 0.15s;
}
.tp-subtab:hover { border-color: var(--primary); }
.tp-subtab.on {
  background: var(--primary-soft); border-color: color-mix(in srgb, var(--primary) 45%, transparent);
  color: var(--primary); font-weight: 700;
}

/* ---- 未保存改动数角标（低饱和柔和风格） ---- */
.tp-dot {
  display: inline-block; min-width: 16px; height: 16px; padding: 0 4px;
  border-radius: 8px; margin-left: 6px; font-style: normal;
  background: color-mix(in srgb, var(--primary) 18%, transparent);
  border: 1px solid color-mix(in srgb, var(--primary) 55%, transparent);
  color: var(--primary); font-size: 11px; line-height: 14px; text-align: center;
}
.tp-tab.on .tp-dot {
  background: rgba(255, 255, 255, 0.22); border-color: rgba(255, 255, 255, 0.65); color: #fff;
}

.tp-h4 { margin: 0 0 10px; font-size: 13px; display: flex; align-items: center; gap: 8px; }
.tp-h4 .sub { color: var(--text-2); font-size: 12px; font-weight: 400; }
</style>
