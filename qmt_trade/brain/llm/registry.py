"""LLM 配置注册表（独立管理，不入 settings.yaml / .env）。

职责
----
- 定义 ``ProviderConfig`` / ``ModelConfig`` / ``SceneConfig`` / ``SelectionConfig`` 数据结构；
- ``load_llm_config`` 从 ``config/llm.yaml`` 加载（找不到 / 损坏则用内置默认，保证不崩）；
- ``LLMConfig`` 提供查表与场景候选解析，供 ``ModelSelector`` / ``LLMManager`` 使用。

API Key 安全：配置里只存 ``api_key_env``（环境变量名），真正的值由运行时从环境变量读取，
绝不落盘。
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 默认配置文件位置：<repo>/config/llm.yaml
_DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "llm.yaml"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    type: str = "openai_like"          # openai_like | mock
    base_url: str = ""
    api_key_env: str = ""
    timeout: float = 60.0
    max_retries: int = 2
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""

    @property
    def is_mock(self) -> bool:
        return self.type == "mock"


@dataclass(frozen=True)
class ModelConfig:
    id: str
    provider: str
    name: str
    capabilities: tuple[str, ...] = ()
    context_window: int = 32000
    price_per_1k_tokens: dict[str, float] = field(default_factory=dict)

    def price(self, kind: str) -> float:
        return float(self.price_per_1k_tokens.get(kind, 0.0))


@dataclass(frozen=True)
class SceneConfig:
    id: str
    prefer: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    description: str = ""


@dataclass
class SelectionConfig:
    strategy: str = "weighted"           # weighted | capability_first
    capability_weight: float = 0.40
    health_weight: float = 0.40
    cost_weight: float = 0.20
    fallback_enabled: bool = True


@dataclass
class LLMConfig:
    enabled: bool = True
    cache_enabled: bool = True
    cache_path: str = "data/llm_cache.db"
    temperature: float = 0.0
    timeout_seconds: float = 60.0
    max_retries: int = 2
    default_model: str = "deepseek-chat"
    budget: dict[str, Any] = field(default_factory=lambda: {
        "daily_cny": 30.0, "monthly_cny": 600.0, "hard_stop": True})
    providers: list[ProviderConfig] = field(default_factory=list)
    models: list[ModelConfig] = field(default_factory=list)
    scenes: list[SceneConfig] = field(default_factory=list)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    _p_idx: dict[str, ProviderConfig] = field(default_factory=dict, repr=False, compare=False)
    _m_idx: dict[str, ModelConfig] = field(default_factory=dict, repr=False, compare=False)
    _s_idx: dict[str, SceneConfig] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._p_idx = {p.id: p for p in self.providers}
        self._m_idx = {m.id: m for m in self.models}
        self._s_idx = {s.id: s for s in self.scenes}

    # ---- 查表 ----
    def get_provider(self, pid: str) -> ProviderConfig | None:
        return self._p_idx.get(pid)

    def get_model(self, mid: str) -> ModelConfig | None:
        return self._m_idx.get(mid)

    def get_scene(self, sid: str) -> SceneConfig | None:
        return self._s_idx.get(sid)

    def candidates_for_scene(self, sid: str | None) -> list[str]:
        if sid:
            sc = self._s_idx.get(sid)
            if sc is not None and sc.candidates:
                return list(sc.candidates)
        return [m.id for m in self.models]

    @property
    def model_ids(self) -> list[str]:
        return [m.id for m in self.models]

    # ---- 序列化（供 Web UI 落盘）----
    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "cache_enabled": self.cache_enabled,
            "cache_path": self.cache_path,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "default_model": self.default_model,
            "budget": dict(self.budget),
            "providers": [asdict(p) for p in self.providers],
            "models": [asdict(m) for m in self.models],
            "scenes": {
                s.id: {
                    "prefer": list(s.prefer),
                    "candidates": list(s.candidates),
                    "description": s.description,
                } for s in self.scenes
            },
            "selection": asdict(self.selection),
        }


# ---------------------------------------------------------------- 加载
def _parse_config(raw: dict) -> LLMConfig:
    raw = raw or {}
    providers = [ProviderConfig(**p) for p in (raw.get("providers") or [])]
    models = [ModelConfig(
        id=m["id"], provider=m["provider"], name=m.get("name", m["id"]),
        capabilities=tuple(m.get("capabilities", []) or []),
        context_window=int(m.get("context_window", 32000)),
        price_per_1k_tokens=dict(m.get("price_per_1k_tokens", {}) or {}),
    ) for m in (raw.get("models") or [])]
    scenes_raw = raw.get("scenes") or {}
    if isinstance(scenes_raw, dict):
        # 形如  scene_id: {prefer: [...], candidates: [...]}（可读性更好）
        scenes = [SceneConfig(
            id=sid, prefer=tuple(s.get("prefer", []) or []),
            candidates=tuple(s.get("candidates", []) or []),
            description=s.get("description", ""),
        ) for sid, s in scenes_raw.items()]
    else:  # 形如  - {id: scene_id, prefer: [...], candidates: [...]}
        scenes = [SceneConfig(
            id=s["id"], prefer=tuple(s.get("prefer", []) or []),
            candidates=tuple(s.get("candidates", []) or []),
            description=s.get("description", ""),
        ) for s in scenes_raw]
    sel_raw = raw.get("selection") or {}
    sel = SelectionConfig(
        strategy=sel_raw.get("strategy", "weighted"),
        capability_weight=float(sel_raw.get("capability_weight", 0.40)),
        health_weight=float(sel_raw.get("health_weight", 0.40)),
        cost_weight=float(sel_raw.get("cost_weight", 0.20)),
        fallback_enabled=bool(sel_raw.get("fallback_enabled", True)),
    )
    return LLMConfig(
        enabled=bool(raw.get("enabled", True)),
        cache_enabled=bool(raw.get("cache_enabled", True)),
        cache_path=str(raw.get("cache_path", "data/llm_cache.db")),
        temperature=float(raw.get("temperature", 0.0)),
        timeout_seconds=float(raw.get("timeout_seconds", 60.0)),
        max_retries=int(raw.get("max_retries", 2)),
        default_model=str(raw.get("default_model",
                                  models[0].id if models else "mock")),
        budget=dict(raw.get("budget", {}) or {}),
        providers=providers, models=models, scenes=scenes, selection=sel,
    )


def _default_config() -> LLMConfig:
    """无 llm.yaml 时的兜底：纯 mock，确保系统不崩（P5）。"""
    return LLMConfig(
        enabled=False,
        default_model="mock",
        providers=[ProviderConfig(id="mock", type="mock")],
        models=[ModelConfig(id="mock", provider="mock", name="mock")],
        scenes=[],
    )


def load_llm_config(path: str | Path | None = None) -> LLMConfig:
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        return _default_config()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # 配置损坏也不崩，降级 mock
        logger.warning("读取 llm.yaml 失败，降级 mock: %s", exc)
        return _default_config()
    return _parse_config(raw)


#: 公开别名（Web UI 用 dict 重建配置后回写）。
def parse_llm_config(raw: dict) -> LLMConfig:
    return _parse_config(raw)


def save_llm_config(cfg: LLMConfig, path: str | Path | None = None) -> Path:
    """把配置写回 ``config/llm.yaml``（首次写盘前留 ``.bak`` 备份）。"""
    p = Path(path) if path else _DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        bak = p.with_name(p.name + ".bak")
        try:
            shutil.copy2(p, bak)
        except OSError:
            pass
    p.write_text(
        yaml.safe_dump(cfg.to_dict(), allow_unicode=True, sort_keys=False,
                       default_flow_style=False),
        encoding="utf-8",
    )
    return p


__all__ = [
    "ProviderConfig", "ModelConfig", "SceneConfig", "SelectionConfig",
    "LLMConfig", "load_llm_config", "parse_llm_config", "save_llm_config",
]
