"""策略推荐（选股漏斗）对外接口。

- ``GET /selection/picks``：读取最近一次选股结果（盘前 selection 任务 / 手动「重新选股」生成），
  含候选池与 Top N 推荐明细（rank/score/industry）。没有则返回空 + 说明。
- ``GET /selection/final``：多 Agent 投票后的最终精选（3~5 只，含选中理由与投票），
  来自 research 任务落库的 daily_picks。
- ``POST /selection/run``：异步触发一次选股（复用与调度器完全相同的 SelectionPipeline），
  结果写回 ``selection:latest``。重量操作（全市场需 ~250 天历史，会实时拉行情），前端轮询 job。
- ``POST /selection/research``：异步触发多 Agent 深度研判（优中选优 3~5 只），
  从 ``selection:latest`` 重建候选池，不受非交易日限制，前端轮询 job。
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Body, Query

import server.context as ctx
from qmt_trade.core.strategies import list_strategy_profiles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/selection", tags=["selection"])

DEFAULT_TOP_N = 50

# universe 规模低于该阈值视为"小样本测试运行"，不覆盖已有的全市场结果。
# 血泪教训：bench 脚本用 syms[:300] 跑选股并落库，恰好 tushare stock_basic
# 按代码升序、前 300 只全是 000.SZ，导致页面候选池只剩 00 开头的票。
MIN_UNIVERSE_FOR_PERSIST = 1000


def _ctx(mode: str = Query("paper")):
    # 选股/研判属研究类操作，live 观察期锁定时自动降级 paper（不拦用户）
    return ctx.make_ctx_research(mode)


def _picks_from_candidateset(cs) -> list[dict]:
    """从 CandidateSet 抽取 Top N 推荐明细。"""
    out: list[dict] = []
    frame = getattr(getattr(cs, "ranking", None), "frame", None)
    if frame is None or getattr(frame, "empty", True):
        return out
    import math

    for _, row in frame.head(DEFAULT_TOP_N).iterrows():
        score = row.get("score", 0) or 0
        try:
            score = round(float(score), 4)
        except (TypeError, ValueError):
            score = 0.0
        out.append({
            "symbol": row.get("symbol"),
            "rank": int(row.get("rank", 0) or 0),
            "score": score if math.isfinite(score) else 0.0,
            "industry": row.get("industry", "") or "",
        })
    return out


def _load_latest(mode: str) -> dict | None:
    c = _ctx(mode)
    try:
        raw = c.shared_repos.system.get("selection:latest")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _frame_records(frame) -> list[dict]:
    """DataFrame → JSON 友好 records（NaN→null，日期→ISO 字符串）。

    把完整候选明细（含因子原值）一并落库，事后手动研判就能直接
    重建 CandidateSet，不必重算全市场因子。
    """
    if frame is None or getattr(frame, "empty", True):
        return []
    import pandas as pd
    df = frame.copy()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].astype(str)
    return json.loads(df.to_json(orient="records", force_ascii=False,
                                 date_format="iso", default_handler=str))


@router.get("/picks")
def get_picks(mode: str = Query("paper")):
    """最近一次选股结果。空时给前端明确的 CTA 文案。"""
    data = _load_latest(mode)
    if not data:
        return {
            "asof": None, "regime": None, "n": 0, "symbols": [],
            "picks": [], "degraded": [],
            "note": "尚未运行选股。点击「重新选股」触发（盘前 selection 任务也会自动生成）。",
        }
    regime = data.get("regime")
    if isinstance(regime, dict):
        regime = regime.get("regime")
    screen = data.get("screen") or {}
    ranking = data.get("ranking") or {}
    return {
        "asof": data.get("asof"),
        "regime": regime,
        "n": data.get("n", len(data.get("picks", []))),
        "symbols": data.get("symbols", []),
        "picks": data.get("picks", []),
        "degraded": data.get("degraded", []),
        "strategy": data.get("strategy"),
        # 漏斗阶梯展示所需数据：L0 硬过滤每一级的 before/after/removed
        "funnel": screen.get("stats") if isinstance(screen, dict) else None,
        "n_universe": data.get("n_universe"),
        "ranking": {
            "n": ranking.get("n") if isinstance(ranking, dict) else None,
            "below_percentile": ranking.get("below_percentile") if isinstance(ranking, dict) else None,
            "below_score": ranking.get("below_score") if isinstance(ranking, dict) else None,
            "crowded_out": ranking.get("crowded_out") if isinstance(ranking, dict) else None,
            "industry_dist": ranking.get("industry_dist") if isinstance(ranking, dict) else None,
        },
        "note": None,
    }


@router.get("/final")
def get_final_picks(mode: str = Query("paper"), date_s: str | None = Query(None, alias="date")):
    """最终精选（3~5 只高胜率标的，含选中理由与多 Agent 投票）。

    date 缺省返回最近一次 research 产出；指定日期返回历史，供滚动跟踪。
    """
    c = _ctx(mode)
    try:
        if date_s:
            rows = c.shared_repos.picks.list_by_date(date_s)
            asof = date_s
        else:
            asof = c.shared_repos.picks.latest_date()
            rows = c.shared_repos.picks.list_by_date(asof) if asof else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("精选读取失败: %s", exc)
        return {"asof": None, "n": 0, "picks": [],
                "note": "精选读取失败，请稍后重试。"}
    if not rows:
        return {
            "asof": None, "n": 0, "picks": [],
            "note": "尚无最终精选。盘前 research 任务（或先跑选股+研判）后自动生成。",
        }
    picks = []
    for r in rows:
        votes = {}
        try:
            votes = json.loads(r.get("votes") or "{}")
        except Exception:  # noqa: BLE001
            pass
        debate, evidence = [], []
        try:
            debate = json.loads(r.get("debate") or "[]")
        except Exception:  # noqa: BLE001
            pass
        try:
            evidence = json.loads(r.get("evidence") or "[]")
        except Exception:  # noqa: BLE001
            pass
        picks.append({
            "symbol": r.get("symbol"),
            "rank": int(r.get("rank") or 0),
            "action": r.get("action"),
            "conviction": r.get("conviction"),
            "confidence": r.get("confidence"),
            "industry": r.get("industry") or "",
            "reason": r.get("reason") or "",
            "votes": votes,
            "bull_case": r.get("bull_case") or "",
            "bear_case": r.get("bear_case") or "",
            "debate": debate,
            "evidence": evidence,
        })
    return {"asof": asof, "n": len(picks), "picks": picks, "note": None}


@router.post("/run")
def run_selection(body: dict = Body(default={}), mode: str = Query("paper")):
    """异步跑一次选股，结果（含 picks）写回 selection:latest。

    body 可选字段：
      universe: list[str]  候选子集；缺省=全市场（重）
      top_n: int           返回的推荐明细条数（默认 50）
      asof: str            YYYY-MM-DD，默认上一交易日截面
      strategy: str        策略预设 id（见 GET /selection/strategies）；缺省=默认均衡多因子
    """
    top_n = int(body.get("top_n", DEFAULT_TOP_N) or DEFAULT_TOP_N)
    universe = body.get("universe")
    asof_s = body.get("asof")
    strategy = body.get("strategy") or None

    job = ctx.new_job("selection_ui")

    def _run():
        c = ctx.make_ctx_research(mode)
        if asof_s:
            asof = date.fromisoformat(asof_s)
        else:
            asof = date.today() - timedelta(days=1)
        cs = c.pipeline.run(asof, universe=universe, strategy=strategy)
        picks = _picks_from_candidateset(cs)
        payload = cs.to_dict()
        payload["picks"] = picks[:top_n]
        try:
            payload["frame"] = _frame_records(getattr(cs, "frame", None))
        except Exception as exc:  # noqa: BLE001
            logger.warning("候选明细序列化失败（研判重建时将降级重算）: %s", exc)
        n_universe = int((cs.screen.to_dict() or {}).get("n_in") or 0) if cs.screen else 0
        payload["n_universe"] = n_universe
        payload["strategy"] = cs.strategy
        persisted = False
        if universe is not None and n_universe < MIN_UNIVERSE_FOR_PERSIST:
            # 小样本运行不落库，避免覆盖盘前全市场选股结果
            logger.warning(
                "小样本选股（universe=%d）跳过落库，避免覆盖全市场结果", n_universe)
        else:
            try:
                c.shared_repos.system.set(
                    "selection:latest",
                    json.dumps(payload, ensure_ascii=False, default=str),
                    reason=f"{cs.n} 只候选",
                )
                persisted = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("候选池落库失败: %s", exc)
        return {"n": cs.n, "picks": len(payload["picks"]),
                "n_universe": n_universe, "persisted": persisted,
                "regime": cs.regime.regime.value, "strategy": cs.strategy}

    ctx.spawn(job, _run)
    return {"job_id": job.id, "kind": "selection_ui"}


# ============================================================ 手动 AI 研判

def _rebuild_frame(c, payload: dict, day: date, regime, symbols: list[str]):
    """从落库 payload 重建研判用 frame。

    新落库结果自带完整候选明细（含因子原值）直接用；旧结果只有
    picks 汇总，则对 shortlist 重算因子原值补齐（数据缓存是热的，代价不大）。
    注意 score/rank/_pct 是全市场截面值，**必须用落库值**，不能拿小样本重算值冒充。
    """
    import pandas as pd
    from qmt_trade.datahub.pit import as_of_pre_open

    recs = payload.get("frame")
    if recs:
        df = pd.DataFrame(recs)
        if not df.empty and "symbol" in df.columns:
            return df

    short = symbols[: int(getattr(c.brain, "max_intents", 10) * 3)]
    picks = pd.DataFrame(payload.get("picks") or [])
    if picks.empty or not short:
        return picks
    try:
        feats = c.pipeline.engine.compute(short, as_of_pre_open(day), regime=regime)
        df = feats.frame if feats.frame is not None else pd.DataFrame()
    except Exception as exc:  # noqa: BLE001
        logger.warning("研判重建因子重算失败，降级为仅 picks 字段: %s", exc)
        df = pd.DataFrame()
    if df.empty:
        return picks
    # 子样本重算的截面类列会误导研判，一律丢弃（FactPack 缺失字段会显式标注）
    drop = [k for k in df.columns
            if k.startswith("cat_") or k in ("score", "rank", "_pct", "raw_rank")]
    keep = [k for k in df.columns if k == "symbol" or k not in picks.columns]
    df = df[[k for k in keep if k not in drop]]
    df = df.merge(picks[["symbol", "rank", "score", "industry"]],
                  on="symbol", how="inner")
    n_cand = max(len(symbols), 1)
    df["_pct"] = 1 - (df["rank"] - 1) / n_cand
    order = {s: i for i, s in enumerate(short)}
    df["_ord"] = df["symbol"].map(order)
    return df.sort_values(by="_ord").drop(columns="_ord").reset_index(drop=True)


def _rebuild_candidateset(c, payload: dict):
    """从 selection:latest 落库 payload 重建 CandidateSet（brain.run 的输入）。"""
    from qmt_trade.features.regime import Regime, RegimeSnapshot
    from qmt_trade.selection.pipeline import CandidateSet

    day = date.fromisoformat(str(payload.get("asof") or date.today())[:10])
    rd = payload.get("regime") or {}
    if not isinstance(rd, dict):
        rd = {}
    try:
        regime_enum = Regime(rd.get("regime", "RANGE"))
    except ValueError:
        regime_enum = Regime.RANGE
    regime = RegimeSnapshot(
        asof=day, regime=regime_enum,
        max_position=float(rd.get("max_position", 0.5)),
        min_score=float(rd.get("min_score", 0.0)),
        min_percentile=float(rd.get("min_percentile", 0.0)),
        scores=rd.get("scores") or {}, metrics=rd.get("metrics") or {},
        reason=rd.get("reason") or "", degraded=bool(rd.get("degraded", False)),
    )
    symbols = list(payload.get("symbols") or [])
    frame = _rebuild_frame(c, payload, day, regime, symbols)
    return CandidateSet(asof=day, regime=regime, symbols=symbols, frame=frame,
                        degraded=list(payload.get("degraded") or []),
                        strategy=payload.get("strategy"))


@router.post("/research")
def run_research(body: dict = Body(default={}), mode: str = Query("paper")):
    """异步触发多 Agent 深度研判：从落库候选池重建 CandidateSet，
    复用 JobRunner 的研判/精选落库/观察池逻辑，产出 3~5 只最终精选。

    与调度版 research 的区别：候选池来自数据库而非内存缓存，
    且不受非交易日限制（手动触发就是明确意图，不该被静默跳过）。

    body 可选字段：
      force: bool  当日已对相同候选池研判过则默认跳过（防重复烧钱），
                   force=true 强制重跑。
    """
    job = ctx.new_job("research_ui")

    def _run():
        c = ctx.make_ctx_research(mode)
        try:
            raw = c.shared_repos.system.get("selection:latest")
        except Exception:  # noqa: BLE001
            raw = None
        if not raw:
            raise RuntimeError("候选池尚未生成，请先点「一键选股」再运行 AI 研判")
        payload = json.loads(raw)
        if not payload.get("symbols"):
            raise RuntimeError("候选池为空（可能 RISK_OFF 空仓），无标的可研判")
        cs = _rebuild_candidateset(c, payload)

        from qmt_trade.scheduler.jobs import JobRunner
        runner = JobRunner(c, trade_date=cs.asof)
        runner.cache["candidates"] = cs
        runner.cache["regime"] = cs.regime
        res = runner.research_candidates(cs, force=bool(body.get("force", False)))
        if res.skipped:
            return {"skipped": True, "reason": res.reason}
        data = dict(res.data or {})
        data["skipped"] = False
        return data

    ctx.spawn(job, _run)
    return {"job_id": job.id, "kind": "research_ui"}


# ============================================================ 策略预设清单

@router.get("/strategies")
def get_strategies(mode: str = Query("paper")):
    """返回全部策略预设（含各 Regime 的因子权重、阈值与适用场景）。

    前端「策略」下拉框据此渲染；缺省策略为 balanced（原均衡多因子）。
    """
    _ctx(mode)  # 仅用于校验 mode 合法 & 触发建表
    return {"strategies": list_strategy_profiles()}


# ============================================================ 重点研究清单（用户自定义）

WATCH_KEY = "selection:watchlist"


def _watchlist_read(c) -> list[str]:
    try:
        raw = c.shared_repos.system.get(WATCH_KEY)
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    return [str(s) for s in items] if isinstance(items, list) else []


def _watchlist_write(c, symbols: list[str]) -> None:
    try:
        c.shared_repos.system.set(
            WATCH_KEY, json.dumps(symbols, ensure_ascii=False),
            reason=f"{len(symbols)} 只重点研究标的",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("重点研究清单落库失败: %s", exc)


@router.get("/watchlist")
def get_watchlist(mode: str = Query("paper")):
    """返回当前「重点研究」清单（用户自定义的重点跟踪标的）。"""
    c = _ctx(mode)
    return {"symbols": _watchlist_read(c)}


@router.post("/watchlist")
def post_watchlist(body: dict = Body(default={}), mode: str = Query("paper")):
    """更新「重点研究」清单。

    body 可选字段：
      symbols: list[str]   全量覆盖的标的列表
      add:     list[str]   追加的标的（与 symbols 二选一；同时传则先合并去重）
      replace: bool        true 时 symbols 全量覆盖（默认合并追加）
    """
    c = _ctx(mode)
    cur = _watchlist_read(c)
    symbols = body.get("symbols")
    add = body.get("add") or []
    replace = bool(body.get("replace", False))

    if symbols is not None:
        new = [str(s) for s in symbols]
    elif add:
        new = cur + [str(s) for s in add]
    else:
        new = cur
    # 去重保序
    seen = set()
    new = [s for s in new if not (s in seen or seen.add(s))]
    _watchlist_write(c, new)
    return {"symbols": new, "count": len(new)}


@router.delete("/watchlist")
def delete_watchlist(body: dict = Body(default={}), mode: str = Query("paper")):
    """从「重点研究」清单移除指定标的。

    body 字段：
      symbols: list[str]   要移除的标的；缺省/空则清空整个清单
    """
    c = _ctx(mode)
    cur = _watchlist_read(c)
    remove = set(str(s) for s in (body.get("symbols") or []))
    if not remove:
        new = []
    else:
        new = [s for s in cur if s not in remove]
    _watchlist_write(c, new)
    return {"symbols": new, "count": len(new), "removed": sorted(remove)}
