// 后端枚举值 → 中文展示的统一字典（全项目唯一来源）。
//
// 边界：本文件只做「展示层翻译」。所有提交给接口的值、v-model 绑定值、
// 配色判断入参仍然使用原始英文枚举码，后端契约与内部逻辑零改动。
//
// 用法：
//   {{ cn(ORDER_STATUS, o.status) }}                     未命中一律回退原始值，不会显示空白
//   <span :title="cnTitle(ORDER_STATUS, o.status)">      译文与原文不同时挂 tooltip，便于与日志对照
//
// 为什么按域分字典、不做全局合并：CLOSED（熔断关闭 vs 休市）、ERROR（告警级别 vs 任务失败）
// 等取值在不同域含义不同，全局合并会串味。调用点显式指定域，语义不会歧义。

export type Dict = Record<string, string>;

// 后端同一含义的取值在不同接口里大小写不一致（如 action 既有 BUY 也有 buy），
// 这里为每个字典补齐大写/小写变体，调用点无需关心来源大小写。
function withCase(d: Dict): Dict {
  const out: Dict = { ...d };
  for (const [k, v] of Object.entries(d)) {
    const up = k.toUpperCase();
    const lo = k.toLowerCase();
    if (!(up in out)) out[up] = v;
    if (!(lo in out)) out[lo] = v;
  }
  return out;
}

// ---------- 风控 / KillSwitch ----------
// 来源：qmt_trade/risk/killswitch.py KillMode
export const KILL_MODE: Dict = withCase({
  NORMAL: "正常",
  REDUCE_ONLY: "只减不加",
  FLATTEN: "强制清仓",
});

// KillSwitch 操作动作 → 中文（确认弹窗/Toast 文案用）。
// 来源：server /system/killswitch 接受的 action 值（engage/flatten/reset）
export const KILL_ACTION: Dict = withCase({
  engage: "降级为只减不加",
  flatten: "强制清仓",
  reset: "恢复正常",
});

// 告警级别。来源：qmt_trade/ops/notify.py Level、qmt_trade/scheduler/jobs.py severity
export const LEVEL: Dict = withCase({
  DEBUG: "调试",
  INFO: "提示",
  WARN: "警告",
  ERROR: "严重",
  CRITICAL: "致命",
});

// 拦截层。来源：qmt_trade/execution/service.py ExecutionResult.rejected_by
export const REJECTED_BY: Dict = withCase({
  guard: "下单护栏",
  risk: "风控门",
  gateway: "券商通道",
  sizer: "仓位计算",
});

// ---------- 交易 ----------
// 来源：qmt_trade/core/trading.py Side
export const SIDE: Dict = withCase({
  BUY: "买入",
  SELL: "卖出",
});

// 来源：qmt_trade/core/trading.py OrderType
export const ORDER_TYPE: Dict = withCase({
  LIMIT: "限价",
  MARKET: "市价",
});

// 来源：qmt_trade/core/trading.py OrderStatus；GUARD_BLOCKED / FAILED 为
// qmt_trade/execution/service.py 落库时追加的字面量状态
export const ORDER_STATUS: Dict = withCase({
  PENDING: "待提交",
  SUBMITTED: "已报",
  PART_FILLED: "部分成交",
  FILLED: "已成交",
  REJECTED: "已拒单",
  CANCELLED: "已撤单",
  GUARD_BLOCKED: "护栏拦截",
  FAILED: "执行失败",
});

// 意图/建议动作。来源：qmt_trade/brain/schemas.py Intent.action
export const TRADE_ACTION: Dict = withCase({
  BUY: "买入",
  SELL: "卖出",
  HOLD: "持有",
  WATCH: "观察",
});

// 确信度。来源：qmt_trade/brain/schemas.py Literal["LOW","MEDIUM","HIGH"]；
// 部分接口用 mid 表示中档
export const CONVICTION: Dict = withCase({
  HIGH: "高确信",
  MEDIUM: "中确信",
  MID: "中确信",
  LOW: "低确信",
});

// 研判立场。来源：qmt_trade/brain 各 agent 投票结果
export const VERDICT: Dict = withCase({
  BULL: "看多",
  BEAR: "看空",
  NEUTRAL: "中性",
  BUY: "看多",
  SELL: "看空",
  HOLD: "持有",
});

export const STANCE: Dict = withCase({
  BULL: "看多",
  BEAR: "看空",
  NEUTRAL: "中性",
});

// 投票 agent 名称
export const AGENT: Dict = withCase({
  technical: "技术面",
  fundamental: "基本面",
  moneyflow: "资金面",
  sentiment: "市场情绪",
  research_manager: "研究主管",
  portfolio_manager: "组合经理",
  risk_officer: "风控官",
});

