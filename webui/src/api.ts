// 统一 API 客户端：默认走同源相对路径 /api（由 vite dev proxy 或后端直托前端转发到 8000），
// 不再写死 http://localhost:7099 —— 避免在不同访问源(localhost/127.0.0.1/预览面板)下跨域或被拦截。
// 如需直连可设 VITE_API_BASE（不含结尾斜杠，如 http://127.0.0.1:8000）。

const BASE: string = (import.meta as any).env?.VITE_API_BASE || "";

async function req<T = any>(
  method: string,
  path: string,
  body?: any,
  params?: Record<string, any>,
  timeoutMs = 30000
): Promise<T> {
  let url = `${BASE}/api${path}`;
  if (params && Object.keys(params).length) {
    const qs = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
      .join("&");
    if (qs) url += (path.includes("?") ? "&" : "?") + qs;
  }
  // 统一超时兜底：慢接口（券商探测/行情拉取/LLM 等）最多等待 timeoutMs，
  // 到期即中断并抛出明确错误（tryReq 会 toast 提示），避免页面无限挂起。
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
  } catch (e: any) {
    if (e?.name === "AbortError") {
      throw new Error(`请求超时（${Math.round(timeoutMs / 1000)}s）：${method} ${path}`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
  const text = await res.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const detail = data?.detail || text || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data as T;
}

function q(mode?: string) {
  return mode ? { mode } : {};
}

export const api = {
  // ---------------- 系统 / 总览 / 风控总开关 / 调度 / 密钥
  overview: (mode?: string) => req("GET", "/overview", undefined, q(mode)),
  health: (mode?: string, notify = false) =>
    req("GET", "/health", undefined, { ...q(mode), notify }),
  killswitch: (mode?: string) => req("GET", "/killswitch", undefined, q(mode)),
  setKillswitch: (mode: string, action: string, reason = "") =>
    req("POST", "/killswitch", { action, reason }, q(mode)),
  schedulerJobs: (mode?: string) => req("GET", "/scheduler/jobs", undefined, q(mode)),
  schedulerUpdateJob: (body: any) => req("PUT", "/scheduler/job", body),
  schedulerRun: (name: string, mode: string, tradeDate?: string) =>
    req("POST", "/scheduler/run", undefined, { name, ...q(mode), trade_date: tradeDate }),
  secrets: () => req("GET", "/secrets"),
  setSecret: (key: string, value: string) => req("PUT", "/secrets", { key, value }),

  // ---------------- LLM 管理
  llmConfig: () => req("GET", "/llm/config"),
  llmSetEnabled: (enabled: boolean) => req("PUT", "/llm/enabled", { enabled }),
  llmAddProvider: (p: any) => req("POST", "/llm/providers", p),
  llmDelProvider: (id: string) => req("DELETE", `/llm/providers/${id}`),
  llmAddModel: (m: any) => req("POST", "/llm/models", m),
  llmDelModel: (id: string) => req("DELETE", `/llm/models/${id}`),
  llmAddScene: (s: any) => req("POST", "/llm/scenes", s),
  llmDelScene: (id: string) => req("DELETE", `/llm/scenes/${id}`),
  llmSetSelection: (s: any) => req("PUT", "/llm/selection", s),
  llmTest: (prompt: string, model?: string, scene?: string) =>
    req("POST", "/llm/test", { prompt, model, scene }),

  // ---------------- 数据源
  datasource: (mode?: string) => req("GET", "/datasource", undefined, q(mode)),
  setDatasourcePriority: (priority: any, mode: string) =>
    req("PUT", "/datasource/priority", { priority }, q(mode)),

  // ---------------- 参数配置
  configAll: () => req("GET", "/config"),
  configSection: (path: string) => req("GET", `/config/section/${path}`),
  configPatch: (patches: any[]) => req("PUT", "/config/patch", { patches }),

  // ---------------- 风控阈值
  riskGates: (mode?: string) => req("GET", "/risk/gates", undefined, q(mode)),
  riskSetGates: (body: any, mode: string) => req("PUT", "/risk/gates", body, q(mode)),

  // ---------------- 行情
  symbols: (mode?: string) => req("GET", "/market/symbols", undefined, q(mode)),
  bars: (symbols: string, start: string, mode?: string, end?: string, limit = 400, adjust = "QFQ") =>
    req("GET", "/market/bars", undefined, { symbols, start, ...q(mode), end, limit, adjust }),
  kline: (symbol: string, period: string, start: string, mode?: string, end?: string, limit = 400, adjust = "QFQ") =>
    req("GET", "/market/kline", undefined, { symbol, period, start, ...q(mode), end, limit, adjust }),
  timeline: (symbol: string, mode?: string) =>
    req("GET", "/market/timeline", undefined, { symbol, ...q(mode) }),
  quote: (symbols: string, mode?: string) =>
    req("GET", "/market/quote", undefined, { symbols, ...q(mode) }),
  news: (mode?: string, symbols?: string, start?: string, end?: string, limit = 50) =>
    req("GET", "/market/news", undefined, { ...q(mode), symbols, start, end, limit }),
  events: (mode?: string, symbols?: string, start?: string, end?: string, limit = 50) =>
    req("GET", "/market/events", undefined, { ...q(mode), symbols, start, end, limit }),

  // ---------------- 交易
  positions: (mode?: string) => req("GET", "/trade/positions", undefined, q(mode)),
  positionsReset: (mode: string) => req("DELETE", "/trade/positions", undefined, q(mode)),
  broker: (mode?: string) => req("GET", "/trade/broker", undefined, q(mode)),
  orders: (mode?: string, date?: string) =>
    req("GET", "/trade/orders", undefined, { ...q(mode), date }),
  intents: (mode?: string, date?: string) =>
    req("GET", "/trade/intents", undefined, { ...q(mode), date }),
  reconcile: (mode?: string, date?: string) =>
    req("GET", "/trade/reconcile", undefined, { ...q(mode), date }),
  reconcileAck: (body: any, mode: string) => req("POST", "/trade/reconcile/ack", body, q(mode)),
  submitIntent: (body: any, mode: string) => req("POST", "/trade/intent", body, q(mode)),
  runPlan: (mode: string, tradeDate?: string, research = false) =>
    req("POST", "/trade/plan", undefined, { ...q(mode), trade_date: tradeDate, research }),

  // ---------------- 策略
  strategyCatalog: () => req("GET", "/strategy/catalog"),
  strategyManagement: (mode?: string) => req("GET", "/strategy/management", undefined, q(mode)),
  strategyDraft: (body: any, mode?: string) => req("POST", "/strategy/instances/draft", body, q(mode)),
  strategyPublish: (body: any, mode?: string) => req("POST", "/strategy/instances/publish", body, q(mode)),
  strategyRollback: (id: string, version: string, mode?: string) => req("POST", `/strategy/instances/${id}/rollback/${version}`, undefined, q(mode)),
  strategyEnabled: (id: string, enabled: boolean, mode?: string) => req("POST", `/strategy/instances/${id}/enabled`, { enabled }, q(mode)),
  strategyPool: (mode?: string) => req("GET", "/strategy/pool", undefined, q(mode)),
  strategyRebalance: (mode: string) => req("POST", "/strategy/rebalance", undefined, q(mode)),
  strategyEvolve: (mode: string, date?: string) =>
    req("POST", "/strategy/evolve", { date }, q(mode)),
  strategyReview: (mode: string, date?: string) =>
    req("POST", "/strategy/review", { trade_date: date }, q(mode)),

  // ---------------- 策略推荐（选股漏斗）
  selectionPicks: (mode?: string) => req("GET", "/selection/picks", undefined, q(mode)),
  selectionFinal: (mode?: string, date?: string) =>
    req("GET", "/selection/final", undefined, { ...q(mode), date }),
  selectionStrategies: (mode?: string) => req("GET", "/selection/strategies", undefined, q(mode)),
  selectionRun: (body: any, mode: string) => req("POST", "/selection/run", body, q(mode)),
  selectionResearch: (mode: string) => req("POST", "/selection/research", {}, q(mode)),
  selectionWatchlist: (mode?: string) => req("GET", "/selection/watchlist", undefined, q(mode)),
  selectionWatchlistAdd: (symbols: string[], mode: string) =>
    req("POST", "/selection/watchlist", { add: symbols }, q(mode)),
  selectionWatchlistSet: (symbols: string[], mode: string) =>
    req("POST", "/selection/watchlist", { symbols, replace: true }, q(mode)),
  selectionWatchlistDel: (symbols: string[], mode: string) =>
    req("DELETE", "/selection/watchlist", { symbols }, q(mode)),

  // ---------------- 绩效/分析报告
  reportList: (kind?: string) => req("GET", "/report/list", undefined, { kind }),
  reportContent: (name: string) => req("GET", "/report/content", undefined, { name }),

  // ---------------- 短期/长期记忆
  memory: (mode?: string) => req("GET", "/memory", undefined, q(mode)),

  // ---------------- 尾盘选股法（独立短线策略）
  tailpickStatus: (mode?: string) => req("GET", "/tailpick/status", undefined, q(mode)),
  tailpickConfig: (body: any) => req("PUT", "/tailpick/config", body),
  tailpickBacktest: (body: any, mode: string) =>
    req("POST", "/tailpick/backtest", body, q(mode)),

  // ---------------- 策略实验室（打板/二板/低吸/趋势等独立策略）
  strategylabStatus: (mode?: string) => req("GET", "/strategylab/status", undefined, q(mode)),
  strategylabBacktest: (body: any, mode: string) =>
    req("POST", "/strategylab/backtest", body, q(mode)),
  strategylabScan: (body: any, mode: string) =>
    req("POST", "/strategylab/scan", body, q(mode)),
  strategylabReport: (sid: string, mode: string) =>
    req("GET", `/strategylab/${sid}/report`, undefined, q(mode)),
  strategylabPaperCandidate: (sid: string, mode: string) =>
    req("POST", `/strategylab/${sid}/paper-candidate`, {}, q(mode)),
  strategylabSetEnabled: (sid: string, enabled: boolean) =>
    req("PUT", `/strategylab/${sid}/enabled`, { enabled }),

  // ---------------- 回测 / 任务
  backtestRun: (body: any, mode: string) => req("POST", "/backtest/run", body, q(mode)),
  job: (id: string) => req("GET", `/jobs/${id}`),
  jobs: (limit = 20) => req("GET", "/jobs", undefined, { limit }),

  // ---------------- 事件驱动
  eventNews: (mode?: string, symbols?: string, start?: string, end?: string, limit = 50) =>
    req("GET", "/event/news", undefined, { ...q(mode), symbols, start, end, limit }),
  eventEvents: (mode?: string, symbols?: string, start?: string, end?: string, limit = 50) =>
    req("GET", "/event/events", undefined, { ...q(mode), symbols, start, end, limit }),
  eventHardNegatives: (mode?: string, symbols?: string, start?: string, end?: string) =>
    req("GET", "/event/hard-negatives", undefined, { ...q(mode), symbols, start, end }),

  // ---------------- 通知
  notifyChannels: () => req("GET", "/notify/channels"),
  notifySetChannels: (channels: any[]) => req("PUT", "/notify/channels", { channels }),
  notifyTest: (body: any, mode: string) => req("POST", "/notify/test", body, q(mode)),
};

export default api;
