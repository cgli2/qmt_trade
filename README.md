# my_qmt_trade — LLM 驱动的 A 股全自动交易系统

基于 LLM 多智能体研判 + 规则化风控/仓位/执行的 A 股自动交易系统。设计目标：**高胜率、可回放、失败安全**。
提供 **WebUI + REST API**（推荐）与 **CLI** 两套操作界面，支持 sim / paper / live 三种模式。

完整设计见 [`docs/系统设计文档-全文.md`](docs/系统设计文档-全文.md)，
部署与日常运维见 [`docs/部署与使用说明.md`](docs/部署与使用说明.md)。

> ## 免责声明
> **本项目仅供学习、研究与技术交流使用，不构成任何投资建议、投资邀约、收益承诺或交易依据。**
> 
> 证券及其他金融市场交易存在风险，使用者应独立判断、自行决策，并自行承担全部风险与后果。因使用本项目的代码、策略、数据、分析结果或其他相关内容而产生的任何亏损、损失或责任，项目作者及贡献者概不负责。
> 
> 使用实盘功能前，请充分了解相关规则与风险，并优先在模拟盘中完成验证。

---

## 1. 一句话架构

```
数据源(QMT/Tushare/Akshare/Mock)  →  DataHub(PIT+质量校验+缓存)
  → 选股漏斗(L2-a,纯因子)  →  LLM 研判(L2-b,可选)
  → 风控+仓位(P1 只发意图,无权下单)  →  执行网关(四道闸门)
  → 调度器(交易日程)  →  运维(监控/通知/报告/进化)
  → FastAPI 后端(常驻调度 + JSON API)  →  Vue WebUI
```

**关键设计红线**（详见设计文档第 3 节）：
- **P1**：LLM 只产出 `TradeIntent`（买/卖/仓位/止损/止盈/失效条件），**无权直接下单、改仓或绕开风控**。
- **P4 失败安全**：关键任务(data_sync / reconcile / intraday)失败 → 自动降级 `REDUCE_ONLY`；通知/通道异常绝不带崩主流程。
- **P5 成本熔断**：LLM 预算超 `daily_cny` 直接停用，系统降级为纯因子模式继续跑。
- **P6 可复现**：所有数据接口带 `asof`（Point-in-Time）时间切片，防未来函数；决策全程 `trace_id` 串联可审计。
- **P7 同代码路径**：回测不是另写模拟成交，而是复用实盘执行层，只换 `SimGateway`。
- **live 装配失败直接拉闸报错，绝不降级用假数据**。

---

## 2. 环境要求与安装

| 组件 | 版本 | 用途 |
|---|---|---|
| Python | 3.11+ | 后端 + 调度器 |
| Node.js | 18+（推荐 20） | 构建前端 WebUI |
| QMT 客户端 | 券商版（仅 live） | 提供 xtquant 网关（仅 Windows） |

```bash
# Python 依赖
python -m venv .venv
.venv\Scripts\activate           # Windows；Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt

# 前端构建（产物 webui/dist，由后端同源托管，无需单独起前端服务）
cd webui
npm install
npm run build
cd ..
```

> live 实盘依赖的 `xtquant` 随 QMT 客户端分发（仅 Windows，无法 pip 安装），不在 requirements.txt 内。

---

## 3. 配置

配置分层加载，优先级：**环境变量 > `config/.env` > `config/settings.yaml` / `config/llm.yaml` > 代码默认值**。

> **LLM 配置已独立**：模型/平台/场景统一由 `config/llm.yaml` 管理（详见 §3.3）——
> 可配**多个平台 provider**、**多种模型**，并按**场景动态智能选模**；API Key 只存环境变量名，绝不落盘。

### 3.1 非敏感配置：`config/settings.yaml`
已随仓库提供，覆盖数据优先级、因子权重、风控闸门、仓位、执行成本、调度时刻等（**不含 `llm` 段**）。
常用调整点：
- `scheduler.jobs.*`：各任务触发时刻（如 `data_sync: "06:25"`）。
- `datahub.priority`：数据源优先级与熔断阈值。
- `risk.*` / `portfolio.*` / `execution.*`：风控闸门、仓位、执行成本等。

