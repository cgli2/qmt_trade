"""qmt_trade —— 基于 LLM 的 A 股自动化交易系统。

分层（自下而上）：

- ``core``       配置 / 日志 / 时钟 / 领域模型 / 异常
- ``datahub``    数据源适配 + PIT 存储 + 质量校验
- ``features``   L1 因子计算
- ``selection``  规则选股流水线（市场状态 → 池 → 打分 → 候选集）
- ``brain``      L2 LLM 研判（只产出 TradeIntent，无下单权）
- ``risk``       三道风控闸门 + KillSwitch
- ``portfolio``  组合状态 / 仓位计算
- ``execution``  下单网关（Sim / QMT）+ 成本模型 + 执行服务
- ``backtest``   与实盘同代码路径的回测引擎
- ``evolution``  复盘 / 经验库 / 策略池进化
- ``ops``        通知 / 对账 / 报告 / 健康检查
- ``storage``    SQLite 持久化
- ``scheduler``  任务编排（jobs）+ 调度器（runner）
- ``app``        Composition Root：全系统唯一的组件装配点
- ``cli``        命令行入口

使用入口只有两个：``qmt_trade.app.build_context()`` 与 ``python -m qmt_trade``。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
