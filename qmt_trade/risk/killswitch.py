"""Kill Switch（设计 6.6.4）：系统级紧急刹车。

三档：NORMAL（正常）/ REDUCE_ONLY（只平不开，默认失败安全态）/ FLATTEN（全平并停机）。

触发源：自动（回撤触线/对账失败/连续下单异常）、定时（非交易时段）、手动（CLI/消息）。
P4 落地：任何未预期异常，系统自动切到 REDUCE_ONLY，绝不"猜着继续交易"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable


class KillMode(str, Enum):
    NORMAL = "NORMAL"
    REDUCE_ONLY = "REDUCE_ONLY"
    FLATTEN = "FLATTEN"


@dataclass
class KillSwitch:
    mode: KillMode = KillMode.NORMAL
    reason: str = ""
    triggered_at: datetime | None = None
    manual: bool = False
    #: 状态变更回调，运维/通知用
    _on_change: list[Callable[["KillSwitch"], None]] = field(default_factory=list)

    @property
    def allow_open(self) -> bool:
        return self.mode is KillMode.NORMAL

    @property
    def allow_close(self) -> bool:
        return self.mode is not KillMode.NORMAL or True  # 任何模式下都允许平仓

    def on_change(self, cb: Callable[["KillSwitch"], None]) -> None:
        self._on_change.append(cb)

    def set(self, mode: KillMode, *, reason: str = "", manual: bool = False) -> None:
        # 同档位且原因未变 → 不算状态变更，不触发回调。
        # 否则健康检查每轮都会用相同原因重新拉闸 → CRITICAL 告警绕过节流
        # 反复推送，同一故障一晚能刷出几十条一模一样的消息。
        # 人工操作例外：reset()/手动拉闸必须每次都算数并播报。
        if not manual and mode == self.mode and (not reason or reason == self.reason):
            return
        self.mode = mode
        self.reason = reason
        self.manual = manual
        self.triggered_at = datetime.now()
        for cb in self._on_change:
            try:
                cb(self)
            except Exception:  # pragma: no cover - 回调异常不应影响切换
                pass

    def engage(self, reason: str, *, manual: bool = False) -> None:
        """升档不降档（REDUCE_ONLY 不会被 FLATTEN 之外的更低档覆盖）。

        ``manual`` 用于区分"系统自动拉闸"和"人按 CLI 拉的闸"——事后复盘时
        这两者的含义完全不同，不能混为一谈。
        """
        if self.mode is KillMode.FLATTEN:
            return
        self.set(KillMode.REDUCE_ONLY, reason=reason, manual=manual)

    def flatten(self, reason: str, *, manual: bool = False) -> None:
        self.set(KillMode.FLATTEN, reason=reason, manual=manual)

    def reset(self, reason: str = "人工恢复") -> None:
        self.set(KillMode.NORMAL, reason=reason, manual=True)

    def to_dict(self) -> dict:
        return {"mode": self.mode.value, "reason": self.reason,
                "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
                "manual": self.manual}
