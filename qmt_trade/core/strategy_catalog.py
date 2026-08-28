"""内置策略目录：把"系统到底有哪些策略、规则是什么"讲清楚。

策略池（``evolution/pool.py``）只负责**资金权重分配与淘汰**，真正的
选股/交易逻辑分散在 selection / features / risk / execution 各模块里。
页面上只看到"一个默认策略"，对用户就是黑盒。

本模块把整条决策链汇总成可读目录，规则里的阈值一律取**当前 settings
的实际值**（不写死文档），保证页面展示的与真实运行的永远一致。
"""

from __future__ import annotations

from typing import Any

from ..features.base import registry
from ..features.engine import DEFAULT_CATEGORY_WEIGHTS
from ..features.regime import DEFAULT_MIN_PERCENTILE, DEFAULT_MIN_SCORE, Regime


def _rule(name: str, detail: str) -> dict[str, str]:
    return {"name": name, "detail": detail}


def _fmt_pct(v: float) -> str:
    return f"{float(v) * 100:.1f}%" if float(v) < 1 else f"{float(v):.0f}%"


# ================================================================ 选股策略
def _selection_strategies(settings) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    # ---- 1. 市场状态判定（Regime）：整条选股链的总开关
    rcfg = settings.section("regime") or {}
    cap = rcfg.get("position_cap") or {}
    risk_factor = rcfg.get("risk_factor") or {}
    out.append({
        "id": "regime",
        "name": "市场状态判定（Regime）",
        "stage": "选股漏斗 · 第 1 级",
        "module": "qmt_trade/features/regime.py",
        "summary": "先判断大盘处于什么状态，再决定敢开多大仓位、选股要多挑剔。"
                   "系统性风险下直接空仓 —— 空仓也是仓位，且是唯一在系统性风险里稳赚的仓位。",
        "rules": [
            _rule("四种状态",
                  "TREND_UP 上涨趋势 / RANGE 震荡 / TREND_DOWN 下跌趋势 / RISK_OFF 系统性风险"),
            _rule("总仓位上限", " | ".join(
                f"{r.value} {_fmt_pct(cap.get(r.value, v))}"
                for r, v in ((Regime.TREND_UP, 0.8), (Regime.RANGE, 0.5),
                             (Regime.TREND_DOWN, 0.2), (Regime.RISK_OFF, 0.0)))),
            _rule("开仓分位门槛", " | ".join(
                f"{r.value} 综合分须排进全市场前 {(1 - DEFAULT_MIN_PERCENTILE[r]) * 100:.0f}%"
                if DEFAULT_MIN_PERCENTILE[r] <= 1 else f"{r.value} 禁止开仓"
                for r in Regime)),
            _rule("RISK_OFF 空仓",
                  "判定为系统性风险时选股直接返回空候选池，只平不开"),
            _rule("判定维度",
                  f"指数 {rcfg.get('index_symbol', '000300.SH')} 的 "
                  f"{rcfg.get('ma_short', 20)}/{rcfg.get('ma_long', 60)} 日均线结构 + "
                  f"市场宽度（强≥{rcfg.get('breadth_strong', 0.6)} / 弱<{rcfg.get('breadth_weak', 0.4)}）+ "
                  f"波动率（>{rcfg.get('vol_risk_off', 0.035)} 触发 RISK_OFF）+ "
                  f"回撤（>{_fmt_pct(rcfg.get('drawdown_risk_off', 0.12))} 触发 RISK_OFF）"),
            _rule("仓位系数", "仓位按状态缩放：" + " | ".join(
                f"{r.value} ×{risk_factor.get(r.value, 1.0)}" for r in Regime)),
        ],
        "params": dict(rcfg),
    })

    # ---- 2. L0 硬过滤：确定性规则快速淘汰
    scfg = settings.section("selection.screener") or {}
    boards = ",".join(scfg.get("allowed_boards") or ["MAIN", "GEM", "STAR"])
    min_amount = float(scfg.get("min_amount_20d", 5e7))
    min_cap = float(scfg.get("min_market_cap", 2e9))
    out.append({
        "id": "screener",
        "name": "L0 硬过滤（9 条确定性规则）",
        "stage": "选股漏斗 · 第 2 级",
        "module": "qmt_trade/selection/screener.py",
        "summary": "把全市场约 5400 只压到 1500~2500 只。全部是零成本的确定性规则，"
                   "每条规则的淘汰数都留痕，候选池突变时可 3 秒定位是哪个闸门卡住。",
        "rules": [
            _rule("黑名单", "人工黑名单标的一律剔除（当前为空）"
                  if not (scfg.get("blacklist") or [])
                  else f"黑名单标的 {len(scfg['blacklist'])} 只"),
            _rule("板块限制", f"只允许 {boards} 板块"),
            _rule("排除 ST/*ST", "风险警示标的一律不参与选股"
                  if scfg.get("exclude_st", True) else "当前未启用"),
            _rule("上市天数", f"上市不足 {scfg.get('min_list_days', 60)} 个交易日剔除（新股波动不可控）"),
            _rule("排除停牌", "停牌标的剔除"),
            _rule("排除一字板", "T-1 一字涨/跌停剔除（买不进/卖不出，下单前还有一道实时拦截）"),
            _rule("成交额", f"{scfg.get('amount_window', 20)} 日均成交额 ≥ {min_amount / 1e8:.2f} 亿（保证流动性）"),
            _rule("市值", f"总市值 ≥ {min_cap / 1e8:.0f} 亿"),
            _rule("股价区间", f"收盘价 ∈ [{scfg.get('min_price', 1.0)}, "
                  f"{scfg.get('max_price', 0) or '不限'}]"),
            _rule("数据缺失策略", "指标 NaN 一律判为不通过：宁可漏选，不可错选"),
        ],
        "params": dict(scfg),
    })

    # ---- 3. 多因子打分：自动选股的核心依据
    fcfg = settings.section("features") or {}
    cat_override = fcfg.get("category_weights") or {}
    factor_weights = fcfg.get("factor_weights") or {}
    groups: dict[str, list[dict]] = {}
    for m in registry.all_meta():
        groups.setdefault(m.category, []).append(
            {"name": m.name, "desc": m.description, "min_periods": m.min_periods})
    cw_display = {}
    for r in Regime:
        base = dict(DEFAULT_CATEGORY_WEIGHTS[r])
        base.update(cat_override.get(r.value, {}) or {})
        total = sum(base.values()) or 1.0
        cw_display[r.value] = {k: round(v / total, 3) for k, v in base.items()}
    cat_zh = {"momentum": "量价动量", "moneyflow": "资金流", "sentiment": "消息情绪",
              "fundamental": "基本面", "quality": "质量"}
    out.append({
        "id": "factors",
        "name": f"多因子打分（{sum(len(v) for v in groups.values())} 个因子 × 5 大类）",
        "stage": "选股漏斗 · 第 3 级",
        "module": "qmt_trade/features/factors/",
        "summary": "对通过硬过滤的标的逐因子打分 → 行业内中性化 → 截面分位化 → "
                   "按 Regime 动态调整大类权重加权合成综合分。权重随市场状态切换，"
                   "这就是自动选股的核心依据。",
        "rules": [
            _rule(f"{cat_zh.get(c, c)}因子 ×{len(fs)}",
                  "；".join(f"{f['name']}（{f['desc']}）" for f in fs))
            for c, fs in groups.items()
        ] + [
            _rule("大类权重随 Regime 切换", " | ".join(
                f"{r}: " + "/".join(
                    f"{cat_zh.get(k, k)}{v:.0%}" for k, v in cw.items()
                    if v > 0)
                for r, cw in cw_display.items())),
            _rule("行业中性化",
                  "量价/资金流/情绪类因子先减行业均值，避免"
                  "“当红行业整体高分=押注单一行业”；估值/质量类保留跨行业差异"),
            _rule("稳健化",
                  f"因子值 {fcfg.get('winsorize_quantile', 0.02):.0%} 分位缩尾去极值 → "
                  "截面分位化（0~1），有效率过低的因子自动剔除"),
            _rule("个股因子加权", "个股内只用自己非空的因子加权，"
                  "避免“数据源缺失=系统性低分”"
                  + (f"；部分因子另有权重覆盖：{factor_weights}" if factor_weights else "")),
        ],
        "params": {"category_weights": cw_display, "factor_weights": factor_weights},
        "factor_groups": groups,
    })

    # ---- 4. L1 排序 + 行业分散
    rkcfg = settings.section("selection.ranker") or {}
    top_n = int(rkcfg.get("top_n", 100))
    cap_ind = int(rkcfg.get("max_per_industry", 15))
    out.append({
        "id": "ranker",
        "name": "L1 排序 + 行业分散",
        "stage": "选股漏斗 · 第 4 级",
        "module": "qmt_trade/selection/ranker.py",
        "summary": "按综合分排序取 Top N，同时限制单行业数量 —— 不加约束时 Top100 常被"
                   "一两个当红行业占掉七八成，表面选股、实际押注单一行业。",
        "rules": [
            _rule("Regime 分位门槛", "只有综合分排进全市场前 "
                  f"{(1 - DEFAULT_MIN_PERCENTILE[Regime.TREND_UP]) * 100:.0f}%"
                  f"（TREND_UP）/ {(1 - DEFAULT_MIN_PERCENTILE[Regime.RANGE]) * 100:.0f}%"
                  f"（RANGE）/ {(1 - DEFAULT_MIN_PERCENTILE[Regime.TREND_DOWN]) * 100:.0f}%"
                  "（TREND_DOWN）的票才保留"),
            _rule("候选数量", f"按综合分取 Top {top_n}"),
            _rule("行业配额", f"单行业最多 {cap_ind} 只，被挤出的高分票留痕可复盘"),
            _rule("不足补位", f"行业配额导致取不满时放宽上限 "
                  f"{rkcfg.get('relax_factor', 1.5)} 倍补位（补位数量计入统计）"),
        ],
        "params": dict(rkcfg),
    })

    # ---- 5. LLM 深度研判：多角色智能体协作
    bcfg = settings.section("brain") or {}
    roll = bcfg.get("rolling") or {}
    out.append({
        "id": "llm_research",
        "name": "LLM 深度研判（多智能体协作）",
        "stage": "选股漏斗 · 第 5 级",
        "module": "qmt_trade/brain/",
        "summary": "前 4 级是确定性漏斗，这里才引入 LLM：四位分析师独立研判 → 多轮辩论 → "
                   "风控官一票否决 → 组合经理拍板，产出最终交易意图。",
        "rules": [
            _rule("输入", f"多因子排序前 {(settings.section('selection') or {}).get('llm_shortlist', 20)} 只送 LLM（shortlist）"),
            _rule("分析师团队", "技术面 / 基本面 / 资金面 / 消息面 四位分析师独立出具评分与论点"),
            _rule("多轮辩论", "观点冲突的标的进入辩论环节，逼出反方论据防一致性偏差"),
            _rule("风控官否决", "硬负面事件（立案/退市风险等）一票否决"),
            _rule("组合经理拍板",
                  f"意图分 ≥ {bcfg.get('min_intent_score', 0.5)} 才保留，最多 "
                  f"{bcfg.get('max_intents', 10)} 条意图，最终精选 "
                  f"{bcfg.get('final_picks_min', 3)}~{bcfg.get('final_picks_max', 5)} 只，"
                  f"单行业权重 ≤ {_fmt_pct(bcfg.get('max_industry_weight', 0.3))}"),
            _rule("滚动观察池",
                  f"已持仓/观察中标的续看 {roll.get('watch_days', 5)} 天、保留前 "
                  f"{roll.get('carry_top_n', 5)} 名，分 ≥ {roll.get('min_score', 0.62)} "
                  "并入候选头部续研，去留由研判决定"),
        ],
        "params": dict(bcfg),
    })
    return out


