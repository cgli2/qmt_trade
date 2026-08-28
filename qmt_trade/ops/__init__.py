"""L6 运维层：通知、健康监控、日报周报。

三个模块的分工：
- ``notify``  —— 消息出口（飞书/企微/钉钉/控制台/文件），永不抛异常、永不刷屏；
- ``monitor`` —— 体检与自愈，不合格自动拉 KillSwitch 进 REDUCE_ONLY；
- ``report``  —— 从 SQLite 还原当日/当周全貌，纯函数、可复现。
"""

from .notify import (
    Channel,
    ConsoleChannel,
    DingTalkChannel,
    FeishuChannel,
    FileChannel,
    Level,
    MemoryChannel,
    Message,
    Notifier,
    WecomChannel,
    build_notifier,
)
from .monitor import (
    CheckResult,
    HealthMonitor,
    HealthReport,
    Watchdog,
    trading_window_guard,
)
from .report import DailyReport, Reporter, WeeklyReport

__all__ = [
    "Level", "Message", "Channel", "ConsoleChannel", "MemoryChannel",
    "FileChannel", "FeishuChannel", "WecomChannel", "DingTalkChannel",
    "Notifier", "build_notifier",
    "CheckResult", "HealthReport", "HealthMonitor", "Watchdog", "trading_window_guard",
    "DailyReport", "WeeklyReport", "Reporter",
]
