"""统一异常体系。

设计原则 P4（失败安全）的基础设施：所有异常都带 ``fail_safe`` 标记，
调度层捕获到 ``fail_safe=True`` 的异常时自动把 KillSwitch 切到 REDUCE_ONLY。
"""

from __future__ import annotations


class QmtTradeError(Exception):
    """所有业务异常的基类。"""

    #: 该异常是否应触发失败安全（停止开仓）
    fail_safe: bool = False

    def __init__(self, message: str, **context):
        super().__init__(message)
        self.message = message
        self.context = context

    def __str__(self) -> str:  # pragma: no cover - 简单格式化
        if self.context:
            ctx = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
            return f"{self.message} ({ctx})"
        return self.message


# ---------------------------------------------------------------- 配置 / 环境
class ConfigError(QmtTradeError):
    """配置缺失或非法。"""

    fail_safe = True


# ---------------------------------------------------------------- 数据层
class DataError(QmtTradeError):
    """数据层通用异常。"""


class DataUnavailableError(DataError):
    """所有数据源均失败，无法取得数据 —— 触发当日停止开仓。"""

    fail_safe = True


class DataQualityError(DataError):
    """数据通过了获取但未通过质量校验。"""

    fail_safe = True


class LookAheadError(DataError):
    """检测到未来函数：返回了 ``publish_time > asof`` 的数据。

    这是回测可信度的红线，任何时候都直接抛出，不做降级。
    """

    fail_safe = True


# ---------------------------------------------------------------- LLM 层
class LLMError(QmtTradeError):
    """LLM 调用异常。"""


class LLMTimeoutError(LLMError):
    """LLM 调用超时。按设计不产生 Intent，而不是瞎猜一个。"""


class LLMCallFailed(LLMError):
    """LLM 请求最终失败（网络异常 / 重试耗尽 / 响应结构异常 / 鉴权错误）。

    调用方（Agent 层）捕获后**降级到规则路径**，绝不因为模型挂了就停摆（P5）。
    """


class LLMBudgetExceeded(LLMError):
    """成本熔断：超出日/月预算，LLM 层停用，系统降级为纯因子模式。"""


class LLMSchemaError(LLMError):
    """LLM 输出不符合 Pydantic 契约且重试耗尽。"""


class FactCheckError(LLMError):
    """事实校验失败：LLM 输出的数字与 FactPack 不一致（幻觉）。"""


# ---------------------------------------------------------------- 风控 / 执行
class RiskRejected(QmtTradeError):
    """订单被风控闸门拒绝。这是正常业务流，不是故障。"""

    def __init__(self, message: str, rule: str = "", **context):
        super().__init__(message, rule=rule, **context)
        self.rule = rule


class KillSwitchActive(QmtTradeError):
    """KillSwitch 处于非 NORMAL 状态，拒绝开仓。"""


class ExecutionError(QmtTradeError):
    """执行层异常。"""


class GatewayNotConnected(ExecutionError):
    """交易网关未连接。"""

    fail_safe = True


class DuplicateOrderError(ExecutionError):
    """幂等键冲突，重复下单被拦截。"""


class ReconcileError(ExecutionError):
    """对账不平 —— 本地状态不可信，次日禁止开仓直到人工确认。"""

    fail_safe = True
