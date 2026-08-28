"""短期 / 长期记忆对外接口。

记忆由盘后自我复盘（review 任务）自动沉淀：
- 短期记忆：按日落库 ``reflection:short_term:<date>``，是「明日待办」动作清单；
- 长期记忆：累积落库 ``reflection:long_term``（JSON 列表），跨日去重、按出现频次排序。

- ``GET /memory``：返回最新短期记忆 + 长期记忆，供用户随时预览。
"""

from __future__ import annotations

import json
import logging
from fastapi import APIRouter, Query

import server.context as ctx
from qmt_trade.evolution.reflection import load_long_term

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/memory", tags=["memory"])


def _ctx(mode: str = Query("paper")):
    # 记忆预览属只读研究类，live 观察期锁定时自动降级 paper（不拦用户）
    return ctx.make_ctx_research(mode)


@router.get("")
def get_memory(mode: str = Query("paper")):
    """返回最新短期记忆（最新一日的待办）与长期记忆（持久原则）。"""
    c = _ctx(mode)
    sys_repo = c.shared_repos.system

    # 最新短期记忆：取 key 字典序最大的 reflection:short_term:*（日期在末尾）
    st_items: list[str] = []
    st_date = ""
    try:
        keys = sys_repo.list_keys("reflection:short_term:")
        if keys:
            latest = max(keys)                 # 形如 reflection:short_term:YYYY-MM-DD
            st_date = latest.rsplit(":", 1)[-1]
            raw = sys_repo.get(latest)
            items = json.loads(raw) if raw else []
            if isinstance(items, list):
                st_items = [str(x) for x in items]
    except Exception as exc:                  # noqa: BLE001
        logger.warning("读取短期记忆失败: %s", exc)

    long_items: list[dict] = []
    try:
        long_items = [m.to_dict() for m in load_long_term(sys_repo.get("reflection:long_term"))]
    except Exception as exc:                  # noqa: BLE001
        logger.warning("读取长期记忆失败: %s", exc)

    return {
        "short_term": {"date": st_date, "items": st_items},
        "long_term": long_items,
    }