// ---------- 任务 / 调度 ----------
// 调度器上次执行结果。来源：qmt_trade/scheduler/jobs.py 写入 job:<name>:last_status
export const RUN_STATUS: Dict = withCase({
  OK: "成功",
  FAIL: "失败",
  SKIP: "跳过",
  "-": "未执行",
});

// 任务/回测作业状态。来源：server/routers/backtests.py
export const JOB_STATUS: Dict = withCase({
  pending: "排队中",
  queued: "排队中",
  running: "运行中",
  done: "已完成",
  succeeded: "已完成",
  error: "失败或中断",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
});

// 调度类型。来源：server/routers/overview.py
export const JOB_KIND: Dict = withCase({
  cron: "定时",
  interval: "高频",
});

// 调度任务启用状态。来源：server/routers/overview.py /scheduler/jobs 的 status 字段
// （由 qmt_trade/scheduler/runner.py JobSpec.status 产出）
// running  = 在跑；
// paused   = 用户在工作台手动暂停（可就地恢复）；
// disabled = 绑定策略未启用，随策略门禁摘除（要去「策略实验室」开策略）。
export const JOB_STATE: Dict = withCase({
  running: "运行中",
  paused: "已暂停",
  disabled: "已停用",
});

// 运行模式。来源：server 全局 mode
export const MODE: Dict = withCase({
  sim: "模拟数据",
  paper: "模拟盘",
  live: "实盘",
});

// ---------- 策略池 ----------
// 来源：qmt_trade/evolution/pool.py Status Literal；
// PROBATION / OBSERVATION / DISABLED 为历史数据与前端徽章兼容保留
export const POOL_STATUS: Dict = withCase({
  ACTIVE: "在用",
  SHADOW: "影子",
  PROBATION: "考察中",
  QUARANTINE: "隔离",
  RETIRED: "已退役",
  DISABLED: "已停用",
  OBSERVATION: "观察中",
});

// ---------- 行情 / 数据 ----------
// 来源：qmt_trade/datahub/types.py Freq
export const FREQ: Dict = withCase({
  "1d": "日线",
  "1m": "1分钟",
  "5m": "5分钟",
  "15m": "15分钟",
  "30m": "30分钟",
  "60m": "60分钟",
});

// 来源：qmt_trade/datahub/types.py Adjust
export const ADJUST: Dict = withCase({
  none: "不复权",
  qfq: "前复权",
  hfq: "后复权",
});

// 来源：qmt_trade/datahub/providers/base.py Capability
export const CAPABILITY: Dict = withCase({
  bars: "K线行情",
  tick: "逐笔快照",
  fundamentals: "财务基本面",
  instruments: "合约列表",
  news: "新闻资讯",
  events: "公司事件",
  index: "指数行情",
  money_flow: "资金流向",
});

// 数据源熔断状态。来源：qmt_trade/datahub 熔断器 state
export const CIRCUIT_STATE: Dict = withCase({
  closed: "正常",
  half_open: "半开试探",
  open: "已熔断",
  OK: "正常",
  FAIL: "异常",
});

// 来源：qmt_trade/core/instruments.py Board
export const BOARD: Dict = withCase({
  MAIN: "主板",
  GEM: "创业板",
  STAR: "科创板",
  BSE: "北交所",
  UNKNOWN: "未知",
});

// 来源：qmt_trade/core/clock.py Session
export const SESSION: Dict = withCase({
  CLOSED: "休市",
  PRE_OPEN: "开盘前",
  AUCTION_OPEN: "集合竞价",
  PRE_TRADE: "盘前准备",
  MORNING: "上午盘",
  LUNCH: "午间休市",
  AFTERNOON: "下午盘",
  AUCTION_CLOSE: "尾盘竞价",
  POST_CLOSE: "收盘后",
});

// 来源：qmt_trade/features/regime.py Regime
export const REGIME: Dict = withCase({
  TREND_UP: "上行趋势",
  RANGE: "震荡",
  TREND_DOWN: "下行趋势",
  RISK_OFF: "避险",
});

// ---------- 复盘 / 报告（经验标签·策略·因子） ----------
// 复盘经验标签 → 中文。大写来源 review.py Lesson.tag；小写来源 reflection.py
// _long_term_candidates 的 tag（factor_ic/regime/selection/recent/lesson），仅在记忆面板 pill 上出现。
export const LESSON_TAG: Dict = withCase({
  FACTOR_INVERTED: "因子反向",
  SAMPLE_TOO_SMALL: "样本过小",
  COST_DRAG_HIGH: "费用拖累偏高",
  STOP_TOO_TIGHT: "止损过紧",
  CONVICTION_INVERTED: "确信度分档失效",
  CUT_WINNERS_EARLY: "盈利单过早了结",
  factor_ic: "因子IC",
  regime: "市场状态",
  selection: "选股有效性",
  recent: "近期经验",
  lesson: "经验",
});

