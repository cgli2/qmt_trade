"""路由包。main.py 通过 ``from server.routers import (...)`` 聚合挂载。"""

from . import (backtest, config, datasource, event, llm, market, notify,
              overview, report, risk, selection, strategy, strategylab,
              trade, tail_pick)

__all__ = ["backtest", "config", "datasource", "event", "llm",
           "market", "notify", "overview", "report", "risk", "selection",
           "strategy", "strategylab", "trade", "tail_pick"]
