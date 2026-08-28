# QMT 交易控制台 WebUI

Vue3 + TypeScript + Vite 前端 · FastAPI 后端，前后端分离。
**与 CLI 完全并存**：Web 端所有操作都调用 `qmt_trade` 现有组合根 `build_context()`，
与 `qmt` 命令行走同一条代码路径、同一份配置、同一个数据库——不存在"Web 一套状态、CLI 另一套"。

---

## 1. 启动

### 1.1 后端 API（端口 7099）

```bash
# 项目根目录
cd F:/workspace/my_qmt_trade

# 依赖（已装可跳过）
python -m pip install fastapi uvicorn python-multipart httpx

# 启动
python -m server.main
# 或
uvicorn server.main:app --host 0.0.0.0 --port 7099
```

- 交互式 API 文档：http://localhost:7099/docs
- 所有业务接口统一前缀 `/api`

### 1.2 前端（端口 5173）

```bash
cd webui
npm install
npm run dev          # 开发模式，热更新
```

打开 http://localhost:5173

生产构建：

```bash
npm run build        # 输出到 webui/dist，可用任意静态服务器托管
npm run preview      # 本地预览构建产物
```

后端地址默认 `http://localhost:7099`，如需改动，在 `webui/.env` 写：

```
VITE_API_BASE=http://192.168.1.10:7099
```

---

## 2. 运行模式

顶栏可切换 **sim / paper / live**，对应 CLI 的 `--mode`：

| 模式 | 数据源 | 交易 | 说明 |
| --- | --- | --- | --- |
| `sim` | mock（可复现） | 全模拟 | 默认，安全沙盒，不碰真实世界 |
| `paper` | 真实数据 | 模拟撮合 | 纸面交易，验证策略 |
| `live` | 真实数据 | QMT 实盘 | **Web 端手动下单被强制禁用**，必须走 CLI |

> 安全设计：`POST /api/trade/intent` 在 `mode=live` 时直接返回 403。
> 真金白银的手动下单只能通过 CLI 显式 `--mode live`，避免误触。

---

## 3. 功能页面

| 页面 | 路径 | 能做什么 |
| --- | --- | --- |
| 系统总览 | `/` | 模式/总开关/LLM 状态、健康体检、任务执行记录、调度任务一键运行、密钥管理 |
| LLM 管理 | `/llm` | provider / model / scene 增删改，选模权重，模型健康与熔断，真实测试调用 |
| 数据源管理 | `/datasource` | 分层优先级编辑、熔断参数、运行时健康快照 |
| 参数配置 | `/config` | `settings.yaml` 全量可视化编辑（按段过滤 + 搜索 + 批量保存） |
| 风控管理 | `/risk` | Gate1/2/3 阈值编辑、Kill Switch 三态操作 |
| 行情数据 | `/market` | K 线 / 实时行情 / 相关新闻，含收盘价迷你走势图 |
| 交易管理 | `/trade` | 持仓、订单、TradeIntent、盘后对账与人工签核、模拟下单、运行交易计划 |
| 策略管理 | `/strategy` | 策略池状态与权重、立即调权、后台进化、复盘总结（任务轮询） |
| 回测管理 | `/backtest` | 提交后台回测、净值曲线、全量指标、历史任务载入 |
| 事件驱动 | `/event` | 公司事件 / 新闻舆情 / 硬负面事件（一票否决）浏览 |
| 消息推送 | `/notify` | 飞书 / 企微 / 钉钉 webhook 频道增删改 + 测试发送 |

---

## 4. 配置落盘规则

| 内容 | 落盘位置 | 备份 |
| --- | --- | --- |
| 系统参数、风控阈值、数据源优先级、推送频道 | `config/settings.yaml` | 首次写入前自动生成 `.bak` |
| LLM provider / model / scene / 选模策略 | `config/llm.yaml` | 同上 |
| API Key、Webhook 地址等密钥 | `config/.env` | 页面只回显掩码，**永不回传明文** |

页面编辑用的 `Settings.load(env_overlay=False)`，保证改的是 YAML 原文，不会把环境变量覆盖值写死进配置。

---

## 5. 后台任务

回测 / 策略进化 / 复盘属于慢操作，走后台线程 + Job 轮询，不阻塞 HTTP：

```
POST /api/backtest/run   →  { "job_id": "a1b2c3d4e5" }
GET  /api/jobs/{job_id}  →  { "status": "running|done|error", "result": {...} }
GET  /api/jobs?limit=20  →  最近任务列表
```

前端自动每 1.5~2 秒轮询直到完成。

---

## 6. API 一览

```
# 系统
GET    /api/overview                   总览（模式/总开关/LLM 状态）
GET    /api/health?notify=false        健康体检
GET    /api/killswitch                 总开关状态
POST   /api/killswitch                 engage | flatten | reset
GET    /api/scheduler/jobs             调度任务表
POST   /api/scheduler/run?name=...     立即运行某任务
GET    /api/secrets                    密钥列表（掩码）
PUT    /api/secrets                    写入密钥到 .env

# LLM
GET    /api/llm/config                 全量配置 + 健康快照
PUT    /api/llm/enabled                启停 LLM
POST   /api/llm/providers              新增/覆写 provider
DELETE /api/llm/providers/{id}
POST   /api/llm/models                 新增/覆写模型
DELETE /api/llm/models/{id}
POST   /api/llm/scenes                 新增/覆写场景
DELETE /api/llm/scenes/{id}
PUT    /api/llm/selection              选模权重
POST   /api/llm/test                   真实发一次请求

# 数据源 / 参数 / 风控
GET    /api/datasource                 优先级 + 熔断 + 健康
PUT    /api/datasource/priority
GET    /api/config                     settings.yaml 全量
GET    /api/config/section/{path}
PUT    /api/config/patch               批量点分路径写入
GET    /api/risk/gates                 三道闸门阈值
PUT    /api/risk/gates                 只允许 risk.* 路径

# 行情 / 事件
GET    /api/market/symbols             当前模式可交易标的
GET    /api/market/bars                K 线
GET    /api/market/quote               实时行情
GET    /api/market/news                新闻
GET    /api/market/events              公司事件
GET    /api/event/news
GET    /api/event/events
GET    /api/event/hard-negatives       硬负面事件

# 交易
GET    /api/trade/positions | orders | intents | reconcile
POST   /api/trade/reconcile/ack        人工签核对账差异
POST   /api/trade/intent               模拟下单（live 返回 403）
POST   /api/trade/plan                 运行交易计划

# 策略 / 回测 / 任务
GET    /api/strategy/pool
POST   /api/strategy/rebalance
POST   /api/strategy/evolve            → job_id
POST   /api/strategy/review            → job_id
POST   /api/backtest/run               → job_id
GET    /api/jobs | /api/jobs/{id}

# 通知
GET    /api/notify/channels
PUT    /api/notify/channels
POST   /api/notify/test
```

所有需要上下文的接口都支持 `?mode=sim|paper|live`，缺省 `sim`。

---

## 7. 与 CLI 的关系

Web 后端**没有重写任何业务逻辑**，只做三件事：

1. 用 `build_context(mode)` 拿到与 CLI 一模一样的上下文；
2. 调用现有组件（`hub` / `execution` / `pool` / `monitor` / `reconciler` / `JobRunner` / `BacktestEngine`…）；
3. 序列化成 JSON。

因此：

- Web 上改的配置，CLI 下次运行立刻生效（同一份 YAML）；
- CLI 跑出的持仓/订单/意图，Web 上直接可见（同一个 SQLite）；
- 停掉 Web 服务，CLI 一切照常，零影响。