// 策略预设 / 独立策略 id → 中文。来源：qmt_trade/core/strategies.py _PRESET_META / _STANDALONE_META
export const STRATEGY: Dict = {
  balanced: "均衡多因子",
  momentum_breakout: "动量突破",
  value_quality: "价值质量",
  moneyflow_resonance: "资金流共振",
  low_vol_defensive: "低波防御",
  tail_pick: "尾盘选股法",
  limit_up: "打板策略",
  second_board: "二板龙头战法",
  dip_buy: "尾盘潜伏低吸",
  trend_buy: "趋势类买点",
  etf_t0: "ETF T+0 日内回转",
  stock_t0: "个股做T",
};

// 因子大类 → 中文。来源：qmt_trade/core/strategy_catalog.py cat_zh
export const CAT_CN: Dict = {
  momentum: "量价动量",
  moneyflow: "资金流",
  sentiment: "消息情绪",
  fundamental: "基本面",
  quality: "质量",
};

// 因子名 → 中文（全项目唯一来源）。来源：qmt_trade/features/factors/*.py 的 @_R(name, category, desc)
// 注册，以及 SelectionView 失效表达式(invalidation_checks) 里出现的别名。
export const FACTOR: Dict = {
  // 量价动量
  ret_20d: "20日收益率", ret_60d: "60日收益率", ret_5d_rev: "5日短期反转",
  mom_12_1: "12-1动量", ma_align: "均线多头排列强度", ma_bullish_score: "均线多头排列分",
  bias_20: "20日乖离率", breakout_60: "距60日高点接近度",
  close_ratio_60d_high: "价格相对60日高点比", distance_from_60d_high: "距60日高点距离",
  high_60d: "60日最高价", atr_ratio: "ATR占价比", downside_vol: "20日下行波动率",
  max_drawdown_60: "60日最大回撤", vol_ratio_5_20: "量比5/20",
  turnover_stability: "换手率稳定性", amount_liquidity: "20日均成交额对数",
  price_volume_corr: "20日价量相关性", limit_up_count_20: "近20日涨停次数",
  turnover_rate: "换手率",
  // 消息情绪
  news_sentiment_5d: "近5日新闻情感", news_heat_5d: "近5日新闻条数",
  event_sentiment_20d: "近20日公告情感", hard_negative_flag: "严重负面事件标记",
  hard_negative_event: "严重负面事件", industry_momentum: "行业动量",
  // 基本面 / 质量
  roe: "净资产收益率", gross_margin: "毛利率", profit_yoy: "净利润同比增速",
  net_profit_yoy: "净利同比", revenue_yoy: "营收同比增速",
  earnings_yield: "盈利收益率", ep_ratio: "盈利收益率",
  debt_safety_score: "偿债安全分", debt_safety: "偿债安全分",
  // 资金流
  main_net_5d: "主力资金5日净流入", main_net_10d: "主力资金10日净流入",
  main_net_ratio: "主力净流入占成交比", large_order_ratio: "近5日大单占比均值",
  flow_consistency: "资金流方向一致性",
  // 表达式 / 其他别名
  close_price: "最新收盘价", close: "最新收盘价", price: "最新收盘价", entry: "买入价",
  score: "综合分", score_percentile: "综合分全市场分位",
  missing_fields_count: "数据缺失字段数", days_between: "间隔天数",
};

// ---------- 事件 ----------
// 来源：qmt_trade/datahub/types.py EventCategory
export const EVENT_CATEGORY: Dict = withCase({
  EARNINGS_FORECAST: "业绩预告",
  EARNINGS_REPORT: "业绩报告",
  RESTRUCTURING: "资产重组",
  SHARE_REDUCTION: "股东减持",
  INVESTIGATION: "立案调查",
  REGULATORY_PENALTY: "监管处罚",
  SUSPENSION: "停牌",
  UNLOCK: "限售解禁",
  DIVIDEND: "分红送转",
  CONTRACT: "重大合同",
  POLICY: "政策动向",
  OTHER: "其他",
});

