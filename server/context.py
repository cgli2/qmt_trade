"""Web 后端共享设施：上下文装配、后台任务 Job 存储、.env 密钥读写。

设计原则
--------
- 所有读/写都复用 ``qmt_trade`` 现有的组合根 ``build_context``，**不重复 new 组件**，
  因此与 CLI 走完全相同的代码路径，绝不会"Web 改了一份状态、CLI 又是另一份"。
- 默认运行模式 ``paper``（真实数据源 + 模拟撮合，绝不下真单）；页面可切 ``live``。
- 慢操作（回测 / 进化）走后台线程 + Job 轮询，避免阻塞 HTTP 请求。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from qmt_trade.app import build_context
from qmt_trade.core.config import PROJECT_ROOT, Settings, load_dotenv

DEFAULT_ENV = PROJECT_ROOT / "config" / ".env"


class LiveModeLockedError(RuntimeError):
    """观察期护栏：虚拟盘观察期内禁止构建 live 上下文。

    解锁方式：在 config/.env 中显式设置 ``QMT_ALLOW_LIVE=1`` 并重启后端。
    """


def _live_allowed() -> bool:
    import os
    load_dotenv(DEFAULT_ENV)          # 幂等：已存在的环境变量不覆盖
    return os.environ.get("QMT_ALLOW_LIVE", "").strip().lower() in ("1", "true", "yes", "on")


def is_live_locked() -> bool:
    """live 当前是否处于观察期锁定状态（QMT_ALLOW_LIVE 未开启）。"""
    return not _live_allowed()


def make_ctx(mode: str = "paper"):
    """按指定模式构建一个 TradingContext（懒加载，真正访问组件时才装配）。

    默认 paper：真实数据源 + 模拟撮合，绝不下真单。任何裸调用也不会落入全模拟(mock)。

    观察期护栏：``mode=live`` 会被拒绝（曾发生过 UI 误触 mode=live 碰出
    trade_live.db 账本的事），除非显式设置 QMT_ALLOW_LIVE。

    每个模式缓存一个实例：**绝不能每次请求都新建**——新上下文会把 KillSwitch
    从持久化状态重新恢复一遍，恢复原因（带「重启恢复：」前缀）与落库原因不同，
    紧跟着的健康检查一拉闸就触发变更回调；前端一轮询，CRITICAL 告警就按分钟刷屏。
    """
    if mode not in ("paper", "live"):
        raise ValueError("mode must be paper or live")
    if mode == "live" and not _live_allowed():
        raise LiveModeLockedError(
            "观察期已锁死 live 模式。确认虚拟盘观察期结束后，"
            "在 config/.env 设置 QMT_ALLOW_LIVE=1 并重启后端方可解锁。")
    with _CTX_LOCK:
        c = _CTX_CACHE.get(mode)
        if c is None:
            c = build_context(mode)
            _CTX_CACHE[mode] = c
        return c


def reset_ctx_cache() -> None:
    """配置保存后让缓存失效，新请求读新配置。

    只清空不 close：常驻调度器可能仍持有旧实例的引用，关掉会误伤。
    """
    with _CTX_LOCK:
        _CTX_CACHE.clear()


def make_ctx_research(mode: str = "paper"):
    """保留明确的账户/实例模式，禁止静默改写。共享行情回测直接使用研究服务。"""
    return make_ctx(mode)


_CTX_CACHE: dict[str, Any] = {}
_CTX_LOCK = threading.Lock()


# ============================================================ 后台 Job 存储
@dataclass
class Job:
    id: str
    kind: str
    status: str = "pending"            # pending | running | done | error
    progress: str = ""
    created: float = 0.0
    finished: float | None = None
    result: Any = field(default=None, repr=False)
    error: str | None = None


_JOB_REPOSITORY = None
_BACKTEST_SERVICE = None
_JOBS_LOCK = threading.RLock()


def job_repository():
    global _JOB_REPOSITORY
    from qmt_trade.storage.db import Database
    from qmt_trade.storage.jobs import JobRepository
    from qmt_trade.storage.runtime import runtime_path
    with _JOBS_LOCK:
        if _JOB_REPOSITORY is None:
            _JOB_REPOSITORY = JobRepository(Database(runtime_path(), schema="jobs"))
        return _JOB_REPOSITORY


def backtest_service():
    global _BACKTEST_SERVICE
    from qmt_trade.backtest.service import BacktestService
    with _JOBS_LOCK:
        if _BACKTEST_SERVICE is None:
            _BACKTEST_SERVICE = BacktestService(job_repository(), lambda: make_ctx("paper"))
        return _BACKTEST_SERVICE


def _legacy_job(row, include_result=True):
    if not row:
        return None
    status = {"queued": "pending", "succeeded": "done", "failed": "error",
              "cancelled": "error", "interrupted": "error"}.get(row["status"], row["status"])
    return Job(id=row["id"], kind=row["kind"], status=status,
               progress=row["stage"] or "", created=row["created"], finished=row["finished"],
               result=job_repository().report(row["report_id"]) if include_result and row["report_id"] else None,
               error=row["error"])


def new_job(kind: str) -> Job:
    return _legacy_job(job_repository().create(kind))


def update_job(job: Job, **fields) -> None:
    repo = job_repository()
    status = fields.get("status")
    if status == "running":
        repo.claim(job.id)
    elif status in ("done", "error"):
        repo.finish(job.id, "succeeded" if status == "done" else "failed",
                    result=fields.get("result"), error=fields.get("error"))
    if "progress" in fields:
        repo.progress(job.id, str(fields["progress"]))
    for key, value in fields.items():
        setattr(job, key, value)


def get_job(jid: str) -> Job | None:
    return _legacy_job(job_repository().get(jid))


def list_jobs(limit: int = 20) -> list[Job]:
    return [_legacy_job(row, include_result=False) for row in job_repository().list(limit)]


from concurrent.futures import ThreadPoolExecutor
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="background-io")
_PENDING = threading.BoundedSemaphore(32)


def spawn(job: Job, fn: Callable[[], Any]) -> None:
    """Bounded I/O work; backtests use BacktestService's isolated processes."""
    if not _PENDING.acquire(blocking=False):
        update_job(job, status="error", error="后台队列已满，请稍后重试")
        return
    def run():
        try:
            if not job_repository().claim(job.id):
                return
            job.status = "running"
            result = fn()
            if job_repository().get(job.id)["cancel_requested"]:
                job_repository().finish(job.id, "cancelled")
            else:
                update_job(job, status="done", result=result, finished=time.time())
        except Exception as exc:
            update_job(job, status="error", error=f"{type(exc).__name__}: {exc}", finished=time.time())
        finally:
            _PENDING.release()
    _EXECUTOR.submit(run)


