"""尾盘选股法 / 一夜持股法（独立短线策略，不改动现有多因子体系）。

设计（2026-08-12）
----------------
小资金高频打法，捕捉"隔日溢价"。与现有多因子选股 / Regime / 风控体系**完全独立**：

- 选股层自立（``TailPickScreener``），不复用 ``SelectionPipeline`` / 因子引擎 / Regime。
- 8 层严格筛选（见 ``TailPickConfig`` 注释）：尾盘涨幅 1.5%~3.5%、量比>2.5、换手率 5%~10%、
  流通市值 50~500 亿、成交量阶梯放大、分时跑赢大盘、尾盘筹码结构（收阳线且现价低于分时均价 VWAP）。
- 纪律：**T 日 14:30 买入，T+1 开盘后时间不对称离场**——高开>0.5% 竞价卖半仓、
  剩余挂止盈；低开<-0.5% 等 9:45 反抽不翻红再砍；隔夜动量强势持至 10:30。
- V5 强趋势+缩量版（2026-08-14）：选股放弃"买跌"，不分强弱市统一"近5日跑赢沪深300
  3%以上 + 当日涨幅 1%~3.5% + 缩量（量<5日均量×1.1）"；离场走**时间不对称链**
  （见 ``_exit_day``：高开竞价卖半仓、低开等翻红否则砍仓、平开保本/止盈/硬止损链，
  可关闭 ``strong_trend_mode`` 回退 V4 双模式）。
- 执行复用 **SimGateway + CostModel + PortfolioState**（与实盘同撮合/成本/账户口径，P7 精神），
  但**不经现有 RiskEngine / PositionSizer**，保持新策略独立、绝不改动现有策略。
- 数据契约：
  * 日线（Freq.D1, Adjust.NONE）：涨幅 / 量比（当日量 ÷ 近 5 日均量）/ 换手率（turnover_rate 列）/
    流通市值（float_share × 收盘）；⑥阶梯放量的日线层（今日量 ≥ 昨日量 × 倍数）与
    ⑦跑赢大盘（个股当日涨幅 − 沪深300 当日涨幅，指数日线 get_index_bars）均恒可严格校验。
  * 分钟线（Freq.M5）：⑥b 阶梯放量分时层（午后连续竞价段量逐段递增）/ ⑧尾盘筹码结构
    （收阳线且现价低于分时均价 VWAP，主力尾盘偷袭形态）。MockProvider 不实现分钟线（对 M5 退化返回日线），
    故 **sim 模式自动降级**——分钟依赖的 2 条规则改为"放行但标记未验证"，结果明确标注【非真实业绩】。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd

from ..backtest.metrics import performance
from ..core.config import Settings
from ..core.trading import Fill, Order, OrderType, Side
from ..datahub.types import Adjust, Bar, Freq, InstrumentInfo
from ..execution.costs import CostModel
from ..execution.gateway.simulator import SimGateway
from ..portfolio.state import PortfolioState

logger = logging.getLogger(__name__)

_INDEX_SYMBOL = "000300.SH"
_NEW_HIGH_EPS = 0.001    # ⑧ 尾盘窗口最高价 vs 日线最高价的相对容差（跨源聚合噪声）
_BAR_MIN = 5.0           # M5 bar 周期（分钟）。QMT 分钟线按【结束时刻】标记（首根 09:35、末根 15:00）


def _trading_offset_min(t: time) -> float:
    """bar 覆盖区间起点的连续竞价分钟偏移（bar 按结束时刻标记）。

    09:35→0（覆盖 09:30~09:35）、11:30→115、13:05→120、15:00→235；非交易时段 -1。
    兼容起点标记源：09:30/13:00 同样映射到段首（0/120）。"""
    m = t.hour * 60 + t.minute
    if 9 * 60 + 30 <= m <= 11 * 60 + 30:
        return max(0.0, float(m - (9 * 60 + 35)))
    if 13 * 60 <= m <= 15 * 60:
        return 120.0 + max(0.0, float(m - (13 * 60 + 5)))
    return -1.0


@dataclass
class TailPickConfig:
    """尾盘选股法全部参数，从 ``config/settings.yaml::strategies.tail_pick`` 加载。"""

    enabled: bool = False
    select_time: str = "14:30"
    entry_time: str = "14:30"
    exit_window_start: str = "09:30"
    exit_window_end: str = "10:00"
    # V5（2026-08-14）：涨幅带统一为 1%~3.5%（温和放量上涨，非大涨避免透支），
    # 不分强弱市（strong_trend_mode 开启后弱市买跌带停用）
    min_pct_change: float = 0.01
    max_pct_change: float = 0.035
    # V4.0 熊市反击版：弱市（指数<MA20）切换「买跌」带 —— 大盘下跌时维持不跌或微涨的票
    # ＝主力护盘/抛压枯竭，次日低开下杀动能弱
    weak_min_pct_change: float = -0.01
    weak_max_pct_change: float = 0.015
    min_volume_ratio: float = 1.0
    min_turnover_rate: float = 0.05
    max_turnover_rate: float = 0.10
    min_float_market_cap: float = 5_000_000_000
    max_float_market_cap: float = 50_000_000_000
    # ⑥ 阶梯放量：a) 日线层——今日量 ≥ 昨日量 × volume_ladder_ratio；
    #              b) 分时层——午后连续竞价时段（13:00 起）等分为 volume_ladder_segments 段，
    #                 各段量逐段递增，后段 ≥ 前段 × volume_ladder_seg_tolerance
    #                 （开盘段量结构性全天最大、盘中 U 型低谷，均不适合参与递增比较）
    volume_ladder_ratio: float = 1.0
    volume_ladder_segments: int = 3
    volume_ladder_seg_tolerance: float = 0.9
    # V4.0 缩量过滤（弱市取代上述阶梯放量）：当日量 ≥ 5日均量 × vol_spike_exclude_ratio
    # 强弱市一律剔除（疑似拉高出货）；弱市另要求量比 < shrink_vol_max_ratio
    # （温和放量或缩量），且停用 ③量比下限 / ⑥a / ⑥b
    shrink_vol_max_ratio: float = 1.2
    vol_spike_exclude_ratio: float = 2.0
    # V5 强趋势+缩量版（2026-08-14，方向三）：放弃"买跌"，不分强弱市统一选股——
    #   ② 涨幅带统一 min/max_pct_change（1%~3.5%）；
    #   ③ 缩量过滤：当日量 < 5日均量 × shrink_volume_ratio_max（主力锁仓、隔夜抛压轻）；
    #      同时停用 ③量比下限 / ⑥a / ⑥b 阶梯放量 / ⑧筹码结构（与强趋势哲学矛盾）；
    #   ⑨ 近5日相对大盘强势：个股近5日涨幅跑赢沪深300 ≥ min_5d_outperf_vs_index。
    # 关闭 strong_trend_mode 即完整回退 V4.0 双模式行为。
    strong_trend_mode: bool = True
    shrink_volume_ratio_max: float = 1.1
    min_5d_outperf_vs_index: float = 0.03
    # V6.0 低位防御+不对称止损（2026-08-15）：针对 V5 盈亏比倒挂（赢小亏大）重构——
    #   选股从"追强势"转"低位缩量止跌"：⑩ 收盘 < MA(low_ma_days)（绝对低位、抛压枯竭）；
    #   ⑪ 当日最低 > 前一日最低（不再创新低确认止跌）；⑫ 振幅 (high-low)/昨收 <
    #   max_amplitude_pct（过滤高波动杂毛）；同时停用 ⑥a/⑥b/⑦/⑧/⑨ 追强势规则。
    #   离场改不对称止损：低开 < 成本×(1-v6_low_cut_pct) 竞价全砍；平开/高开首 5min
    #   不卖，冲高 +v6_be_trigger_pct 激活移动保本（跌破成本全砍）；触及 +v6_take_profit_pct
    #   卖 v6_tp_sell_ratio（70%），余仓从最高点回撤 v6_trail_pullback_pct 离场，
    #   无触发 10:00 市价走。关闭 defense_mode 即完整回退 V5 行为。
    defense_mode: bool = True
    low_ma_days: int = 20
    max_amplitude_pct: float = 0.05
    v6_low_cut_pct: float = 0.003
    v6_be_trigger_pct: float = 0.008
    v6_take_profit_pct: float = 0.02
    v6_tp_sell_ratio: float = 0.7
    v6_trail_pullback_pct: float = 0.012
    # V7.0 回归强势+严控风险（2026-08-15）：V6「低位防御」一年回测 -23.0% 证伪
    #   （MA20 下方缩量反弹隔夜低开率 52%，下跌中继非主力试盘）——彻底放弃低位：
    #   大盘前置硬空仓：沪深300 收盘 ≤ MA(market_ma_days) 全天不出票（取消弱市试错）；
    #   站上 MA 满仓档（ratio=1.0）出击。个股：⑬ 收盘站上 MA(low_ma_days)
    #   （与 V6 ⑩ 相反，中期趋势向上）+ 恢复 ⑨ 近5日跑赢沪深300 ≥
    #   min_5d_outperf_vs_index + ② 涨幅带 1%~3.5% + ③ 缩量 <5日均量×
    #   shrink_volume_ratio_max。离场完全复用 V6 不对称止损链
    #   （需同时开 defense_mode）。关闭 v7_strong_mode 即回退 V6 低位防御行为。
    v7_strong_mode: bool = False
    # V5 离场链即 _exit_day 的时间不对称分支（TAIL_HALF_OPEN/TAIL_LOWOPEN_CUT/
    # TAIL_GAP*/TAIL_HARD_STOP/TAIL_BREAKEVEN/TAIL_TP）。
    # （2026-08-15 P0：删除死配置 auction_decision_enabled / auction_high_pct /
    #  auction_low_pct —— V5 竞价决策分支从未在 _exit_day 实现，字段只存在于注释中。）
    # ⑦ 跑赢大盘：个股当日涨幅 − 沪深300 当日涨幅 ≥ 该值
    min_intraday_outperf_vs_index: float = 0.0
    # ⑧'筹码结构 VWAP 容差：尾盘收 > VWAP×(1+容差) 才剔除（实测「收阳」票多全天
    # 运行在均价线上方，严格 <VWAP 通过率仅 4%~35%，+1% 容差避免边界误杀）
    chip_vwap_tolerance_pct: float = 0.01
    # 纪律
    overnight_stop_pct: float = 0.03  # 仅日线降级路径保留；分钟级低开分支改由 9:45 砍仓兜底
    max_positions: int = 5
    position_fraction: float = 0.20
    cash_usage_ratio: float = 0.95
    universe_top_n: int = 100
    require_minute_bars: bool = True
    # 离场增强：优先级链（分钟级逐 bar）——2026-08-13 四段式革命改为时间不对称卖出：
    #   高开 > gapup_threshold_pct：竞价近似（首根 bar open）卖半仓 TAIL_HALF_OPEN，
    #        剩余半仓 ①硬止损 / ③保本 / ④止盈 链至窗口结束；
    #        隔夜动量（open>昨收 且首根 bar 量 ≥ open_momentum_vol_mult×T日bar均量）
    #        → 取消止盈，持至 open_momentum_hold_until 离场 TAIL_MOMENTUM_EXIT
    #   低开 < -gapdown_threshold_pct：竞价不卖，等 low_open_check_time（9:45）；
    #        期间翻红（最高 > T日收盘）→ 正常离场链；否则 9:45 砍仓 TAIL_LOWOPEN_CUT
    #   平开 ±阈值内：①硬止损 → ②缺口保护（5分钟缓冲）→ ③保本 → ④止盈 → 窗口尾离场
    #   （V4.0：⑤VWAP 止损默认关闭让位 -1.2% 硬止损；日线降级路径仍保留 ①隔夜硬止损 TAIL_STOP）
    gap_protect_enabled: bool = True
    gap_buffer_enabled: bool = True
    gap_buffer_pct: float = 0.005
    breakeven_trigger_pct: float = 0.010
    take_profit_pct: float = 0.028   # V4.0：+1.8%→+2.8%（盈亏比重构 2.8/1.2≈2.3）
    vwap_exit_enabled: bool = False  # V4.0：VWAP 止损默认关闭让位硬止损（字段保留，可手动开回）
    hard_stop_pct: float = 0.012     # V4.0：-1.2% 硬止损，触及立即市价离场（动量分支亦保底）
    # 大市温度计（V4.0 熊市反击版）：
    #   条件A 趋势：沪深300 站上 MA(market_ma_days) 且上涨家数 > weak_market_adv_min
    #        → 满仓档（ratio=1.0，追涨模式）；adv ≤ weak_market_adv_min → 强市广度闸门空仓
    #   条件B 弱市试错：指数跌破 MA 但上涨家数 ≥ breadth_block_below(1000)
    #        → 侦察兵仓位（ratio=weak_market_position_ratio=0.2，买跌模式：
    #        weak_min/max_pct_change 涨幅带 + 缩量过滤）
    #   条件C 绝对空仓：上涨家数 < breadth_block_below
    market_filter_enabled: bool = True
    market_ma_days: int = 20
    # 选项A 大盘趋势硬空仓（2026-08-15）：V5/V6/V7 三版一年回测全负后的框架级验证——
    #   只在沪深300 收盘 > MA(market_ma_days) 时允许交易（站上保留 V5 满仓档与
    #   强市广度闸门），跌破绝对空仓、取消一切弱市侦察兵试错。默认关闭，
    #   开启后优先于 V7/广度分档逻辑生效；与选股因子正交，可叠加任意版本。
    strict_trend_gate: bool = False
    market_breadth_required: bool = True
    weak_market_adv_min: int = 2500
    weak_market_position_ratio: float = 0.2  # V4.0：弱市仓位 0.5 半仓降为 0.2 侦察兵仓位
    breadth_block_below: int = 1000
    # 板块效应（2026-08-13 四段式革命·杀手锏）：当日行业涨幅排名前 sector_top_n
    # 的候选股权重 ×sector_boost_mult（允许加倍仓位）；后 sector_bottom_n 直接剔除。
    # 行业映射为东财板块【当前快照】，回测存在板块调整偏差。
    sector_enabled: bool = True
    sector_top_n: int = 5
    sector_bottom_n: int = 50
    sector_boost_mult: float = 2.0
    industry_map_path: str = "data/industry_map_em.json"
    # 时间不对称离场新增参数
    gapup_threshold_pct: float = 0.005
    gapdown_threshold_pct: float = 0.005
    low_open_check_time: str = "09:45"
    # B 方案（2026-08-15 低开反事实预验证）：低开时不再等 9:45 翻红/砍仓，
    #   开盘即按 -hard_stop_pct 止损单执行（跳空穿透按开盘价），未触及则 10:30 bar
    #   close 离场。默认 False 保持 V5 原逻辑（9:45 未翻红砍仓），仅供回测 A/B 对比。
    low_open_stop_mode: bool = False
    open_momentum_enabled: bool = True
    open_momentum_vol_mult: float = 2.0
    open_momentum_hold_until: str = "10:30"
    # 硬排除
    exclude_st: bool = True
    min_list_days: int = 60
    exclude_suspended: bool = True
    exclude_limit_locked: bool = True
    allowed_boards: list[str] = field(default_factory=lambda: ["MAIN", "GEM"])

    @classmethod
    def from_settings(cls, settings: Settings) -> "TailPickConfig":
        cfg = settings.section("strategies.tail_pick") or {}
        known = {
            "enabled", "select_time", "entry_time", "exit_window_start", "exit_window_end",
            "min_pct_change", "max_pct_change", "weak_min_pct_change", "weak_max_pct_change",
            "min_volume_ratio", "min_turnover_rate",
            "max_turnover_rate", "min_float_market_cap", "max_float_market_cap",
            "volume_ladder_ratio", "volume_ladder_segments", "volume_ladder_seg_tolerance",
            "shrink_vol_max_ratio", "vol_spike_exclude_ratio",
            "strong_trend_mode", "shrink_volume_ratio_max", "min_5d_outperf_vs_index",
            "defense_mode", "low_ma_days", "max_amplitude_pct", "v6_low_cut_pct",
            "v6_be_trigger_pct", "v6_take_profit_pct", "v6_tp_sell_ratio", "v6_trail_pullback_pct",
            "v7_strong_mode",
            "min_intraday_outperf_vs_index",
            "chip_vwap_tolerance_pct",
            "overnight_stop_pct", "max_positions", "position_fraction", "cash_usage_ratio",
            "universe_top_n", "require_minute_bars", "exclude_st", "min_list_days",
            "exclude_suspended", "exclude_limit_locked", "allowed_boards",
            "gap_protect_enabled", "gap_buffer_enabled", "gap_buffer_pct",
            "breakeven_trigger_pct", "take_profit_pct", "hard_stop_pct",
            "vwap_exit_enabled", "market_filter_enabled", "market_ma_days",
            "strict_trend_gate",
            "market_breadth_required", "weak_market_adv_min",
            "weak_market_position_ratio", "breadth_block_below",
            "sector_enabled", "sector_top_n", "sector_bottom_n", "sector_boost_mult",
            "industry_map_path", "gapup_threshold_pct", "gapdown_threshold_pct",
            "low_open_check_time", "low_open_stop_mode",
            "open_momentum_enabled", "open_momentum_vol_mult",
            "open_momentum_hold_until",
        }
        kw: dict[str, Any] = {}
        for k, v in cfg.items():
            if k in known:
                kw[k] = v
        return cls(**kw)


@dataclass
class TailPickPick:
    """一只通过 8 层筛选的候选标的。"""

    symbol: str
    entry_price: float
    entry_bar: Bar
    pct_change: float = 0.0
    volume_ratio: float = 0.0
    turnover_rate: float = 0.0
    float_market_cap: float = 0.0
    minute_verified: bool = False        # 分钟依赖的规则（⑥b/⑧）是否经过严格验证
    sector_boost: bool = False           # 命中当日板块涨幅前 N（仓位可加倍）
    industry: str = ""                   # 东财行业名（快照映射，缺省空串）
    reasons: list[str] = field(default_factory=list)


# ============================================================================ 选股
class TailPickScreener:
    """8 层严格筛选。完全独立，不依赖 SelectionPipeline / Regime / 因子引擎。"""

    def __init__(self, config: TailPickConfig):
        self.cfg = config
        # 最近一次 screen() 的大市仓位档位（1.0 满仓 / 0.2 侦察兵 / 0 空仓），供回测资金分配与实盘下单权重使用
        self.regime_ratio: float = 1.0
        # 最近一次 screen() 是否弱市买跌模式（指数<MA20）：切换涨幅带与量规则
        self.regime_weak: bool = False
        # 最近一次 screen() 的板块排名结果（板块效应）
        self.top_sectors: set[str] = set()
        self.industry_map: dict[str, str] = self._load_industry_map(config.industry_map_path)

    def screen(
        self,
        hub: Any,
        day: date,
        universe: list[str],
        instr_map: dict[str, InstrumentInfo],
        minute_available: bool,
        top_n: int = 0,
    ) -> list[TailPickPick]:
        """对 ``universe`` 做 8 层筛选，返回通过且按综合得分排序的候选。

        ``minute_available`` 为 False（sim/mock 无分钟线）时，规则⑥b/⑧ 改为
        best-effort（放行但 ``minute_verified=False``）。规则⑦跑赢大盘用指数日线
        口径，恒可严格校验。``top_n > 0`` 时先用日线粗筛收窄候选
        （避免对全市场拉分钟线），再进入 8 层精筛。
        """
        if not universe:
            return []

        # ---- 浮筹覆盖率检查（P0，2026-08-15）：QMT 日线无 turnover_rate 列时，
        #     规则④换手率用 成交量/流通股本 推算；float_share 缺失的标的会被
        #     静默当成 0 换手剔除（候选池失真）。每次实例只检查一次。----
        if not getattr(self, "_float_share_checked", False):
            self._float_share_checked = True
            n_instr = len(instr_map)
            if n_instr:
                n_fs = sum(1 for i in instr_map.values()
                           if float(getattr(i, "float_share", 0.0) or 0.0) > 0)
                if n_fs < n_instr:
                    logger.warning(
                        "浮筹覆盖率：%d/%d 标的 float_share>0（%.1f%%）—— 无 float_share 的标的"
                        "在换手率规则④（量/浮筹推算）下会被静默剔除",
                        n_fs, n_instr, n_fs / n_instr * 100)

        # ---- 日线特征（规则②③④⑤ + 硬排除）----
        daily = self._daily_frame(hub, day, universe)
        if daily is None or daily.empty:
            return []
        daily = daily.copy()
        daily["date"] = pd.to_datetime(daily["date"])
        # QMT 等源的日线不含换手率列：用 成交量/流通股本 推算（口径与 akshare 一致）
        if "turnover_rate" not in daily.columns:
            logger.warning("尾盘选股：日线无 turnover_rate 列（源=%s），改用 成交量/流通股本 推算",
                           getattr(hub, "name", hub.__class__.__name__))
            daily["turnover_rate"] = daily.apply(
                lambda r: (float(r["volume"]) / float_share)
                if (float_share := float(getattr(instr_map.get(r["symbol"]), "float_share", 0.0) or 0.0)) > 0
                else 0.0,
                axis=1,
            )
        else:
            daily["turnover_rate"] = pd.to_numeric(daily["turnover_rate"], errors="coerce").fillna(0.0)
        # ---- 大市温度计（三档弹性仓位）：空仓档当日不出票，半仓档收紧个股量比 ----
        ratio, why = self._market_regime_ratio(hub, day, daily)
        if ratio <= 0:
            logger.info("尾盘选股 %s 大市过滤拦截：%s，当日不出票", day, why)
            return []
        if ratio < 1.0:
            if self.cfg.defense_mode and self.cfg.v7_strong_mode:
                logger.info("尾盘选股 %s 大市弱市档：%s，侦察兵仓位 %.0f%%（V7 仅指数站上MA%d出票，此档不应出现）",
                            day, why, self.cfg.weak_market_position_ratio * 100, self.cfg.market_ma_days)
            elif self.cfg.defense_mode:
                logger.info("尾盘选股 %s 大市弱市档：%s，侦察兵仓位 %.0f%%（V6 低位防御选股，不切买跌带）",
                            day, why, self.cfg.weak_market_position_ratio * 100)
            elif self.cfg.strong_trend_mode:
                logger.info("尾盘选股 %s 大市弱市档：%s，侦察兵仓位 %.0f%%（V5 统一强趋势+缩量选股，不切买跌带）",
                            day, why, self.cfg.weak_market_position_ratio * 100)
            else:
                logger.info("尾盘选股 %s 大市弱市档：%s，侦察兵仓位 %.0f%% · 买跌带 %.1f%%~%.1f%% · 缩量过滤 <%.1f",
                            day, why, self.cfg.weak_market_position_ratio * 100,
                            self.cfg.weak_min_pct_change * 100, self.cfg.weak_max_pct_change * 100,
                            self.cfg.shrink_vol_max_ratio)
        # V5/V6 不分强弱市统一涨幅带；V4 双模式：弱市用「买跌」带 / 强市用「追涨」带
        if self.cfg.strong_trend_mode or self.cfg.defense_mode:
            band_lo, band_hi = self.cfg.min_pct_change, self.cfg.max_pct_change
        else:
            band_lo = self.cfg.weak_min_pct_change if self.regime_weak else self.cfg.min_pct_change
            band_hi = self.cfg.weak_max_pct_change if self.regime_weak else self.cfg.max_pct_change
        # ---- 板块效应：行业当日涨幅排名 → 后 N 剔除 / 前 N 仓位加倍 ----
        self.top_sectors = set()
        sector_excl: set[str] = set()
        boost_map: dict[str, bool] = {}
        if self.cfg.sector_enabled and self.industry_map:
            ranked, sector_excl, boost_map = self._industry_rank(daily, day)
            if ranked:
                self.top_sectors = set(ranked[: max(0, self.cfg.sector_top_n)])
        elif self.cfg.sector_enabled:
            logger.warning("尾盘选股 %s 板块效应已启用但行业映射缺失（%s），跳过板块过滤",
                           day, self.cfg.industry_map_path)
        if top_n > 0:
            idx5_ret = (self._index_period_return(hub, day, 5)
                        if (self.cfg.strong_trend_mode and not self.cfg.defense_mode)
                        or self.cfg.v7_strong_mode else None)
            universe = self._coarse_prefilter(daily, universe, instr_map, day, top_n,
                                              idx5_ret=idx5_ret)
            if not universe:
                return []
        g = daily.groupby("symbol")

        # ---- 分钟特征（规则⑥b/⑧）与指数日线涨跌幅（规则⑦/⑨）----
        minute = self._minute_frame(hub, day, universe) if minute_available else None
        idx_ret = self._index_day_return(hub, day)
        idx_ret5 = (self._index_period_return(hub, day, 5)
                    if (self.cfg.strong_trend_mode and not self.cfg.defense_mode)
                    or self.cfg.v7_strong_mode else None)

        picks: list[TailPickPick] = []
        for sym in universe:
            if sym not in g.groups:
                continue
            s = g.get_group(sym).sort_values("date")
            if len(s) < 2:
                continue
            today = s.iloc[-1]
            # 陈旧数据防护：源未更新到选股日时，尾行会停在 T-1，
            # 直接拿来做 T 日判定会用错日数据（曾致涨幅/新高全错）。宁缺毋滥。
            if pd.Timestamp(today["date"]).date() < day:
                continue
            prev = s.iloc[:-1]

            # —— 硬排除 ——
            instr = instr_map.get(sym)
            if self.cfg.exclude_st and instr and instr.is_st:
                continue
            if self.cfg.min_list_days and instr and instr.list_days(day) < self.cfg.min_list_days:
                continue
            if self.cfg.exclude_suspended and bool(today.get("is_suspended", False)):
                continue
            # 涨停封死不碰（买不进）
            if self.cfg.exclude_limit_locked and self._is_limit_locked_up(today, self.cfg):
                continue
            board_ok = self._board_ok(instr)
            if not board_ok:
                continue
            # 板块效应·剔除：属当日行业涨幅排名后 N 的股票直接淘汰（哪怕指标全符合）
            industry = self.industry_map.get(sym, "")
            if industry and industry in sector_excl:
                continue

            close = float(today["close"])
            prev_close = float(today["prev_close"]) if float(today["prev_close"]) > 0 else float(s.iloc[-2]["close"])
            if prev_close <= 0 or close <= 0:
                continue

            # ② 涨幅带：V5 统一 1%~3.5%（不分强弱市）；V4 强市追涨（3%~5%）/ 弱市买跌（-1%~+1.5%）
            pct = close / prev_close - 1
            if not (band_lo <= pct <= band_hi):
                continue

            # ③ 量规则：V5 缩量上限（量<5日均量×1.1，主力锁仓）；V4 全市场硬剔除异常放量
            # ≥ vol_spike_exclude_ratio（疑似拉高出货）+ 弱市缩量过滤 / 强市量比下限
            vol5 = prev.tail(5)["volume"].astype(float).mean()
            volume_ratio = float(today["volume"]) / vol5 if vol5 > 0 else float("inf")
            if self.cfg.strong_trend_mode or self.cfg.defense_mode:
                if volume_ratio >= self.cfg.shrink_volume_ratio_max:
                    continue
            else:
                if volume_ratio >= self.cfg.vol_spike_exclude_ratio:
                    continue
                if self.regime_weak:
                    if volume_ratio >= self.cfg.shrink_vol_max_ratio:
                        continue
                elif volume_ratio <= self.cfg.min_volume_ratio:
                    continue

            # ④ 换手率 5%~10%
            turnover = float(today.get("turnover_rate", 0.0) or 0.0)
            if not (self.cfg.min_turnover_rate <= turnover <= self.cfg.max_turnover_rate):
                continue

            # ⑤ 流通市值 50~500 亿
            float_share = float(getattr(instr, "float_share", 0.0) or 0.0)
            float_mcap = float_share * close
            if not (self.cfg.min_float_market_cap <= float_mcap <= self.cfg.max_float_market_cap):
                continue

            # —— 趋势位置铁律：V7 ⑬ 收盘站上 MA(low_ma_days)（强势回归、中期趋势向上）；
            # V6 ⑩收盘<MA + ⑪止跌 + ⑫低振幅（低位防御，与 V7 互斥）——
            if self.cfg.defense_mode and self.cfg.v7_strong_mode:
                if len(s) < self.cfg.low_ma_days:
                    continue
                ma_low = float(s["close"].tail(self.cfg.low_ma_days).astype(float).mean())
                if close < ma_low:
                    continue
            elif self.cfg.defense_mode:
                # ⑩ 绝对低位：收盘 < MA(low_ma_days)（低位票隔夜低开幅度显著小于高位票）
                if len(s) < self.cfg.low_ma_days:
                    continue
                ma_low = float(s["close"].tail(self.cfg.low_ma_days).astype(float).mean())
                if close >= ma_low:
                    continue
                # ⑪ 不再创新低：当日最低 > 前一日最低（止跌确认）
                if float(today["low"]) <= float(s.iloc[-2]["low"]):
                    continue
                # ⑫ 低振幅：(high-low)/昨收 < max_amplitude_pct（过滤高波动妖股）
                amplitude = (float(today["high"]) - float(today["low"])) / prev_close
                if amplitude >= self.cfg.max_amplitude_pct:
                    continue

            # ⑥a 阶梯放量 · 日线层（仅 V4 强市；V5 缩量哲学停用 / V4.0 弱市改缩量过滤）
            day_ladder = 0.0
            if not self.cfg.strong_trend_mode and not self.regime_weak:
                yest_vol = float(s.iloc[-2]["volume"])
                vol_today = float(today["volume"])
                day_ladder = vol_today / yest_vol if yest_vol > 0 else 0.0
                if day_ladder < self.cfg.volume_ladder_ratio:
                    continue

            # ⑦ 跑赢大盘：个股当日涨幅 − 沪深300 当日涨幅 ≥ 阈值（日线口径，恒可严格校验）；
            # V6 低位防御停用（低位票当日跑输大盘是常态，该规则与低位哲学矛盾）
            if self.cfg.defense_mode:
                outperf_txt = "⑦停用(V7仅看⑨5日超额)" if self.cfg.v7_strong_mode else "⑦停用(V6低位防御)"
            elif idx_ret is not None:
                outperf = pct - idx_ret
                if outperf < self.cfg.min_intraday_outperf_vs_index:
                    continue
                outperf_txt = f"⑦跑赢{outperf:+.2%}"
            else:
                outperf_txt = "⑦best-effort(无指数日线)"

            # ⑨ 近5日相对大盘强势：个股近5日涨幅 − 沪深300 同期涨幅 ≥ 阈值
            # （V6 低位防御停用；V7 强势回归恢复启用）
            outperf5_txt = ""
            if self.cfg.defense_mode and not self.cfg.v7_strong_mode:
                outperf5_txt = "⑨停用(V6低位防御)"
            elif self.cfg.strong_trend_mode or self.cfg.v7_strong_mode:
                if len(s) >= 6:
                    base5 = float(s.iloc[-6]["close"])
                    if base5 > 0 and idx_ret5 is not None:
                        outperf5 = close / base5 - 1 - idx_ret5
                        if outperf5 < self.cfg.min_5d_outperf_vs_index:
                            continue
                        outperf5_txt = f"⑨5日超额{outperf5:+.2%}"
                    else:
                        outperf5_txt = "⑨best-effort(指数5日不可得)"
                else:
                    outperf5_txt = "⑨best-effort(历史不足5日)"

            # ---- 分钟依赖规则（⑥b/⑧）----
            minute_verified = minute_available
            if self.cfg.defense_mode and self.cfg.v7_strong_mode:
                reasons = [f"涨幅{pct:+.1%}", f"缩量{volume_ratio:.2f}", f"换手{turnover:.1%}",
                           f"流通市值{float_mcap/1e8:.0f}亿", f"⑬站上MA{self.cfg.low_ma_days}",
                           outperf5_txt]
            elif self.cfg.defense_mode:
                reasons = [f"涨幅{pct:+.1%}", f"缩量{volume_ratio:.2f}", f"换手{turnover:.1%}",
                           f"流通市值{float_mcap/1e8:.0f}亿", f"⑩MA{self.cfg.low_ma_days}下方",
                           "⑪止跌(低点抬高)", f"⑫振幅{amplitude:.1%}"]
            elif self.regime_weak:
                reasons = [f"买跌涨幅{pct:+.1%}", f"缩量{volume_ratio:.2f}", f"换手{turnover:.1%}",
                           f"流通市值{float_mcap/1e8:.0f}亿", outperf_txt]
            else:
                reasons = [f"涨幅{pct:+.1%}", f"量比{volume_ratio:.1f}", f"换手{turnover:.1%}",
                           f"流通市值{float_mcap/1e8:.0f}亿", f"⑥今/昨量{day_ladder:.2f}", outperf_txt]

            tail_open = tail_high = tail_low = tail_close = None
            tail_vol = 0.0
            if minute_available and minute is not None:
                m = self._slice_minute(minute, sym, day, time(14, 25), time(15, 0))
                if not m.empty:
                    tail_open = float(m["open"].iloc[0])
                    tail_high = float(m["high"].max())
                    tail_low = float(m["low"].min())
                    tail_close = float(m["close"].iloc[-1])
                    tail_vol = float(m["volume"].sum())

                if self.cfg.defense_mode:
                    # V6/V7 选股均无分钟依赖规则（⑥b/⑧ 追强势血统停用），
                    # 仅沿用尾盘切片确定 14:30 入场参考价
                    reasons.append("V7强势回归(无分钟选股规则)" if self.cfg.v7_strong_mode
                                   else "V6低位防御(无分钟选股规则)")
                else:
                    # ⑧ 尾盘筹码结构（2026-08-13 四段式革命，取代原「尾盘新高+站上VWAP」）：
                    #    a) 收阳线：尾盘现价 > 尾盘窗口开盘（当日温和走强）；
                    #    b) 现价 < 分时均价 VWAP×(1+chip_vwap_tolerance_pct)：全天均价下方
                    #       尾盘拉升＝主力偷袭形态，次日爆发力优于全天在均价线上方运行的票；
                    #       +1% 容差为混合方案回调（严格 <VWAP 实测通过率过低致 0 笔）。
                    vwap = self._day_vwap(today, minute, sym, day)
                    chip_ok = bool(tail_open and tail_close
                                   and tail_close > tail_open
                                   and vwap and tail_close < vwap * (1 + self.cfg.chip_vwap_tolerance_pct))
                    if not chip_ok:
                        minute_verified = False
                        reasons.append("⑧未通过(筹码结构:收阳且低于VWAP+容差)")
                        # 真实模式下严格：未验证即淘汰；best-effort 模式放行
                        if self.cfg.require_minute_bars and minute_available:
                            continue
                    else:
                        reasons.append("⑧筹码结构:收阳低于VWAP+容差")

                    # ⑥b 阶梯放量 · 分时层（仅强市；V4.0 弱市停用，缩量过滤生效）：
                    #    午后连续竞价时段（13:00 起）等分 N 段，各段量逐段递增
                    #    （验证尾盘买入前吸筹持续放量）。
                    if self.regime_weak:
                        reasons.append("⑥b停用(弱市缩量过滤)")
                    else:
                        seg_vols = self._ladder_seg_volumes(minute, sym, day)
                        tol = self.cfg.volume_ladder_seg_tolerance
                        ladder_ok = (len(seg_vols) >= 2 and all(v > 0 for v in seg_vols)
                                     and all(seg_vols[i] >= seg_vols[i - 1] * tol
                                             for i in range(1, len(seg_vols))))
                        if not ladder_ok:
                            minute_verified = False
                            reasons.append("⑥b未通过(分时量未逐段递增)")
                            if self.cfg.require_minute_bars and minute_available:
                                continue
                        else:
                            reasons.append("⑥b分时量逐段递增")
            else:
                reasons.append("⑥b/⑧best-effort(无分钟线)")
            if boost_map.get(industry, False):
                reasons.append("板块前%d加权" % max(0, self.cfg.sector_top_n))

            # ---- 入场参考价与撮合 Bar ----
            if tail_close is not None and tail_close > 0:
                entry_price = tail_close
                entry_bar = Bar(
                    symbol=sym, date=day, open=tail_open or tail_close,
                    high=tail_high or tail_close, low=tail_low or tail_close,
                    close=tail_close, volume=tail_vol, amount=0.0,
                )
            else:
                entry_price = close
                entry_bar = Bar(
                    symbol=sym, date=day, open=float(today["open"]), high=float(today["high"]),
                    low=float(today["low"]), close=close, volume=float(today["volume"]),
                    amount=float(today.get("amount", 0.0) or 0.0),
                )

            picks.append(TailPickPick(
                symbol=sym, entry_price=round(entry_price, 4), entry_bar=entry_bar,
                pct_change=pct, volume_ratio=volume_ratio, turnover_rate=turnover,
                float_market_cap=float_mcap, minute_verified=minute_verified,
                sector_boost=boost_map.get(industry, False), industry=industry,
                reasons=reasons,
            ))

        # 排序：分钟已验证优先，其次综合（涨幅靠近区间中枢 + 量比越大越靠前）得分。
        # 注意 reverse=True 会同时反转所有键：量比必须取正号才能"大者优先"。
        pct_center = (band_lo + band_hi) / 2
        picks.sort(key=lambda p: (p.minute_verified, -abs(p.pct_change - pct_center) + p.volume_ratio / 100),
                   reverse=True)
        return picks

    # ---------------------------------------------------------- 粗筛收窄
    def _coarse_prefilter(self, daily: pd.DataFrame, universe: list[str],
                          instr_map: dict[str, InstrumentInfo], day: date,
                          top_n: int, idx5_ret: float | None = None) -> list[str]:
        """日线层粗筛：先按完整日线规则（硬排除 + ②③④⑤ + ⑥a）取合格集，
        仅当合格集超过 ``top_n`` 时才按成交额取前 ``top_n`` 截断。

        目的：把分钟线取数/精筛范围从全市场（5000+）收窄，粗筛只用日线特征
        （已缓存），不引入未来信息。
        口径修正（2026-08-13）：原实现对「涨幅带内全部票」直接按成交额取 top_n，
        策略目标区间（流通市值 50~500 亿）的合格票成交额排名靠后会被系统性
        切掉（实测连续多日 ⑧ 通过票 0 存活）。改为只在日线层合格票内截断，
        保证任何能通过 8 层筛选的票不会在粗筛阶段被误杀。
        """
        g = daily.groupby("symbol")
        eligible: list[tuple[float, str]] = []
        for sym in universe:
            if sym not in g.groups:
                continue
            s = g.get_group(sym).sort_values("date")
            if len(s) < 2:
                continue
            today = s.iloc[-1]
            # 与精筛同口径：尾行未更新到选股日的陈旧序列直接排除
            if pd.Timestamp(today["date"]).date() < day:
                continue
            instr = instr_map.get(sym)
            if self.cfg.exclude_st and instr and instr.is_st:
                continue
            if self.cfg.min_list_days and instr and instr.list_days(day) < self.cfg.min_list_days:
                continue
            if self.cfg.exclude_suspended and bool(today.get("is_suspended", False)):
                continue
            if self.cfg.exclude_limit_locked and self._is_limit_locked_up(today, self.cfg):
                continue
            if not self._board_ok(instr):
                continue
            close = float(today["close"])
            prev_close = float(today["prev_close"]) if float(today["prev_close"]) > 0 else float(s.iloc[-2]["close"])
            if prev_close <= 0 or close <= 0:
                continue
            pct = close / prev_close - 1
            # ② 涨幅带（粗筛放宽 1% 缓冲，避免 turnover/浮点微差把边缘票提前切掉）；
            # V5/V6 统一带不分强弱市（与精筛同口径），V4 随强/弱市模式切换
            if self.cfg.strong_trend_mode or self.cfg.defense_mode:
                band_lo, band_hi = self.cfg.min_pct_change, self.cfg.max_pct_change
            else:
                band_lo = self.cfg.weak_min_pct_change if self.regime_weak else self.cfg.min_pct_change
                band_hi = self.cfg.weak_max_pct_change if self.regime_weak else self.cfg.max_pct_change
            if not (band_lo - 0.01 <= pct <= band_hi + 0.01):
                continue
            # ③ 量规则（与精筛同口径）：V5/V6 缩量上限；V4 全市场剔除异常放量，
            # 弱市无下限（缩量在精筛收紧）、强市量比下限
            vol5 = s.iloc[:-1].tail(5)["volume"].astype(float).mean()
            volume_ratio = float(today["volume"]) / vol5 if vol5 > 0 else float("inf")
            if self.cfg.strong_trend_mode or self.cfg.defense_mode:
                if volume_ratio >= self.cfg.shrink_volume_ratio_max:
                    continue
            else:
                if volume_ratio >= self.cfg.vol_spike_exclude_ratio:
                    continue
                if not self.regime_weak and volume_ratio <= self.cfg.min_volume_ratio:
                    continue
            # ④ 换手率（与精筛同口径：列缺失时用 量/流通股本 推算）
            turnover = float(today.get("turnover_rate", 0.0) or 0.0)
            if turnover <= 0:
                fs = float(getattr(instr, "float_share", 0.0) or 0.0)
                turnover = float(today["volume"]) / fs if fs > 0 else 0.0
            if not (self.cfg.min_turnover_rate - 0.005 <= turnover <= self.cfg.max_turnover_rate + 0.005):
                continue
            # ⑤ 流通市值
            fs = float(getattr(instr, "float_share", 0.0) or 0.0)
            float_mcap = fs * close
            if not (self.cfg.min_float_market_cap * 0.95 <= float_mcap
                    <= self.cfg.max_float_market_cap * 1.05):
                continue
            # —— 趋势位置铁律（粗筛口径：加缓冲防边缘票被提前误杀）——
            if self.cfg.defense_mode and self.cfg.v7_strong_mode:
                # ⑬ 收盘站上 MA(low_ma_days)（缓冲 -1%：略低于线的票精筛日可能站回线上）
                if len(s) < self.cfg.low_ma_days:
                    continue
                ma_low = float(s["close"].tail(self.cfg.low_ma_days).astype(float).mean())
                if close < ma_low * 0.99:
                    continue
            elif self.cfg.defense_mode:
                # ⑩ 收盘 < MA(low_ma_days)（缓冲 +1%：略高于线的票精筛日可能跌破线）
                if len(s) < self.cfg.low_ma_days:
                    continue
                ma_low = float(s["close"].tail(self.cfg.low_ma_days).astype(float).mean())
                if close >= ma_low * 1.01:
                    continue
                # ⑪ 止跌：当日最低 > 前一日最低
                if float(today["low"]) <= float(s.iloc[-2]["low"]):
                    continue
                # ⑫ 低振幅（缓冲 +1%）
                amplitude = (float(today["high"]) - float(today["low"])) / prev_close
                if amplitude >= self.cfg.max_amplitude_pct + 0.01:
                    continue
            # ⑥a 日线层阶梯放量（仅 V4 强市；V5 缩量哲学 / V6 低位防御 / V4.0 弱市均停用）
            if not self.cfg.strong_trend_mode and not self.cfg.defense_mode and not self.regime_weak:
                yest_vol = float(s.iloc[-2]["volume"])
                if yest_vol <= 0 or float(today["volume"]) / yest_vol < self.cfg.volume_ladder_ratio:
                    continue
            # ⑨ 近5日跑赢大盘（粗筛放宽 2% 缓冲，避免边缘票在精筛前被误杀；
            # V5 强趋势 / V7 强势回归且指数5日收益可得时生效；V6 低位防御停用）
            if (((self.cfg.strong_trend_mode and not self.cfg.defense_mode)
                    or self.cfg.v7_strong_mode)
                    and idx5_ret is not None and len(s) >= 6):
                base5 = float(s.iloc[-6]["close"])
                if base5 > 0:
                    outperf5 = close / base5 - 1 - idx5_ret
                    if outperf5 < self.cfg.min_5d_outperf_vs_index - 0.02:
                        continue
            eligible.append((float(today.get("amount", 0.0) or 0.0), sym))
        if len(eligible) <= top_n:
            return [sym for _, sym in eligible]
        eligible.sort(reverse=True)
        return [sym for _, sym in eligible[:top_n]]

    # ---------------------------------------------------------- 数据 helper
    def _daily_frame(self, hub, day, universe):
        try:
            # 12 个自然日窗口：保证节前节后都能凑足 5 个交易日（量比分母）；
            # V6/V7 需 MA(low_ma_days) → 扩窗至 low_ma_days*2+15 个自然日
            window_days = 12
            if self.cfg.defense_mode:
                window_days = max(window_days, self.cfg.low_ma_days * 2 + 15)
            return hub.get_bars(universe, Freq.D1, day - timedelta(days=window_days), day,
                                Adjust.NONE, validate=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("尾盘选股日线取数失败 %s: %s", day, exc)
            return None

    def _minute_frame(self, hub, day, universe):
        try:
            return hub.get_bars(universe, Freq.M5, day, day, Adjust.NONE)
        except Exception as exc:  # noqa: BLE001
            logger.warning("尾盘选股分钟线取数失败 %s: %s", day, exc)
            return None

    @staticmethod
    def _slice_minute(df: pd.DataFrame, sym: str, day: date, t0: time, t1: time) -> pd.DataFrame:
        sub = df[df["symbol"] == sym] if "symbol" in df.columns else df
        dt = pd.to_datetime(sub["date"])
        mask = (dt.dt.date == day) & (dt.dt.time >= t0) & (dt.dt.time <= t1)
        return sub[mask]

    @staticmethod
    def _index_day_return(hub: Any, day: date) -> float | None:
        """沪深300 当日涨跌幅（指数日线口径）。取不到（源缺/未更新到当日）返回 None。"""
        try:
            df = hub.get_index_bars(_INDEX_SYMBOL, day - timedelta(days=12), day)
        except Exception:  # noqa: BLE001
            return None
        if df is None or df.empty:
            return None
        df = df.sort_values("date")
        sub = df[pd.to_datetime(df["date"]).dt.date <= day]
        if len(sub) < 2 or pd.Timestamp(sub["date"].iloc[-1]).date() != day:
            return None
        prev_close = float(sub["close"].iloc[-2])
        close = float(sub["close"].iloc[-1])
        return close / prev_close - 1 if prev_close > 0 else None

    @staticmethod
    def _index_period_return(hub: Any, day: date, n: int) -> float | None:
        """沪深300 近 n 个交易日区间涨跌幅（含当日，指数日线口径）。

        即 close(day) / close(day-n个交易日) - 1，与个股近5日涨幅
        （close/base5 - 1，base5 为 5 个交易日前收盘）同口径，用于规则⑨
        5日超额收益比较。取不到（源缺/未更新到当日/历史不足）返回 None。
        """
        try:
            df = hub.get_index_bars(_INDEX_SYMBOL, day - timedelta(days=n * 3 + 10), day)
        except Exception:  # noqa: BLE001
            return None
        if df is None or df.empty:
            return None
        df = df.sort_values("date")
        sub = df[pd.to_datetime(df["date"]).dt.date <= day]
        if len(sub) < n + 1 or pd.Timestamp(sub["date"].iloc[-1]).date() != day:
            return None
        base = float(sub["close"].iloc[-1 - n])
        close = float(sub["close"].iloc[-1])
        return close / base - 1 if base > 0 else None

    def _market_regime_ratio(self, hub: Any, day: date, daily: pd.DataFrame) -> tuple[float, str]:
        """大市温度计弹性仓位（V4.0 熊市反击版）。

        条件A：沪深300 收盘 ≥ MA(market_ma_days)：上涨家数 > weak_market_adv_min(2500)
            → 满仓档（1.0，追涨模式）；adv ≤ 2500 → 「强市广度闸门」空仓
            （回测证实强市缩量日出票胜率低，-2.66% → -5.58% 的教训）。
        条件B：指数跌破 MA 但上涨家数 ≥ breadth_block_below(1000) → 侦察兵仓位
            （weak_market_position_ratio=0.2，买跌模式：weak_min/max_pct_change 涨幅带
            + 缩量过滤；1000~2500 记「震荡市」、>2500 记「弱市」）。
        条件C：上涨家数 < breadth_block_below（含 breadth 数据缺失）→ 绝对空仓（0）。

        指数日线取不到时条件A 放行（best-effort，不因数据缺失误杀）；
        涨跌家数用当日日线帧全 universe 统计（与选股同源，无额外取数）。

        V7.0（v7_strong_mode）：只看指数 MA(market_ma_days)——站上 → 满仓档 1.0，
        跌破 → 硬空仓 0（彻底取消广度分档与弱市试错）。

        选项A（strict_trend_gate，优先级最高）：沪深300 > MA(market_ma_days) 才允许
        交易（站上仍过强市广度闸门），跌破绝对空仓；与选股因子正交。
        """
        self.regime_ratio = 1.0
        self.regime_weak = False
        if not self.cfg.market_filter_enabled:
            return 1.0, ""
        # 条件A：沪深300 收盘 ≥ MA(N)（含当日）
        above_ma, ma_note = True, ""
        try:
            idx = hub.get_index_bars(_INDEX_SYMBOL, day - timedelta(days=self.cfg.market_ma_days * 3), day)
        except Exception:  # noqa: BLE001
            idx = None
        if idx is not None and not idx.empty:
            idx = idx.sort_values("date")
            sub = idx[pd.to_datetime(idx["date"]).dt.date <= day]
            closes = pd.to_numeric(sub["close"], errors="coerce").dropna()
            if len(closes) >= self.cfg.market_ma_days:
                ma = float(closes.tail(self.cfg.market_ma_days).mean())
                if float(closes.iloc[-1]) < ma:
                    above_ma = False
                    ma_note = f"条件A:沪深300收盘{float(closes.iloc[-1]):.0f}<MA{self.cfg.market_ma_days}({ma:.0f})"
        # 广度统计（强/弱市共用）
        t = daily[pd.to_datetime(daily["date"]).dt.date == day]
        adv: int | None = None
        if not t.empty:
            pc = pd.to_numeric(t["prev_close"], errors="coerce")
            cl = pd.to_numeric(t["close"], errors="coerce")
            valid = (pc > 0) & (cl > 0)
            adv = int((cl[valid] > pc[valid]).sum())
        if self.cfg.strict_trend_gate:
            # 选项A：沪深300 > MA(N) 才允许交易（站上保留 V5 强市广度闸门），
            # 跌破绝对空仓——放弃在指数下跌期出票的一切试错
            if above_ma:
                if self.cfg.market_breadth_required and adv is not None \
                        and adv <= self.cfg.weak_market_adv_min:
                    self.regime_ratio = 0.0
                    return 0.0, f"强市广度{adv}≤{self.cfg.weak_market_adv_min}，空仓"
                return 1.0, ma_note or f"沪深300站上MA{self.cfg.market_ma_days}，满仓档"
            self.regime_ratio = 0.0
            return 0.0, f"趋势硬空仓:{ma_note or '沪深300收盘<MA%d' % self.cfg.market_ma_days}"
        if self.cfg.v7_strong_mode:
            # V7.0 大盘前置硬空仓：指数站上 MA(N) 满仓出击，跌破全天不出票
            if above_ma:
                return 1.0, ma_note or f"V7:沪深300站上MA{self.cfg.market_ma_days}，满仓档"
            self.regime_ratio = 0.0
            return 0.0, f"V7硬空仓:{ma_note or '沪深300收盘<MA%d' % self.cfg.market_ma_days}"
        if above_ma:
            # 强市也要广度闸门：回测证实强市缩量日（adv 不足）出票胜率低
            if self.cfg.market_breadth_required and adv is not None \
                    and adv <= self.cfg.weak_market_adv_min:
                self.regime_ratio = 0.0
                return 0.0, f"强市广度{adv}≤{self.cfg.weak_market_adv_min}，空仓"
            return 1.0, ma_note
        if not self.cfg.market_breadth_required:
            # 未开启广度分档：弱市保守直接空仓
            self.regime_ratio = 0.0
            return 0.0, f"{ma_note};弱市且未启用广度分档，空仓"
        # 弱市/震荡市：上涨家数 ≥ breadth_block_below(1000) 即侦察兵仓位试错
        # （V4.0：0.5 半仓降为 weak_market_position_ratio=0.2；买跌带/缩量过滤在 screen 内切换）
        if adv is None:
            self.regime_ratio = 0.0
            return 0.0, f"{ma_note};涨跌家数不可得，保守空仓"
        if adv >= self.cfg.breadth_block_below:
            self.regime_ratio = self.cfg.weak_market_position_ratio
            self.regime_weak = True
            tag = "震荡市" if adv <= self.cfg.weak_market_adv_min else "弱市"
            return self.cfg.weak_market_position_ratio, \
                f"{ma_note};上涨{adv}≥{self.cfg.breadth_block_below}（{tag}），侦察兵仓位档"
        self.regime_ratio = 0.0
        return 0.0, f"{ma_note};上涨{adv}<{self.cfg.breadth_block_below}（绝对空仓）"

    @staticmethod
    def _day_vwap(today: Any, minute: pd.DataFrame, sym: str, day: date) -> float | None:
        """当日分时均价 VWAP：优先分钟线全天聚合 sum(amount)/sum(volume)，
        不可用时退化为日线 amount/volume。"""
        try:
            sub = minute[minute["symbol"] == sym] if "symbol" in minute.columns else minute
            dt = pd.to_datetime(sub["date"])
            w = sub[dt.dt.date == day]
            if not w.empty and "amount" in w.columns:
                vol = float(w["volume"].sum())
                if vol > 0:
                    return float(w["amount"].sum()) / vol
        except Exception:  # noqa: BLE001
            pass
        try:
            amt = float(today["amount"] or 0.0)
            vol = float(today["volume"] or 0.0)
        except Exception:  # noqa: BLE001
            return None
        return amt / vol if vol > 0 else None

    def _ladder_seg_volumes(self, minute: pd.DataFrame, sym: str, day: date) -> list[float]:
        """把当日午后连续竞价时段（13:00 起）等分为 N 段，返回各段累计量（⑥b 阶梯放量）。

        口径修正（2026-08-13）：原按全天（含 9:30 集合竞价 bar）等分要求逐段递增，
        与 A 股日内 U 型量分布（开盘/尾盘量大、盘中小）叠加后在真实数据上
        几乎不可能成立（实测候选集通过率 0/32），属逻辑漏洞。
        策略本意是验证尾盘买入前吸筹持续放量，故只看午后连续竞价段：
        13:00 起等分 N 段、各段量逐段递增。实测同口径通过率 3/32，保持严格且可达。
        分段按数据实际覆盖的分钟空间（最晚 bar 偏移 + 一个 bar 周期），
        盘中（如 14:30）选股时不会拿尚未发生的时段做比较。
        """
        sub = self._slice_minute(minute, sym, day, time(13, 0), time(15, 0))
        if sub.empty:
            return []
        offs = pd.to_datetime(sub["date"]).dt.time.map(_trading_offset_min)
        valid = offs >= 0
        sub = sub[valid]
        offs = offs[valid]
        if sub.empty:
            return []
        offs = offs - offs.min()  # 以窗口起点（13:00/盘中实际首 bar）归一
        span = float(offs.max()) + _BAR_MIN
        n = max(2, int(self.cfg.volume_ladder_segments))
        seg_len = span / n
        vols = [0.0] * n
        for off, v in zip(offs, sub["volume"].astype(float)):
            vols[min(int(off // seg_len), n - 1)] += float(v)
        return vols

    @staticmethod
    def _is_limit_locked_up(today: Any, cfg: TailPickConfig) -> bool:
        close = float(getattr(today, "close", 0) or (today.get("close") if hasattr(today, "get") else 0))
        lim = getattr(today, "limit_up", None)
        if lim is None and hasattr(today, "get"):
            lim = today.get("limit_up")
        if lim is None or lim <= 0:
            return False
        return close >= lim * 0.999

    def _board_ok(self, instr: InstrumentInfo | None) -> bool:
        if instr is None:
            return True
        board = getattr(instr, "board", None) or ""
        # InstrumentInfo 可能用 industry/name 字段；优先用 build_profile 推断
        if not board:
            try:
                from ..core.instruments import build_profile, normalize_symbol
                board = build_profile(normalize_symbol(instr.symbol)).board.value
            except Exception:  # noqa: BLE001
                return True
        allowed = set(self.cfg.allowed_boards) or {"MAIN", "GEM"}
        return board in allowed

    # ---------------------------------------------------------- 板块效应
    @staticmethod
    def _load_industry_map(path: str) -> dict[str, str]:
        """加载东财行业映射快照 {symbol: 行业名}。文件缺失/损坏返回空 dict
        （板块过滤降级为中性放行，不阻断选股）。"""
        import json
        import os
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            m = raw.get("map", raw) if isinstance(raw, dict) else {}
            return {str(k): str(v) for k, v in m.items() if v}
        except Exception as exc:  # noqa: BLE001
            logger.warning("行业映射加载失败 %s: %s", path, exc)
            return {}

    def _industry_rank(self, daily: pd.DataFrame,
                       day: date) -> tuple[list[str], set[str], dict[str, bool]]:
        """当日行业涨幅排名（历史回测可用口径：日线帧内成分股涨幅中位数）。

        返回（按涨幅降序的行业列表，后 sector_bottom_n 行业集合，
        前 sector_top_n 行业的 boost 映射 {行业: True}）。
        只用当日在 daily 帧内的成分股统计（与选股同源，无未来信息）；
        成分股少于 3 只的行业不参与排名（样本不足易被单票扰动）。
        """
        t = daily[pd.to_datetime(daily["date"]).dt.date == day]
        if t.empty:
            return [], set(), {}
        pc = pd.to_numeric(t["prev_close"], errors="coerce")
        cl = pd.to_numeric(t["close"], errors="coerce")
        valid = (pc > 0) & (cl > 0)
        t = t[valid]
        pct = cl[valid] / pc[valid] - 1
        syms = t["symbol"].astype(str).tolist()
        rets: dict[str, list[float]] = {}
        for sym, r in zip(syms, pct.tolist()):
            ind = self.industry_map.get(sym, "")
            if ind:
                rets.setdefault(ind, []).append(float(r))
        ranked = sorted(((float(np.median(v)), k) for k, v in rets.items() if len(v) >= 3),
                        reverse=True)
        names = [k for _, k in ranked]
        n_bottom = max(0, self.cfg.sector_bottom_n)
        excl = set(names[-n_bottom:]) if n_bottom else set()
        boost = {k: True for k in names[: max(0, self.cfg.sector_top_n)]}
        return names, excl, boost


# ============================================================================ 执行
class TailPickExecutor:
    """一夜持股的执行：复用 SimGateway + CostModel + PortfolioState（同实盘撮合口径）。

    不经现有 RiskEngine / PositionSizer——本策略自带独立仓位与隔夜硬止损纪律，
    保证不改动现有策略。
    """

    def __init__(self, cost: CostModel, gateway: SimGateway, portfolio: PortfolioState,
                 config: TailPickConfig):
        self.cost = cost
        self.gateway = gateway
        self.portfolio = portfolio
        self.cfg = config
        self.seq = 0

    def buy(self, sym: str, entry_bar: Bar, entry_price: float, asof: date,
            max_notional: float) -> Fill | None:
        cash = self.portfolio.cash
        notional = min(max_notional, cash * self.cfg.cash_usage_ratio)
        if entry_price <= 0 or notional <= 0:
            return None
        shares = int(notional / entry_price // 100 * 100)
        if shares < 100:
            return None
        self.seq += 1
        order = Order(
            order_id=f"tp_buy_{asof.isoformat()}_{self.seq}_{sym}",
            symbol=sym, side=Side.BUY, quantity=shares,
            price=round(entry_price, 4), order_type=OrderType.LIMIT,
        )
        fill = self.gateway.submit(order, asof, entry_bar, self.cost)
        if fill is None:
            return None
        self.portfolio.apply_fill(fill, fill.total_fee, is_buy=True,
                                  signal="TAIL_BUY", asof=asof)
        # 关键：apply_fill 的买入分支不写 opened_at（与现有 ExecutionService
        # 在 _attach_risk_meta 中单独设置一致）。本执行器直接走 apply_fill，
        # 必须自己补上 opened_at，否则 mark_t1 永远无法解锁 can_use → 隔夜仓卖不出。
        pos = self.portfolio.positions.get(sym)
        if pos is not None:
            pos.opened_at = asof
        return fill

    def sell(self, sym: str, exit_bar: Bar, exit_price: float, asof: date,
             market: bool = False, signal: str = "TAIL_SELL",
             qty: int | None = None) -> Fill | None:
        pos = self.portfolio.positions.get(sym)
        if pos is None:
            return None
        shares = pos.can_use if qty is None else min(int(qty), pos.can_use)
        if shares <= 0:
            return None
        self.seq += 1
        order = Order(
            order_id=f"tp_sell_{asof.isoformat()}_{self.seq}_{sym}",
            symbol=sym, side=Side.SELL, quantity=shares,
            price=None if market else round(exit_price, 4),
            order_type=OrderType.MARKET if market else OrderType.LIMIT,
        )
        fill = self.gateway.submit(order, asof, exit_bar, self.cost)
        if fill is None:
            return None
        self.portfolio.apply_fill(fill, fill.total_fee, is_buy=False,
                                  signal=signal, asof=asof)
        return fill


# ============================================================================ 回测
@dataclass
class TailPickResult:
    equity_curve: list[float] = field(default_factory=list)
    trades: list = field(default_factory=list)
    closed_trades: list = field(default_factory=list)
    open_positions: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    details: list = field(default_factory=list)
    minute_available: bool = False
    #: P0 成本归因（2026-08-15）：毛/净拆分、滑点、费用、换手、往返成本率
    cost_attribution: dict = field(default_factory=dict)
    #: P0 离场信号归因：信号 × 笔数 × 胜率 × 笔均收益 × 盈亏合计
    gap_attribution: list = field(default_factory=list)


# ============================================================================ 归因（P0，2026-08-15）
def _slip_frac(cost: CostModel, side: Side) -> float:
    """当前成本模型下该方向的滑点比例（正数）。异常按 0 处理，不阻塞归因。"""
    try:
        return abs(float(cost.slippage(side, 1.0, volume_ratio=0.0)))
    except Exception:  # noqa: BLE001
        return 0.0


def build_cost_attribution(cost: CostModel, fills: list, closed_trades: list,
                           initial_cash: float) -> dict:
    """已实现盈亏的成本归因：净盈亏 = 信号毛盈亏 − 滑点 − 显式费用。

    - 滑点按 SimGateway 口径从成交价反推（买入价含 +frac、卖出价含 −frac，
      FIXED 模型下即 base_slippage）；
    - 显式费用 = 佣金 + 印花税 + 过户费（逐笔 Fill 自带）；
    - ``single_side_turnover`` = (买入+卖出成交额)/2 ÷ 期初资金（单边年换手倍数）；
    - ``roundtrip_cost_rate`` = 成本拖累 ÷ 平均单边成交额（每完成一次买卖循环的成本占比）；
    - ``n_round_trips`` = 合并 TP70/TRAIL 拆笔后的真实笔数（按 opened_at+symbol 聚合）。
    """
    buy_notional = sell_notional = 0.0
    slip_buy = slip_sell = 0.0
    fee_total = 0.0
    n_buys = n_sells = 0
    frac_b = _slip_frac(cost, Side.BUY)
    frac_s = _slip_frac(cost, Side.SELL)
    for f in fills or []:
        amt = float(f.price) * int(f.quantity or 0)
        fee_total += (float(getattr(f, "commission", 0.0) or 0.0)
                      + float(getattr(f, "stamp_tax", 0.0) or 0.0)
                      + float(getattr(f, "transfer_fee", 0.0) or 0.0))
        if f.side is Side.BUY:
            buy_notional += amt
            n_buys += 1
            if frac_b > 0:
                slip_buy += amt * frac_b / (1 + frac_b)
        else:
            sell_notional += amt
            n_sells += 1
            if frac_s > 0:
                slip_sell += amt * frac_s / (1 - frac_s)
    net_pnl = sum(float(t.get("pnl", 0.0) or 0.0) for t in (closed_trades or []))
    slip = slip_buy + slip_sell
    cost_drag = slip + fee_total
    avg_one_side = (buy_notional + sell_notional) / 2.0
    return {
        "net_pnl": round(net_pnl, 2),
        "gross_pnl": round(net_pnl + cost_drag, 2),      # 零成本口径（信号毛盈亏）
        "slippage": round(slip, 2),
        "explicit_fees": round(fee_total, 2),
        "cost_drag": round(cost_drag, 2),
        "cost_drag_pct": (cost_drag / initial_cash) if initial_cash else 0.0,
        "single_side_turnover": (avg_one_side / initial_cash) if initial_cash else 0.0,
        "roundtrip_cost_rate": (cost_drag / avg_one_side) if avg_one_side else 0.0,
        "buy_notional": round(buy_notional, 2),
        "sell_notional": round(sell_notional, 2),
        "n_buys": n_buys,
        "n_sells": n_sells,
        "n_round_trips": len({(t.get("opened_at"), t.get("symbol"))
                              for t in (closed_trades or [])}),
    }


def build_gap_attribution(cost: CostModel, closed_trades: list) -> list[dict]:
    """离场信号归因：每个离场信号 × 笔数 × 胜率 × 笔均持有收益 × 盈亏。

    ``avg_ret`` = 还原滑点后的 entry→exit 收益（GAPCUT 类信号即真实隔夜跳空，
    其余为持有至离场线的收益）。只统计已平仓（realized），按笔数降序输出，
    末行 ``ALL`` 为全部平仓合计。"""
    if not closed_trades:
        return []
    frac_b = _slip_frac(cost, Side.BUY)
    frac_s = _slip_frac(cost, Side.SELL)
    by_reason: dict[str, list] = {}
    for t in closed_trades:
        by_reason.setdefault(str(t.get("reason") or "?"), []).append(t)

    def _ret(t) -> float | None:
        entry = float(t.get("entry_price") or 0.0)
        exit_px = float(t.get("exit_price") or 0.0)
        if entry <= 0:
            return None
        entry_clean = entry / (1 + frac_b) if frac_b > 0 else entry
        exit_clean = exit_px / (1 - frac_s) if frac_s > 0 else exit_px
        return (exit_clean / entry_clean - 1.0) if entry_clean > 0 else None

    n_total = len(closed_trades)
    out: list[dict] = []
    for r, group in by_reason.items():
        pnls = [float(t.get("pnl", 0.0) or 0.0) for t in group]
        wins = sum(1 for p in pnls if p > 0)
        ret_w, notional_w = 0.0, 0.0
        for t in group:
            ret = _ret(t)
            if ret is None:
                continue
            shares = float(t.get("shares", 0) or 0)
            entry = float(t.get("entry_price") or 0.0)
            notional = (entry / (1 + frac_b) if frac_b > 0 else entry) * shares
            ret_w += ret * notional
            notional_w += notional
        out.append({
            "reason": r,
            "n": len(group),
            "win_rate": wins / len(group) if group else 0.0,
            "avg_ret": (ret_w / notional_w) if notional_w else 0.0,
            "avg_pnl": (sum(pnls) / len(pnls)) if pnls else 0.0,
            "total_pnl": sum(pnls),
            "pct_of_exits": len(group) / n_total if n_total else 0.0,
        })
    out.sort(key=lambda d: -d["n"])
    all_pnls = [float(t.get("pnl", 0.0) or 0.0) for t in closed_trades]
    wins = sum(1 for p in all_pnls if p > 0)
    ret_w, notional_w = 0.0, 0.0
    for t in closed_trades:
        ret = _ret(t)
        if ret is None:
            continue
        shares = float(t.get("shares", 0) or 0)
        entry = float(t.get("entry_price") or 0.0)
        notional = (entry / (1 + frac_b) if frac_b > 0 else entry) * shares
        ret_w += ret * notional
        notional_w += notional
    out.append({
        "reason": "ALL",
        "n": len(closed_trades),
        "win_rate": wins / len(all_pnls) if all_pnls else 0.0,
        "avg_ret": (ret_w / notional_w) if notional_w else 0.0,
        "avg_pnl": (sum(all_pnls) / len(all_pnls)) if all_pnls else 0.0,
        "total_pnl": sum(all_pnls),
        "pct_of_exits": 1.0,
    })
    return out


class TailPickBacktester:
    """分钟级精确撮合的一夜持股回测：T 日 14:30 买入，T+1 时间不对称离场（09:30~10:30）。

    复用 SimGateway + CostModel + PortfolioState（P7 同代码路径）。分钟线可用时按
    14:30-15:00 买入、次日开盘后卖出精确撮合；不可用（sim/mock）时降级为日线近似
    （T 收买入 / T+1 开或收卖出），结果明确标注【非真实业绩】。
    """

    def __init__(self, settings: Settings, hub: Any, *, initial_cash: float = 1_000_000.0,
                 config: TailPickConfig | None = None, require_minute: bool | None = None):
        self.settings = settings
        self.hub = hub
        self.initial_cash = initial_cash
        self.cfg = config or TailPickConfig.from_settings(settings)
        self.cost = CostModel.from_settings(settings)
        self.gateway = SimGateway()
        self.portfolio = PortfolioState(cash=initial_cash)
        self.screener = TailPickScreener(self.cfg)
        self.require_minute = require_minute
        self.universe: list[str] = []
        self.minute_available: bool = False
        self._day0: date | None = None
        self.opened_day: dict[str, date] = {}   # symbol → 开仓日（隔夜动量基准用）

    # ---------------------------------------------------------- 主循环
    def run(self, start: date, end: date) -> TailPickResult:
        days = self._trading_days(start, end)
        if len(days) < 2:
            return TailPickResult(details=["交易日不足"])
        self._day0 = days[0]
        self.universe = self._universe(days[0])
        # 分钟线可用性自动探测：真实源有 M5 → True；MockProvider 对 M5 退化返回日线 → False
        detected = self._detect_minute(days[0])
        if self.require_minute is not None:
            self.minute_available = self.require_minute and detected
        else:
            self.minute_available = detected
        if not self.minute_available and self.cfg.require_minute_bars:
            logger.warning("⚠ 无分钟线源（sim/mock 或源未实现 M5）：分钟依赖规则⑥b/⑧按 "
                           "best-effort 放行，本回测【非真实业绩】，仅验证机制。")

        # 预热（范围感知）：日线全 universe（已磁盘缓存，便宜）。
        # M5 不做全市场预热——粗筛后每天只对 top_n 候选拉分钟线。
        # V6/V7 需 MA(low_ma_days) → 预热窗口同步扩展
        warm_days = 14
        if self.cfg.defense_mode:
            warm_days = max(warm_days, self.cfg.low_ma_days * 2 + 15)
        # 指数预热：regime 的 MA(market_ma_days) 需窗口内足量指数日线，
        # 起始日前 warm 日历日不足时逐日现取会因历史不够 best-effort 误放行
        try:
            self.hub.get_index_bars(_INDEX_SYMBOL,
                                    days[0] - timedelta(days=warm_days + self.cfg.market_ma_days * 3),
                                    days[-1])
        except Exception as exc:  # noqa: BLE001
            logger.warning("指数预热失败（回测继续，逐日取数）: %s", exc)
        try:
            self.hub.get_bars(self.universe, Freq.D1, days[0] - timedelta(days=warm_days), days[-1],
                              Adjust.NONE, validate=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("日线预热失败（回测继续，逐日取数）: %s", exc)
        instr_map = {i.symbol: i for i in (self.hub.get_instruments(self.universe) or [])}

        executor = TailPickExecutor(self.cost, self.gateway, self.portfolio, self.cfg)
        result = TailPickResult(minute_available=self.minute_available)
        logger.info("尾盘选股回测启动 minute=%s max_pos=%d stop=%.1f%%",
                    self.minute_available, self.cfg.max_positions, self.cfg.overnight_stop_pct * 100)

        for d in days:
            # T+1 解锁隔夜仓（昨日买入今日可卖）
            self.portfolio.mark_t1(d)
            # —— 离场：卖出昨日 14:30 买入的仓（今日时间不对称离场）——
            self._exit_day(d, executor, result)
            # —— 选股 + 买入：今日 14:30 尾盘 ——
            self._enter_day(d, executor, instr_map, result)
            # —— 记录权益（今日收盘）——
            self._record_equity(d, result)

        result.closed_trades = list(self.portfolio.closed_trades)
        result.open_positions = [
            {"symbol": s, "avg_cost": round(p.avg_cost, 4),
             "last_price": round(p.last_price or p.avg_cost, 4), "shares": p.shares,
             "opened_at": p.opened_at.isoformat() if p.opened_at else None}
            for s, p in self.portfolio.positions.items()
        ]
        result.metrics = performance(result.equity_curve, trades=result.trades,
                                     realized_log=self.portfolio.realized_log)
        # P0 归因（2026-08-15）：成本拖累 / 换手 / 离场信号 GAP 分布 ——
        # 只统计已实现（realized），与 metrics 的 win_rate 同口径
        result.cost_attribution = build_cost_attribution(
            self.cost, result.trades, result.closed_trades, self.initial_cash)
        result.gap_attribution = build_gap_attribution(self.cost, result.closed_trades)
        return result

    # ---------------------------------------------------------- 每日动作
    @staticmethod
    def _parse_hhmm(s: str, default: time) -> time:
        try:
            h, m = str(s).split(":")[:2]
            return time(int(h), int(m))
        except Exception:  # noqa: BLE001
            return default

    def _day_avg_bar_vol(self, sym: str, d: date) -> float:
        """T 日平均每根 M5 bar 量（隔夜动量放量判定基准，取不到返回 0）。"""
        try:
            df = self.hub.get_bars(sym, Freq.M5, d, d, Adjust.NONE)
        except Exception:  # noqa: BLE001
            return 0.0
        if df is None or df.empty:
            return 0.0
        w = self._slice_window(df, d, time(9, 31), time(15, 0))
        if w.empty:
            return 0.0
        return float(w["volume"].astype(float).mean())

    def _exit_day(self, d: date, executor: TailPickExecutor, result: TailPickResult) -> None:
        """对昨日买入的仓，于今日时间不对称离场（2026-08-13 四段式革命）。

        以首根 09:35 bar open（近似集合竞价）vs T 日收盘分三支：
          高开 > gapup_threshold_pct：竞价卖半仓（TAIL_HALF_OPEN）；若隔夜动量成立
              （open>昨收 且首根 bar 量 ≥ vol_mult × T日bar均量）→ 剩余取消止盈，
              仅保留 VWAP 保底，持至 open_momentum_hold_until（TAIL_MOMENTUM_EXIT）；
              否则剩余走 ③保本/④止盈/⑤VWAP 链至 10:00。
          低开 < -gapdown_threshold_pct：竞价不卖，等 low_open_check_time(9:45)；
              期间最高曾翻红（> T日收盘）→ 转正常离场链；否则 9:45 砍仓（TAIL_LOWOPEN_CUT）。
          平开：②缺口保护（含5分钟缓冲）→ ③保本 → ④止盈 → ⑤VWAP → 窗口尾离场。
        日线降级路径（无分钟源）保留旧逻辑：开盘硬止损 / 收盘近似离场。
        bar 内多条件同时命中时按保守序（先止损后止盈）处理。
        """
        check_t = self._parse_hhmm(self.cfg.low_open_check_time, time(9, 45))
        mom_t = self._parse_hhmm(self.cfg.open_momentum_hold_until, time(10, 30))
        normal_end = self._parse_hhmm(self.cfg.exit_window_end, time(10, 0))
        for sym in list(self.portfolio.positions):
            pos = self.portfolio.positions[sym]
            if pos.shares <= 0:
                continue
            entry = pos.avg_cost
            win = None
            if self.minute_available:
                try:
                    df = self.hub.get_bars(sym, Freq.M5, d, d, Adjust.NONE)
                except Exception:  # noqa: BLE001
                    df = None
                if df is not None and not df.empty:
                    # 窗口扩至动量持有截止时点（非动量分支按 normal_end 提前离场）
                    win = self._slice_window(df, d, time(9, 31), max(mom_t, normal_end))
            if win is None or win.empty:
                # 日线降级（无分钟源）：保留旧逻辑——开盘硬止损 / 收盘近似离场
                exit_bar = self._exit_bar(sym, d)
                if exit_bar is None:
                    # 无法构造离场窗口 Bar（如 T+1 停牌）：持仓顺延，留痕供审计
                    result.details.append({"date": d.isoformat(), "action": "TAIL_EXIT_SKIPPED",
                                           "symbol": sym, "price": None})
                    logger.warning("尾盘离场跳过 %s %s：无离场窗口行情（顺延持有）", sym, d)
                    continue
                if exit_bar.open > 0 and exit_bar.open <= entry * (1 - self.cfg.overnight_stop_pct):
                    fill = executor.sell(sym, exit_bar, exit_bar.open, d, market=True,
                                         signal="TAIL_STOP")
                    sig = "TAIL_STOP"
                else:
                    fill = executor.sell(sym, exit_bar, exit_bar.close, d, market=False,
                                         signal="TAIL_EXIT")
                    sig = "TAIL_EXIT"
                if fill is not None:
                    result.trades.append(fill)
                    result.details.append({"date": d.isoformat(), "action": sig,
                                           "symbol": sym, "price": round(fill.price, 4)})
                continue

            # —— V6.0 低位防御离场链（替换 V5 时间不对称链）——
            if self.cfg.defense_mode:
                self._exit_day_v6(sym, d, executor, result, win, entry)
                continue

            # —— 分钟级时间不对称离场 ——
            first = win.iloc[0]
            open_px = float(first["open"])
            prev_close = pos.last_price if (pos.last_price or 0) > 0 else entry
            gap = open_px / prev_close - 1 if prev_close > 0 else 0.0

            # —— 高开分支：竞价（首根 bar open 近似）卖半仓锁定利润 ——
            half_qty = 0
            if open_px > 0 and gap > self.cfg.gapup_threshold_pct:
                half_qty = int((pos.can_use // 2) // 100 * 100)
                if half_qty >= 100:
                    hf = executor.sell(sym, self._mk_bar(sym, d, win.iloc[:1]), open_px, d,
                                       market=True, signal="TAIL_HALF_OPEN", qty=half_qty)
                    if hf is not None:
                        result.trades.append(hf)
                        result.details.append({"date": d.isoformat(), "action": "TAIL_HALF_OPEN",
                                               "symbol": sym, "price": round(hf.price, 4)})
                    # 半仓卖出后仓位已清零 → 剩余流程无需再走
                    if sym not in self.portfolio.positions:
                        continue

            # —— 隔夜动量：open>昨收 且首根 bar 放量 → 放弃止盈持有至 mom_t ——
            momentum = False
            if self.cfg.open_momentum_enabled and open_px > prev_close:
                tday = getattr(self, "opened_day", {}).get(sym)
                base_vol = self._day_avg_bar_vol(sym, tday) if tday else 0.0
                momentum = base_vol > 0 and float(first["volume"]) >= base_vol * self.cfg.open_momentum_vol_mult

            # —— 低开分支：B 方案（早止损）或 V5 原逻辑（等 check_t 翻红/砍仓）——
            recovered = False
            if open_px > 0 and gap < -self.cfg.gapdown_threshold_pct:
                if self.cfg.low_open_stop_mode:
                    # 开盘即挂 -hard_stop_pct 止损单：跳空穿透按开盘价成交，
                    # bar 内触及按止损价成交；未触及则 10:30 bar close 离场
                    lo_stop = entry * (1 - self.cfg.hard_stop_pct)
                    lo_win = win[pd.to_datetime(win["date"]).dt.time <= mom_t]
                    if lo_win.empty:
                        lo_win = win
                    lo_sig = lo_price = None
                    lo_market = False
                    if open_px <= lo_stop:
                        lo_sig, lo_price, lo_market = "TAIL_LOSTOP_GAP", open_px, True
                    else:
                        for _, b in lo_win.iterrows():
                            if float(b["low"]) <= lo_stop:
                                lo_sig, lo_price, lo_market = "TAIL_LOSTOP", lo_stop, True
                                break
                        if lo_sig is None:
                            lo_sig, lo_price = "TAIL_LOSTOP_EXIT", float(lo_win["close"].iloc[-1])
                    lo_fill = executor.sell(sym, self._mk_bar(sym, d, lo_win), lo_price, d,
                                            market=lo_market, signal=lo_sig)
                    if lo_fill is not None:
                        result.trades.append(lo_fill)
                        result.details.append({"date": d.isoformat(), "action": lo_sig,
                                               "symbol": sym, "price": round(lo_fill.price, 4)})
                    continue
                early = win[pd.to_datetime(win["date"]).dt.time <= check_t]
                recovered = not early.empty and float(early["high"].astype(float).max()) > prev_close
                if not recovered:
                    cut_rows = win[pd.to_datetime(win["date"]).dt.time <= check_t]
                    if not cut_rows.empty:
                        last_cut = cut_rows.iloc[-1]
                        fill = executor.sell(sym, self._mk_bar(sym, d, cut_rows),
                                             float(last_cut["close"]), d, market=False,
                                             signal="TAIL_LOWOPEN_CUT")
                        if fill is not None:
                            result.trades.append(fill)
                            result.details.append({"date": d.isoformat(), "action": "TAIL_LOWOPEN_CUT",
                                                   "symbol": sym, "price": round(fill.price, 4)})
                    continue

            # —— 剩余仓位：①硬止损 / ③保本 / ④止盈 链（V4.0；动量分支取消止盈、硬止损保底）——
            deadline = mom_t if momentum else normal_end
            loop_win = win[pd.to_datetime(win["date"]).dt.time <= deadline]
            if loop_win.empty:
                loop_win = win
            # 平开分支保留缺口保护：首根 bar 在成本 ±gap_buffer 内的反抽机会
            sig = price = None
            market_flag = False
            if (not momentum and half_qty == 0 and open_px > 0
                    and -self.cfg.gapdown_threshold_pct <= gap <= self.cfg.gapup_threshold_pct
                    and self.cfg.gap_protect_enabled and open_px <= prev_close):
                if self.cfg.gap_buffer_enabled and open_px >= entry * (1 - self.cfg.gap_buffer_pct):
                    if float(first["high"]) >= entry:
                        sig, price = "TAIL_GAPRECOV", entry
                    else:
                        # 限价挂首根 bar 收盘（回测口径等价 9:35 市价；该 bar high<entry
                        # 而 close≤high，限价必可成交）
                        sig, price = "TAIL_GAPWAIT", float(first["close"])
                else:
                    sig, price = "TAIL_GAPSTOP", open_px
            fill = None
            if sig is not None:
                fill = executor.sell(sym, self._mk_bar(sym, d, loop_win), price, d,
                                     market=market_flag, signal=sig)
            else:
                be_line = entry * (1 + self.cfg.breakeven_trigger_pct)
                tp_line = entry * (1 + self.cfg.take_profit_pct)
                stop_line = entry * (1 - self.cfg.hard_stop_pct)
                be_active = False
                cum_vol = cum_amt = 0.0
                for _, b in loop_win.iterrows():
                    hi, lo = float(b["high"]), float(b["low"])
                    cl = float(b["close"])
                    bt = pd.Timestamp(b["date"]).time()
                    # ③ 保本触发：开盘 5min 内（首根 09:35 bar）触及触发价
                    if not be_active and not momentum and bt <= time(9, 35) and hi >= be_line:
                        be_active = True
                    # bar 内保守序：先止损类，后止盈
                    # ① V4.0 硬止损：触及 -hard_stop_pct 立即市价离场（动量分支亦保底）
                    if lo <= stop_line:
                        sig, price = "TAIL_HARD_STOP", stop_line
                        market_flag = True
                        break
                    if be_active and lo <= entry:
                        sig, price = "TAIL_BREAKEVEN", entry
                        break
                    if not momentum and hi >= tp_line:
                        sig, price = "TAIL_TP", tp_line
                        break
                    cum_vol += float(b["volume"])
                    cum_amt += float(b.get("amount", 0.0) or 0.0)
                    # ⑤ VWAP 止损（V4.0 默认关闭让位硬止损；vwap_exit_enabled 可手动开回）
                    if self.cfg.vwap_exit_enabled and cum_vol > 0:
                        vwap_now = cum_amt / cum_vol
                        if cl < vwap_now:
                            sig, price = "TAIL_VWAP_EXIT", cl
                            break
                if sig is None:
                    # 截止时点（普通 10:00 / 动量 10:30）：无触发 → 按收盘离场
                    if momentum:
                        sig, price, market_flag = "TAIL_MOMENTUM_EXIT", float(loop_win["close"].iloc[-1]), True
                    else:
                        sig, price = "TAIL_EXIT", float(loop_win["close"].iloc[-1])
                fill = executor.sell(sym, self._mk_bar(sym, d, loop_win), price, d,
                                     market=(market_flag or sig in ("TAIL_VWAP_EXIT",)), signal=sig)
            if fill is not None:
                result.trades.append(fill)
                result.details.append({"date": d.isoformat(), "action": sig,
                                       "symbol": sym, "price": round(fill.price, 4)})

    def _exit_day_v6(self, sym: str, d: date, executor: TailPickExecutor,
                     result: TailPickResult, win: pd.DataFrame, entry: float) -> None:
        """V6.0 低位防御离场链（不对称止损）：
        ① 低开 < 成本×(1-v6_low_cut_pct) → 竞价全砍（TAIL_V6_GAPCUT）；
        ② 平开/高开：首根 09:35 bar 不卖，高点触及 成本×(1+v6_be_trigger_pct) 激活移动保本；
        ③ 保本激活后回落触及成本 → 全仓（TAIL_V6_BE）；
        ④ 触及 成本×(1+v6_take_profit_pct) → 卖 v6_tp_sell_ratio（70%）落袋（TAIL_V6_TP70），
           余仓自最高点回撤 v6_trail_pullback_pct 离场（TAIL_V6_TRAIL）；
        ⑤ 窗口截止（10:00）无触发 → 余仓市价离场（TAIL_V6_EXIT）。
        bar 内保守序：先止损类（TRAIL/BE）后止盈（TP70）。
        """
        first = win.iloc[0]
        open_px = float(first["open"])
        # ① 低开全砍（以成本价为基准，不赌反抽）
        if open_px > 0 and open_px < entry * (1 - self.cfg.v6_low_cut_pct):
            fill = executor.sell(sym, self._mk_bar(sym, d, win.iloc[:1]), open_px, d,
                                 market=True, signal="TAIL_V6_GAPCUT")
            if fill is not None:
                result.trades.append(fill)
                result.details.append({"date": d.isoformat(), "action": "TAIL_V6_GAPCUT",
                                       "symbol": sym, "price": round(fill.price, 4)})
            return
        be_line = entry * (1 + self.cfg.v6_be_trigger_pct)
        tp_line = entry * (1 + self.cfg.v6_take_profit_pct)
        normal_end = self._parse_hhmm(self.cfg.exit_window_end, time(10, 0))
        loop_win = win[pd.to_datetime(win["date"]).dt.time <= normal_end]
        if loop_win.empty:
            loop_win = win
        # ② 首根 bar（9:30~9:35）只观察不卖：冲高 +v6_be_trigger_pct 激活移动保本
        be_active = float(first["high"]) >= be_line
        peak = float(first["high"])
        tp_done = False
        sig = price = None
        market_flag = False
        for _, b in loop_win.iloc[1:].iterrows():
            hi, lo = float(b["high"]), float(b["low"])
            bt = pd.Timestamp(b["date"]).time()
            if not be_active and bt <= time(9, 35) and hi >= be_line:
                be_active = True
            peak = max(peak, hi)
            # 保守序：先止损类后止盈
            if tp_done and lo <= peak * (1 - self.cfg.v6_trail_pullback_pct):
                sig, price = "TAIL_V6_TRAIL", peak * (1 - self.cfg.v6_trail_pullback_pct)
                break
            if be_active and lo <= entry:
                sig, price = "TAIL_V6_BE", entry
                break
            if not tp_done and hi >= tp_line:
                pos = self.portfolio.positions.get(sym)
                can_use = pos.can_use if pos is not None else 0
                tp70_qty = int(can_use * self.cfg.v6_tp_sell_ratio // 100 * 100)
                if tp70_qty < 100:
                    tp70_qty = can_use
                peak = hi
                f70 = executor.sell(sym, self._mk_bar(sym, d, loop_win), tp_line, d,
                                    market=False, signal="TAIL_V6_TP70", qty=tp70_qty)
                if f70 is not None:
                    result.trades.append(f70)
                    result.details.append({"date": d.isoformat(), "action": "TAIL_V6_TP70",
                                           "symbol": sym, "price": round(f70.price, 4)})
                tp_done = True
                if sym not in self.portfolio.positions:
                    return
        if sig is None:
            # ⑤ 10:00 截止无触发 → 余仓市价离场
            sig, price = "TAIL_V6_EXIT", float(loop_win["close"].iloc[-1])
            market_flag = True
        fill = executor.sell(sym, self._mk_bar(sym, d, loop_win), price, d,
                             market=market_flag, signal=sig)
        if fill is not None:
            result.trades.append(fill)
            result.details.append({"date": d.isoformat(), "action": sig,
                                   "symbol": sym, "price": round(fill.price, 4)})

    def _enter_day(self, d: date, executor: TailPickExecutor, instr_map: dict,
                   result: TailPickResult) -> None:
        if len(self.portfolio.positions) >= self.cfg.max_positions:
            return
        picks = self.screener.screen(self.hub, d, self.universe, instr_map,
                                     self.minute_available, top_n=self.cfg.universe_top_n)
        picks = picks[: max(0, self.cfg.max_positions - len(self.portfolio.positions))]
        regime_ratio = getattr(self.screener, "regime_ratio", 1.0)
        for p in picks:
            # 大市温度计弹性仓位：半仓档只使用 50% 资金买入
            max_notional = self.portfolio.cash * self.cfg.position_fraction * regime_ratio
            # 板块效应：当日板块涨幅前 N 的行业权重加倍（以可用现金为上限，executor 内再封顶）
            if p.sector_boost:
                max_notional *= self.cfg.sector_boost_mult
            fill = executor.buy(p.symbol, p.entry_bar, p.entry_price, d, max_notional)
            if fill is not None:
                # 记录开仓日，供 T+1 隔夜动量取 T 日 bar 均量基准
                self.opened_day[p.symbol] = d
                result.trades.append(fill)
                result.details.append({"date": d.isoformat(), "action": "TAIL_BUY",
                                       "symbol": p.symbol, "price": round(fill.price, 4),
                                       "minute_verified": p.minute_verified,
                                       "sector_boost": p.sector_boost})

    def _record_equity(self, d: date, result: TailPickResult) -> None:
        last_prices: dict[str, float] = {}
        for sym in self.portfolio.positions:
            bar = self._daily_bar(sym, d)
            if bar is not None and bar.close > 0:
                last_prices[sym] = bar.close
        self.portfolio.refresh(last_prices)
        self.portfolio.record_equity(day_end=True)
        result.equity_curve.append(round(self.portfolio.total_asset, 2))

    # ---------------------------------------------------------- 撮合 Bar 构造
    def _exit_bar(self, sym: str, d: date) -> Bar | None:
        """构造 T+1 09:30~10:00 离场窗口的撮合 Bar（日线降级路径用）。"""
        if self.minute_available:
            try:
                df = self.hub.get_bars(sym, Freq.M5, d, d, Adjust.NONE)
            except Exception:  # noqa: BLE001
                df = None
            if df is not None and not df.empty:
                win = self._slice_window(df, d, time(9, 31), time(10, 0))
                if not win.empty:
                    return self._mk_bar(sym, d, win)
        # 日线降级：用当日 OHLC 近似（开盘=9:30，收盘=10:00 代理）
        bar = self._daily_bar(sym, d)
        if bar is None:
            return None
        return Bar(symbol=sym, date=d, open=bar.open, high=bar.high, low=bar.low,
                   close=bar.close, volume=bar.volume, amount=bar.amount)

    @staticmethod
    def _mk_bar(sym: str, d: date, win: pd.DataFrame) -> Bar:
        """由离场窗口分钟帧聚合出撮合 Bar（SimGateway 限价单校验用）。"""
        return Bar(
            symbol=sym, date=d, open=float(win["open"].iloc[0]),
            high=float(win["high"].max()), low=float(win["low"].min()),
            close=float(win["close"].iloc[-1]), volume=float(win["volume"].sum()),
        )

    def _daily_bar(self, sym: str, d: date) -> Bar | None:
        try:
            df = self.hub.get_bars(sym, Freq.D1, d, d, Adjust.NONE)
        except Exception:  # noqa: BLE001
            return None
        if df is None or df.empty:
            return None
        row = df.iloc[-1]
        return Bar(
            symbol=sym, date=d, open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]), volume=float(row["volume"]),
            amount=float(row.get("amount", 0.0) or 0.0),
            limit_up=row.get("limit_up"), limit_down=row.get("limit_down"),
        )

    @staticmethod
    def _slice_window(df: pd.DataFrame, day: date, t0: time, t1: time) -> pd.DataFrame:
        dt = pd.to_datetime(df["date"])
        mask = (dt.dt.date == day) & (dt.dt.time >= t0) & (dt.dt.time <= t1)
        return df[mask]

    # ---------------------------------------------------------- 基础
    def _trading_days(self, start: date, end: date) -> list[date]:
        idx = self.hub.get_index_bars(_INDEX_SYMBOL, start, end)
        if idx is None or idx.empty:
            return []
        return sorted(pd.to_datetime(idx["date"]).dt.date.unique().tolist())

    def _universe(self, day: date) -> list[str]:
        try:
            infos = self.hub.get_instruments()
        except Exception:  # noqa: BLE001
            return []
        if isinstance(infos, dict):
            return list(infos.keys())
        return [getattr(i, "symbol", str(i)) for i in (infos or [])]

    def _detect_minute(self, day: date) -> bool:
        # 采样最多 5 只（首只可能停牌/未收录分钟线），任一有效即认为分钟源可用
        sample = self.universe[:5] if self.universe else ["000001.SZ"]
        for sym in sample:
            try:
                df = self.hub.get_bars([sym], Freq.M5, day, day, Adjust.NONE)
            except Exception:  # noqa: BLE001
                continue
            if df is None or df.empty:
                continue
            sub = df[pd.to_datetime(df["date"]).dt.date == day] if "date" in df.columns else df
            if len(sub) >= 2:
                return True
        return False


# ============================================================================ 选股策略（调度器/实时用）
class TailPickStrategy:
    """供调度器/实时调用的薄封装：选股 → TradeIntent。

    与回测共用同一套 ``TailPickScreener``；执行可经现有 ``ExecutionService``（live）
    或 ``TailPickExecutor``（回测/模拟）。
    """

    def __init__(self, settings: Settings, hub: Any):
        self.cfg = TailPickConfig.from_settings(settings)
        self.hub = hub
        self.screener = TailPickScreener(self.cfg)

    def select(self, day: date, universe: list[str] | None = None,
               minute_available: bool = True) -> list[TailPickPick]:
        if universe is None:
            try:
                infos = self.hub.get_instruments()
                universe = list(infos.keys()) if isinstance(infos, dict) else \
                    [getattr(i, "symbol", str(i)) for i in (infos or [])]
            except Exception:  # noqa: BLE001
                universe = []
        instr_map = {i.symbol: i for i in (self.hub.get_instruments(universe) or [])}
        return self.screener.screen(self.hub, day, universe, instr_map, minute_available,
                                    top_n=self.cfg.universe_top_n)

    def to_intents(self, picks: list[TailPickPick], valid_until: date) -> list:
        from ..brain.schemas import TradeIntent
        out = []
        for p in picks:
            # 板块效应：命中当日板块涨幅前 N 的标的权重加倍（与回测 _enter_day 同口径）
            weight = self.cfg.position_fraction * self.screener.regime_ratio
            if p.sector_boost:
                weight *= self.cfg.sector_boost_mult
            out.append(TradeIntent(
                symbol=p.symbol, action="BUY", confidence=0.9, conviction="MEDIUM",
                entry_type="LIMIT", entry_ref_price=p.entry_price,
                stop_loss_type="FIXED_PCT", stop_loss_value=self.cfg.overnight_stop_pct,
                max_weight_hint=weight,
                max_holding_days=1,
                valid_until=valid_until,
                reasoning="尾盘选股法入选：" + "; ".join(p.reasons),
            ))
        return out


__all__ = ["TailPickConfig", "TailPickPick", "TailPickScreener",
           "TailPickExecutor", "TailPickBacktester", "TailPickResult", "TailPickStrategy",
           "build_cost_attribution", "build_gap_attribution"]
