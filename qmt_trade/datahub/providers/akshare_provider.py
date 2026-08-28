"""AkShare 数据源：新闻、公告、资金流、日线兜底。

AkShare 接口签名变动频繁，因此这里所有调用都包在 try 里，单个接口失效不影响整体，
由 DataHub 的健康度机制自动降级。
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import pandas as pd

from ...core.instruments import parse_symbol
from ...core.logging import get_logger
from ..types import (Adjust, CorpEvent, EventCategory, Freq, Fundamental,
                     InstrumentInfo, NewsItem, Tick)
from .base import Capability, DataProvider

logger = get_logger("datahub.akshare")

#: 公告标题关键词 → 事件类别。规则先行的事件识别，不依赖 LLM（设计 6.5.2）
_EVENT_KEYWORDS: list[tuple[tuple[str, ...], EventCategory]] = [
    (("立案", "调查", "被查"), EventCategory.INVESTIGATION),
    (("处罚", "警示函", "监管措施", "违规"), EventCategory.REGULATORY_PENALTY),
    (("业绩预告", "业绩预增", "业绩预减", "业绩快报"), EventCategory.EARNINGS_FORECAST),
    (("年度报告", "半年度报告", "季度报告"), EventCategory.EARNINGS_REPORT),
    (("重组", "收购", "资产注入", "并购"), EventCategory.RESTRUCTURING),
    (("减持", "股份减持"), EventCategory.SHARE_REDUCTION),
    (("停牌",), EventCategory.SUSPENSION),
    (("解禁", "限售股上市"), EventCategory.UNLOCK),
    (("分红", "利润分配", "派息"), EventCategory.DIVIDEND),
    (("中标", "重大合同", "签订"), EventCategory.CONTRACT),
]


def classify_announcement(title: str) -> EventCategory:
    t = str(title)
    for keywords, cat in _EVENT_KEYWORDS:
        if any(k in t for k in keywords):
            return cat
    return EventCategory.OTHER


def _timeout_call(fn, timeout: float, label: str = ""):
    """在线程里执行 fn（akshare 偶发挂起），超时/异常返回 None，绝不阻塞主流程。

    守护线程：超时后即便仍在跑也不拖住进程退出（匹配 diag_factor_ic 的隔离思路）。
    用于个股资金流/新闻这类**逐票 akshare 调用**——单票网络挂起时跳过该票，
    而不是让整次因子计算卡死在 90s 超时。"""
    box: dict = {}

    def _run():
        try:
            box["r"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["e"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        logger.warning("akshare 调用超时(%.0fs)跳过: %s", timeout, label)
        return None
    if "e" in box:
        logger.debug("akshare 调用异常: %s: %s", label, box["e"])
        return None
    return box.get("r")


def _ak_symbol(symbol: str) -> str:
    code, _ = parse_symbol(symbol)
    return code


def _em_delay_industry_map(timeout: float = 12.0) -> dict[str, str]:
    """东财延迟接口（push2delay）直连版行业映射，akshare 主接口不可达时的回退。

    与 ``stock_board_industry_name_em/cons_em`` 同库同口径（fs=m:90+t:2），
    仅行情延迟（行业归属为静态分类，不受影响）。push2 主域名偶发被网络
    拒绝（ConnectionError）时该域名通常仍可达。
    """
    import requests

    from ...core.instruments import normalize_symbol

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    base = ("https://push2delay.eastmoney.com/api/qt/clist/get"
            "?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12&fs={fs}&fields=f12,f14")

    def _fetch_all(fs: str) -> list[dict]:
        rows: list[dict] = []
        pn = 1
        while pn <= 60:  # 硬上限保护
            r = requests.get(base.format(pn=pn, fs=fs), headers=headers, timeout=timeout)
            data = (r.json() or {}).get("data") or {}
            diff = data.get("diff") or []
            rows.extend(diff)
            total = int(data.get("total") or 0)
            if len(rows) >= total or not diff:
                break
            pn += 1
        return rows

    boards = _fetch_all("m:90+t:2+f:!50")
    mapping: dict[str, str] = {}
    for b in boards:
        bk, name = str(b.get("f12", "")), str(b.get("f14", ""))
        if not bk or not name:
            continue
        try:
            for c in _fetch_all(f"b:{bk}"):
                code = str(c.get("f12", ""))
                if code.isdigit():
                    try:
                        mapping.setdefault(normalize_symbol(code), name)
                    except Exception:  # noqa: BLE001
                        continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("东财延迟行业成分失败[%s]: %s", name, exc)
    logger.info("东财延迟行业映射构建完成：%d 板块 → %d 只个股", len(boards), len(mapping))
    return mapping


def _recent_quarter_ends(ref: date, n: int = 3) -> list[str]:
    """ref 之前最近的 n 个已完结报告期（yyyymmdd），用于批量拉取业绩报表。"""
    ends: list[date] = []
    for y in (ref.year, ref.year - 1):
        for m, dd in ((3, 31), (6, 30), (9, 30), (12, 31)):
            d = date(y, m, dd)
            if d < ref:
                ends.append(d)
    ends.sort(reverse=True)
    return [d.strftime("%Y%m%d") for d in ends[:n]]


def _to_date(v) -> date | None:
    """公告日期解析；空值/无法解析返回 None（调用方按无公告日丢弃）。"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        ts = pd.to_datetime(str(v)[:10])
        return ts.date() if not pd.isna(ts) else None
    except (ValueError, TypeError):
        return None


