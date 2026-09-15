"""RC2 剩余项验证：CRITICAL_JOBS 的 misfire 改为「补跑本体」而非只补心跳。

场景——机器休眠/卡顿使 cron 触发点超过宽限期，APScheduler 丢弃该次执行并派发
EVENT_JOB_MISSED。修复前：一律只补心跳，导致关键任务（如 data_sync 预热行情）
明明没跑，体检却看着"活着"，数据缺口被掩盖。修复后：
  1. 关键任务（data_sync/reconcile/intraday）错过 → 补跑本体（_guarded_fire/_fire）；
  2. 非关键任务（research/plan/evolve 等）错过 → 仍只补心跳（避免错误时点副作用）；
  3. 空 job_id / 异常都被吞掉，绝不把回调异常抛回 APScheduler 线程。

全程用桩对象，绝不触碰生产库、绝不真正下载行情或下单。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qmt_trade.scheduler.jobs import CRITICAL_JOBS          # noqa: E402
from qmt_trade.scheduler.runner import TradingScheduler     # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("verify_rc2")

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


class FakeRunner:
    """只记录 _beat 调用，不跑任何真实任务。"""

    def __init__(self):
        self.beats: list[str] = []

    def _beat(self, name: str) -> None:               # noqa: D401
        self.beats.append(name)


class FakeEvent:
    def __init__(self, job_id):
        self.job_id = job_id


def make_sched():
    """绕过 __init__ 造一个只含回调所需属性的调度器，_fire 用桩记录调用。"""
    sched = TradingScheduler.__new__(TradingScheduler)
    sched.runner = FakeRunner()
    sched.fires: list[str] = []

    def _fake_fire(name: str):
        sched.fires.append(name)
    sched._fire = _fake_fire                           # type: ignore[attr-assignment]
    return sched


def main() -> int:
    logger.info("CRITICAL_JOBS = %s", sorted(CRITICAL_JOBS))

    logger.info("\n[1] 关键任务 data_sync 被错过 → 补跑本体，不走只补心跳分支")
    s = make_sched()
    s._on_job_missed(FakeEvent("data_sync"))
    check("data_sync 触发 _fire（补跑本体）", s.fires == ["data_sync"], str(s.fires))
    check("data_sync 未走 _beat-only 分支", s.runner.beats == [], str(s.runner.beats))

    logger.info("\n[2] 关键任务 reconcile / intraday 被错过 → 均补跑本体")
    for job in ("reconcile", "intraday"):
        s = make_sched()
        s._on_job_missed(FakeEvent(job))
        check(f"{job} 触发 _fire", s.fires == [job], str(s.fires))
        check(f"{job} 未走 _beat-only 分支", s.runner.beats == [], str(s.runner.beats))

    logger.info("\n[3] 非关键任务被错过 → 只补心跳，绝不补跑本体")
    for job in ("research", "plan", "evolve", "selection", "review",
                "tail_pick_select", "etf_t0_intraday"):
        s = make_sched()
        s._on_job_missed(FakeEvent(job))
        check(f"{job} 只补心跳", s.runner.beats == [job], str(s.runner.beats))
        check(f"{job} 未补跑本体", s.fires == [], str(s.fires))

    logger.info("\n[4] 所有 CRITICAL_JOBS 都走补跑本体分支（全覆盖）")
    for job in sorted(CRITICAL_JOBS):
        s = make_sched()
        s._on_job_missed(FakeEvent(job))
        check(f"{job} ∈ CRITICAL → 补跑本体", s.fires == [job], str(s.fires))

    logger.info("\n[5] 空/None job_id → 提前返回，不补跑也不补心跳")
    for bad in ("", None):
        s = make_sched()
        s._on_job_missed(FakeEvent(bad))
        check(f"job_id={bad!r} 无副作用",
              s.fires == [] and s.runner.beats == [],
              f"fires={s.fires} beats={s.runner.beats}")

    logger.info("\n[6] 回调内部异常被吞掉，不抛回 APScheduler 线程")
    s = make_sched()

    def _boom(name: str):
        raise RuntimeError("补跑炸了")
    s._fire = _boom                                    # type: ignore[attr-assignment]
    try:
        s._on_job_missed(FakeEvent("data_sync"))       # 关键任务 → _guarded_fire → _fire
        raised = False
    except Exception:                                  # noqa: BLE001
        raised = True
    check("补跑本体抛异常时回调不向外传播", not raised)

    logger.info("\n%s\n结果: PASS=%d FAIL=%d\n%s", "=" * 52, PASS, FAIL, "=" * 52)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
