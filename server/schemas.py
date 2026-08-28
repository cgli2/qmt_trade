"""请求体模型。响应多以 dict 直接返回（FastAPI 自动序列化）。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- LLM 管理
class ProviderIn(BaseModel):
    id: str
    type: str = "openai_like"          # openai_like | mock
    base_url: str = ""
    api_key_env: str = ""
    timeout: float = 60.0
    max_retries: int = 2
    extra_headers: dict[str, str] = Field(default_factory=dict)


class ModelIn(BaseModel):
    id: str
    provider: str
    name: str
    capabilities: list[str] = Field(default_factory=list)
    context_window: int = 32000
    price_per_1k_tokens: dict[str, float] = Field(default_factory=dict)


class SceneIn(BaseModel):
    id: str
    prefer: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    description: str = ""


class SelectionIn(BaseModel):
    strategy: str = "weighted"
    capability_weight: float = 0.40
    health_weight: float = 0.40
    cost_weight: float = 0.20
    fallback_enabled: bool = True


class LLMTestIn(BaseModel):
    prompt: str = "请用一句话介绍你自己。"
    model: Optional[str] = None
    scene: Optional[str] = None
    temperature: Optional[float] = None


# ---------------------------------------------------------------- 参数配置
class SettingPatch(BaseModel):
    path: str                          # 点分路径，如 risk.gate1.max_positions
    value: Any


# ---------------------------------------------------------------- 风控
class KillSwitchAction(BaseModel):
    action: str                        # engage | flatten | reset | status
    reason: str = ""


# ---------------------------------------------------------------- 交易
class TradeIntentIn(BaseModel):
    symbol: str
    action: str                        # BUY | SELL | HOLD
    shares: int = 0
    price: Optional[float] = None
    confidence: float = 0.5
    conviction: str = "MEDIUM"
    reason: str = ""
    stop_loss_type: str = "percent"
    stop_loss_value: float = 0.0


class ReconcileAck(BaseModel):
    trade_date: Optional[str] = None
    operator: str = "webui"
    note: str = ""


# ---------------------------------------------------------------- 策略/进化/复盘
class EvolveIn(BaseModel):
    date: Optional[str] = None


class StrategyVersionIn(BaseModel):
    """策略实例草稿/版本请求；参数必须是对象，避免把无效配置发布到运行实例。"""
    strategy_id: str
    instance_id: str | None = None
    name: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


class StrategyInstanceStateIn(BaseModel):
    enabled: bool


# ---------------------------------------------------------------- 回测
class BacktestIn(BaseModel):
    start: str
    end: Optional[str] = None
    cash: float = 1_000_000.0
    top_n: int = 10
    warmup: int = 250
    llm: bool = False


# ---------------------------------------------------------------- 通知
class NotifyTestIn(BaseModel):
    title: str = "WebUI 测试"
    body: str = "这是来自 Web 控制台的测试消息。"
    channel: Optional[str] = None      # None = 全部已启频道


# ---------------------------------------------------------------- 密钥
class SecretIn(BaseModel):
    key: str
    value: str