### 3.2 敏感配置：`config/.env`（**不入库，需自建**）
敏感项只从环境变量读取，绝不写进 YAML。复制以下内容创建 `config/.env`：

```dotenv
# ---------- LLM（不填则自动退回 MockLLM，系统照常跑）----------
# 变量名需与 llm.yaml 里各 provider 的 api_key_env 一致
DEEPSEEK_API_KEY=sk-xxxxxxxx               # deepseek 平台
DASHSCOPE_API_KEY=sk-xxxxxxxx              # 通义 qwen 平台
MOONSHOT_API_KEY=sk-xxxxxxxx               # moonshot 平台
OPENAI_API_KEY=sk-xxxxxxxx                 # openai 平台

# ---------- 行情数据源（按需）----------
TUSHARE_TOKEN=your_tushare_token          # tushare 源需要

# ---------- 实盘（仅 --mode live 需要）----------
QMT_ACCOUNT_ID=88888888                   # 资金账号，不落盘

# ---------- QMT 客户端路径（可选，优先于 settings.yaml）----------
QMT_MINI_PATH=D:/国金QMT交易端模拟/userdata_mini

# ---------- 通知（可选，启用 wecom/dingtalk 通道时填）----------
WECOM_WEBHOOK=https://qyapi.weixin.qq.com/...
DINGTALK_WEBHOOK=https://oapi.dingtalk.com/...
```

> 加载规则：`.env` 中 `KEY=VALUE`（可加 `export ` 前缀，支持 `#` 注释）。
> 也可用环境变量覆盖任意 YAML 项，格式 `QMT_<层级用__连接>`：
> `QMT_RISK__GATE1__MAX_POSITIONS=8` 等价于把 `risk.gate1.max_positions` 改为 8。

### 3.3 LLM 独立管理：`config/llm.yaml`

该文件**只管 LLM**，与交易/风控配置彻底解耦。四大能力：

1. **多平台（providers）**：`deepseek` / `qwen` / `moonshot` / `openai` 等，统一 `type: openai_like`，
   各自 `base_url` + `api_key_env`（仅存环境变量名）。想加新平台，往 `providers` 里加一项即可。
2. **多模型（models）**：每个模型绑定 provider，带 `capabilities`（general/fast/cheap/reasoning/deep/code）、
   `context_window`、`price_per_1k_tokens`。成本按人民币 `input/output` 单价精确计费。
3. **场景智能选模（scenes + selection）**：声明「什么任务用什么模型」——
   如 `market_analysis`/`risk_assess` 偏好 `reasoning`+`deep`，`quick_classify`/`research_summary` 偏好 `fast`+`cheap`。
   `ModelSelector` 按 **capability 匹配度(0.40) + 健康度(0.40) + 单价(0.20)** 加权打分排序；
   坏模型（连续失败/熔断）会被自动降级到候选链后面甚至出局。
4. **失败安全（P4/P5）**：主选失败自动 fallback；连续失败达阈值自动熔断（默认 5 次 / 300s）；
   当日成本超 `budget.daily_cny`（默认 ¥30）停用 LLM 层降级纯因子；配置丢失/无 Key 整体退回 MockLLM，系统不崩。

常用调整点：

| 配置项 | 作用 |
|---|---|
| `enabled` | `false` 一键关闭 LLM，系统降级纯因子模式（P5） |
| `default_model` | 无场景/无显式模型时的兜底模型 |
| `scenes.<id>.candidates` | 某场景的候选模型顺序 |
| `selection.capability_weight/health_weight/cost_weight` | 智能选模三项权重 |
| `budget.daily_cny` / `monthly_cny` | 成本熔断阈值 |
| `cache_enabled` / `cache_path` | LLM 响应缓存（回测可重放、降本） |

---

## 4. 怎么启动

### 4.1 运行模式

| 模式 | 含义 | 是否真实下单 |
|---|---|---|
| `sim` | 全模拟（Mock 数据 + SimGateway，开发自测） | 否 |
| `paper` | 真实行情 + 模拟撮合（真数据模拟盘，默认） | 否 |
| `live` | 实盘（接 QMT 网关，仅 Windows） | **是，需显式指定** |

