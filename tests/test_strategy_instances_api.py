"""策略页「编辑运行实例 → 改字段 → 保存/应用」的接口回归。

复现：编辑已有实例、修改一个字段后点保存/应用会 500（后端 NameError）。
"""
from types import SimpleNamespace

import pytest

from qmt_trade.storage.db import Database
import server.routers.strategy as R
from server.schemas import StrategyVersionIn


@pytest.fixture()
def db(monkeypatch):
    database = Database()
    monkeypatch.setattr(
        R, "_ctx", lambda mode="paper": SimpleNamespace(repos=SimpleNamespace(db=database))
    )
    yield database
    database.close()


def _changed_value(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value / 2 if value else 0.5
    return value


def _sample_field():
    from qmt_trade.core.config import get_settings
    from qmt_trade.core.parameter_schema import defaults

    sid = "trend_buy"
    base = defaults(sid, get_settings())
    key = "max_positions" if "max_positions" in base else next(iter(base))
    return sid, key, _changed_value(base[key])


def test_save_draft_then_publish_and_rollback(db):
    sid, key, changed = _sample_field()

    draft = R.save_draft(StrategyVersionIn(strategy_id=sid, name="t", params={key: changed}), mode="paper")
    iid = draft["instance"]["id"]
    assert draft["instance"]["draft"]["params"][key] == changed

    pub = R.publish_version(
        StrategyVersionIn(strategy_id=sid, instance_id=iid, params={key: changed}), mode="paper"
    )
    assert pub["version"]["params"][key] == changed
    assert pub["instance"]["active_version"] == "v1"

    rolled = R.rollback(iid, "v1", mode="paper")
    assert rolled["instance"]["active_version"] == "v1"
