"""B 方案验证：job 心跳 + data_freshness 跨 mode 共享（修 RC1 回归）。

复现根因场景——常驻调度器跑在 paper schema、前端体检读 live schema，二者
物理隔离导致调度器写的心跳 live 永远看不见，纯 cron 任务被误判“失联”→ 拉闸。

验证修复后：
  1. paper 调度器写的 job:* 心跳，live 体检能读到（不再误判失联）；
  2. job 心跳过期仍会被判失联（阈值未削弱，保护仍在）；
  3. watchdog 等非 job 心跳只留本 mode，不跨 mode 污染；
  4. data:freshness 由 data_sync 写入共享库，live 体检读到同一水位；
  5. 共享水位过期 → freshness 报警（保护未削弱）；
  6. 无共享水位时 fallback 到本 mode 快照（向后兼容）；
  7. 不注入 shared_repos 时退回本 mode 库（旧行为不回归）。

全程用临时 DuckDB 文件 / 内存库，绝不触碰生产 data/db/qmt.duckdb。
"""
from __future__ import annotations

import logging
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qmt_trade.ops.monitor import HealthMonitor              # noqa: E402
from qmt_trade.storage.db import Database                    # noqa: E402
from qmt_trade.storage.models import Repos                   # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("verify_b")

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        logger.info("  [OK]   %s %s", name, extra)
    else:
        FAIL += 1
        logger.info("  [FAIL] %s %s", name, extra)
    return bool(cond)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="verify_b_")) / "qmt.duckdb"
    # 同一物理文件、两个 schema：完全复现生产的 paper/live 隔离。
    # shared_repos 对 paper 与 live 都指向 paper schema（见 app.py:154-160）。
    shared_repos = Repos.create(Database(str(tmp), schema="trading_paper_test"))
    live_repos = Repos.create(Database(str(tmp), schema="trading_live_test"))

    logger.info("\n[1] paper 调度器写 job 心跳 → 落共享库(paper schema)")
    mon_paper = HealthMonitor(repos=shared_repos, shared_repos=shared_repos)
    mon_paper.heartbeat("job:data_sync")
    mon_paper.heartbeat("job:tail_pick_select")
    mon_paper.heartbeat("job:evolve")
    check("共享库有 hb:job:data_sync",
          shared_repos.system.get("hb:job:data_sync") is not None)

    logger.info("\n[2] live 体检回读 → 看见 paper 写的 job 心跳（RC1 修复核心）")
    mon_live = HealthMonitor(repos=live_repos, shared_repos=shared_repos)
    beats = mon_live._read_db_beats()
    check("live 读到 job:data_sync", "job:data_sync" in beats)
    check("live 读到 job:tail_pick_select", "job:tail_pick_select" in beats)
    check("live 读到 job:evolve", "job:evolve" in beats)
    check("live 本 mode 库未被写入 job 心跳（证明读的是共享库而非本 mode）",
          live_repos.system.get("hb:job:data_sync") is None)

    logger.info("\n[3] _check_heartbeats：job 心跳新鲜 → 不报失联")
    r = mon_live._check_heartbeats()
    check("heartbeat 检查通过", r.ok, r.message)

    logger.info("\n[4] 让 job:data_sync 心跳过期 → 应报失联（阈值仍生效）")
    stale = time.time() - 90000                      # > cron_heartbeat_seconds(86400)
    shared_repos.system.set("hb:job:data_sync", f"{stale:.0f}")
    r2 = HealthMonitor(repos=live_repos, shared_repos=shared_repos)._check_heartbeats()
    check("过期 job 被判失联", (not r2.ok) and "job:data_sync" in r2.message, r2.message)
    shared_repos.system.set("hb:job:data_sync", f"{time.time():.0f}")   # 复原

    logger.info("\n[5] watchdog 等非 job 心跳 → 只写本 mode，不污染共享库")
    mon_live3 = HealthMonitor(repos=live_repos, shared_repos=shared_repos)
    mon_live3.heartbeat("watchdog:intraday")
    check("watchdog 写入 live 本 mode 库",
          live_repos.system.get("hb:watchdog:intraday") is not None)
    check("watchdog 未写入共享库",
          shared_repos.system.get("hb:watchdog:intraday") is None)

    logger.info("\n[6] data:freshness 共享水位 → live 体检读到同一新鲜度")
    shared_repos.system.set("data:freshness", date.today().isoformat())
    rf = mon_live3._check_data_freshness()
    check("freshness 通过(读共享水位)", rf.ok, rf.message)
    check("freshness 来源标记 data_sync",
          rf.detail.get("src") == "data_sync", str(rf.detail))

    logger.info("\n[7] 共享水位过期(落后 35 天) → freshness 报警(保护未削弱)")
    shared_repos.system.set("data:freshness",
                            (date.today() - timedelta(days=35)).isoformat())
    rf2 = HealthMonitor(repos=live_repos, shared_repos=shared_repos)._check_data_freshness()
    check("落后 35 天被判 ERROR", (not rf2.ok) and "35" in rf2.message, rf2.message)

    logger.info("\n[8] 无共享水位时 fallback 到本 mode 快照(向后兼容)")
    fb_repos = Repos.create(Database(":memory:"))
    fb_repos.snapshots.save(date.today() - timedelta(days=10), total_asset=1e6,
                            cash=5e5, market_value=5e5, position_count=1, regime="RANGE")
    rf3 = HealthMonitor(repos=fb_repos)._check_data_freshness()   # 不传 shared_repos
    check("fallback 读快照仍报落后 10 天",
          (not rf3.ok) and "10" in rf3.message, rf3.message)

    logger.info("\n[9] 向后兼容：不注入 shared_repos → 退回本 mode 库")
    solo = Repos.create(Database(":memory:"))
    mon_solo = HealthMonitor(repos=solo)
    check("shared_repos 默认等于 repos", mon_solo.shared_repos is solo)
    mon_solo.heartbeat("job:regime")
    check("job 心跳写入本 mode 库", solo.system.get("hb:job:regime") is not None)

    logger.info("\n%s\n结果: PASS=%d FAIL=%d\n%s", "=" * 52, PASS, FAIL, "=" * 52)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
