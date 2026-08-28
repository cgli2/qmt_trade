"""Point-in-Time 时间切片 —— 防未来函数的第一道防线。

TradingAgents-CN 的数据接口完全没有 ``asof`` 概念（因为它根本不回测），
qmt_etf 的回测直接用全量 DataFrame 切片、财务数据按报告期对齐。两者都会穿越。

本模块的态度分两层，不要混淆：

1. **切片层（DataHub 内部）**：数据源天然会返回超过 ``asof`` 的数据，裁剪是正常职责，
   用 ``strict=False`` 静默裁剪即可，裁完再自检一次（``assert_no_lookahead``）。
2. **消费层（策略/因子/回测）**：拿到的数据里但凡出现未来记录，就是**代码 bug**，
   必须 ``strict=True`` 立刻抛异常。在回测中静默吞掉会让人误以为数据没问题，
   等到实盘才发现收益对不上。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Iterable, Sequence, TypeVar

import pandas as pd

from ..core.errors import LookAheadError
from ..core.logging import get_logger

logger = get_logger("datahub.pit")

T = TypeVar("T")


def to_datetime(value: date | datetime | str | pd.Timestamp | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, date):
        # 纯日期视为当日收盘后（15:00），这样「asof=某日」能拿到该日的日线
        return datetime.combine(value, time(15, 0))
    return datetime.fromisoformat(str(value))


def as_of_end_of_day(day: date | str) -> datetime:
    d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    return datetime.combine(d, time(15, 0))


def as_of_pre_open(day: date | str) -> datetime:
    """盘前时点：当日 09:00。此时**当日日线尚不存在**，只能看到昨日及之前。"""
    d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    return datetime.combine(d, time(9, 0))


class PITGuard:
    """PIT 校验器。

    用法::

        guard = PITGuard(asof=datetime(2026, 8, 8, 9, 0))
        news  = guard.filter_records(all_news, time_attr="publish_time")
        bars  = guard.filter_frame(all_bars, time_col="date")
    """

    def __init__(self, asof: date | datetime | str | None, *, strict: bool = True):
        self.asof = to_datetime(asof)
        self.strict = strict

    # ------------------------------------------------------------ 记录级
    def is_visible(self, record: Any, time_attr: str = "publish_time") -> bool:
        """记录在 ``asof`` 时点是否可见。

        F3 修复（2026-08-12）：**无时间戳的记录一律视为不可见（fail-closed）**。
        之前 ``ts is None → return True`` 是 fail-open：无法证明它不是未来数据，
        回测里静默放行就等于泄漏。无法验证的记:录宁可丢弃，也不能冒险混入。
        （live 模式 asof=None 时全部可见，不受影响。）
        """
        if self.asof is None:
            return True
        ts = self._ts_of(record, time_attr)
        if ts is None:
            return False
        return ts <= self.asof

    @staticmethod
    def _ts_of(record: Any, time_attr: str) -> datetime | None:
        return to_datetime(getattr(record, time_attr, None) if not isinstance(record, dict)
                           else record.get(time_attr))

    def filter_records(
        self, records: Iterable[T], time_attr: str = "publish_time", *, label: str = ""
    ) -> list[T]:
        if self.asof is None:
            return list(records)
        kept, leaked, unstamped = [], 0, 0
        for r in records:
            ts = self._ts_of(r, time_attr)
            if ts is None:
                unstamped += 1
                continue  # 无时间戳 = 无法证明非未来数据，fail-closed 排除
            if ts <= self.asof:
                kept.append(r)
            else:
                leaked += 1
        if unstamped:
            msg = (f"检测到无时间戳记录 {unstamped} 条 (asof={self.asof}, 来源={label})"
                   f" —— 已排除（无法证明非未来数据）")
            if self.strict:
                raise LookAheadError(msg, asof=str(self.asof), leaked=unstamped,
                                     label=f"{label}:无时间戳")
            logger.warning(msg)
        if leaked:
            self._report(leaked, label or time_attr)
        return kept

    # ------------------------------------------------------------ DataFrame 级
    def filter_frame(
        self, df: pd.DataFrame, time_col: str = "date", *, label: str = ""
    ) -> pd.DataFrame:
        if self.asof is None or df is None or df.empty or time_col not in df.columns:
            return df
        ts = pd.to_datetime(df[time_col])
        # 纯日期列（无时分秒）视为当日 15:00
        if getattr(ts.dt, "normalize", None) is not None and (ts == ts.dt.normalize()).all():
            ts = ts + pd.Timedelta(hours=15)
        mask = ts <= pd.Timestamp(self.asof)
        leaked = int((~mask).sum())
        if leaked:
            self._report(leaked, label or time_col)
        return df.loc[mask].copy()

    def _report(self, leaked: int, label: str) -> None:
        msg = f"检测到未来数据 {leaked} 条 (asof={self.asof}, 来源={label})"
        if self.strict:
            raise LookAheadError(msg, asof=str(self.asof), leaked=leaked, label=label)
        logger.warning("%s —— 已过滤（strict=False）", msg)


def latest_fundamental_asof(
    records: Sequence[Any], asof: date | datetime | str, *,
    symbol_attr: str = "symbol", time_attr: str = "ann_date",
) -> dict[str, Any]:
    """取每个标的在 ``asof`` 时点**已公告**的最新一期财务数据。

    这是财务因子唯一正确的取数方式：按 ``ann_date`` 过滤，再按报告期取最新。

    F3 修复（2026-08-12）：显式用 ``ann_date`` 作为过滤键（此前依赖
    ``Fundamental.publish_time`` property 隐式取 ann_date，行为等价但语义不透明，
    且任何未来新增、无该 property 的财务记录类型都会静默绕过 PIT 过滤）。
    """
    guard = PITGuard(asof, strict=False)
    visible = guard.filter_records(records, time_attr=time_attr, label="fundamentals")
    out: dict[str, Any] = {}
    for rec in visible:
        sym = getattr(rec, symbol_attr)
        cur = out.get(sym)
        if cur is None or getattr(rec, "report_period", None) > getattr(cur, "report_period", None):
            out[sym] = rec
    return out


def assert_no_lookahead(df: pd.DataFrame, asof: date | datetime, time_col: str = "date") -> None:
    """单测辅助：断言 DataFrame 不含未来数据。"""
    PITGuard(asof, strict=True).filter_frame(df, time_col=time_col, label="assert")
