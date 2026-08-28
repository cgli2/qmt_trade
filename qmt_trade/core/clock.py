"""交易日历与交易时段。借鉴 qmt_etf/utils/trading_time.py，补齐两个短板：

1. 原实现只判断星期几，**不认法定节假日**（春节照样开工）；这里内置节假日表并支持外部覆盖；
2. 原实现没有「集合竞价 / 尾盘集合竞价」的细分时段，而这两个时段的下单规则完全不同。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------- 时段常量
AUCTION_OPEN_START = time(9, 15)
AUCTION_OPEN_END = time(9, 25)
MORNING_START = time(9, 30)
MORNING_END = time(11, 30)
AFTERNOON_START = time(13, 0)
AFTERNOON_END = time(14, 57)
AUCTION_CLOSE_START = time(14, 57)
AUCTION_CLOSE_END = time(15, 0)


class Session(str, Enum):
    CLOSED = "CLOSED"                 # 非交易日或盘外
    PRE_OPEN = "PRE_OPEN"             # 交易日 00:00-09:15
    AUCTION_OPEN = "AUCTION_OPEN"     # 09:15-09:25 开盘集合竞价
    PRE_TRADE = "PRE_TRADE"           # 09:25-09:30 静默
    MORNING = "MORNING"               # 09:30-11:30
    LUNCH = "LUNCH"                   # 11:30-13:00
    AFTERNOON = "AFTERNOON"           # 13:00-14:57
    AUCTION_CLOSE = "AUCTION_CLOSE"   # 14:57-15:00 收盘集合竞价
    POST_CLOSE = "POST_CLOSE"         # 15:00 之后

    @property
    def is_continuous(self) -> bool:
        """是否为连续竞价时段（可挂限价单并即时撮合）。"""
        return self in (Session.MORNING, Session.AFTERNOON)

    @property
    def is_tradable(self) -> bool:
        """是否可以提交委托（含集合竞价）。"""
        return self in (
            Session.AUCTION_OPEN,
            Session.MORNING,
            Session.AFTERNOON,
            Session.AUCTION_CLOSE,
        )


# 内置法定节假日（休市日，不含周末）。数据来源：国务院办公厅放假安排。
# 外部可通过 config/holidays.txt（每行 YYYY-MM-DD）覆盖或追加。
_BUILTIN_HOLIDAYS: dict[int, list[str]] = {
    2024: [
        "2024-01-01",
        "2024-02-09", "2024-02-12", "2024-02-13", "2024-02-14", "2024-02-15", "2024-02-16",
        "2024-04-04", "2024-04-05",
        "2024-05-01", "2024-05-02", "2024-05-03",
        "2024-06-10",
        "2024-09-16", "2024-09-17",
        "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",
    ],
    2025: [
        "2025-01-01",
        "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04",
        "2025-04-04",
        "2025-05-01", "2025-05-02", "2025-05-05",
        "2025-06-02",
        "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08",
    ],
    2026: [
        "2026-01-01", "2026-01-02",
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
        "2026-04-06",
        "2026-05-01",
        "2026-06-19",
        "2026-09-25",
        "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
    ],
}

# 调休上班日中恰好落在周末、但**证券市场依然休市**——A 股周末一律休市，
# 因此不需要 workday 表，只需 holidays。这点与「企业调休」不同，容易搞错。


@dataclass(frozen=True)
class TradingDayInfo:
    day: date
    is_trading: bool
    reason: str = ""


class TradingCalendar:
    """A 股交易日历。

    默认使用内置节假日表；若 ``holidays_file`` 存在则合并（文件优先，可用于纠错/扩展年份）。
    对于既无内置数据也无文件的年份，退化为「周一至周五皆为交易日」并在
    :meth:`has_data_for` 中返回 False，调用方应告警。
    """

    def __init__(self, holidays_file: Path | str | None = None, extra_holidays: set[date] | None = None):
        self._holidays: set[date] = set()
        self._years: set[int] = set()
        for year, items in _BUILTIN_HOLIDAYS.items():
            self._years.add(year)
            for s in items:
                self._holidays.add(date.fromisoformat(s))
        if holidays_file:
            path = Path(holidays_file)
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    d = date.fromisoformat(line)
                    self._holidays.add(d)
                    self._years.add(d.year)
        if extra_holidays:
            for d in extra_holidays:
                self._holidays.add(d)
                self._years.add(d.year)

    # ------------------------------------------------------------ 基础判断
    def has_data_for(self, year: int) -> bool:
        return year in self._years

    def is_trading_day(self, day: date | datetime | str) -> bool:
        d = _to_date(day)
        if d.weekday() >= 5:
            return False
        return d not in self._holidays

    def info(self, day: date | datetime | str) -> TradingDayInfo:
        d = _to_date(day)
        if d.weekday() >= 5:
            return TradingDayInfo(d, False, "周末")
        if d in self._holidays:
            return TradingDayInfo(d, False, "法定节假日")
        if not self.has_data_for(d.year):
            return TradingDayInfo(d, True, f"{d.year} 年节假日数据缺失，按工作日推断")
        return TradingDayInfo(d, True, "")

    # ------------------------------------------------------------ 时段
    def session_of(self, moment: datetime) -> Session:
        if not self.is_trading_day(moment.date()):
            return Session.CLOSED
        t = moment.time()
        if t < AUCTION_OPEN_START:
            return Session.PRE_OPEN
        if t < AUCTION_OPEN_END:
            return Session.AUCTION_OPEN
        if t < MORNING_START:
            return Session.PRE_TRADE
        if t < MORNING_END:
            return Session.MORNING
        if t < AFTERNOON_START:
            return Session.LUNCH
        if t < AUCTION_CLOSE_START:
            return Session.AFTERNOON
        if t < AUCTION_CLOSE_END:
            return Session.AUCTION_CLOSE
        return Session.POST_CLOSE

    def is_trading_time(self, moment: datetime) -> bool:
        return self.session_of(moment).is_continuous

    def can_submit_order(self, moment: datetime) -> bool:
        return self.session_of(moment).is_tradable

    # ------------------------------------------------------------ 日期推算
    def next_trading_day(self, day: date | datetime | str, n: int = 1) -> date:
        d = _to_date(day)
        step = 1 if n >= 0 else -1
        remaining = abs(n)
        while remaining > 0:
            d += timedelta(days=step)
            if self.is_trading_day(d):
                remaining -= 1
        return d

    def prev_trading_day(self, day: date | datetime | str, n: int = 1) -> date:
        return self.next_trading_day(day, -abs(n))

    def trading_days(self, start: date | str, end: date | str) -> list[date]:
        s, e = _to_date(start), _to_date(end)
        out: list[date] = []
        cur = s
        while cur <= e:
            if self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def count_trading_days(self, start: date | str, end: date | str) -> int:
        return len(self.trading_days(start, end))

    def align_to_trading_day(self, day: date | datetime | str, *, forward: bool = False) -> date:
        """把任意日期对齐到最近的交易日（默认向前找上一个交易日）。"""
        d = _to_date(day)
        if self.is_trading_day(d):
            return d
        return self.next_trading_day(d) if forward else self.prev_trading_day(d)


def _to_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


#: 默认日历实例
calendar = TradingCalendar()
