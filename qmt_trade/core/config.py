"""配置中心：分层加载 环境变量 > .env > settings.yaml > 代码默认值。

修正 qmt_etf 的缺陷 #2（配置全硬编码：QMT_PATH / ACCOUNT_ID / TOTAL_CAPITAL 写死在 config.py）。
敏感项只从环境变量读取，永远不会出现在 YAML 里。
"""

from __future__ import annotations

import copy
import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_INSTRUMENTS = PROJECT_ROOT / "config" / "instruments.yaml"
DEFAULT_ENV = PROJECT_ROOT / "config" / ".env"
STRATEGY_CONFIG_DIRNAME = "strategies"
STRATEGY_MIGRATION_BACKUP_SUFFIX = ".before-strategy-split.bak"


def _load_yaml_mapping(path: Path) -> dict:
    """读取 YAML 映射；空文件按空映射处理。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件根节点必须是映射: {path}")
    return raw


def _load_and_migrate_strategy_configs(path: Path, raw: dict) -> dict:
    """将旧 ``settings.yaml.strategies`` 无损拆分至 ``strategies/*.yaml``。

    策略文件是唯一的持久化来源；为兼容尚未改造的调用方，加载后会把它们
    临时合并回内存中的 ``strategies`` 节点。首次加载旧配置时自动备份原文件。
    """
    strategy_dir = path.parent / STRATEGY_CONFIG_DIRNAME
    legacy = raw.pop("strategies", None)
    if legacy is not None and not isinstance(legacy, dict):
        raise ConfigError(f"strategies 必须是映射: {path}")

    if isinstance(legacy, dict):
        strategy_dir.mkdir(parents=True, exist_ok=True)
        backup = path.with_name(path.name + STRATEGY_MIGRATION_BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(path, backup)
        for sid, values in legacy.items():
            if not isinstance(values, dict):
                raise ConfigError(f"策略 {sid} 的配置必须是映射: {path}")
            target = strategy_dir / f"{sid}.yaml"
            # 已存在的策略文件优先，旧 settings 仅补齐其缺失键，避免覆盖新配置。
            merged = _deep_merge(values, _load_yaml_mapping(target) if target.exists() else {})
            target.write_text(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False,
                                             default_flow_style=False), encoding="utf-8")
        path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False,
                                       default_flow_style=False), encoding="utf-8")

    loaded: dict[str, dict] = {}
    if strategy_dir.exists():
        for strategy_file in sorted(strategy_dir.glob("*.yaml")):
            loaded[strategy_file.stem] = _load_yaml_mapping(strategy_file)
    raw["strategies"] = loaded
    return raw


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典，override 优先。"""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _coerce(value: str) -> Any:
    """把环境变量字符串转成合适的 Python 类型。"""
    low = value.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", ""):
        return None
    try:
        if "." in value or "e" in low:
            return float(value)
        return int(value)
    except ValueError:
        return value


def load_dotenv(path: Path | str = DEFAULT_ENV, *, override: bool = False) -> dict[str, str]:
    """极简 .env 加载器，避免为一个功能引入额外依赖。"""
    path = Path(path)
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        loaded[key] = val
        if override or key not in os.environ:
            os.environ[key] = val
    return loaded


class Settings:
    """配置访问器。

    用点分路径访问：``settings.get("risk.gate1.max_positions")``。

    环境变量覆盖规则：``QMT_RISK__GATE1__MAX_POSITIONS=8`` 会覆盖上面这一项
    （前缀 ``QMT_``，层级分隔符 ``__``）。
    """

    ENV_PREFIX = "QMT_"

    def __init__(self, data: dict | None = None, *, env_overlay: bool = True):
        self._data: dict = data or {}
        self._path: Path | None = None
        if env_overlay:
            self._apply_env_overlay()

    # ------------------------------------------------------------ 构造
    @classmethod
    def load(
        cls,
        settings_path: Path | str = DEFAULT_SETTINGS,
        *,
        env_path: Path | str | None = DEFAULT_ENV,
        env_overlay: bool = True,
    ) -> "Settings":
        if env_path is not None:
            load_dotenv(env_path)
        path = Path(settings_path)
        if not path.exists():
            raise ConfigError(f"配置文件不存在: {path}")
        raw = _load_yaml_mapping(path)
        raw = _load_and_migrate_strategy_configs(path, raw)
        inst = cls(raw, env_overlay=env_overlay)
        inst._path = path
        return inst

    # ------------------------------------------------------------ 落盘
    def save(self, path: Path | str | None = None, *, backup: bool = True) -> Path:
        """把当前配置写回 YAML。供 Web UI 修改参数后持久化。

        - 首次写盘前会留一份 ``.bak`` 备份（注释信息会丢失，属已知取舍）；
        - 非从文件加载的 Settings（如单测里 ``Settings(dict)``）必须显式给 ``path``。
        """
        target = Path(path) if path else self._path
        if target is None:
            raise ConfigError("save 需要显式 path（该 Settings 非从文件加载）")
        if backup and target.exists():
            bak = target.with_name(target.name + ".bak")
            try:
                shutil.copy2(target, bak)
            except OSError:
                pass
        data = copy.deepcopy(self._data)
        strategies = data.pop("strategies", {})
        if strategies and not isinstance(strategies, dict):
            raise ConfigError("strategies 必须是映射")
        strategy_dir = target.parent / STRATEGY_CONFIG_DIRNAME
        if isinstance(strategies, dict):
            strategy_dir.mkdir(parents=True, exist_ok=True)
            for sid, values in strategies.items():
                if not isinstance(values, dict):
                    raise ConfigError(f"策略 {sid} 的配置必须是映射")
                (strategy_dir / f"{sid}.yaml").write_text(
                    yaml.safe_dump(values, allow_unicode=True, sort_keys=False,
                                   default_flow_style=False), encoding="utf-8")
        target.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                           default_flow_style=False),
            encoding="utf-8",
        )
        return target

    # ------------------------------------------------------------ 环境覆盖
    def _apply_env_overlay(self) -> None:
        for env_key, env_val in os.environ.items():
            if not env_key.startswith(self.ENV_PREFIX):
                continue
            body = env_key[len(self.ENV_PREFIX):]
            if "__" not in body:
                continue
            parts = [p.lower() for p in body.split("__")]
            node = self._data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
                if not isinstance(node, dict):  # pragma: no cover - 防御
                    break
            else:
                node[parts[-1]] = _coerce(env_val)

    # ------------------------------------------------------------ 访问
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, path: str) -> Any:
        value = self.get(path, _MISSING)
        if value is _MISSING:
            raise ConfigError(f"缺少必需配置项: {path}")
        return value

    def section(self, path: str) -> dict:
        value = self.get(path, {})
        return value if isinstance(value, dict) else {}

    def set(self, path: str, value: Any) -> None:
        """运行期覆盖，主要给测试和参数寻优用。"""
        parts = path.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def clone(self) -> "Settings":
        return Settings(copy.deepcopy(self._data), env_overlay=False)

    def merged(self, override: dict) -> "Settings":
        """返回一个合并了 override 的新实例（不改原对象），供 walk-forward 寻优用。"""
        return Settings(_deep_merge(self._data, override), env_overlay=False)

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)

    # ------------------------------------------------------------ 路径
    @property
    def data_dir(self) -> Path:
        p = PROJECT_ROOT / str(self.get("app.data_dir", "data"))
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def log_dir(self) -> Path:
        p = PROJECT_ROOT / str(self.get("app.log_dir", "logs"))
        p.mkdir(parents=True, exist_ok=True)
        return p


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "<MISSING>"


_MISSING = _Missing()


class Secrets:
    """敏感配置，只从环境变量读取，绝不落盘到 YAML。"""

    @staticmethod
    def get(key: str, default: str | None = None) -> str | None:
        return os.environ.get(key, default)

    @staticmethod
    def require(key: str) -> str:
        val = os.environ.get(key)
        if not val:
            raise ConfigError(
                f"缺少敏感配置环境变量 {key}，请在 config/.env 中设置（参考 config/.env.example）"
            )
        return val

    # 常用项的语义化封装
    @classmethod
    def qmt_path(cls) -> str:
        return cls.require("QMT_MINI_PATH")

    @classmethod
    def qmt_account(cls) -> str:
        return cls.require("QMT_ACCOUNT_ID")

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """全局单例。测试里请直接构造 ``Settings(dict)`` 而不是用这个。"""
    return Settings.load()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
