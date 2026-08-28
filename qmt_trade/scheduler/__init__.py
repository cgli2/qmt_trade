"""L7 调度层：把一天的任务串成链，并挂到时间表上。

- ``jobs``   —— 任务本体，每个都自带失败隔离与留痕；
- ``runner`` —— APScheduler 封装（未安装时退化为内置轮询）。
"""

from .jobs import CRITICAL_JOBS, JOB_MAP, JobResult, JobRunner, run_job
from .runner import JobSpec, TradingScheduler, next_run_at

__all__ = [
    "JobResult", "JobRunner", "JOB_MAP", "run_job", "CRITICAL_JOBS",
    "JobSpec", "TradingScheduler", "next_run_at",
]