三种模式共用同一套决策/风控/执行代码。**决策产物跨模式共享**（统一存 `data/trade.db`），
账本按模式隔离（live 单独用 `data/trade_live.db`）。

### 4.2 WebUI + 后端服务（推荐，日常就用它）

后端进程（`uvicorn server.main:app`）**同时承担常驻调度器**——进程必须保持运行，
盘前选股/研判/下单等定时任务才会触发；启动时还会自动补跑当日已错过的任务。

```bash
# 后台常驻（推荐），日志 logs/backend.log；脚本会自动清理占用 7099 的旧进程
./scripts/start_backend.sh

# 前台调试（Ctrl+C 停止）
./scripts/start_backend.sh --fg
```

就绪后访问 **http://127.0.0.1:7099**（WebUI + API 同端口），API 文档在 `/docs`。

长期挂机建议再看门狗 + 开机自启（见部署文档 §1.4）：

```bash
bash scripts/watchdog_backend.sh    # 每 30s 健康检查，掉线约 1 分钟内自动拉起
```

### 4.3 CLI（不走 WebUI 时）

入口：`python -m qmt_trade`。任何会真实下单的命令都必须带 `--mode live`。

```bash
# 1) 先看调度日程（不真正跑，验证装配是否 OK）
python -m qmt_trade --mode sim run --plan-only

# 2) 起调度器，按 config/settings.yaml 的日程自动跑（Ctrl-C 退出）
python -m qmt_trade --mode paper run

# 3) 只跑单个任务（调试用）
python -m qmt_trade --mode paper run --once selection

# 4) 把某天整套流程跑一遍（replay）
python -m qmt_trade --mode sim run --replay 2026-08-07

# 5) 实盘（务必确认 QMT 已连接、账号已配置）
python -m qmt_trade --mode live run
```

### 4.4 Docker（sim / paper）

```bash
docker compose up -d --build      # 一键构建并后台启动，访问 http://localhost:7099
```

> xtquant 为 Windows 专属库，**live 实盘不能跑在 Docker 里**，只能原生 Windows 部署。
> 挂载、密钥注入、运维命令详见 [`docs/部署与使用说明.md`](docs/部署与使用说明.md) §2。

---

## 5. WebUI 页面

访问 http://localhost:7099，主要页面：

| 页面 | 功能 |
|---|---|
| 概览 | 账户快照、KillSwitch 档位、当日任务执行状态、调度日程 |
| 选股 | 一键选股（全市场漏斗）、LLM 研判精选、观察池；盘前由调度器自动产出 |
| 行情 | 市场状态（regime）、指数与个股行情 |
| 实盘 | live 模式的持仓/委托/计划与下单执行 |
| 回测 | 历史区间回测（与实盘同一执行链路） |
| 风控 | 三闸门事件、KillSwitch 操作 |
| 事件 / 报告 / 策略 | 新闻事件库、日报周报、策略池权重与进化、独立策略实验室（打板/二板/低吸/趋势/ETF T+0 日内回转）启停与回测 |
| LLM / 设置 | LLM 用量与模型状态、settings.yaml / llm.yaml 在线编辑、数据源健康度 |

**日常节奏**（默认日程见 `config/settings.yaml` 的 `scheduler.jobs`）：

```
06:25 data_sync 数据同步体检 → 07:30 regime 市场状态
→ 08:00 selection 盘前选股 → 08:15 llm_research LLM 研判精选
→ 09:00 plan 生成交易计划 → 09:20 auction_check 集合竞价
→ 09:30~15:00 intraday 盘中守护（30s 间隔）
→ 15:05 reconcile 对账 → 16:00 review 复盘（周六 evolve 策略进化）
```

错过时段开机不用慌：后端启动时自动按依赖顺序补跑当日已错过的任务。

---

## 6. CLI 命令一览

