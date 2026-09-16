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
def overview(request: Request, mode: str = Query("paper")):
    c = _ctx(mode)
    ks = c.killswitch.to_dict()
    return {
        "mode": c.mode,
        "is_live": c.is_live,
        # 常驻调度器（自动交易/ETF T+0）实际运行的账本模式，与本请求的 mode 无关。
        # 二者不一致即「UI 看 live、自动交易跑 paper」的错位，前端据此告警。
        "scheduler_mode": getattr(request.app.state, "scheduler_mode", None),
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
    from qmt_trade.core.config import get_settings
    from qmt_trade.scheduler.jobs import JobRunner
    from qmt_trade.scheduler.runner import _DOW, TradingScheduler, next_run_at

    c = _ctx(mode)
    # 必须用新鲜的 settings：save_settings 落盘后只失效 get_settings() 单例，
    # 并不清 ctx 缓存（常驻调度器还持有旧实例），故 c.settings 可能是"改开关前"
    # 的旧快照 —— 拿它算启用状态会导致「策略已关，页面仍显示运行中」。
    try:
        settings = get_settings()
    except Exception:                              # noqa: BLE001
        settings = c.settings
    sched = TradingScheduler(JobRunner(c), settings)
    out = []
    now = datetime.now()
    for spec in sched.specs:
        # 停用的任务已从日程上摘除，根本不会触发，给出 next_run 是在骗用户
        nxt = next_run_at(spec, now) if spec.enabled else None
        item = {
            "name": spec.name,
            "kind": spec.kind,                          # cron | interval
            "label": spec.time_label,                   # 人类可读的执行计划
            "cron": spec.cron_expr(),                   # 标准 cron（interval 为空）
            "description": spec.description,            # 中文用途说明
            "next_run": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None,
            # ---- 启用状态：人工开关 AND 绑定策略门禁（runner.job_enabled）----
            "enabled": spec.enabled,
            "status": spec.status,                      # running | paused | disabled
            "status_detail": spec.status_detail,        # 停用原因，UI 挂 tooltip
            "bound_strategies": list(spec.bound_strategies),
            # 两道闸门分开给：编辑弹窗的状态开关只反映/只写 manual_enabled，
            # 策略门禁那半边是灰的（要去「策略实验室」开），前端据此禁用并说明。
            "manual_enabled": spec.manual_enabled,
            "strategy_gate": spec.strategy_gate,
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
    """调度任务编辑：执行时刻/窗口 + 人工启停开关。

    - cron 任务用 ``time``（``evolve`` 另带 ``day_of_week``）；
    - interval 任务用 ``interval_seconds`` + 窗口 ``start``/``end``；
    - ``enabled`` 是**人工开关**，与策略门禁 AND 后才是最终生效状态
      （见 ``runner.job_enabled``）。

    字段全部可选：只落盘传上来的那些，没传的原样保留（允许只改状态、不动时刻）。
    """
    name: str
    time: str | None = None                 # "HH:MM"
    day_of_week: int | None = None          # 0=周一 … 6=周日（仅 evolve）
    interval_seconds: int | None = None
    start: str | None = None
    end: str | None = None
    enabled: bool | None = None             # None = 不改状态


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
    """修改调度任务的执行时刻 / 人工启停，写 settings.yaml 并热更新常驻调度器。

    任务名 ↔ 配置键的对应关系统一走 runner 的 ``JOB_TIME_KEYS`` / ``JOB_WINDOW_KEYS``。
    此前这里另维护了一份白名单，漏掉 ``strategylab_open``/``strategylab_run``/
    ``etf_t0_intraday``/``stock_t0_intraday`` 四个任务，编辑它们一律 400
    「未知调度任务」—— 白名单在前后端各写一份必然漂移，改成单一来源。
    """
    from qmt_trade.core.config import get_settings
    from qmt_trade.scheduler.runner import (JOB_TIME_KEYS, JOB_WINDOW_KEYS,
                                            bound_strategy_enabled, job_enabled,
                                            manual_enabled)

    name = body.name
    if name not in JOB_TIME_KEYS and name not in JOB_WINDOW_KEYS:
        raise HTTPException(400, f"未知调度任务：{name}")

    s = load_settings_editor()
    changed: list[str] = []

    if body.enabled is not None:
        # 只写人工开关，不碰 strategies.*：策略门禁那半边归「策略实验室」管。
        # 手动关掉后即使绑定策略后来被启用，任务也不会自己复活（符合用户直觉）。
        s.set(f"scheduler.jobs.{name}_enabled", bool(body.enabled))
        changed.append("状态")

    if name in JOB_WINDOW_KEYS:
        start_key, end_key, sec_key = JOB_WINDOW_KEYS[name]
        seconds = body.interval_seconds
        if seconds is not None and not (1 <= int(seconds) <= 3600):
            raise HTTPException(400, "巡检间隔须在 1~3600 秒之间")
        start = _parse_hm_strict(body.start, "开始时间") if body.start else None
        end = _parse_hm_strict(body.end, "结束时间") if body.end else None
        if start and end and start >= end:
            raise HTTPException(400, f"执行窗口非法：开始 {start} 不早于结束 {end}")
        if seconds is not None:
            s.set(f"scheduler.jobs.{sec_key}", int(seconds))
            changed.append("巡检间隔")
        if start:
            s.set(f"scheduler.jobs.{start_key}", start)
            changed.append("窗口开始")
        if end:
            s.set(f"scheduler.jobs.{end_key}", end)
            changed.append("窗口结束")
    elif body.time is not None:
        s.set(f"scheduler.jobs.{JOB_TIME_KEYS[name]}",
              _parse_hm_strict(body.time, "执行时间"))
        changed.append("执行时刻")
        if name == "evolve" and body.day_of_week is not None:
            if not 0 <= int(body.day_of_week) <= 6:
                raise HTTPException(400, f"星期序号须在 0~6 之间（0=周一），收到 {body.day_of_week}")
            s.set("scheduler.jobs.evolve_weekday", int(body.day_of_week))
            changed.append("执行日")

    if not changed:
        raise HTTPException(400, "没有需要保存的改动（时刻与状态都未提供）")

    # 全部校验通过才落盘：中途 400 时上面的 s.set 只改了内存里的编辑器副本
    save_settings(s)

    # 热更新常驻调度器；拿不到实例（非 lifespan 启动）时提示重启即可
    reloaded = False
    sched = getattr(getattr(request.app, "state", None), "scheduler", None)
    if sched is not None:
        try:
            reloaded = sched.reload()
        except Exception as exc:                         # noqa: BLE001
            raise HTTPException(500, f"配置已保存但调度器热更新失败: {exc}")

    # 回传落盘后的真实生效状态：人工开关拨到"启用"、但绑定策略仍全关时任务照样不跑，
    # 前端要据此提示"还需去策略实验室开启"，否则用户会以为保存没生效。
    try:
        fresh = get_settings()          # save_settings 已失效单例，这里是最新 YAML
    except Exception:                                    # noqa: BLE001
        fresh = s
    return {"ok": True, "name": name, "changed": changed, "reloaded": reloaded,
            "manual_enabled": manual_enabled(name, fresh),
            "strategy_gate": bound_strategy_enabled(name, fresh),
            "enabled": job_enabled(name, fresh),
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
