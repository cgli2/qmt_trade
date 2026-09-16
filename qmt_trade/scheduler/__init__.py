"""L7 调度层：把一天的任务串成链，并挂到时间表上。

- ``jobs``   —— 任务本体，每个都自带失败隔离与留痕；
- ``runner`` —— APScheduler 封装（未安装时退化为内置轮询）。
"""

from .jobs import CRITICAL_JOBS, JOB_MAP, JobResult, JobRunner, run_job
from .runner import (JOB_DESCRIPTIONS, JOB_INTERVAL_DEFAULTS, JOB_STATES,
                     JOB_STRATEGY_BINDING, JOB_TIME_KEYS, JOB_WINDOW_KEYS,
                     JobSpec, TradingScheduler, bound_strategy_enabled,
                     job_enabled, job_interval_seconds, manual_enabled,
                     next_run_at)

__all__ = [
    "JobResult", "JobRunner", "JOB_MAP", "run_job", "CRITICAL_JOBS",
    "JobSpec", "TradingScheduler", "next_run_at",
    "JOB_DESCRIPTIONS", "JOB_STATES", "JOB_STRATEGY_BINDING",
    "JOB_TIME_KEYS", "JOB_WINDOW_KEYS", "JOB_INTERVAL_DEFAULTS",
    "job_interval_seconds",
    "bound_strategy_enabled", "manual_enabled", "job_enabled",
]
