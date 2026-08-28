"""日志封装。借鉴 qmt_etf/utils/log_utils.py 的 LogManager，做了三处改进：

1. 增加 ``trace_id`` 上下文，一次决策链路的所有日志可串起来（P6 可审计）；
2. 按日切分 + 分级文件（error 单独一份，便于告警抓取）；
3. 幂等初始化，重复调用不会重复挂 handler（原实现会重复输出）。
"""

from __future__ import annotations

import contextvars
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")
_CONFIGURED = False

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(trace_id)s | %(name)s | %(message)s"


class _TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id.get()
        return True


def set_trace_id(trace_id: str) -> contextvars.Token:
    """设置当前上下文的追踪 ID，返回 token 供 ``reset_trace_id`` 还原。"""
    return _trace_id.set(trace_id)


def reset_trace_id(token: contextvars.Token) -> None:
    _trace_id.reset(token)


def get_trace_id() -> str:
    return _trace_id.get()


class trace_context:
    """``with trace_context("plan-20260808"): ...`` 内的所有日志自动带上该 ID。"""

    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        self._token: contextvars.Token | None = None

    def __enter__(self) -> str:
        self._token = set_trace_id(self.trace_id)
        return self.trace_id

    def __exit__(self, *exc) -> None:
        if self._token is not None:
            reset_trace_id(self._token)


def setup_logging(
    log_dir: Path | str | None = None,
    level: str | int = "INFO",
    *,
    console: bool = True,
    force: bool = False,
) -> logging.Logger:
    """初始化根日志。幂等：重复调用不会重复添加 handler。"""
    global _CONFIGURED
    root = logging.getLogger("qmt_trade")
    if _CONFIGURED and not force:
        return root
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.setLevel(level if isinstance(level, int) else str(level).upper())
    root.propagate = False
    formatter = logging.Formatter(LOG_FORMAT)
    trace_filter = _TraceFilter()

    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(formatter)
        sh.addFilter(trace_filter)
        root.addHandler(sh)

    if log_dir is not None:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(
            d / "qmt_trade.log", when="midnight", backupCount=60, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        fh.addFilter(trace_filter)
        root.addHandler(fh)

        eh = TimedRotatingFileHandler(
            d / "error.log", when="midnight", backupCount=60, encoding="utf-8"
        )
        eh.setLevel(logging.ERROR)
        eh.setFormatter(formatter)
        eh.addFilter(trace_filter)
        root.addHandler(eh)

    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    """获取子 logger。``name`` 建议用模块名，如 ``execution.guard``。"""
    if not _CONFIGURED:
        setup_logging(console=True)
    full = name if name.startswith("qmt_trade") else f"qmt_trade.{name}"
    logger = logging.getLogger(full)
    return logger
