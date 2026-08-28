"""系统总览 / 健康体检 / 总开关 / 调度任务 / 密钥管理。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

import server.context as ctx
from server.context import load_settings_editor, save_settings
from server.schemas import KillSwitchAction, SecretIn

router = APIRouter(tags=["overview"])


def _ctx(mode: str = Query("paper")):
    return ctx.make_ctx(mode)


@router.get("/overview")
def overview(mode: str = Query("paper")):
    c = _ctx(mode)
    ks = c.killswitch.to_dict()
    return {
        "mode": c.mode,
        "is_live": c.is_live,
        "db_path": str(c.db.path) if hasattr(c.db, "path") else None,
        "data_dir": str(c.settings.data_dir),
        "killswitch": ks,
        "llm_enabled": bool(c.brain is not None),
    }


@router.get("/health")
def health(mode: str = Query("paper"), notify: bool = False):
    c = _ctx(mode)
    rep = c.monitor.check(notify=notify)
    checks = []
    # HealthReport 的检查项字段是 results（曾误写为 checks 导致前端体检表恒空）
    for chk in rep.results:
        checks.append({
            "name": chk.name, "ok": chk.ok,
            "level": chk.level.name,
            "message": chk.message,
        })
    # 最近任务执行情况
    rows = []
    for name in ("data_sync", "selection", "research", "intraday",
                 "reconcile", "review"):
        last = c.repos.system.get(f"job:{name}:last_run") or "-"
        status = c.repos.system.get(f"job:{name}:last_status") or "-"
        rows.append({"name": name, "status": status, "last_run": last})
    return {
        "healthy": rep.healthy,
        "degraded": rep.degraded,
        "degrade_reasons": rep.degrade_reasons,
        "rendered": rep.render(),
        "checks": checks,
        "killswitch": c.killswitch.to_dict(),
        "recent_jobs": rows,
    }


@router.get("/killswitch")
def get_killswitch(mode: str = Query("paper")):
    return _ctx(mode).killswitch.to_dict()


@router.post("/killswitch")
def post_killswitch(body: KillSwitchAction, mode: str = Query("paper")):
    ks = _ctx(mode).killswitch
    a = body.action
    if a == "engage":
        ks.engage(body.reason or "Web 控制台手动降级", manual=True)
    elif a == "flatten":
        ks.flatten(body.reason or "Web 控制台强制平仓", manual=True)
    elif a == "reset":
        ks.reset(body.reason or "人工恢复")
    elif a == "status":
        pass
    else:
        raise HTTPException(400, f"未知 action: {a}")
    return ks.to_dict()


@router.get("/scheduler/jobs")
def scheduler_jobs(mode: str = Query("paper")):
    from qmt_trade.scheduler.jobs import JobRunner
    from qmt_trade.scheduler.runner import _DOW, TradingScheduler, next_run_at

    c = _ctx(mode)
    sched = TradingScheduler(JobRunner(c), c.settings)
    out = []
    now = datetime.now()
    for spec in sched.specs:
        nxt = next_run_at(spec, now)
        item = {
            "name": spec.name,
            "kind": spec.kind,                          # cron | interval
            "label": spec.time_label,                   # 人类可读的执行计划
            "cron": spec.cron_expr(),                   # 标准 cron（interval 为空）
            "description": spec.description,            # 中文用途说明
            "next_run": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None,
        }
        if spec.kind == "interval":
            item.update({
                "seconds": spec.seconds,
                "start": spec.start_time.strftime("%H:%M") if spec.start_time else None,
                "end": spec.end_time.strftime("%H:%M") if spec.end_time else None,
            })
        else:
            item.update({
                "hour": spec.hour,
                "minute": spec.minute,
                "day_of_week": _DOW.index(spec.day_of_week) if spec.day_of_week else None,
            })
        out.append(item)
    return {"schedule_text": sched.describe(), "jobs": out}


class JobScheduleIn(BaseModel):
    """调度时刻编辑。cron 任务用 time（evolve 另带 day_of_week）；
    intraday 用 interval_seconds + 窗口 start/end。"""
    name: str
    time: str | None = None                 # "HH:MM"
    day_of_week: int | None = None          # 0=周一 … 6=周日（仅 evolve）
    interval_seconds: int | None = None
    start: str | None = None
    end: str | None = None


def _parse_hm_strict(text: str, field_name: str) -> str:
    """把 "HH:MM" 校验后规范化；非法直接 400，绝不静默兜底。"""
    parts = str(text).split(":")
    if len(parts) != 2:
        raise HTTPException(400, f"{field_name} 必须是 HH:MM 格式，收到 {text!r}")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise HTTPException(400, f"{field_name} 必须是 HH:MM 格式，收到 {text!r}")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise HTTPException(400, f"{field_name} 超出范围（{h:02d}:{m:02d}）")
    return f"{h:02d}:{m:02d}"


@router.put("/scheduler/job")
def scheduler_update_job(body: JobScheduleIn, request: Request):
    """修改某个调度任务的执行时刻，写 settings.yaml 并热更新常驻调度器。"""
    from qmt_trade.scheduler.runner import _DOW

    name = body.name
    cfg_key = "llm_research" if name == "research" else name
    s = load_settings_editor()

    if name == "intraday":
        seconds = body.interval_seconds
        if seconds is not None and not (1 <= int(seconds) <= 3600):
            raise HTTPException(400, "巡检间隔须在 1~3600 秒之间")
        start = _parse_hm_strict(body.start, "开始时间") if body.start else None
        end = _parse_hm_strict(body.end, "结束时间") if body.end else None
        if start and end and start >= end:
            raise HTTPException(400, f"执行窗口非法：开始 {start} 不早于结束 {end}")
        if seconds is not None:
            s.set("scheduler.jobs.intraday_interval_seconds", int(seconds))
        if start:
            s.set("scheduler.jobs.intraday_start", start)
        if end:
            s.set("scheduler.jobs.intraday_end", end)
    elif name == "evolve":
        hm = _parse_hm_strict(body.time, "执行时间")
        if body.day_of_week is not None and not (0 <= int(body.day_of_week) <= 6):
            raise HTTPException(400, f"星期序号须在 0~6 之间（0=周一），收到 {body.day_of_week}")
        s.set(f"scheduler.jobs.{cfg_key}", hm)
        if body.day_of_week is not None:
            s.set("scheduler.jobs.evolve_weekday", int(body.day_of_week))
    else:
        if name not in ("data_sync", "regime", "selection", "research", "plan",
                        "auction_check", "reconcile", "review",
                        "tail_pick_select", "tail_pick_exit"):
            raise HTTPException(400, f"未知调度任务：{name}")
        hm = _parse_hm_strict(body.time, "执行时间")
        s.set(f"scheduler.jobs.{cfg_key}", hm)

    save_settings(s)

    # 热更新常驻调度器；拿不到实例（非 lifespan 启动）时提示重启即可
    reloaded = False
    sched = getattr(getattr(request.app, "state", None), "scheduler", None)
    if sched is not None:
        try:
            reloaded = sched.reload()
        except Exception as exc:                         # noqa: BLE001
            raise HTTPException(500, f"配置已保存但调度器热更新失败: {exc}")
    return {"ok": True, "name": name, "reloaded": reloaded,
            "hint": "已生效" if reloaded else "配置已保存，重启后端后生效"}


@router.post("/scheduler/run")
def scheduler_run_once(name: str, mode: str = Query("paper"),
                       trade_date: str | None = None):
    from qmt_trade.scheduler.jobs import JobRunner, run_job

    c = _ctx(mode)
    runner = JobRunner(c, trade_date=trade_date)
    res = run_job(runner, name)
    return {"ok": res.ok, "name": res.name, "reason": res.reason,
            "elapsed": res.elapsed, "rendered": res.render(),
            "data": res.data}


@router.get("/secrets")
def secrets():
    return ctx.list_secrets()


@router.put("/secrets")
def put_secret(body: SecretIn):
    ok = ctx.set_secret(body.key, body.value)
    if not ok:
        raise HTTPException(400, f"不允许写入未知密钥 {body.key}")
    return {"ok": True, "key": body.key}