| 命令 | 作用 | 示例 |
|---|---|---|
| `run` | 启调度器 / 单任务 / replay | `run --plan-only`、`run --once selection`、`run --replay 2026-08-07` |
| `select` | 跑一次选股（可加 `--research` 顺带研判） | `select --top 20 --research` |
| `backtest` | 历史回测（同代码路径，`--llm` 启用研判） | `backtest --start 2025-01-01 --end 2025-06-30 --cash 1000000` |
| `report` | 生成日/周报 | `report --weekly --save --push` |
| `reconcile` | 盘后对账 / 人工确认差异 | `reconcile`、`reconcile --ack "已核对券商流水"` |
| `health` | 系统体检（含 KillSwitch 与最近任务） | `health --schedule --notify` |
| `killswitch` | 查看 / 操作总开关 | `killswitch --status`、`killswitch --engage "人工避险"` |
| `evolve` | 策略池调权 + 复盘 | `evolve` |

### 6.1 总开关 KillSwitch（最重要的人工操作）
- `killswitch --status`：查看当前档位（`NORMAL` / `REDUCE_ONLY` / `FLATTEN`）。
- `killswitch --engage "理由"`：降级为 `REDUCE_ONLY`（停止开新仓，只减仓/守护）。
- `killswitch --flatten "理由"`：升级为 `FLATTEN`（全部平仓）。
- `killswitch --reset`：恢复 `NORMAL`。
- 人工操作标记 `manual=True` 并持久化到 `system_state`，**重启不丢**，且可与系统自动拉闸区分。
- 也可在 WebUI 风控页操作。

### 6.2 对账
盘后 `reconcile` 对比券商账户与本地 `positions` 视图；不一致触发告警，需人工 `reconcile --ack "理由"` 确认解除限制。

---

## 7. 数据库与数据流

系统使用 **SQLite** 作主数据库（`data/trade.db`，live 账本为 `data/trade_live.db`），
首次启动由 `storage/models.py` 的 `SCHEMA` **自动建表（幂等）**。行情/财务等 bulk 数据落 **Parquet**（`data/parquet/`，用于 PIT 回放）。

### 7.1 表清单

| 表 | 作用 | 关键字段 |
|---|---|---|
| `intents` | LLM 产出的交易意图 | trade_date, symbol, action, confidence, conviction, payload(JSON), trace_id |
| `plans` | 意图经风控+仓位后的可执行计划 | intent_id, side, planned_shares, stop_loss_price, take_profit, status |
| `orders` | 订单（**幂等键防重复下单**） | idempotency_key(UNIQUE), symbol, side, price, volume, status, reject_reason |
| `trades` | 成交流水 | price, volume, amount, commission, stamp_duty, slippage_cost, realized_pnl |
| `positions` | 本地持仓视图（每日与券商对账） | volume, available, avg_cost, stop_loss_price, industry |
| `account_snapshots` | 账户每日快照 | total_asset, cash, market_value, realized_pnl, unrealized_pnl, regime |
| `risk_events` | 风控事件（三道闸门判定记录） | gate, rule, symbol, severity, message, trace_id |
| `llm_calls` | LLM 调用记录（缓存+成本+回放三合一） | prompt_hash(PK), model, input_tokens, output_tokens, cost_cny |
| `experiences` | 复盘经验库（供后续决策检索） | situation, action, outcome, pnl_pct, lesson, embedding |
| `system_state` | KillSwitch 等系统状态（持久化，重启不丢） | key(PK), value, reason, updated_at |
| `reconcile_logs` | 盘后对账日志 | trade_date, passed, detail |
| `news` | 新闻与事件 | symbol, title, publish_time, category, sentiment, importance |

全链路（意图→计划→订单→成交→风控→LLM 调用）用 `trace_id` 串联，可全程审计（P6）。

### 7.2 数据怎么入库

数据**不是**一次性批量灌库，而是**按需拉取 + 缓存 + 质量校验**，由调度任务驱动：

```
DataHub.get_bars(symbols, asof, start, end)
   └─ 按 datahub.priority 依次尝试 provider（qmt → tushare → akshare）
        ├─ 熔断打开的源直接跳过（不浪费超时）
        ├─ PIT 切片：晚于 asof 的数据一律不可见（防未来函数）
        ├─ 质量校验：缺失率 / 单日涨跌幅异常判脏
        └─ 命中/写入 ParquetStore 缓存（data/parquet/）
```

- **`data_sync` 任务**：取全市场标的 → 抽样拉取近 180 天行情做质量体检，脏数据 → 任务失败 → 自动拉闸 `REDUCE_ONLY`（绝不用脏数据决策）。
- **交易记录**（`intents/plans/orders/trades/positions/...`）由执行链路在运行时自动落库。
- 即使外部接口全挂，`DataHub` 抛 `DataUnavailableError`，调度层转为「当日停止开仓」（P4），系统不会裸奔。

