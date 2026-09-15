"""RC2 附带项验证：server.main 日志采集盲区已闭合。

后端经 `uvicorn server.main:app` 启动，不走 cli.py 的 setup_logging()。修复前
qmt_trade.* 与 server.* 都没有 handler，INFO 日志（调度器启动、补跑链路、每个任务
run_job→_fire 的结果 render）全部丢失，只有 WARNING+ 经 logging.lastResort 落到
stderr——排查“data_sync/reconcile 到底跑没跑、返回什么”因此成了盲区。

本脚本在同一进程内：
  [A] 修复前状态（尚未 import server.main）：INFO 丢失、WARNING 仍可见 → 坐实盲区；
  [B] import server.main 触发日志初始化后：qmt_trade.* 与 server.*（含 routers）的
      INFO 都被 console handler 采集 → 盲区闭合；
  [C] 两个命名空间共用同一 handler 且 propagate=False → 不重复输出、不污染根 logger。

全程 stdlib + 导入 server.main（已验证导入不碰生产库），绝不触发 lifespan/调度器。
"""
from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 让 import server.main 找得到项目根

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name} {extra}", file=sys.__stdout__)
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}", file=sys.__stdout__)
    return bool(cond)


def phase_a_blindspot() -> None:
    """修复前：qmt_trade/server 无 handler，INFO 经 lastResort 被丢，WARNING 才可见。"""
    print("\n[A] 修复前盲区复现（尚未 import server.main）", file=sys.__stdout__)
    q = logging.getLogger("qmt_trade")
    s = logging.getLogger("server")
    check("qmt_trade 无 handler（未初始化）", len(q.handlers) == 0, str(q.handlers))
    check("server 无 handler（未初始化）", len(s.handlers) == 0, str(s.handlers))

    err = io.StringIO()
    saved = sys.stderr
    sys.stderr = err
    try:
        logging.getLogger("qmt_trade.scheduler.runner").info("A_INFO_LOST_MARKER")
        logging.getLogger("qmt_trade.scheduler.runner").warning("A_WARN_SHOWN_MARKER")
    finally:
        sys.stderr = saved
    captured = err.getvalue()
    check("INFO 被 lastResort 丢弃（盲区）", "A_INFO_LOST_MARKER" not in captured)
    check("WARNING 仍经 lastResort 落 stderr", "A_WARN_SHOWN_MARKER" in captured)


def phase_b_after_fix() -> str:
    """import server.main 触发日志初始化，返回采集到的 stdout 文本。"""
    print("\n[B] import server.main 后：INFO 被采集（盲区闭合）", file=sys.__stdout__)
    out = io.StringIO()
    saved = sys.stdout
    sys.stdout = out                       # setup_logging 的 console handler 绑定此刻 stdout
    try:
        import server.main                 # noqa: F401  触发模块级日志初始化
        logging.getLogger("qmt_trade.scheduler.runner").info("B_QMT_INFO_MARKER data_sync ok")
        logging.getLogger("qmt_trade.ops.monitor").info("B_QMT_MONITOR_MARKER heartbeat")
        logging.getLogger("server.main").info("B_SERVER_MAIN_MARKER 常驻调度器已启动")
        logging.getLogger("server.routers.selection").info("B_ROUTER_MARKER 补跑 selection")
    finally:
        sys.stdout = saved
    return out.getvalue()


def main() -> int:
    phase_a_blindspot()

    captured = phase_b_after_fix()
    check("qmt_trade.* INFO 被采集", "B_QMT_INFO_MARKER" in captured)
    check("qmt_trade.ops.monitor INFO 被采集", "B_QMT_MONITOR_MARKER" in captured)
    check("server.main INFO 被采集（调度器启动可见）", "B_SERVER_MAIN_MARKER" in captured)
    check("server.routers.* INFO 被采集（补跑链路可见）", "B_ROUTER_MARKER" in captured)

    print("\n[C] 命名空间配置正确、无重复输出", file=sys.__stdout__)
    q = logging.getLogger("qmt_trade")
    s = logging.getLogger("server")
    root = logging.getLogger()
    check("qmt_trade 已挂 handler", len(q.handlers) >= 1, str(len(q.handlers)))
    check("qmt_trade level=INFO", q.level == logging.INFO, str(q.level))
    check("qmt_trade propagate=False（不污染根）", q.propagate is False)
    check("server 已挂 handler", len(s.handlers) >= 1, str(len(s.handlers)))
    check("server level=INFO", s.level == logging.INFO, str(s.level))
    check("server propagate=False（不污染根）", s.propagate is False)
    check("server 与 qmt_trade 复用同一 handler 实例",
          all(h in q.handlers for h in s.handlers), str(s.handlers))
    # 每条 marker 只应出现一次（两命名空间各自独立、propagate=False 不回根）
    check("无重复输出（marker 各出现一次）",
          captured.count("B_QMT_INFO_MARKER") == 1
          and captured.count("B_SERVER_MAIN_MARKER") == 1,
          f"qmt={captured.count('B_QMT_INFO_MARKER')} srv={captured.count('B_SERVER_MAIN_MARKER')}")
    check("根 logger 未被强挂业务 handler（保持干净）",
          not any(getattr(h, "formatter", None) and
                  "trace_id" in (h.formatter._fmt or "") for h in root.handlers),
          str(root.handlers))

    print("\n" + "=" * 52, file=sys.__stdout__)
    print(f"结果: PASS={PASS} FAIL={FAIL}", file=sys.__stdout__)
    print("=" * 52, file=sys.__stdout__)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
