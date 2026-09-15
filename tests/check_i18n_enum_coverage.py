"""前端枚举中文化覆盖率校验。

两项检查：

1. **字典覆盖率**：后端 Enum / Literal 的每一个取值，都必须在
   ``webui/src/labels.ts`` 对应域的字典里有非空中文译文，且译文不等于原码。
   漏项直接报出，避免后端新增枚举后前端静默退回英文。

2. **裸渲染回归**：扫描所有 ``.vue`` 模板插值，凡是直接输出枚举类字段
   （status / side / kill_mode ...）而未经 ``cn()`` 或本地 LABEL 表翻译的，
   一律报出。已确认合理保留原文的位置登记在 IGNORE 白名单里。

用法::

    python tests/check_i18n_enum_coverage.py

退出码 0 表示全部通过。仅做静态校验，不依赖后端服务是否启动。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文报告与 SystemExit 消息会乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
LABELS_TS = ROOT / "webui" / "src" / "labels.ts"
VUE_DIRS = [ROOT / "webui" / "src" / "views", ROOT / "webui" / "src" / "components"]
VUE_EXTRA = [ROOT / "webui" / "src" / "App.vue"]

# 译文允许与原码同形的少数技术缩写
ALLOW_SAME = {"JSON", "ROE"}

# ---------------------------------------------------------------------------
# 1. 解析 labels.ts
# ---------------------------------------------------------------------------

_DICT_RE = re.compile(
    r"export const (\w+): Dict = withCase\(\{(.*?)\n\}\);", re.S)
_PAIR_RE = re.compile(r'(?:\"([^\"]+)\"|([A-Za-z_]\w*))\s*:\s*\"([^\"]*)\"')


def parse_labels_ts() -> dict[str, dict[str, str]]:
    """解析 labels.ts 里的 ``withCase({...})`` 字典（仅声明态键，不含自动补的大小写变体）。"""
    text = LABELS_TS.read_text(encoding="utf-8")
    out: dict[str, dict[str, str]] = {}
    for name, body in _DICT_RE.findall(text):
        pairs: dict[str, str] = {}
        for qk, bk, val in _PAIR_RE.findall(body):
            pairs[qk or bk] = val
        out[name] = pairs
    if not out:
        raise SystemExit(f"未能从 {LABELS_TS} 解析出任何字典，正则可能已失配")
    return out


# ---------------------------------------------------------------------------
# 2. 后端取值来源
# ---------------------------------------------------------------------------

def backend_enum_values() -> dict[str, list[str]]:
    """导入 qmt_trade 全部 Enum，返回 {类名: [取值...]}。"""
    import enum
    import importlib
    import inspect
    import pkgutil

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import qmt_trade

    found: dict[str, list[str]] = {}
    for mi in pkgutil.walk_packages(qmt_trade.__path__, "qmt_trade."):
        try:
            mod = importlib.import_module(mi.name)
        except Exception:  # 可选依赖缺失的模块跳过，不影响枚举校验
            continue
        for name, obj in vars(mod).items():
            if (inspect.isclass(obj) and issubclass(obj, enum.Enum)
                    and obj.__module__ == mod.__name__):
                vals = [str(m.value) for m in obj]
                # IntEnum（notify.Level）按名字暴露给前端
                if all(v.isdigit() for v in vals):
                    vals = [m.name for m in obj]
                found.setdefault(f"{obj.__module__}.{name}", vals)
    return found


def literal_values(rel_path: str, pattern: str) -> list[str]:
    """从源码里抓 Literal[...] 或正则命中的字面量取值。"""
    text = (ROOT / rel_path).read_text(encoding="utf-8")
    m = re.search(pattern, text, re.M)
    if not m:
        raise SystemExit(f"{rel_path} 未匹配到 {pattern}")
    return re.findall(r'"([^"]+)"', m.group(0))


# 后端枚举 → labels.ts 字典
ENUM_MAP: dict[str, str] = {
    "qmt_trade.risk.killswitch.KillMode": "KILL_MODE",
    "qmt_trade.ops.notify.Level": "LEVEL",
    "qmt_trade.core.trading.Side": "SIDE",
    "qmt_trade.core.trading.OrderType": "ORDER_TYPE",
    "qmt_trade.core.trading.OrderStatus": "ORDER_STATUS",
    "qmt_trade.core.events.EventType": "EVENT_TYPE",
    "qmt_trade.core.instruments.Board": "BOARD",
    "qmt_trade.core.clock.Session": "SESSION",
    "qmt_trade.features.regime.Regime": "REGIME",
    "qmt_trade.datahub.types.Freq": "FREQ",
    "qmt_trade.datahub.types.Adjust": "ADJUST",
    "qmt_trade.datahub.types.EventCategory": "EVENT_CATEGORY",
    "qmt_trade.datahub.providers.base.Capability": "CAPABILITY",
    "qmt_trade.execution.costs.SlippageModel": "SLIPPAGE_MODEL",
}

# 非 Enum（Literal / 字面量约定）→ (源文件, 提取正则, 字典名)
LITERAL_MAP: list[tuple[str, str, str, str]] = [
    ("策略池状态", "qmt_trade/evolution/pool.py",
     r"^Status = Literal\[[^\]]*\]", "POOL_STATUS"),
    ("确信度", "qmt_trade/brain/schemas.py",
     r"conviction: Literal\[[^\]]*\]", "CONVICTION"),
]

# 纯字面量约定（源码里无集中定义，按实际写入值固定）
FIXED_SETS: dict[str, list[str]] = {
    # qmt_trade/execution/service.py 落库时追加的订单状态
    "ORDER_STATUS": ["GUARD_BLOCKED", "FAILED"],
    # qmt_trade/scheduler/jobs.py 写入 job:<name>:last_status
    "RUN_STATUS": ["OK", "FAIL", "SKIP", "-"],
    # server/routers/backtests.py 作业状态
    "JOB_STATUS": ["pending", "running", "done", "error", "queued",
                   "succeeded", "failed", "cancelled", "interrupted"],
    # server/routers/overview.py 调度类型
    "JOB_KIND": ["cron", "interval"],
    # server 全局运行模式
    "MODE": ["sim", "paper", "live"],
    # qmt_trade/datahub 熔断器状态
    "CIRCUIT_STATE": ["closed", "open", "half_open", "OK", "FAIL"],
    # qmt_trade/execution/service.py ExecutionResult.rejected_by
    "REJECTED_BY": ["guard", "risk", "gateway", "sizer"],
    # qmt_trade/brain/llm/registry.py ProviderConfig.type
    "PROVIDER_TYPE": ["openai_like", "mock"],
    # server/routers/config.py 配置项值类型
    "CONFIG_KIND": ["bool", "number", "text", "json"],
    # 通知渠道
    "CHANNEL": ["feishu", "wecom", "dingtalk", "console"],
    # 投票 agent
    "AGENT": ["technical", "fundamental", "moneyflow", "sentiment",
              "research_manager", "portfolio_manager", "risk_officer"],
    # 研判立场
    "STANCE": ["BULL", "BEAR", "NEUTRAL"],
    "VERDICT": ["BULL", "BEAR", "NEUTRAL", "BUY", "SELL", "HOLD"],
    "TRADE_ACTION": ["BUY", "SELL", "HOLD", "WATCH"],
}

# ---------------------------------------------------------------------------
# 3. 裸渲染扫描
# ---------------------------------------------------------------------------

# 承载枚举取值的字段名。命中这些字段的 {{ }} 插值必须经过翻译。
ENUM_FIELDS = (
    "status", "state", "side", "action", "conviction", "type", "kind",
    "level", "mode", "category", "freq", "frequency", "verdict", "stance",
    "board", "session", "kill_mode", "killswitch", "min_level", "last_status",
    "severity", "adjust", "rejected_by", "channel", "regime", "order_type",
    "slippage_model",
)
_FIELD_ALT = "|".join(re.escape(f) for f in ENUM_FIELDS)
# {{ obj.field }} 或 {{ field }}，排除 obj.field_extra（如 kind_label）
_INTERP_RE = re.compile(
    r"\{\{\s*(?!cn\()([A-Za-z_$][\w$.]*)?\.?(" + _FIELD_ALT + r")\s*(\|\|[^}]*)?\}\}")

# 已确认合理保留原文/已有中文兜底的位置：(文件相对路径, 该行必含的片段)
# 用内容指纹而非行号，避免上方 import 增删导致行号漂移后白名单失效。
IGNORE: set[tuple[str, str]] = {
    # 主标签已是 PRESETS 中文，下方小字刻意展示原始渠道码供配置对照
    ("webui/src/views/NotifyView.vue", "PRESETS[ch.type]?.label || ch.type"),
}


def vue_files() -> list[Path]:
    files: list[Path] = []
    for d in VUE_DIRS:
        files.extend(sorted(d.glob("*.vue")))
    files.extend(p for p in VUE_EXTRA if p.exists())
    return files


def scan_bare_interpolations() -> list[str]:
    problems: list[str] = []
    for path in vue_files():
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            if any(rel == f and needle in line for f, needle in IGNORE):
                continue
            # 已用本地 LABEL 表 / 三元表达式产出中文的行不算裸渲染
            if "_LABEL[" in line or "cn(" in line:
                continue
            for m in _INTERP_RE.finditer(line):
                problems.append(f"{rel}:{lineno}: {{{{ {m.group(0)[2:-2].strip()} }}}}")
    return problems


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def check_dict_coverage(dicts: dict[str, dict[str, str]]) -> list[str]:
    problems: list[str] = []

    def verify(dict_name: str, values: list[str], src: str) -> None:
        d = dicts.get(dict_name)
        if d is None:
            problems.append(f"[{src}] labels.ts 缺少字典 {dict_name}")
            return
        for v in values:
            if v not in d:
                problems.append(f"[{src}] {dict_name} 缺少取值 {v!r}")
                continue
            cn_text = d[v]
            if not cn_text.strip():
                problems.append(f"[{src}] {dict_name}[{v!r}] 译文为空")
            elif cn_text == v and v not in ALLOW_SAME:
                problems.append(
                    f"[{src}] {dict_name}[{v!r}] 译文与原码相同（{cn_text!r}）")

    enums = backend_enum_values()
    for enum_path, dict_name in ENUM_MAP.items():
        vals = enums.get(enum_path)
        if vals is None:
            problems.append(f"[后端] 未找到枚举 {enum_path}（已重命名或删除？）")
            continue
        verify(dict_name, vals, enum_path)

    for label, rel, pattern, dict_name in LITERAL_MAP:
        verify(dict_name, literal_values(rel, pattern), label)

    for dict_name, vals in FIXED_SETS.items():
        verify(dict_name, vals, dict_name)

    # 反向：字典里的值必须非空且尽量含中文
    for name, d in dicts.items():
        for k, v in d.items():
            if not v.strip():
                problems.append(f"[labels.ts] {name}[{k!r}] 译文为空")
    return problems


def main() -> int:
    dicts = parse_labels_ts()
    print(f"labels.ts 解析到 {len(dicts)} 个字典，"
          f"共 {sum(len(d) for d in dicts.values())} 条声明态映射")

    problems = check_dict_coverage(dicts)
    problems += [f"[裸渲染] {p}" for p in scan_bare_interpolations()]

    if problems:
        print(f"\n发现 {len(problems)} 个问题：")
        for p in problems:
            print("  -", p)
        return 1
    print("\n全部通过：后端枚举取值 100% 有中文译文，模板无裸英文枚举插值。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