---

## 8. 目录结构

```
my_qmt_trade/
├── config/
│   ├── settings.yaml          # 非敏感配置（入库）
│   ├── llm.yaml               # LLM 独立管理：多平台/多模型/场景智能选模（入库）
│   └── .env                   # 敏感配置（自建，不入库）
├── data/
│   ├── trade.db               # SQLite 主库（决策产物 + sim/paper 账本，自动建表）
│   ├── trade_live.db          # live 实盘账本（按模式隔离）
│   ├── llm_cache.db           # LLM 响应缓存
│   └── parquet/               # 行情/财务 PIT 缓存
├── qmt_trade/                 # 核心业务包
│   ├── app.py                 # 装配容器 TradingContext（唯一组件装配点）
│   ├── cli.py                 # 命令行入口
│   ├── datahub/               # L0 数据层（DataHub/PIT/providers/ParquetStore）
│   ├── features/              # L1 特征层（因子）
│   ├── selection/             # L2-a 选股漏斗
│   ├── brain/                 # L2-b LLM 决策层（agents/llm）
│   ├── risk/ execution/ portfolio/   # L3 风控/仓位、L4 执行
│   ├── backtest/ evolution/   # L4 回测、L5 进化
│   ├── ops/                   # L6 监控/通知/报告
│   ├── scheduler/             # 调度层（jobs/runner，含错过任务补跑）
│   └── storage/               # 数据库 Schema 与仓储层
├── server/                    # FastAPI 后端（/api JSON 接口 + 常驻调度器 + SPA 托管）
├── webui/                     # Vue3 + Vite 前端（构建产物 webui/dist 由后端托管）
├── scripts/                   # start_backend.sh / watchdog_backend.sh / autostart_backend.bat
├── docs/                      # 设计文档（01 / _part2~4 / 全文）+ 部署与使用说明
├── tests/                     # 冒烟测试（smoke_*.py，14 个文件 / 700 项断言）
├── reports/                   # 生成的日报/周报
├── Dockerfile  docker-compose.yml   # 容器化部署（sim/paper）
└── requirements.txt           # Python 依赖
```

---

## 9. 自检与测试

```bash
# 冒烟测试（14 个文件，约 700 项断言），逐个运行即可
python tests/smoke_llm_adapter.py     # LLM 独立管理层 + 场景智能选模 + P5 降级闭环
python tests/smoke_scheduler.py       # 调度层 + 失败安全 + 补跑
python tests/smoke_webui_api.py       # WebUI API 接口
# ... 其余 tests/smoke_*.py 同理（datahub/features/selection/brain/risk/gateway/backtest/evolution/ops/reconcile/reflection/strategy）
```

覆盖数据、特征、选股、研判、风控、仓位、执行、回测、进化、运维、调度、LLM、WebUI 各层，
重点验证「挂了 / 选错 / 超预算之后系统怎么办」，全程不发真实请求。

---

## 10. 常见问题（速查）

| 现象 | 处理 |
|---|---|
| WebUI 打开是 JSON 而非页面 | `webui/dist` 未构建：`cd webui && npm run build` 后重启后端 |
| LLM 相关功能全部走 Mock | 未配置 API Key（属正常降级 P5）；检查 `config/.env` 与 `llm.yaml` 的 `api_key_env` |
| 当日成本超限后 LLM 停用 | 预算熔断生效（默认 ¥30/日）；调 `budget.daily_cny` 或次日自动恢复 |
| 端口 7099 被占用 | `start_backend.sh` 会自动清理；手动：`netstat -ano | findstr :7099` |
| QMT 连接失败 | 确认 QMT 客户端已登录 mini 模式；`QMT_MINI_PATH` 环境变量优先于 settings.yaml |
| 改了代码不生效 | 必须重启后端：`./scripts/start_backend.sh`（Docker：`docker compose up -d --build`） |

更多排查与紧急制动流程见 [`docs/部署与使用说明.md`](docs/部署与使用说明.md) §4。
