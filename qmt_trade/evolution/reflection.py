"""盘后自我复盘与总结（设计 L5 → 闭环增强）。

这是用户明确要求的「每日盘后自我复盘」能力的落地：在 ``review`` 任务（16:00）之后
自动跑一遍，产出一个** introspective **的 Markdown 复盘，区别于 ``ops/report.py`` 那种
只罗列账户/成交的机械日报。本模块回答四个问题：

1. **反思选股** —— 精选标的理由是否牵强？多空辩论是否充分？选股逻辑近期是否真的有效？
2. **反思交易** —— 胜率/盈亏比健康吗？止损是否被噪声扫出？费用拖累是否过高？
3. **改进策略** —— 给出下一交易日可落地的具体动作（联动上一轮新增的 strategy 预设）。
4. **沉淀记忆** —— 形成「短期记忆」（明日待办）与「长期记忆」（跨日累积的持久原则）。

成本纪律（自进化架构师要求）：
- **默认纯规则引擎**，零 LLM 调用、零成本，无 API key 也能跑；
- LLM 仅为「叙述增强」，由配置 ``ops.reflection.llm_enabled`` 显式开启，
  且复用 ``LLMManager`` 的预算熔断 + 候选链降级；调用失败/解析失败一律回退规则引擎，
  绝不让复盘因 LLM 异常而缺失。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


def _pct(x: float | None, digits: int = 2) -> str:
    return "-" if x is None else f"{x * 100:+.{digits}f}%"


def _fmt(x: float | None, digits: int = 2) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


@dataclass
class MemoryItem:
    """长期记忆条目。跨日累积，按出现频次排序，去重后持久化。"""

    text: str
    tag: str = ""
    first_seen: str = ""
    last_seen: str = ""
    occurrences: int = 1

    def to_dict(self) -> dict:
        return {
            "text": self.text, "tag": self.tag,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "occurrences": self.occurrences,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryItem":
        return cls(
            text=str(d.get("text", "")),
            tag=str(d.get("tag", "")),
            first_seen=str(d.get("first_seen", "")),
            last_seen=str(d.get("last_seen", "")),
            occurrences=int(d.get("occurrences", 1) or 1),
        )


@dataclass
class ReflectionReport:
    """一份盘后自我复盘。所有字段都是给人（明早的你）看的。"""

    trade_date: date
    regime: str = ""
    overview: list[str] = field(default_factory=list)
    selection_reflection: list[str] = field(default_factory=list)
    trading_reflection: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    lessons_summary: list[str] = field(default_factory=list)
    short_term: list[str] = field(default_factory=list)
    long_term: list[MemoryItem] = field(default_factory=list)
    llm_used: bool = False
    prev_short_term: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        L: list[str] = []
        L.append(f"# 盘后自我复盘与总结 {self.trade_date}")
        L.append("")
        gen = "LLM 增强叙述" if self.llm_used else "规则引擎（零 LLM 成本）"
        L.append(f"> 生成方式：{gen} ｜ 市场状态：{self.regime or '-'}")
        L.append("")

        L.append("## 一、当日概览")
        L.append("")
        if self.overview:
            L += [f"- {x}" for x in self.overview]
        else:
            L.append("_无可用数据_")
        L.append("")

        L.append("## 二、选股反思")
        L.append("")
        if self.selection_reflection:
            L += [f"- {x}" for x in self.selection_reflection]
        else:
            L.append("_当日无最终精选，无法评估选股质量。_")
        L.append("")

        L.append("## 三、交易反思")
        L.append("")
        if self.trading_reflection:
            L += [f"- {x}" for x in self.trading_reflection]
        else:
            L.append("_当日无平仓/成交，交易执行层面无可复盘。_")
        L.append("")

        L.append("## 四、改进策略（下一步动作）")
        L.append("")
        if self.improvements:
            L += [f"- {x}" for x in self.improvements]
        else:
            L.append("_暂无明确改进项。_")
        L.append("")

        L.append("## 五、经验总结")
        L.append("")
        if self.lessons_summary:
            L += [f"- {x}" for x in self.lessons_summary]
        else:
            L.append("_暂无结构化经验。_")
        L.append("")

        L.append("## 六、短期记忆（明日待办）")
        L.append("")
        if self.short_term:
            L += [f"- [ ] {x}" for x in self.short_term]
        else:
            L.append("_无明确待办。_")
        if self.prev_short_term:
            L.append("")
            L.append(f"**📌 延续自昨日短期记忆（{len(self.prev_short_term)} 项，请确认是否已完成）：**")
            L += [f"- {x}" for x in self.prev_short_term]
        L.append("")

        L.append("## 七、长期记忆（持久原则）")
        L.append("")
        if self.long_term:
            for m in self.long_term:
                occ = f" · 出现 {m.occurrences} 次" if m.occurrences > 1 else ""
                tag = f"[{m.tag}] " if m.tag else ""
                L.append(f"- {tag}{m.text}{occ}")
        else:
            L.append("_尚未沉淀长期记忆。_")
        L.append("")

        L.append(f"_生成于 {date.today():%Y-%m-%d %H:%M:%S}_")
        return "\n".join(L)


#: Regime → 推荐 strategy 预设（联动 qmt_trade.core.strategies 的预设 id）。
_REGIME_STRATEGY: dict[str, str] = {
    "TREND_UP": "momentum_breakout",
    "TREND_DOWN": "low_vol_defensive",
    "RANGE": "value_quality",
    "VOLATILE": "moneyflow_resonance",
    "RISK_OFF": "low_vol_defensive",
}


class ReflectionEngine:
    """复盘反思引擎。纯函数式：输入结构化数据，输出 ReflectionReport。

    不持有任何运行时状态，方便单测与回放（P6）。
    """

    def __init__(self, settings=None):
        cfg = (settings.section("ops").get("reflection", {}) if settings is not None else {}) or {}
        self.llm_scene = str(cfg.get("llm_scene", "reflection"))
        self.llm_enabled = bool(cfg.get("llm_enabled", False))
        self.max_long = int(cfg.get("max_long_term", 40))

    # ============================================================ 主入口
    def run(self, trade_date: date, *, review_result=None,
            picks: list[dict] | None = None,
            trades: list[dict] | None = None,
            regime: str = "",
            factor_ic: dict[str, float] | None = None,
            selection_hit: dict[str, float] | None = None,
            recent_experiences: list[str] | None = None,
            short_term_prev: list[str] | None = None,
            long_term_prev: list[MemoryItem] | None = None,
            llm=None) -> ReflectionReport:
        picks = picks or []
        trades = trades or []
        factor_ic = factor_ic or {}
        recent_experiences = recent_experiences or []
        short_term_prev = short_term_prev or []
        long_term_prev = long_term_prev or []
        rev = review_result

        # 合并 ReviewResult 自带的因子 IC（review 任务算出的），避免只因外部未传而丢失。
        rev_fic = getattr(rev, "factor_ic", None) or {}
        factor_ic_eff = {**factor_ic, **rev_fic}

        overview = self._overview(trade_date, picks, trades, regime, selection_hit)
        selection_reflection = self._reflect_selection(
            picks, regime, selection_hit, factor_ic_eff, rev)
        trading_reflection = self._reflect_trading(rev, trades)
        improvements = self._improvements(rev, regime, factor_ic_eff, selection_hit, picks)
        lessons_summary = self._lessons_summary(rev, recent_experiences)

        # 短期记忆：下一交易日可落地的动作（不含延续项，延续项单独标注）
        short_term = self._extract_short_term(
            improvements, rev, regime, selection_hit)
        # 长期记忆：候选原则合并进历史累积
        long_candidates = self._long_term_candidates(
            rev, recent_experiences, factor_ic_eff, regime, selection_hit)
        long_term = self._merge_long_term(long_candidates, long_term_prev, trade_date)

        rep = ReflectionReport(
            trade_date=trade_date, regime=regime,
            overview=overview, selection_reflection=selection_reflection,
            trading_reflection=trading_reflection, improvements=improvements,
            lessons_summary=lessons_summary, short_term=short_term,
            long_term=long_term, prev_short_term=short_term_prev,
        )

        # 可选的 LLM 叙述增强：规则引擎已给出完整复盘，LLM 仅做润色/补充。
        if llm is not None:
            try:
                self._llm_enrich(rep, llm)
            except Exception as exc:                       # noqa: BLE001
                logger.warning("复盘 LLM 增强失败，回退规则引擎: %s", exc)
        return rep

    # ============================================================ 概览
    def _overview(self, d, picks, trades, regime, hit) -> list[str]:
        out: list[str] = []
        out.append(f"市场状态：**{regime or '未知'}**")
        out.append(f"当日最终精选：**{len(picks)}** 只"
                   + (f"（{', '.join(p.get('symbol','') for p in picks[:5])}…）"
                      if picks else ""))
        n_trades = len([t for t in trades
                        if t.get("realized_pnl") is not None or t.get("side")])
        out.append(f"当日成交：**{n_trades}** 笔")
        if hit and hit.get("eval_days"):
            hr = hit["hit_days"] / hit["eval_days"] if hit["eval_days"] else 0
            out.append(f"近阶段选股命中率：{hr:.0%}（评估 {int(hit['eval_days'])} 期，"
                       f"Top-K 前向 {_pct(hit.get('top_avg'))} vs 截面 {_pct(hit.get('all_avg'))}）")
        else:
            out.append("选股命中率：尚无足够前向样本（需 N 日候选+后向收益）")
        return out

    # ============================================================ 选股反思
    def _reflect_selection(self, picks, regime, hit, factor_ic, rev) -> list[str]:
        out: list[str] = []
        if not picks:
            out.append("当日无最终精选（候选池为空或研判未跑），无法评估选股质量；"
                       "次日应确认 selection/research 任务是否正常产出。")
            return out

        # 1) 多空辩论充分性 + 牵强票检测
        weak = []
        debated = 0
        for p in picks:
            bull = (p.get("bull_case") or "").strip()
            bear = (p.get("bear_case") or "").strip()
            ev = p.get("evidence")
            try:
                ev_list = json.loads(ev) if isinstance(ev, str) else (ev or [])
            except Exception:
                ev_list = []
            if bull and bear:
                debated += 1
            # 牵强判定：低置信但仍入选，或多方论据缺失，或证据全为中性/空
            conf = float(p.get("confidence") or 0)
            ev_verdicts = [e.get("verdict") for e in (ev_list or []) if isinstance(e, dict)]
            strong_ev = [v for v in ev_verdicts if v in ("bull", "bear")]
            if (not bull) or conf < 0.6 or (ev_list and not strong_ev):
                weak.append(p.get("symbol", "?"))
        out.append(f"多空辩论覆盖：{debated}/{len(picks)} 只精选具备明确多空双方论据；"
                   + (f"其中 **{len(weak)} 只理由偏牵强**（{', '.join(weak)}），"
                      "建议关注其多方论据与证据强度，必要时移出精选。"
                      if weak else "辩论较充分。"))

        # 2) 与近期选股命中率对照
        if hit and hit.get("eval_days"):
            hr = hit["hit_days"] / hit["eval_days"] if hit["eval_days"] else 0
            if hr < 0.5:
                out.append(f"选股命中率仅 **{hr:.0%}**，低于随机基准；"
                           "选股逻辑可能阶段性失效，需 walk-forward 回测复核因子权重，"
                           "而非继续机械沿用。")
            else:
                out.append(f"选股命中率 **{hr:.0%}** 高于随机基准，选股逻辑整体有效，"
                           "维持但持续观察。")
        else:
            out.append("尚无足够前向样本判断选股有效性，先按『证据强度』人工把关精选质量。")

        # 3) 因子 IC 异常点名
        inverted = [(k, v) for k, v in factor_ic.items() if v <= -0.15]
        if inverted:
            names = "、".join(f"{k}(IC={v:+.3f})" for k, v in inverted[:5])
            out.append(f"因子 IC 显著为负：**{names}**，方向可能反了，"
                       "应在下轮寻优中降权或反向验证。")

        # 4) Regime 契合度
        if regime in ("RISK_OFF",) and any(str(p.get("action")) == "BUY" for p in picks):
            out.append("当前为 **RISK_OFF**，仍产出 BUY 精选，需复核风险闸门是否生效；"
                       "避险市下应偏向低波/防御型标的。")
        if regime == "RANGE":
            out.append("震荡市下动量因子易失效，精选若偏动量型需警惕『追涨杀跌』，"
                       "建议向价值/质量倾斜。")
        return out

    # ============================================================ 交易反思
    def _reflect_trading(self, rev, trades) -> list[str]:
        out: list[str] = []
        atts = getattr(rev, "attributions", None) or []
        stats = getattr(rev, "stats", None) or {}
        if not atts:
            opened = [t for t in trades if str(t.get("side")) in ("BUY", "ADD")]
            if opened:
                out.append(f"当日新开 **{len(opened)}** 笔，尚无平仓，"
                           "持仓至次日观察；请确认止损/止盈已按计划在持仓上生效。")
            else:
                out.append("当日无成交，交易执行层面无可复盘；"
                           "关注现有持仓的止损/止盈设置是否合理。")
            return out

        wr = stats.get("win_rate")
        pf = stats.get("profit_factor")
        cd = stats.get("cost_drag")
        out.append(f"平仓 **{stats.get('n', len(atts))}** 笔：胜率 **{_pct(wr, 1)}**"
                   f"，盈亏比 **{_fmt(pf)}**"
                   + (f"，费用拖累 **{_pct(cd, 3)}**" if cd is not None else ""))
        if wr is not None and wr < 0.4:
            out.append(f"胜率仅 **{_pct(wr, 1)}** 偏低，优先排查『看对做错』——"
                       "精选/计划与执行是否一致、入场时机是否被竞价跳空干扰。")
        if pf is not None and pf < 1.0:
            out.append("盈亏比 < 1，盈利单被过早了结（截断利润、放大亏损），"
                       "应引入移动止盈让利润奔跑。")
        if cd is not None and cd > 0.006:
            out.append(f"费用拖累 **{_pct(cd, 3)}** 偏高，换手过频；"
                       "延长持有期或提高入选门槛以降本。")

        # 止损被噪声扫出
        stops = [a for a in atts if getattr(a, "reason", "") == "STOP_LOSS"]
        if atts and len(stops) / len(atts) > 0.45:
            out.append(f"止损触发占比 **{len(stops)/len(atts):.0%}**，可能被日内噪声扫出，"
                       "止损过紧，应提高 ATR 倍数或改用结构位止损。")

        # 计划外交易
        pick_syms = set()
        try:
            pick_syms = {p.get("symbol") for p in (getattr(rev, "_picks", None) or [])}
        except Exception:
            pick_syms = set()
        if trades:
            planned = {t.get("symbol") for t in trades if str(t.get("side")) in ("BUY", "ADD")}
            unplanned = planned - pick_syms
            if unplanned and pick_syms:
                out.append(f"存在 **{len(unplanned)}** 笔计划外交易（{', '.join(unplanned)}），"
                           "不在当日精选内，需核对计划执行链路是否漏单/错单。")
        return out

    # ============================================================ 改进策略
    def _improvements(self, rev, regime, factor_ic, hit, picks) -> list[str]:
        out: list[str] = []
        lessons = getattr(rev, "lessons", None) or []
        for les in lessons:
            sug = getattr(les, "suggestion", "") or getattr(les, "message", "")
            tag = getattr(les, "tag", "")
            if sug:
                out.append(f"[{tag}] {sug}")

        # Regime → 策略切换（联动 strategy 预设）
        rec = _REGIME_STRATEGY.get(regime)
        if rec:
            from ..core.strategies import get_strategy_profile
            try:
                prof = get_strategy_profile(rec)
                out.append(f"当前 {regime} 市况，建议切换至「{prof.get('name')}」策略"
                           f"（{prof.get('best_for','')}），并在 selection 任务显式传 strategy={rec}。")
            except Exception:
                out.append(f"当前 {regime} 市况，建议切换至 `{rec}` 策略。")

        # 因子 IC 倒置 → 降权
        inverted = [(k, v) for k, v in factor_ic.items() if v <= -0.15]
        for k, v in inverted[:5]:
            out.append(f"对因子 `{k}`（IC={v:+.3f}）降权或反向验证。")

        # 选股命中率低 → 回测复核
        if hit and hit.get("eval_days") and hit["hit_days"] / hit["eval_days"] < 0.5:
            out.append("选股有效性不足，启动 walk-forward 回测复核因子权重与入选门槛，"
                       "必要时整体下调 min_percentile。")
        return out

    # ============================================================ 经验总结
    def _lessons_summary(self, rev, recent_experiences) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for les in (getattr(rev, "lessons", None) or []):
            txt = getattr(les, "render", None)
            txt = txt() if callable(txt) else str(les)
            if txt and txt not in seen:
                seen.add(txt)
                out.append(txt)
        for e in recent_experiences:
            if e and e not in seen:
                seen.add(e)
                out.append(f"（近期）{e}")
        return out

    # ============================================================ 短期记忆
    def _extract_short_term(self, improvements, rev, regime, hit) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        # 改进项中带明确『动作』语义的转为明日待办（去重，截断到可执行数量）
        for imp in improvements:
            if imp in seen:
                continue
            seen.add(imp)
            out.append(imp)
        # Regime 切换作为独立待办强调
        rec = _REGIME_STRATEGY.get(regime)
        if rec and regime in ("RANGE", "RISK_OFF", "VOLATILE", "TREND_DOWN"):
            from ..core.strategies import get_strategy_profile
            try:
                prof = get_strategy_profile(rec)
                item = f"明日若维持 {regime}，切换至「{prof.get('name')}」策略并复核仓位上限。"
            except Exception:
                item = f"明日若维持 {regime}，切换至 `{rec}` 策略。"
            if item not in out:
                out.append(item)
        if hit and hit.get("eval_days") and hit["hit_days"] / hit["eval_days"] < 0.5:
            item = "明日对精选池加强人工复核，降低对单一因子/赛道的暴露。"
            if item not in out:
                out.append(item)
        return out[:12]

    # ============================================================ 长期记忆
    def _long_term_candidates(self, rev, recent_experiences, factor_ic, regime, hit) -> list[tuple[str, str]]:
        cands: list[tuple[str, str]] = []
        for les in (getattr(rev, "lessons", None) or []):
            sev = str(getattr(les, "severity", ""))
            if sev not in ("WARN", "CRITICAL"):
                continue
            txt = getattr(les, "suggestion", "") or getattr(les, "message", "")
            tag = getattr(les, "tag", "")
            if txt:
                cands.append((txt, tag or "lesson"))
        for e in recent_experiences:
            if e:
                cands.append((e, "recent"))
        for k, v in factor_ic.items():
            if v <= -0.15:
                cands.append((f"因子 `{k}` 方向可能反了，长期应降权或反向验证。", "factor_ic"))
        if regime in ("RANGE", "RISK_OFF"):
            cands.append(("震荡/避险市下动量因子易失效，宜偏向价值/质量/低波类策略。",
                          "regime"))
        if hit and hit.get("eval_days") and hit["hit_days"] / hit["eval_days"] < 0.5:
            cands.append(("选股逻辑阶段性失效时需 walk-forward 回测复核，而非机械沿用。",
                          "selection"))
        return cands

    def _merge_long_term(self, candidates, prev: list[MemoryItem], d: date) -> list[MemoryItem]:
        ds = d.isoformat()
        idx: dict[str, MemoryItem] = {}
        for m in prev:
            idx[m.text] = m
        for text, tag in candidates:
            text = text.strip()
            if not text:
                continue
            if text in idx:
                m = idx[text]
                m.occurrences += 1
                m.last_seen = ds
                if tag and not m.tag:
                    m.tag = tag
            else:
                idx[text] = MemoryItem(text=text, tag=tag, first_seen=ds, last_seen=ds,
                                       occurrences=1)
        merged = list(idx.values())
        merged.sort(key=lambda m: (-m.occurrences, m.last_seen))
        return merged[: self.max_long]

    # ============================================================ LLM 增强
    def _llm_enrich(self, rep: ReflectionReport, llm) -> None:
        """用 LLM 润色叙述。规则引擎结果已完整，这里只做『增强』，
        不依赖其成功；任何异常由调用方捕获回退。"""
        import json as _json

        facts = {
            "trade_date": str(rep.trade_date),
            "regime": rep.regime,
            "overview": rep.overview,
            "selection_reflection": rep.selection_reflection,
            "trading_reflection": rep.trading_reflection,
            "improvements": rep.improvements,
            "lessons_summary": rep.lessons_summary,
            "short_term": rep.short_term,
            "long_term": [m.to_dict() for m in rep.long_term],
        }
        prompt = (
            "你是 A 股量化系统的盘后复盘助手。下面是一份由规则引擎生成的自我复盘"
            "（JSON）。请在不改变事实与结论的前提下，把 selection_reflection / "
            "trading_reflection / improvements / lessons_summary 四个字段改写成更连贯、"
            "更具洞察的中文叙述（每条仍为独立 bullet，不要合并或编造新事实）。\n"
            "只输出 JSON，结构为："
            '{"selection_reflection":[...],"trading_reflection":[...],'
            '"improvements":[...],"lessons_summary":[...]}。\n'
            f"输入：\n{_json.dumps(facts, ensure_ascii=False, indent=2)}"
        )
        resp = llm.complete(prompt, scene=self.llm_scene, temperature=0.3, tag="reflection")
        data = _json.loads(resp.text)
        if isinstance(data.get("selection_reflection"), list) and data["selection_reflection"]:
            rep.selection_reflection = [str(x) for x in data["selection_reflection"]]
        if isinstance(data.get("trading_reflection"), list) and data["trading_reflection"]:
            rep.trading_reflection = [str(x) for x in data["trading_reflection"]]
        if isinstance(data.get("improvements"), list) and data["improvements"]:
            rep.improvements = [str(x) for x in data["improvements"]]
        if isinstance(data.get("lessons_summary"), list) and data["lessons_summary"]:
            rep.lessons_summary = [str(x) for x in data["lessons_summary"]]
        rep.llm_used = True


def load_long_term(raw: str | None) -> list[MemoryItem]:
    """从 system_state 的 JSON 还原长期记忆。"""
    if not raw:
        return []
    try:
        return [MemoryItem.from_dict(d) for d in json.loads(raw) if isinstance(d, dict)]
    except Exception:
        return []


def dump_long_term(items: list[MemoryItem]) -> str:
    return json.dumps([m.to_dict() for m in items], ensure_ascii=False)


__all__ = ["ReflectionEngine", "ReflectionReport", "MemoryItem",
           "load_long_term", "dump_long_term"]
