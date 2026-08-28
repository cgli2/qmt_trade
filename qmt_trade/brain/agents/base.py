"""智能体基类。

设计取舍：**每个 Agent 都必须能在没有 LLM 的情况下工作**（P5）。
每个 Agent 实现两条路径：

- ``_rule_based(state)`` —— 纯规则/因子推断，确定性、零成本、永远可用；
- ``_llm_based(state)``  —— LLM 增强，失败/超预算时**自动回退到规则路径**。

这样「LLM 挂了系统就瘫」的问题从架构上就不存在。
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

from ...core.errors import LLMBudgetExceeded
from ..state import AgentState, AnalystReport, Stance

logger = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_json_loose(text: str) -> dict[str, Any]:
    """从 LLM 输出里抠出第一个 JSON 对象。抠不出来返回空 dict（调用方回退规则路径）。"""
    if not text:
        return {}
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    m = _JSON_RE.search(t)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def extract_numbers(text: str, limit: int = 40) -> list[float]:
    """抽出文本中的所有数字，供事实校验器做幻觉比对。"""
    out: list[float] = []
    for tok in re.findall(r"-?\d+(?:\.\d+)?%?", text or ""):
        try:
            out.append(float(tok[:-1]) / 100.0 if tok.endswith("%") else float(tok))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def stance_of(score: float, *, hi: float = 0.60, lo: float = 0.40) -> Stance:
    return "BULL" if score >= hi else ("BEAR" if score <= lo else "NEUTRAL")


class Agent(ABC):
    """所有智能体的基类。"""

    name: str = "agent"
    #: LLM 不可用时是否仍然产出结果（分析师=True；纯 LLM 修饰类节点可设 False）
    degrade_ok: bool = True
    temperature: float = 0.0
    #: 该 Agent 对应的 LLM 场景（用于智能选模）；None 则走默认模型
    scene: str | None = None

    def __init__(self, client=None, *, model: str | None = None, use_llm: bool = True):
        self.client = client
        self.model = model
        self.use_llm = use_llm and client is not None

    # ------------------------------------------------------------- 子类实现
    @abstractmethod
    def _rule_based(self, state: AgentState) -> AnalystReport:
        """确定性路径。必须实现。"""

    def _prompt(self, state: AgentState) -> str:
        """LLM 路径的 prompt。不实现则永远走规则路径。"""
        return ""

    def _merge_llm(self, base: AnalystReport, data: dict[str, Any],
                   raw: str, state: AgentState) -> AnalystReport:
        """把 LLM 的结构化输出合并进规则结论。默认：LLM 只微调分数与补充要点。"""
        if "score" in data:
            try:
                llm_score = max(0.0, min(1.0, float(data["score"])))
                # 规则为主 LLM 为辅：7:3 加权，避免模型一句话推翻因子体系
                base.score = round(0.7 * base.score + 0.3 * llm_score, 4)
            except (TypeError, ValueError):
                base.issues.append("LLM score 非法")
        if isinstance(data.get("highlights"), list):
            base.highlights.extend(str(x) for x in data["highlights"][:3])
        if isinstance(data.get("risks"), list):
            base.risks.extend(str(x) for x in data["risks"][:3])
        if "confidence" in data:
            try:
                base.confidence = max(0.0, min(1.0, float(data["confidence"])))
            except (TypeError, ValueError):
                pass
        base.stance = stance_of(base.score)
        base.raw = raw
        base.cited_numbers = extract_numbers(raw)
        return base

    # --------------------------------------------------------------- 主入口
    def run(self, state: AgentState) -> AnalystReport:
        with state.timeit(self.name):
            report = self._rule_based(state)
            prompt = self._prompt(state) if self.use_llm else ""
            if not prompt:
                return report
            try:
                resp = self.client.complete(
                    prompt, scene=self.scene, model=self.model,
                    temperature=self.temperature, tag=self.name
                )
                if resp.cached:
                    # 缓存命中：没有新的真实调用与费用，只计数（避免成本虚报）
                    state.llm_cached += 1
                else:
                    state.llm_calls += 1
                    state.llm_cost_cny += resp.cost_cny
                data = parse_json_loose(resp.content)
                if not data:
                    state.degraded.append(f"{self.name}:LLM输出不可解析")
                    return report
                return self._merge_llm(report, data, resp.content, state)
            except LLMBudgetExceeded as exc:
                # 成本熔断：整个 LLM 层停用，降级纯因子（设计 6.4.4）
                state.degraded.append(f"{self.name}:预算熔断")
                logger.warning("LLM 预算熔断，降级纯因子模式: %s", exc)
                self.use_llm = False
                return report
            except Exception as exc:
                state.errors.append(f"{self.name}:{type(exc).__name__}")
                logger.warning("%s LLM 调用失败，回退规则路径: %s", self.name, exc)
                return report