// 来源：qmt_trade/core/events.py EventType
export const EVENT_TYPE: Dict = withCase({
  TICK: "逐笔行情",
  BAR: "K线",
  ORDER_SUBMITTED: "订单已报",
  ORDER_FILLED: "订单成交",
  ORDER_PARTIAL: "订单部分成交",
  ORDER_CANCELLED: "订单撤单",
  ORDER_REJECTED: "订单拒单",
  TRADE: "成交回报",
  POSITION_CHANGED: "持仓变动",
  GATEWAY_CONNECTED: "通道已连接",
  GATEWAY_DISCONNECTED: "通道已断开",
  RISK_REJECTED: "风控拦截",
  RISK_ALERT: "风险告警",
  KILLSWITCH_CHANGED: "熔断档位变更",
  RECONCILE_FAILED: "对账失败",
  NEWS: "新闻",
  CORP_ACTION: "公司行动",
  INTENT_CREATED: "意图生成",
  PLAN_CREATED: "计划生成",
  ERROR: "错误",
  HEARTBEAT: "心跳",
});

// ---------- 回测 / 成本 ----------
// 来源：qmt_trade/execution/costs.py SlippageModel
export const SLIPPAGE_MODEL: Dict = withCase({
  FIXED: "固定滑点",
  VOLUME_RATIO: "成交量占比",
});

// ---------- 通知 / LLM / 配置 ----------
// 通知渠道类型。来源：webui NotifyView PRESETS 与 server/routers/notify.py
export const CHANNEL: Dict = withCase({
  feishu: "飞书群机器人",
  wecom: "企业微信群机器人",
  dingtalk: "钉钉群机器人",
  console: "控制台输出",
});

// LLM 供应商类型。来源：qmt_trade/brain/llm/registry.py ProviderConfig.type
export const PROVIDER_TYPE: Dict = withCase({
  openai_like: "OpenAI 兼容接口",
  openai: "OpenAI 官方",
  anthropic: "Anthropic",
  ollama: "本地 Ollama",
  mock: "本地模拟",
});

// 配置项值类型。来源：server/routers/config.py
export const CONFIG_KIND: Dict = withCase({
  bool: "开关",
  number: "数值",
  text: "文本",
  json: "JSON",
});

// ---------- 查表函数 ----------

// 空值统一显示为 "-"，避免表格出现空白单元格
const EMPTY = "-";

function raw(v: unknown): string {
  if (v === null || v === undefined) return "";
  const s = String(v).trim();
  return s;
}

/** 在指定域字典中查中文译文；未命中或空值回退原始值（空值回退 "-"）。 */
export function cn(dict: Dict, v: unknown): string {
  const s = raw(v);
  if (!s) return EMPTY;
  return dict[s] ?? s;
}

/**
 * 返回用于 :title 悬停的原始英文码。
 * 仅当译文与原文不同才返回原文，否则返回空串（不挂无意义的 tooltip）。
 */
export function cnTitle(dict: Dict, v: unknown): string {
  const s = raw(v);
  if (!s) return "";
  const t = dict[s];
  return t && t !== s ? s : "";
}

// ---------- 混排文本就地翻译 ----------
// 复盘报告正文/记忆面板是中英混排的自由文本（如「当前 TREND_DOWN 市况，建议切换至
// `low_vol_defensive` 策略」「因子 `ret_20d_q` 方向可能反了」）。这里按标识符 token 逐个查表翻译，
// 仅替换已收录的枚举码/因子名/策略 id，其余原样保留，不改后端契约。
function trToken(tok: string): string {
  // X_q → <X>分位（engine.py 截面分位化因子，如 ret_20d_q）
  if (tok.endsWith("_q")) {
    const base = tok.slice(0, -2);
    if (FACTOR[base]) return FACTOR[base] + "分位";
  }
  // cat_X → <X>类因子（大类聚合因子，如 cat_momentum）
  if (tok.startsWith("cat_")) {
    const c = tok.slice(4);
    if (CAT_CN[c]) return CAT_CN[c] + "类因子";
  }
  if (FACTOR[tok]) return FACTOR[tok];
  if (STRATEGY[tok]) return STRATEGY[tok];
  // 经验标签/市况/级别只译全大写形态（正文里它们以大写出没，如 [WARN]、FACTOR_INVERTED、TREND_DOWN），
  // 避免误伤正文中的普通英文小写词；小写标签（factor_ic/regime/…）只在记忆面板 pill 上单独翻译。
  if (tok === tok.toUpperCase()) {
    return LESSON_TAG[tok] ?? REGIME[tok] ?? LEVEL[tok] ?? tok;
  }
  return tok;
}

/** 就地把混排文本中的英文码译为中文；仅替换已收录 token，其余原样保留。 */
export function translateText(s: unknown): string {
  const t = raw(s);
  if (!t) return "";
  return t.replace(/[A-Za-z_][A-Za-z0-9_]*/g, (m) => trToken(m));
}