def _num(v) -> float:
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return float("nan")


class AkshareProvider(DataProvider):
    name = "akshare"
    capabilities = {
        Capability.BARS,
        Capability.TICK,
        Capability.NEWS,
        Capability.EVENTS,
        Capability.INSTRUMENTS,
        Capability.MONEY_FLOW,
        Capability.INDEX,
        Capability.FUNDAMENTALS,
    }

    def is_available(self) -> bool:
        try:
            import akshare  # noqa: F401
        except ImportError:
            return False
        return True

    @staticmethod
    def _ak():
        import akshare as ak

        return ak

    # ------------------------------------------------------------ 行情
    def get_bars(
        self,
        symbols: Sequence[str],
        freq: Freq = Freq.D1,
        start: date | str | None = None,
        end: date | str | None = None,
        adjust: Adjust = Adjust.HFQ,
    ) -> pd.DataFrame:
        ak = self._ak()
        adj = {Adjust.HFQ: "hfq", Adjust.QFQ: "qfq", Adjust.NONE: ""}[adjust]
        s = str(start).replace("-", "")[:8] if start else "19900101"
        e = str(end).replace("-", "")[:8] if end else datetime.now().strftime("%Y%m%d")
        frames = []
        for sym in symbols:
            df = None
            for attempt in range(4):
                try:
                    df = ak.stock_zh_a_hist(
                        symbol=_ak_symbol(sym), period="daily", start_date=s, end_date=e, adjust=adj
                    )
                    break
                except Exception as exc:  # 网络抖动（东方财富接口偶发断连），退避重试
                    logger.debug("akshare 日线(%s) 第 %d 次失败: %s", sym, attempt + 1, exc)
                    if attempt < 3:
                        import time as _t
                        _t.sleep(0.8 * (attempt + 1))
            if df is None or df.empty:
                continue
            df = df.rename(
                columns={
                    "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
                    "最低": "low", "成交量": "volume", "成交额": "amount", "换手率": "turnover_rate",
                }
            )
            df["date"] = pd.to_datetime(df["date"])
            df["symbol"] = sym.upper()
            df["volume"] = df["volume"] * 100.0
            df["prev_close"] = df["close"].shift(1)
            df.loc[df.index[0], "prev_close"] = df["close"].iloc[0]
            df["is_suspended"] = False
            keep = ["date", "symbol", "open", "high", "low", "close", "volume", "amount",
                    "prev_close", "is_suspended"]
            if "turnover_rate" in df.columns:
                df["turnover_rate"] = df["turnover_rate"] / 100.0
                keep.append("turnover_rate")
            frames.append(df[keep])
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"]).reset_index(drop=True)

    def get_index_bars(
        self, index_symbol: str, start: date | str | None = None, end: date | str | None = None
    ) -> pd.DataFrame:
        ak = self._ak()
        code, _ = parse_symbol(index_symbol)
        df = ak.stock_zh_index_daily(symbol=("sh" if index_symbol.upper().endswith("SH") else "sz") + code)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"date": "date"})
        df["date"] = pd.to_datetime(df["date"])
        df["symbol"] = index_symbol.upper()
        df["prev_close"] = df["close"].shift(1).bfill()
        if start:
            df = df.loc[df["date"] >= pd.Timestamp(start)]
        if end:
            df = df.loc[df["date"] <= pd.Timestamp(end)]
        return df.reset_index(drop=True)

    # ------------------------------------------------------------ 基础信息
    def get_instruments(self, symbols: Sequence[str] | None = None) -> list[InstrumentInfo]:
        ak = self._ak()
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return []
        wanted = {str(s).split(".")[0] for s in symbols} if symbols else None
        out: list[InstrumentInfo] = []
        for row in df.itertuples(index=False):
            code = str(getattr(row, "代码", ""))
            if not code or (wanted and code not in wanted):
                continue
            name = str(getattr(row, "名称", ""))
            from ...core.instruments import normalize_symbol

            out.append(
                InstrumentInfo(
                    symbol=normalize_symbol(code),
                    name=name,
                    is_st="ST" in name.upper(),
                    market_cap=float(getattr(row, "总市值", 0) or 0),
                )
            )
        return out

    def get_industry_map(self, per_board_timeout: float = 20.0) -> dict[str, str]:
        """东财行业板块映射 {symbol: 行业名}（尾盘策略板块效应数据链）。

        ``ak.stock_board_industry_name_em()`` 取板块列表，逐板块
        ``ak.stock_board_industry_cons_em()`` 取成分股。单板块用
        ``_timeout_call`` 隔离（akshare 偶发挂起），失败板块跳过不阻断。
        symbol 统一为 ``normalize_symbol`` 格式（如 600519.SH），
        与日线帧 symbol 列同口径，可直接查表。

        akshare 主接口不可达（push2 域名被网络拒绝）时自动回退到东财
        延迟接口 push2delay（同库同口径），实际数据源记录在
        ``last_industry_map_source`` 供快照元信息使用。
        """
        ak = self._ak()
        self.last_industry_map_source = ""
        from ...core.instruments import normalize_symbol
        try:
            boards = _timeout_call(ak.stock_board_industry_name_em, per_board_timeout,
                                   "行业板块列表")
        except Exception as exc:  # noqa: BLE001
            logger.warning("东财行业板块列表获取失败: %s", exc)
            return {}
        if boards is None or boards.empty:
            logger.warning("东财行业板块列表为空，回退 push2delay 延迟接口")
            try:
                mapping = _em_delay_industry_map()
            except Exception as exc:  # noqa: BLE001
                logger.warning("东财延迟行业映射失败: %s", exc)
                return {}
            if mapping:
                self.last_industry_map_source = (
                    "东财行业板块（push2delay 延迟接口直连，与 cons_em 同库同口径）")
            return mapping
        mapping: dict[str, str] = {}
        names = boards["板块名称"].astype(str).tolist() if "板块名称" in boards.columns else []
        for name in names:
            try:
                cons = _timeout_call(lambda n=name: ak.stock_board_industry_cons_em(symbol=n),
                                     per_board_timeout, f"行业成分[{name}]")
            except Exception:  # noqa: BLE001
                cons = None
            if cons is None or cons.empty or "代码" not in cons.columns:
                continue
            for code in cons["代码"].astype(str):
                try:
                    mapping.setdefault(normalize_symbol(code), name)
                except Exception:  # noqa: BLE001
                    continue
        logger.info("东财行业映射构建完成：%d 板块 → %d 只个股", len(names), len(mapping))
        if mapping:
            self.last_industry_map_source = (
                "akshare stock_board_industry_name_em / cons_em（东财行业板块）")
        return mapping

    # ------------------------------------------------------------ 新闻与公告
    def get_news(
        self,
        symbols: Sequence[str] | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 200,
    ) -> list[NewsItem]:
        out: list[NewsItem] = []
        if symbols:
            # 性能修复（2026-08-12）：逐票新闻拉取从**串行**改**并行**。
            # 回测对全市场 4500+ 只逐票联网（akshare stock_news_em），串行一轮 ≈ 2 小时；
            # 并行（默认 8 线程，可用 news_workers 调）首轮降到 ~15-20 分钟。
            # 已落盘的 news_*.parquet 直接命中磁盘缓存，不联网。
            # 注意：东方财富接口有频率限制，并发不宜过大；单票仍有 12s 超时 + 1 次重试。
            workers = int(self.options.get("news_workers", 8))

            def _load(sym: str) -> list[NewsItem]:
                df = self._news_cached_raw(sym)
                if df is None:
                    df = self._news_fetch_raw(sym)
                    if df is not None:
                        self._news_save_raw(sym, df)
                return self._parse_news(df, sym)

            if workers > 1 and len(symbols) > 1:
                with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                    for items in ex.map(_load, symbols):
                        out.extend(items)
            else:
                for sym in symbols:
                    out.extend(_load(sym))
        else:
            try:
                df = self._ak().stock_info_global_cls(symbol="全部")
                out.extend(self._parse_news(df, None))
            except Exception as exc:
                logger.debug("akshare 全局新闻失败: %s", exc)
        if start:
            lo = pd.Timestamp(start).to_pydatetime()
            out = [n for n in out if n.publish_time >= lo]
        if end:
            hi = pd.Timestamp(end).to_pydatetime()
            out = [n for n in out if n.publish_time <= hi]
        out.sort(key=lambda n: n.publish_time, reverse=True)
        return out[:limit]

    # ---- 新闻（单票原始 DataFrame：超时 + 磁盘缓存）----
    def _news_fetch_raw(self, sym: str) -> pd.DataFrame | None:
        ak = self._ak()
        for attempt in range(2):
            df = _timeout_call(lambda: ak.stock_news_em(symbol=_ak_symbol(sym)),
                               timeout=12.0, label=f"新闻({sym})")
            if df is not None:
                return df
            if attempt < 1:
                import time as _t
                _t.sleep(1.0)
        return None

    def _news_cached_raw(self, sym: str) -> pd.DataFrame | None:
        code, _ = parse_symbol(sym)
        path = self._cache_path("news", code)
        if path is not None and path.exists():
            try:
                return pd.read_parquet(path)
            except Exception:  # noqa: BLE001
                path.unlink(missing_ok=True)
        return None

    def _news_save_raw(self, sym: str, df: pd.DataFrame) -> None:
        code, _ = parse_symbol(sym)
        path = self._cache_path("news", code)
        if path is None:
            return
        try:
            df.to_parquet(path, index=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug("akshare 新闻(%s) 缓存写入失败: %s", sym, exc)

    @staticmethod
    def _parse_news(df: pd.DataFrame | None, symbol: str | None) -> list[NewsItem]:
        if df is None or df.empty:
            return []
        cols = {c: str(c) for c in df.columns}
        title_col = next((c for c in cols if "标题" in str(c)), None)
        time_col = next((c for c in cols if "时间" in str(c) or "日期" in str(c)), None)
        content_col = next((c for c in cols if "内容" in str(c) or "摘要" in str(c)), None)
        url_col = next((c for c in cols if "链接" in str(c) or "url" in str(c).lower()), None)
        if title_col is None or time_col is None:
            return []
        items: list[NewsItem] = []
        for row in df.itertuples(index=False):
            title = str(getattr(row, str(title_col), "") or "")
            raw_time = getattr(row, str(time_col), None)
            try:
                pub = pd.to_datetime(raw_time).to_pydatetime()
            except Exception:
                continue
            nid = hashlib.md5(f"{symbol}|{title}|{pub}".encode("utf-8")).hexdigest()[:16]
            items.append(
                NewsItem(
                    id=nid,
                    title=title,
                    content=str(getattr(row, str(content_col), "") or "") if content_col else "",
                    publish_time=pub,
                    symbol=symbol.upper() if symbol else None,
                    source="akshare",
                    url=str(getattr(row, str(url_col), "") or "") if url_col else "",
                )
            )
        return items

    def get_events(
        self,
        symbols: Sequence[str] | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
    ) -> list[CorpEvent]:
        """用公告标题关键词做规则分类，产出结构化事件。"""
        news = self.get_news(symbols=symbols, start=start, end=end, limit=500)
        events: list[CorpEvent] = []
        for n in news:
            cat = classify_announcement(n.title)
            if cat is EventCategory.OTHER:
                continue
            events.append(
                CorpEvent(
                    id=n.id,
                    symbol=n.symbol or "",
                    category=cat,
                    title=n.title,
                    ann_time=n.publish_time,
                    detail=n.content,
                    importance=0.9 if cat in (EventCategory.INVESTIGATION,
                                              EventCategory.REGULATORY_PENALTY) else 0.5,
                )
            )
        return events

    # ------------------------------------------------------------ 财务（批量，PIT 关键）
    def get_fundamentals(
        self, symbols: Sequence[str], start: date | str | None = None, end: date | str | None = None
    ) -> list[Fundamental]:
        """东方财富业绩报表**全市场批量**取数（一次调用覆盖全部标的）。

        与 tushare fina_indicator 的逐票调用（全市场 4000+ 次必然限流）不同，
        ``stock_yjbb_em(date=报告期)`` 单次返回全市场业绩报表，天然适合回测。
        PIT 纪律与 tushare 路径一致：按「最新公告日期」过滤，无公告日的记录丢弃。
        """
        from ...core.instruments import normalize_symbol

        wanted = {str(s).split(".")[0]: s for s in symbols} if symbols else None
        out: list[Fundamental] = []
        for period in _recent_quarter_ends(date.today(), n=3):
            df = self._yjbb_cached(period)
            if df is None or df.empty:
                continue
            zcfz = self._zcfz_cached(period)
            debt_by_code = self._debt_ratio_map(zcfz)
            # 注意：列名含"-"（如 营业收入-同比增长）不是合法 Python 标识符，
            # itertuples 会改名为位置名，必须按列字典取数
            for rec in df.to_dict("records"):
                code = str(rec.get("股票代码", "") or "")
                if not code or (wanted and code not in wanted):
                    continue
                ann = _to_date(rec.get("最新公告日期"))
                if ann is None:
                    continue  # ★ 没有公告日一律丢弃：宁可少数据，不能拿报告期当公告日
                eps = _num(rec.get("每股收益"))
                net_profit = _num(rec.get("净利润-净利润"))
                ocf_ps = _num(rec.get("每股经营现金流量"))
                # 现金流比率需要总量口径：由 net_profit/eps 反推总股本折算
                ocf = ocf_ps * net_profit / eps if eps and net_profit == net_profit else float("nan")
                out.append(
                    Fundamental(
                        symbol=normalize_symbol(code),
                        ann_date=ann,
                        report_period=datetime.strptime(period, "%Y%m%d").date(),
                        # yjbb 实际列名为「营业总收入-*」，保留旧名作回退（akshare 版本差异）
                        revenue=_num(rec.get("营业总收入-营业总收入")
                                     or rec.get("营业收入-营业收入")),
                        net_profit=net_profit,
                        roe=_num(rec.get("净资产收益率")) / 100.0,
                        revenue_yoy=_num(rec.get("营业总收入-同比增长")
                                         or rec.get("营业收入-同比增长")) / 100.0,
                        profit_yoy=_num(rec.get("净利润-同比增长")) / 100.0,
                        gross_margin=_num(rec.get("销售毛利率")) / 100.0,
                        debt_ratio=debt_by_code.get(code, float("nan")),
                        ocf=ocf,
                        eps=eps,
                        bps=_num(rec.get("每股净资产")),
                    )
                )
        logger.info("akshare 基本面批量取数完成：%d 条记录（近 3 个报告期）", len(out))
        return out

    # ---- 业绩报表 / 资产负债表（带磁盘缓存，历史报告期内容不再变化）----
    def _cache_path(self, tag: str, period: str) -> Path | None:
        root = self.options.get("cache_dir")
        if not root:
            return None
        p = Path(root)
        p.mkdir(parents=True, exist_ok=True)
        return p / f"{tag}_{period}.parquet"

    def _yjbb_cached(self, period: str) -> pd.DataFrame | None:
        return self._batch_cached("fundamentals_yjbb", period,
                                  lambda: self._ak().stock_yjbb_em(date=period))

    def _zcfz_cached(self, period: str) -> pd.DataFrame | None:
        return self._batch_cached("fundamentals_zcfz", period,
                                  lambda: self._ak().stock_zcfz_em(date=period))

    def _batch_cached(self, tag: str, period: str, fetch) -> pd.DataFrame | None:
        path = self._cache_path(tag, period)
        if path is not None and path.exists():
            try:
                return pd.read_parquet(path)
            except Exception:  # noqa: BLE001  缓存损坏则重拉
                path.unlink(missing_ok=True)
        df = None
        for attempt in range(3):
            try:
                df = fetch()
                break
            except Exception as exc:          # 东财接口偶发断连，退避重试
                logger.debug("akshare %s(%s) 第 %d 次失败: %s", tag, period, attempt + 1, exc)
                if attempt < 2:
                    import time as _t
                    _t.sleep(1.5 * (attempt + 1))
        if df is None or df.empty:
            logger.warning("akshare %s(%s) 取数失败", tag, period)
            return None
        if path is not None:
            try:
                df.to_parquet(path, index=False)
            except Exception as exc:  # noqa: BLE001  写缓存失败不影响本次使用
                logger.debug("akshare %s(%s) 缓存写入失败: %s", tag, period, exc)
        return df

    @staticmethod
    def _debt_ratio_map(zcfz: pd.DataFrame | None) -> dict[str, float]:
        """从资产负债表批量结果里提取资产负债率（列名模糊匹配，接口变动兜底）。"""
        if zcfz is None or zcfz.empty:
            return {}
        col = next((c for c in zcfz.columns if "负债率" in str(c)), None)
        if col is None or "股票代码" not in zcfz.columns:
            return {}
        out: dict[str, float] = {}
        for row in zcfz[["股票代码", col]].itertuples(index=False):
            code = str(row[0])
            v = _num(row[1])
            if code and v == v:
                out[code] = v / 100.0
        return out

    # ------------------------------------------------------------ 资金流
    def get_money_flow(
        self, symbols: Sequence[str], start: date | str | None = None, end: date | str | None = None
    ) -> pd.DataFrame:
        """个股主力资金流。逐票 akshare 调用，加 **超时 + 重试 + 磁盘缓存**：

        - 单票调用挂起(网络抖动)时超时跳过该票，不拖垮整次因子计算；
        - 历史资金流不变，缓存后重复运行秒回，根除 90s TIMEOUT 卡死。
        """
        frames = []
        for sym in symbols:
            code, market = parse_symbol(sym)
            df = self._moneyflow_cached(code)
            if df is None:
                df = self._moneyflow_fetch(code, market, sym)
                if df is not None:
                    self._moneyflow_save(code, df)
            if df is None or df.empty:
                continue
            df = df.rename(columns={"日期": "date", "主力净流入-净额": "net_inflow"})
            if "date" not in df.columns or "net_inflow" not in df.columns:
                continue
            df["date"] = pd.to_datetime(df["date"])
            df["symbol"] = sym.upper()
            df["big_order_ratio"] = 0.0
            frames.append(df[["date", "symbol", "net_inflow", "big_order_ratio"]])
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        if start:
            out = out.loc[out["date"] >= pd.Timestamp(start)]
        if end:
            out = out.loc[out["date"] <= pd.Timestamp(end)]
        return out.reset_index(drop=True)

    def _moneyflow_fetch(self, code: str, market: str, sym: str) -> pd.DataFrame | None:
        ak = self._ak()
        for attempt in range(2):
            df = _timeout_call(
                lambda: ak.stock_individual_fund_flow(stock=code, market=market.lower()),
                timeout=12.0, label=f"资金流({sym})")
            if df is not None:
                return df
            if attempt < 1:
                import time as _t
                _t.sleep(1.0)
        return None

    def _moneyflow_cached(self, code: str) -> pd.DataFrame | None:
        path = self._cache_path("moneyflow", code)
        if path is not None and path.exists():
            try:
                return pd.read_parquet(path)
            except Exception:  # noqa: BLE001
                path.unlink(missing_ok=True)
        return None

    def _moneyflow_save(self, code: str, df: pd.DataFrame) -> None:
        path = self._cache_path("moneyflow", code)
        if path is None:
            return
        try:
            df.to_parquet(path, index=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug("akshare 资金流(%s) 缓存写入失败: %s", code, exc)

    # ------------------------------------------------------------ 实时行情
    def get_realtime(self, symbols: Sequence[str]) -> dict[str, Tick]:
        """东方财富全市场快照，按标的过滤出实时行情（真实数据）。

        全市场快照偶发连接中断（RemoteDisconnected），加重试提升健壮性。
        """
        ak = self._ak()
        df = None
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                df = ak.stock_zh_a_spot_em()
                break
            except Exception as exc:               # 网络抖动（东方财富实时接口偶发断连），退避重试
                last_exc = exc
                logger.debug("akshare 实时行情第 %d 次失败: %s", attempt + 1, exc)
                if attempt < 3:
                    import time as _t
                    _t.sleep(0.8 * (attempt + 1))
        if df is None or df.empty:
            if last_exc is not None:
                logger.warning("akshare 实时行情持续失败: %s", last_exc)
            return {}
        wanted = {str(s).split(".")[0]: s for s in symbols}
        if not wanted:
            return {}
        now = datetime.now()
        out: dict[str, Tick] = {}
        for row in df.itertuples(index=False):
            code = str(getattr(row, "代码", ""))
            target = wanted.get(code)
            if target is None:
                continue
            last = float(getattr(row, "最新价", 0) or 0)
            out[target] = Tick(
                symbol=target,
                time=now,
                last=last,
                open=float(getattr(row, "今开", 0) or 0),
                high=float(getattr(row, "最高", 0) or 0),
                low=float(getattr(row, "最低", 0) or 0),
                prev_close=float(getattr(row, "昨收", 0) or 0),
                volume=float(getattr(row, "成交量", 0) or 0) * 100.0,   # 手 → 股
                amount=float(getattr(row, "成交额", 0) or 0),            # 元
            )
            if len(out) >= len(wanted):
                break
        return out
