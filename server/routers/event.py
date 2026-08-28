"""事件驱动管理：新闻/公司事件浏览 + 硬负面事件（规则先行一票否决）统计。

系统没有独立的"事件驱动交易"模块——事件/新闻经因子层（news_sentiment_*、
event_sentiment_20d、hard_negative_flag）与硬负面否决融入决策。本路由把它暴露为
可浏览、可检索的面板，便于人工审视"事件驱动"影响。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

import server.context as ctx
from qmt_trade.datahub.types import HARD_NEGATIVE_EVENTS

router = APIRouter(prefix="/event", tags=["event"])


def _ctx(mode: str = Query("paper")):
    # 只读浏览类接口，live 观察期锁定时自动降级 paper（不拦用户）
    return ctx.make_ctx_research(mode)


def _split(symbols):
    return [s.strip() for s in (symbols or "").split(",") if s.strip()]


@router.get("/news")
def news(symbols: str | None = None, start: str | None = None,
         end: str | None = None, limit: int = 100, mode: str = Query("paper")):
    c = _ctx(mode)
    items = c.hub.get_news(
        _split(symbols),
        date.fromisoformat(start) if start else None,
        date.fromisoformat(end) if end else None,
        limit=limit,
    )
    return {"news": [it.to_row() for it in (items or [])],
            "hard_negative_categories": [e.value for e in HARD_NEGATIVE_EVENTS]}


@router.get("/events")
def events(symbols: str | None = None, start: str | None = None,
           end: str | None = None, limit: int = 100, mode: str = Query("paper")):
    c = _ctx(mode)
    items = c.hub.get_events(
        _split(symbols),
        date.fromisoformat(start) if start else None,
        date.fromisoformat(end) if end else None,
    )
    items = (items or [])[:limit]
    out = []
    for it in items:
        out.append({
            "id": it.id, "symbol": it.symbol, "category": it.category.value,
            "title": it.title, "ann_time": it.ann_time.isoformat(),
            "importance": it.importance, "sentiment": it.sentiment,
            "is_hard_negative": it.is_hard_negative,
        })
    return {"events": out,
            "hard_negative_categories": [e.value for e in HARD_NEGATIVE_EVENTS]}


@router.get("/hard-negatives")
def hard_negatives(symbols: str | None = None, start: str | None = None,
                  end: str | None = None, mode: str = Query("paper")):
    """只列出命中"硬负面"的事件——这些会触发规则先行减仓，不等 LLM。"""
    c = _ctx(mode)
    items = c.hub.get_events(
        _split(symbols),
        date.fromisoformat(start) if start else None,
        date.fromisoformat(end) if end else None,
    )
    items = (items or [])[:500]
    out = [{
        "id": it.id, "symbol": it.symbol, "category": it.category.value,
        "title": it.title, "ann_time": it.ann_time.isoformat(),
        "sentiment": it.sentiment,
    } for it in (items or []) if it.is_hard_negative]
    return {"hard_negatives": out, "count": len(out)}
