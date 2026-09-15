"""回归守卫：live ctx 的 shared_repos 必须真正指向 paper 共享库。

根因（静默失效，无异常无锁错误）：``ctx.monitor`` 属性构造 HealthMonitor 时
实参按源码顺序求值——``repos=self.repos`` 先于 ``shared_repos=self.shared_repos``。
``repos`` 属性会惰性把 ``self._repos`` 从 None 置为新建的 live Repos；随后求值
``shared_repos`` 时，其守卫 ``if self._repos is not None`` 误把"刚被惰性创建的
_repos"当成"外部注入的 repos"，于是短路 ``self._shared_repos = self.repos``，
live 的 shared_repos 塌缩回 live schema，``self._shared_db`` 永远为 None。

后果：常驻调度器（paper）通过 ``_beat_all`` 写入 paper schema 的 job:* 心跳，
live 体检从 live schema 回读——那里只有陈旧残留（data_sync 停在 2026-08-11、
tail_pick_select/evolve），纯 cron 任务被误判"失联"→ KillSwitch 误拉闸
REDUCE_ONLY，全天禁止开仓。

全程用临时 DuckDB 文件（QMT_RUNTIME_DB），绝不触碰生产 data/db/qmt.duckdb。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qmt_trade.app import build_context                     # noqa: E402
from qmt_trade.storage.db import Database                   # noqa: E402
from qmt_trade.storage.models import Repos                  # noqa: E402

_JOB_NAMES = ("data_sync", "evolve", "tail_pick_select")


@pytest.fixture()
def runtime_env(monkeypatch, tmp_path):
    """把 runtime 库指向本用例独占的临时文件，并给 live 一个测试账本号。

    同一物理文件 + 按 mode 分 schema，与生产 data/db/qmt.duckdb 拓扑同构。
    """
    monkeypatch.setenv("QMT_RUNTIME_DB", str(tmp_path / "runtime.duckdb"))
    monkeypatch.setenv("QMT_ACCOUNT_ID", "wiring-test-account")
    monkeypatch.setenv("QMT_ALLOW_LIVE", "1")
    yield


def test_live_shared_repos_not_collapsed_when_repos_touched_first(runtime_env):
    """复刻 monitor 属性的求值顺序：先碰 repos，再取 shared_repos 不能塌缩。"""
    ctx = build_context("live")
    try:
        _ = ctx.repos                    # monitor 里 repos=self.repos 先求值
        shared = ctx.shared_repos        # 随后 shared_repos=self.shared_repos

        assert ctx._shared_db is not None, (
            "live shared_repos 未另开 paper 共享库：_repos 被惰性设置后，"
            "守卫误判为外部注入而塌缩回 live schema"
        )
        assert shared is not ctx.repos, "live shared_repos 塌缩回了 live repos"
        assert shared.db is not ctx.repos.db
        assert ctx.repos.db.schema.startswith("trading_live_"), ctx.repos.db.schema
        assert shared.db.schema.startswith("trading_paper_"), shared.db.schema
    finally:
        ctx.close()


def test_live_monitor_wiring_opens_paper_shared_db(runtime_env):
    """生产真实路径：ctx.monitor 构造后，monitor 拿到的 shared_repos 必须是 paper 库。"""
    ctx = build_context("live")
    try:
        mon = ctx.monitor
        assert ctx._shared_db is not None, "monitor 接线使 live shared_repos 塌缩"
        assert mon.shared_repos is not mon.repos, (
            "monitor.shared_repos 与 repos 指向同一 live 库（应为不同 schema）"
        )
        assert mon.shared_repos.db.schema.startswith("trading_paper_"), (
            mon.shared_repos.db.schema
        )
        assert mon.repos.db.schema.startswith("trading_live_"), mon.repos.db.schema
    finally:
        ctx.close()


def test_live_reads_paper_job_heartbeat(runtime_env):
    """行为级守卫：paper 调度器写的 job:* 心跳，live 体检必须回读得到。"""
    paper = build_context("paper")
    live = None
    try:
        now = time.time()
        # 模拟常驻 paper 调度器 _beat_all：把 job 心跳写进 paper schema
        for name in _JOB_NAMES:
            paper.repos.system.set(f"hb:job:{name}", f"{now:.0f}")

        live = build_context("live")
        _ = live.monitor                 # 触发生产接线
        beats = live.monitor._read_db_beats()
        for name in _JOB_NAMES:
            assert f"job:{name}" in beats, (
                f"live 读不到 paper 写入的 job:{name} 心跳——"
                f"shared_repos 仍指向 live schema（读到={sorted(beats)}）"
            )
    finally:
        if live is not None:
            live.close()
        paper.close()


def test_injected_repos_and_paper_share_self(runtime_env):
    """向后兼容：外部注入 repos / paper 模式，shared_repos 仍等于自身，不另开库。"""
    # 外部注入 repos（测试/回测路径）
    injected = Repos.create(Database(":memory:", schema="trading_paper_inj"))
    ctx_inj = build_context("live", repos=injected)
    try:
        assert ctx_inj.shared_repos is injected
        assert ctx_inj._shared_db is None
    finally:
        ctx_inj.close()

    # paper 模式：共享库就是自己
    ctx_paper = build_context("paper")
    try:
        assert ctx_paper.shared_repos is ctx_paper.repos
        assert ctx_paper._shared_db is None
    finally:
        ctx_paper.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
