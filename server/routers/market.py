"""行情数据：K线、实时行情、新闻、公司事件。"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

import server.context as ctx
from qmt_trade.datahub.types import Adjust, Freq

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/market", tags=["market"])


def _ctx(mode: str = Query("paper")):
    # 本路由全部为只读行情查询（K线/分时/报价/新闻），属研究类操作，
    # live 观察期锁定时自动降级 paper（不拦用户）
    return ctx.make_ctx_research(mode)


def _split(symbols: str | None) -> list[str]:
    if not symbols:
        return []
    return [s.strip() for s in symbols.split(",") if s.strip()]


@router.get("/bars")
def get_bars(
    symbols: str = Query(..., description="逗号分隔，如 600000,000001"),
    freq: str = "D1",
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str | None = None,
    adjust: str = "QFQ",
    limit: int = 800,
    mode: str = Query("paper"),
):
    c = _ctx(mode)
    try:
        df = c.hub.get_bars(
            _split(symbols), freq=getattr(Freq, freq, Freq.D1),
            start=date.fromisoformat(start),
            end=date.fromisoformat(end) if end else date.today(),
            adjust=getattr(Adjust, adjust, Adjust.QFQ),
        )
    except Exception as exc:                     # noqa: BLE001
        raise HTTPException(500, f"取数失败: {exc}")
    if df is None or len(df) == 0:
        return {"rows": [], "count": 0}
    rows = df.tail(limit).to_dict(orient="records")
    out = []
    for r in rows:
        rec = {}
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                rec[k] = v.isoformat()
            elif hasattr(v, "item"):
                rec[k] = float(v.item())
            else:
                rec[k] = v
        out.append(rec)
    return {"rows": out, "count": len(out)}


# ---------------------------------------------------------------- K线图表
#: 图表周期 → pandas 重采样规则（日线无需重采样）。
#: 周线按交易周归并（周五为周界，节假日落在周内最后交易日）；月/年按自然月/年归并。
_RESAMPLE_RULE = {"W1": "W-FRI", "M1": "ME", "Y1": "YE"}
#: MA60 需要 60 根前置K线：各周期向前多取的日历天数（预热段只参与均线计算，不返回给前端，
#: 保证用户所选区间左边界处的均线值不因样本不足而失真）
_WARMUP_DAYS = {"D1": 140, "W1": 480, "M1": 1950, "Y1": 22500}
#: 图表叠加的均线周期
_MA_PERIODS = (5, 10, 20, 60)


def _json_val(v):
    """序列化为 JSON 友好值：日期→ISO 字符串，numpy 标量→float，NaN→None。"""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "item"):
        f = float(v.item())
        return None if f != f else f                # NaN 检查
    if isinstance(v, float) and v != v:
        return None
    return v


@router.get("/kline")
def get_kline(
    symbol: str = Query(..., description="单个标的，如 600000.SH"),
    period: str = "D1",
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str | None = None,
    adjust: str = "QFQ",
    limit: int = 400,
    mode: str = Query("paper"),
):
    """K线图表数据：日/周/月/年四个周期 + MA5/10/20/60 均线。

    周/月/年由日线（与回测/实盘同一条 DataHub 取数路径，P7）重采样聚合：
    open=首、high=max、low=min、close=末、volume/amount=求和；
    均线在含预热段的完整序列上计算，再裁剪回用户区间，避免边界均线失真。
    """
    period = period.upper()
    if period not in ("D1", *_RESAMPLE_RULE):
        raise HTTPException(400, f"不支持的周期: {period}（可选 D1/W1/M1/Y1）")
    c = _ctx(mode)
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end) if end else date.today()
    warm_start = start_d - timedelta(days=_WARMUP_DAYS[period])
    try:
        df = c.hub.get_bars(
            [symbol], freq=Freq.D1, start=warm_start, end=end_d,
            adjust=getattr(Adjust, adjust, Adjust.QFQ),
        )
    except Exception as exc:                     # noqa: BLE001
        raise HTTPException(500, f"取数失败: {exc}")
    if df is None or len(df) == 0:
        return {"rows": [], "count": 0, "period": period}
    # 单标的图表：只取该标的（normalize 后的代码），按日期升序
    df = df[df["symbol"] == df["symbol"].iloc[0]].copy()
    work = df[["date", "open", "high", "low", "close", "volume", "amount"]]
    work = work.set_index("date").sort_index()
    work["trade_date"] = work.index              # K线实际日期取周期内最后交易日
    if _RESAMPLE_RULE.get(period):
        work = (
            work.resample(_RESAMPLE_RULE[period])
            .agg({
                "trade_date": "last", "open": "first", "high": "max",
                "low": "min", "close": "last", "volume": "sum", "amount": "sum",
            })
            .dropna(subset=["close"])            # 去掉无交易的空周期
        )
    for p in _MA_PERIODS:
        work[f"ma{p}"] = work["close"].rolling(p).mean()
    # 预热段只用于均线计算，返回前裁剪回用户请求的区间
    work = work[(work["trade_date"] >= pd.Timestamp(start_d))
                & (work["trade_date"] <= pd.Timestamp(end_d))].tail(limit)
    cols = ["trade_date", "open", "high", "low", "close", "volume", "amount",
            "ma5", "ma10", "ma20", "ma60"]
    out = []
    for rec in work[cols].to_dict(orient="records"):
        out.append({("date" if k == "trade_date" else k): _json_val(v)
                    for k, v in rec.items()})
    return {"rows": out, "count": len(out), "period": period}


# ---------------------------------------------------------------- 分时图
#: A股连续竞价 09:31-11:30 + 13:01-15:00 共 240 个分钟槽位（与行情软件分时图一致）
_TL_SLOTS = 240
_TL_SLOT_LABELS = [None] * _TL_SLOTS
for _i in range(_TL_SLOTS):
    if _i < 120:
        _m = 9 * 60 + 31 + _i
    else:
        _m = 13 * 60 + 1 + (_i - 120)
    _TL_SLOT_LABELS[_i] = f"{_m // 60:02d}:{_m % 60:02d}"


def _detect_offsets(minutes: set[int]) -> tuple[int, int]:
    """根据数据中是否存在 09:30/13:00 首根推断槽位偏移。

    分钟 bar 存在两种标注约定：起点标注(09:30/13:00 起)与终点标注(09:31/13:01 起)，
    二者对同一天都恰好 120+120 根，按首根时间自动对齐到 240 槽位。
    """
    am_off = 9 * 60 + 30 if (9 * 60 + 30) in minutes else 9 * 60 + 31
    pm_off = 13 * 60 if (13 * 60) in minutes else 13 * 60 + 1
    return am_off, pm_off


def _slot_of(m: int, am_off: int, pm_off: int):
    """分钟数(时*60+分) → 240 槽位索引；午休/盘外返回 None。"""
    if am_off <= m < 13 * 60:
        return min(m - am_off, 119)
    if pm_off <= m <= 15 * 60:
        return min(120 + m - pm_off, _TL_SLOTS - 1)
    return None


def _to_minute_series(s: pd.Series) -> pd.Series:
    """量/额列兼容：单调不减视为当日累计值（QMT），做差分还原分钟值；否则按分钟值原样返回。"""
    # pandas 2.x 移除了 is_monotonic_non_decreasing 别名，用 is_monotonic_increasing（允许相等，即非严格递增）
    if len(s) >= 3 and bool(s.is_monotonic_increasing) and (s.diff().fillna(0) > 0).sum() >= 2:
        d = s.diff()
        if len(s):
            d.iloc[0] = s.iloc[0]
        return d.clip(lower=0)
    return s


def _vol_unit_scale(price: float, vol: float, amt: float) -> float:
    """成交量单位探测：QMT 的 volume 以「手」计（1手=100股）、amount 以元计，
    此时 amount/volume ≈ 100×价格；返回需乘到 volume 上对齐单位的系数。"""
    if price > 0 and vol > 0 and amt > 0:
        ratio = (amt / vol) / price
        if 50 < ratio < 200:
            return 100.0
    return 1.0


@router.get("/timeline")
def get_timeline(
    symbol: str = Query(..., description="单个标的，如 600000.SH"),
    mode: str = Query("paper"),
):
    """分时图数据：最近交易日 240 个分钟槽位的价格/均价/成交量 + 实时报价。

    取数与回测/实盘同走 DataHub（分钟数据目前仅 QMT 源支持，缺失自动补下载）；
    均价 = 累计成交额/累计成交量（VWAP）；昨收优先取实时 tick，其次取前一交易日日线收盘。
    """
    c = _ctx(mode)
    today = date.today()
    stale = False
    try:
        # 展示用前复权（最新价≈真实价，与实时 tick 衔接）；默认 HFQ 是回测专用。
        # 优先取「今日」分时：盘中/盘后应当看到当天。取不到（盘前/数据源未就绪）
        # 再退到最近 10 个交易日、并标 stale，避免静默把上一交易日当今天展示。
        df = c.hub.get_bars([symbol], freq=Freq.M1, start=today, end=today,
                            adjust=Adjust.QFQ)
        if df is None or len(df) == 0:
            df = c.hub.get_bars([symbol], freq=Freq.M1, start=today - timedelta(days=10), end=today,
                                adjust=Adjust.QFQ)
            stale = True
    except (ValueError, KeyError) as exc:        # 非法标的/交易所后缀：降级为空而非 500
        return {"symbol": symbol, "date": None, "live": False, "prev_close": None,
                "stale": False, "today": today.isoformat(),
                "points": [], "quote": None, "note": f"标的无效或无分钟数据: {exc}"}
    except Exception as exc:                     # noqa: BLE001
        raise HTTPException(500, f"分时数据获取失败: {exc}")
    if df is None or len(df) == 0:
        return {"symbol": symbol, "date": None, "live": False, "prev_close": None,
                "stale": False, "today": today.isoformat(),
                "points": [], "quote": None,
                "note": "当前数据源无分钟数据（需 QMT 行情源）"}
    df = df.sort_values("date")
    day = df["date"].dt.date.iloc[-1]            # 最近一个有分钟数据的交易日（盘中即今日）
    df = df[df["date"].dt.date == day]
    # 即便 fallback 取到了数据，只要不是今天，就标 stale（前端提示「非今日」）
    stale = stale or (day != today)

    vols = _to_minute_series(df["volume"].astype(float))
    amts = _to_minute_series(df["amount"].astype(float))
    # 均价：盘中快照的 amount 可能滞后，此时退化为 价格×量 累计
    if float(amts.sum()) <= 0:
        amts = (df["close"].astype(float) * vols).astype(float)
    # volume 单位对齐（QMT 以手计）：探测不到时 scale=1，VWAP 不受影响
    scale = _vol_unit_scale(float(df["close"].astype(float).mean()), float(vols.sum()), float(amts.sum()))
    cum_v, cum_a = vols.cumsum(), amts.cumsum()
    avg = (cum_a / (cum_v * scale).replace(0, pd.NA)).astype(float)

    points: list[dict] = []
    seen: set[int] = set()
    mins = {int(ts.hour * 60 + ts.minute) for ts in df["date"]}
    am_off, pm_off = _detect_offsets(mins)
    for (ts, close), v, a in zip(df[["date", "close"]].itertuples(index=False, name=None),
                                 vols.tolist(), avg.tolist()):
        s = _slot_of(int(ts.hour * 60 + ts.minute), am_off, pm_off)
        if s is None or s in seen:
            continue                             # 盘外点/同槽重复取首条
        seen.add(s)
        points.append({"t": _TL_SLOT_LABELS[s], "s": s,
                       "p": _json_val(close), "a": _json_val(a), "v": _json_val(v)})

    # 实时报价：永不缓存，盘中时作为最新点补齐最后一分钟
    quote = None
    try:
        ticks = c.hub.get_realtime([symbol]) or {}
        t = next(iter(ticks.values()), None) if ticks else None
    except Exception as exc:                     # noqa: BLE001
        logger.warning("分时实时报价获取失败（降级为纯历史分钟线）: %s", exc)
        t = None
    live = bool(t and getattr(t, "last", 0) and day == today)
    if t is not None:
        quote = {
            "last": t.last, "open": t.open, "high": t.high, "low": t.low,
            "prev_close": t.prev_close, "volume": t.volume, "amount": t.amount,
            "bid1": t.bid1, "ask1": t.ask1,
        }
        if live:
            s = _slot_of(int(t.time.hour * 60 + t.time.minute), am_off, pm_off)
            if s is not None:
                cum_v_f = float(cum_v.iloc[-1] or 0)
                cum_a_f = float(cum_a.iloc[-1] or 0)
                tv, ta = float(t.volume or 0), float(t.amount or 0)
                # 分钟侧 amount 滞后退化为价×量时 scale 探不出来，用 tick 累计量额补探一次
                if scale == 1.0:
                    scale = _vol_unit_scale(float(t.last), tv, ta)
                # tick 量额为当日累计：与分钟累计尾值做差即最后一分钟增量
                mv, ma = max(tv - cum_v_f, 0.0), max(ta - cum_a_f, 0.0)
                denom = (cum_v_f + mv) * scale
                point_a = ((cum_a_f + ma) / denom) if denom > 0 else t.last
                pt = {"t": _TL_SLOT_LABELS[s], "s": s,
                      "p": float(t.last), "a": round(point_a, 4), "v": mv}
                if points and points[-1]["s"] == s:
                    # tick 落在最后一根分钟 bar 同一槽位：实时价覆盖该分钟，量取两者较大
                    pt["v"] = max(float(points[-1].get("v") or 0), mv)
                    points[-1] = pt
                elif s not in seen:
                    points.append(pt)

    # 昨收：优先 tick.prev_close，其次前一交易日日线收盘，均无则 None（前端按首日开盘基准）
    prev_close = None
    if t is not None and getattr(t, "prev_close", 0) > 0:
        prev_close = float(t.prev_close)
    else:
        try:
            dd = c.hub.get_bars([symbol], freq=Freq.D1, start=day - timedelta(days=15), end=day,
                                adjust=Adjust.QFQ)
            if dd is not None and len(dd):
                dd = dd.sort_values("date")
                prior = dd[dd["date"].dt.date < day]
                if len(prior):
                    prev_close = float(prior["close"].iloc[-1])
        except Exception as exc:                 # noqa: BLE001
            logger.warning("昨收取价获取失败 %s: %s", symbol, exc)
    return {"symbol": symbol, "date": day.isoformat(), "live": live,
            "prev_close": prev_close, "points": points, "quote": quote,
            "stale": bool(stale), "today": today.isoformat()}


@router.get("/symbols")
def get_symbols(mode: str = Query("paper")):
    """当前数据源可交易的标的列表（仅 paper/live 的真实标的）。同时返回 sources/real 供前端标注真实数据源。"""
    c = _ctx(mode)
    try:
        insts = c.hub.get_instruments()
    except Exception as exc:                     # noqa: BLE001
        raise HTTPException(500, f"获取标的不满: {exc}")
    sources: list[str] = []
    try:
        sources = [s.get("name") for s in (c.hub.health_snapshot() or [])]
    except Exception:                            # noqa: BLE001
        sources = []
    real = "mock" not in sources
    return {
        "symbols": [
            {"symbol": it.symbol, "name": it.name, "industry": it.industry}
            for it in (insts or [])
        ],
        "sources": sources,
        "real": real,
    }


@router.get("/quote")
def get_quote(symbols: str = Query(..., description="逗号分隔"), mode: str = Query("paper")):
    c = _ctx(mode)
    try:
        ticks = c.hub.get_realtime(_split(symbols))
    except Exception as exc:                     # noqa: BLE001
        # 实时行情源（东方财富）偶发断连属正常，降级为空而非 500，前端据 empty 提示重试
        logger.warning("实时行情获取失败（降级为空）: %s", exc)
        return {"quotes": {}, "note": "实时行情数据源暂不可用，请稍后重试"}
    out = {}
    for sym, t in (ticks or {}).items():
        out[sym] = {
            "symbol": sym,
            "last": getattr(t, "last", None),
            "open": getattr(t, "open", None),
            "high": getattr(t, "high", None),
            "low": getattr(t, "low", None),
            "prev_close": getattr(t, "prev_close", None),
            "volume": getattr(t, "volume", None),
            "amount": getattr(t, "amount", None),
            "bid1": getattr(t, "bid1", None),
            "ask1": getattr(t, "ask1", None),
        }
    return {"quotes": out}


@router.get("/news")
def get_news(
    symbols: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
    mode: str = Query("paper"),
):
    c = _ctx(mode)
    start_d = date.fromisoformat(start) if start else None
    end_d = date.fromisoformat(end) if end else None
    try:
        items = c.hub.get_news(_split(symbols), start_d, end_d, limit=limit)
    except Exception as exc:                     # noqa: BLE001
        raise HTTPException(500, f"新闻获取失败: {exc}")
    return {"news": [it.to_row() for it in (items or [])]}


@router.get("/events")
def get_events(
    symbols: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
    mode: str = Query("paper"),
):
    c = _ctx(mode)
    start_d = date.fromisoformat(start) if start else None
    end_d = date.fromisoformat(end) if end else None
    try:
        items = c.hub.get_events(_split(symbols), start_d, end_d)
    except Exception as exc:                     # noqa: BLE001
        raise HTTPException(500, f"事件获取失败: {exc}")
    items = (items or [])[:limit]
    out = []
    for it in (items or []):
        out.append({
            "id": it.id, "symbol": it.symbol, "category": it.category.value,
            "title": it.title, "ann_time": it.ann_time.isoformat(),
            "detail": it.detail, "importance": it.importance,
            "sentiment": it.sentiment, "is_hard_negative": it.is_hard_negative,
        })
    return {"events": out}