# ================================================================ 交易策略
def _trading_strategies(settings) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    # ---- 1. 仓位管理：风险预算法
    pcfg = settings.section("portfolio") or {}
    conv = pcfg.get("conviction_factor") or {"LOW": 0.6, "MEDIUM": 1.0, "HIGH": 1.4}
    out.append({
        "id": "sizer",
        "name": "仓位管理（风险预算法）",
        "stage": "交易执行 · 买入前",
        "module": "qmt_trade/portfolio/sizer.py",
        "summary": "每笔交易允许亏损的金额恒定，而不是买入金额恒定：高波动股止损距离大→自动少买，"
                   "低波动股→多买。",
        "rules": [
            _rule("风险预算", f"单笔基础风险预算 = 总资产 × {_fmt_pct(pcfg.get('base_risk_pct', 0.006))}"),
            _rule("置信度调整", " | ".join(f"{k} ×{v}" for k, v in conv.items())),
            _rule("Regime 缩放", "TREND_UP ×1.0 | RANGE ×0.7 | TREND_DOWN ×0.4 | RISK_OFF 禁止开仓"),
            _rule("反推股数", "股数 = 风险预算 ÷ 止损距离，再按 A 股 100 股取整"),
            _rule("多重上限",
                  f"单票权重 ≤ {_fmt_pct(pcfg.get('max_weight_pct', 0.15))}；"
                  f"组合总风险预算 ≤ {_fmt_pct(pcfg.get('total_risk_budget', 0.03))}；"
                  f"行业风险预算 ≤ {_fmt_pct(pcfg.get('industry_risk_budget', 0.012))}；"
                  f"现金保留 {100 - float(pcfg.get('cash_usage_ratio', 0.95)) * 100:.0f}%；"
                  f"单笔不超过当日成交量的 {_fmt_pct(pcfg.get('max_volume_ratio_of_adv', 0.05))}"),
        ],
        "params": dict(pcfg),
    })

    # ---- 2. 盘前风控 Gate-1
    g1 = settings.section("risk.gate1") or {}
    out.append({
        "id": "gate1",
        "name": "盘前风控闸门（Gate-1）",
        "stage": "交易执行 · 下单时",
        "module": "qmt_trade/risk/engine.py",
        "summary": "每笔买单发出前的最后一道组合级审查，任何一条不过即拒单。",
        "rules": [
            _rule("持仓数量", f"最多持 {g1.get('max_positions', 5)} 只；每日新开仓 ≤ {g1.get('max_new_positions_per_day', 3)} 只"),
            _rule("集中度", f"单票权重 ≤ {_fmt_pct(g1.get('max_single_weight', 0.15))}；"
                  f"单行业 ≤ {_fmt_pct(g1.get('max_industry_weight', 0.30))}"),
            _rule("相关性约束",
                  f"60 日收益相关系数 > {g1.get('correlation_threshold', 0.8)} 的持仓最多 "
                  f"{g1.get('max_correlated_positions', 3)} 只（防止隐性同涨同跌）"),
            _rule("组合熔断",
                  f"当日亏损 > {_fmt_pct(g1.get('max_daily_loss', 0.02))} / "
                  f"5 日回撤 > {_fmt_pct(g1.get('max_5d_drawdown', 0.05))} / "
                  f"总回撤 > {_fmt_pct(g1.get('max_drawdown', 0.15))} → 停止开新仓"),
            _rule("单笔限额", f"单笔金额 ≤ 总资产 {_fmt_pct(g1.get('max_order_value_ratio', 0.10))}；"
                  f"现金缓冲 {_fmt_pct(g1.get('cash_buffer', 0.005))}"),
        ],
        "params": dict(g1),
    })

    # ---- 3. 盘中监控 Gate-2
    g2 = settings.section("risk.gate2") or {}
    out.append({
        "id": "gate2",
        "name": "盘中监控（Gate-2 · 移动止盈止损）",
        "stage": "交易执行 · 持仓中",
        "module": "qmt_trade/risk/engine.py",
        "summary": "持仓期间高频巡检：止损线被打穿立即平仓；盈利达标后切换为移动止盈，"
                   "锁住利润但不封死上涨空间。",
        "rules": [
            _rule("移动止盈",
                  f"浮盈 ≥ {_fmt_pct(g2.get('trailing_activate_profit', 0.08))} 激活；"
                  f"自高点回落 {_fmt_pct(g2.get('trailing_drawdown', 0.04))} 触发止盈卖出"),
            _rule("分批止盈", "部分止盈" if g2.get("partial_tp", True) else "未启用",
                  ),
            _rule("巡检频率", f"每 {g2.get('check_interval_seconds', 3)} 秒一轮"),
            _rule("Kill Switch", "连续止损/账户异常等触发三级熔断（NORMAL→REDUCE_ONLY→HALT），"
                  "REDUCE_ONLY 只平不开，HALT 全停"),
        ],
        "params": dict(g2),
    })

    # ---- 4. 订单护栏
    ecfg = settings.section("execution") or {}
    guard = ecfg.get("guard") or {}
    costs = ecfg.get("costs") or {}
    out.append({
        "id": "order_guard",
        "name": "订单护栏（执行层）",
        "stage": "交易执行 · 报单瞬间",
        "module": "qmt_trade/execution/order_guard.py",
        "summary": "报单发出前的实时拦截：涨停板不追买、跌停板不强卖、频率限流防乌龙指。",
        "rules": [
            _rule("实时一字板拦截", "盘前硬过滤是 T-1 数据，这里用实时价再拦一次"),
            _rule("频率限制", f"同一标的每日 ≤ {guard.get('max_orders_per_symbol_per_day', 6)} 单；"
                  f"全局 ≤ {guard.get('max_orders_per_second', 3)} 单/秒；"
                  f"撤改单冷却 {guard.get('cooldown_seconds', 120)} 秒"),
            _rule("滑点与追单", f"滑点容忍 {_fmt_pct(ecfg.get('slippage_tolerance', 0.003))}；"
                  f"追价最多 {ecfg.get('max_chase_times', 2)} 次；"
                  f"未成交 {ecfg.get('pending_timeout_seconds', 60)} 秒超时处理"),
            _rule("成本模型",
                  f"佣金 {float(costs.get('commission_rate', 0.00025)) * 1e4:.1f}‱"
                  f"（最低 {costs.get('commission_min', 5)} 元）、"
                  f"印花税 {_fmt_pct(costs.get('stamp_duty_rate', 0.0005))}、"
                  f"过户费 {float(costs.get('transfer_fee_rate', 1e-5)) * 1e4:.2f}‱"),
        ],
        "params": {"guard": guard, "costs": costs},
    })

    # ---- 5. 策略池资金调权（达尔文式进化）
    pool = (settings.section("evolution") or {}).get("pool") or {}
    out.append({
        "id": "pool",
        "name": "策略池资金调权（达尔文式进化）",
        "stage": "资金管理 · 每日收盘后",
        "module": "qmt_trade/evolution/pool.py",
        "summary": "策略管理页表格里的就是它：按各策略近期风险调整后表现分配资金权重。"
                   "刻意不做“追逐近期收益”：半衰期加权、新策略先影子、调权限幅、隔离观察后才退休。",
        "rules": [
            _rule("状态机", "SHADOW 影子（只记账不给钱）→ ACTIVE 活跃 → QUARANTINE 隔离 → RETIRED 退休"),
            _rule("影子转正", f"跑满 {pool.get('promote_min_obs', 40)} 个样本且得分为正才给资金"),
            _rule("隔离条件", f"窗口回撤 > {_fmt_pct(pool.get('quarantine_dd', 0.15))} 或 "
                  f"夏普 < {pool.get('quarantine_sharpe', -0.5)}"),
            _rule("退休机制", f"连续 {pool.get('retire_after', 3)} 次不合格才退休，给均值回复留机会"),
            _rule("调权限幅", f"单次变动 ≤ {_fmt_pct(pool.get('max_step', 0.20))}、"
                  f"单策略 ≤ {_fmt_pct(pool.get('max_weight', 0.5))}；减仓不限幅（风控必须即时）"),
            _rule("兜底现金", "全部策略不合格时资金归入 __cash__ 持币，权重恒等于 1"),
        ],
        "params": dict(pool),
    })

    # ---- 6. 尾盘选股法（独立短线策略，默认关闭）----
    tp = (settings.section("strategies") or {}).get("tail_pick") or {}
    if tp:
        sel = str(tp.get("select_time", "14:30"))
        ext = str(tp.get("exit_window_start", "09:30"))
        exit_end = str(tp.get("exit_window_end", "10:00"))
        out.append({
            "id": "tail_pick",
            "name": "尾盘选股法 / 一夜持股法（独立短线）",
            "stage": "交易执行 · 独立短线",
            "module": "qmt_trade/strategies/tail_pick.py",
            "summary": "小资金高频打法：T 日 14:30 后 8 层严格筛选捕捉隔日溢价；"
                       "T+1 开盘 30min 内无论盈亏离场（一夜持股）。完全独立于多因子/Regime 体系，"
                       "启用开关在 strategies.tail_pick.enabled（默认关闭，UI 参数配置页可勾选）。",
            "rules": [
                _rule("① 操作时刻", f"T 日 {sel} 后选股并买入；T+1 {ext}–{exit_end} 离场"),
                _rule("② 涨幅", f"{_fmt_pct(tp.get('min_pct_change', 0.03))}–{_fmt_pct(tp.get('max_pct_change', 0.05))}"),
                _rule("③ 量比", f"> {tp.get('min_volume_ratio', 1.0)}（当日量 / 近 5 日均量）"),
                _rule("④ 换手率", f"{_fmt_pct(tp.get('min_turnover_rate', 0.05))}–{_fmt_pct(tp.get('max_turnover_rate', 0.10))}"),
                _rule("⑤ 流通市值", f"{float(tp.get('min_float_market_cap', 5e9)) / 1e8:.0f}–{float(tp.get('max_float_market_cap', 5e10)) / 1e8:.0f} 亿"),
                _rule("⑥ 阶梯放量", f"今日量 ≥ 昨日量 × {tp.get('volume_ladder_ratio', 1.0)}，且午后连续竞价段"
                      f"（13:00 起）等分 {tp.get('volume_ladder_segments', 3)} 段量逐段递增"
                      f"（后段 ≥ 前段 × {tp.get('volume_ladder_seg_tolerance', 1.0)}）"),
                _rule("⑦ 跑赢大盘", f"个股当日涨幅 − 沪深300 当日涨幅 ≥ {float(tp.get('min_intraday_outperf_vs_index', 0.0)):.0%}"),
                _rule("⑧ 尾盘筹码结构", "尾盘收阳（现价 > 尾盘窗口开盘）且现价 < 全天分时均价 VWAP×"
                      f"(1+{float(tp.get('chip_vwap_tolerance_pct', 0.01)):.0%})"
                      "（主力尾盘偷袭形态；无分钟线时 best-effort 放行）"),
                _rule("隔夜硬止损", f"T+1 开盘较成本跌超 {_fmt_pct(tp.get('overnight_stop_pct', 0.03))} 立即砍，防跳空深亏"),
                _rule("持仓纪律", f"同时 ≤ {tp.get('max_positions', 5)} 只，每只 ≤ {_fmt_pct(tp.get('position_fraction', 0.2))} 可用现金；"
                                  f"主板/创业板（{', '.join(tp.get('allowed_boards', ['MAIN', 'GEM']))}）"),
            ],
            "params": dict(tp),
        })

    # ---- 7. ETF T+0 日内回转（独立短线策略，配置在 config/strategies/etf_t0.yaml）----
    etf = (settings.section("strategies") or {}).get("etf_t0") or {}
    if etf:
        syms = "、".join(etf.get("symbols", ["513100.SH", "518880.SH"]))
        out.append({
            "id": "etf_t0",
            "name": "ETF T+0 日内回转（独立短线）",
            "stage": "交易执行 · 独立短线",
            "module": "qmt_trade/strategies/etf_t0.py",
            "summary": "对支持日内回转的 ETF 做底仓 + 日内做T：底仓吃趋势，T 仓网格吃波动。"
                       "现价偏离当日 VWAP 超阈值开腿、回归平腿，单日亏损上限触发停开新腿；"
                       "完全独立于多因子/Regime 体系（启用开关在 config/strategies/etf_t0.yaml::enabled）。",
            "rules": [
                _rule("标的池", syms),
                _rule("底仓与做T仓", f"每只底仓占总资产 {_fmt_pct(etf.get('base_fraction', 0.12))}；"
                      f"做T股数 = 底仓 × {_fmt_pct(etf.get('t_slice_ratio', 0.3))}（不足一手取一手）"),
                _rule("开腿信号", f"现价高于 VWAP {_fmt_pct(etf.get('sell_dev_threshold', 0.008))} → 卖出做T；"
                      f"低于 VWAP {_fmt_pct(etf.get('buy_dev_threshold', 0.008))} → 买入做T"),
                _rule("平腿信号", f"偏离回归 VWAP 至 {_fmt_pct(etf.get('close_leg_dev', 0.002))} 内平腿；"
                      f"或网格止盈 {_fmt_pct(etf.get('grid_step', 0.005))}；"
                      f"单腿止损 {_fmt_pct(etf.get('stop_pct', 0.005))}"),
                _rule("开腿时段", f"{etf.get('open_t_start', '09:35')}–{etf.get('open_t_end', '14:30')}；"
                      f"每标的最多 {etf.get('max_trades_per_symbol_per_day', 2)} 次/日，"
                      f"开腿间隔 ≥ {etf.get('min_interval_minutes', 5)} 分钟"),
                _rule("尾盘强平", f"{etf.get('force_flat_time', '14:50')} 当日 T 仓归零"),
                _rule("风控", f"单日 T0 净亏损 > {_fmt_pct(etf.get('max_daily_loss_pct', 0.003))} 当日停开新腿；"
                      f"当日分钟线不足 {etf.get('min_minutes_per_day', 5)} 根不参与；"
                      f"买回金额 > 总资产 {_fmt_pct(etf.get('buyback_max_notional_ratio', 0.03))} 放弃买回"),
                _rule("日内动量增强", f"{etf.get('momentum_mode', 'filter')} 模式，"
                      f"回看 {etf.get('momentum_window_min', 15)} 分钟，"
                      f"阈值 {_fmt_pct(etf.get('momentum_threshold', 0.004))}"),
            ],
            "params": dict(etf),
        })
    return out


def build_strategy_catalog(settings) -> dict[str, Any]:
    """构建完整策略目录。返回 {"selection": [...], "trading": [...]}。"""
    return {
        "selection": _selection_strategies(settings),
        "trading": _trading_strategies(settings),
    }