# ============================================================ .env 密钥读写
# 页面只显示「密钥名」，写值时落盘到 config/.env（不入库、不出现在 settings.yaml）。
_KNOWN_SECRET_KEYS = (
    "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "MOONSHOT_API_KEY", "OPENAI_API_KEY",
    "TUSHARE_TOKEN", "QMT_MINI_PATH", "QMT_ACCOUNT_ID",
    "WECOM_WEBHOOK", "DINGTALK_WEBHOOK",
)


def read_env_raw(path: Path = DEFAULT_ENV) -> list[tuple[str, str | None]]:
    """返回 [(key, value) | (None, raw_line)] 保留顺序与注释。"""
    if not path.exists():
        return []
    out: list[tuple[str, str | None]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            out.append((None, line))
            continue
        if s.lower().startswith("export "):
            s = s[7:]
        if "=" not in s:
            out.append((None, line))
            continue
        k, _, v = s.partition("=")
        out.append((k.strip(), v.strip().strip('"').strip("'")))
    return out


def list_secrets() -> list[dict]:
    """列出已知密钥的「存在性 + 长度掩码」，绝不回传明文值。"""
    present = {k: v for k, v in read_env_raw() if k}
    out = []
    for k in _KNOWN_SECRET_KEYS:
        val = present.get(k)
        out.append({
            "key": k,
            "set": bool(val),
            "masked": ("*" * min(len(val), 12)) if val else "",
            "length": len(val) if val else 0,
        })
    return out


def set_secret(key: str, value: str, path: Path = DEFAULT_ENV) -> bool:
    """在 .env 中新增或覆写某个密钥（保留其他行与注释）。"""
    if key not in _KNOWN_SECRET_KEYS:
        # 允许自定义键，但仅当已存在时才允许覆写，避免误建未知键
        existing = {k for k, _ in read_env_raw(path) if k}
        if key not in existing:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new_lines: list[str] = []
    replaced = False
    for line in lines:
        s = line.strip()
        cur = s[7:] if s.lower().startswith("export ") else s
        if "=" in cur and cur.partition("=")[0].strip() == key:
            new_lines.append(f'{key}="{value}"')
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f'{key}="{value}"')
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    # 让当前进程也立刻生效（仅本会话内）
    load_dotenv(path, override=True)
    return True


def load_settings_editor():
    """编辑用 Settings：不叠加环境变量，保证页面改的是 YAML 原文。"""
    return Settings.load(env_overlay=False)


def save_settings(s: Settings) -> None:
    """页面改配置的统一落盘入口：写文件后让进程内 get_settings() 单例失效，
    否则之后新建的运行上下文仍读到启动时的旧快照（"保存成功但没生效"）。"""
    from qmt_trade.core.config import reset_settings_cache
    from qmt_trade.storage.configuration import publish
    publish("settings", s.as_dict(), s.data_dir)
    reset_settings_cache()
    # Keep current trading contexts alive; scheduler consumes strategy versions
    # at a boundary. New config remains durable across application restarts.
