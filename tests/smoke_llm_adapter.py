"""LLM 管理层冒烟测试（独立 llm.yaml + 多 provider/多 model + 场景智能选模）。

重点不是"能调通"，而是**挂了 / 选错 / 超预算之后系统怎么办**：

- 多平台多模型配置能从 ``config/llm.yaml`` 正确加载；
- 场景化智能选模按 capability/health/cost 加权打分，推理场景挑推理模型、
  便宜场景挑便宜模型、熔断模型自动出局；
- ``LLMManager.complete`` 按候选链 fallback，主选失败自动下一个；
- 成本按人民币算并进熔断；预算超限抛 ``LLMBudgetExceeded``；
- 网络抖动/限流 → 退避重试；配置错(4xx) 立即失败；无论怎么失败 Agent 必须降级规则路径（P5）；
- 缺 API Key / provider=mock → 静默退回 MockLLM，系统照常起得来。

全程不发真实网络请求：openai_like 用假 Session 注入；manager 用 monkey-patch 注入假 adapter。
"""

from __future__ import annotations
import logging

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qmt_trade.brain.llm.base import LLMAdapter, LLMResponse          # noqa: E402
from qmt_trade.brain.llm.cost import CostTracker                      # noqa: E402
from qmt_trade.brain.llm.factory import build_adapter                 # noqa: E402
from qmt_trade.brain.llm.manager import LLMManager                    # noqa: E402
from qmt_trade.brain.llm.registry import (                            # noqa: E402
    LLMConfig, ModelConfig, ProviderConfig, SceneConfig,
    SelectionConfig, load_llm_config,
)
from qmt_trade.brain.llm.health import ModelHealth                    # noqa: E402
from qmt_trade.brain.llm.selector import ModelSelector                # noqa: E402
from qmt_trade.brain.llm.mock import MockLLM                         # noqa: E402
from qmt_trade.brain.llm.openai_like import OpenAILikeAdapter         # noqa: E402
from qmt_trade.core.errors import LLMBudgetExceeded, LLMCallFailed    # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger(__name__)

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        logger.info(f"  [OK]   {name} {extra}")
    else:
        FAIL += 1
        logger.info(f"  [FAIL] {name} {extra}")
    return bool(cond)


# ------------------------------------------------------------------ 假 HTTP
class FakeResp:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.last_payload = None
        self.headers = {}

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        self.last_payload = json
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def _ok_payload(content='{"action":"BUY"}', pt=1000, ct=500, model="deepseek-chat"):
    return {"id": "x", "model": model,
            "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct}}


PRICES = {"deepseek-chat": {"input": 0.001, "output": 0.002}}


def _adapter(script, **kw):
    a = OpenAILikeAdapter(base_url="https://fake/v1", api_key="sk-test",
                          default_model="deepseek-chat", prices=PRICES,
                          max_retries=kw.pop("max_retries", 2), name="fake", **kw)
    a._session = FakeSession(script)
    return a


# ====================================================== 1. 正常调用（openai_like）
def test_happy_path() -> None:
    logger.info("\n[1] 正常调用")
    a = _adapter([FakeResp(200, _ok_payload())])
    r = a.complete("SYMBOL: 600000.SH\n请研判", model="deepseek-chat", temperature=0.0)
    check("返回 LLMResponse", isinstance(r, LLMResponse))
    check("内容透传", r.content == '{"action":"BUY"}', r.content)
    check("token 数取自 usage", r.prompt_tokens == 1000 and r.completion_tokens == 500,
          f"{r.prompt_tokens}/{r.completion_tokens}")
    check("成本按人民币价目表算", abs(r.cost_cny - 0.002) < 1e-9, f"¥{r.cost_cny:.6f}")
    check("只发了一次请求", a._session.calls == 1, f"{a._session.calls} 次")

    p = a._session.last_payload
    check("请求体含 model", p.get("model") == "deepseek-chat")
    check("请求体含 messages", isinstance(p.get("messages"), list) and p["messages"])
    check("默认非流式", p.get("stream") is False)

    a2 = _adapter([FakeResp(200, _ok_payload())])
    a2.complete("x", json_mode=True, max_tokens=512)
    p2 = a2._session.last_payload
    check("json_mode 生成 response_format",
          p2.get("response_format") == {"type": "json_object"})
    check("max_tokens 透传", p2.get("max_tokens") == 512)


# ====================================================== 2. 重试
def test_retry() -> None:
    logger.info("\n[2] 可重试错误 —— 退避后重试")
    for status in (429, 500, 502, 503):
        a = _adapter([FakeResp(status, text="busy"), FakeResp(200, _ok_payload())],
                     max_retries=2)
        r = a.complete("x")
        check(f"HTTP {status} 重试后成功", r.content == '{"action":"BUY"}',
              f"{a._session.calls} 次请求")

    a = _adapter([FakeResp(429, text="rate limited")], max_retries=2)
    ok = False
    try:
        a.complete("x")
    except LLMCallFailed as exc:
        ok = "429" in str(exc)
    check("重试耗尽抛 LLMCallFailed", ok)
    check("确实重试了 max_retries+1 次", a._session.calls == 3, f"{a._session.calls} 次")

    a = _adapter([ConnectionError("网络断了"), FakeResp(200, _ok_payload())])
    r = a.complete("x")
    check("连接异常也重试", r.content == '{"action":"BUY"}', f"{a._session.calls} 次")

    a = _adapter([TimeoutError("超时")], max_retries=1)
    ok = False
    try:
        a.complete("x")
    except LLMCallFailed:
        ok = True
    check("连接异常重试耗尽抛 LLMCallFailed", ok)
    check("超时也按 max_retries+1 次", a._session.calls == 2, f"{a._session.calls} 次")


# ====================================================== 3. 不可重试
def test_no_retry() -> None:
    logger.info("\n[3] 配置类错误 —— 立即失败不浪费配额")
    for status in (400, 401, 403, 404, 422):
        a = _adapter([FakeResp(status, text="bad key")], max_retries=3)
        ok = False
        try:
            a.complete("x")
        except LLMCallFailed as exc:
            ok = str(status) in str(exc)
        check(f"HTTP {status} 立即失败", ok)
        check(f"HTTP {status} 未重试", a._session.calls == 1, f"{a._session.calls} 次")

    a = _adapter([FakeResp(200, None, text="<html>网关错误</html>")])
    ok = False
    try:
        a.complete("x")
    except LLMCallFailed as exc:
        ok = "非 JSON" in str(exc)
    check("响应非 JSON → LLMCallFailed", ok)

    a = _adapter([FakeResp(200, {"choices": []})])
    ok = False
    try:
        a.complete("x")
    except LLMCallFailed as exc:
        ok = "结构异常" in str(exc)
    check("响应结构异常 → LLMCallFailed", ok)


# ====================================================== 4. 成本
def test_cost() -> None:
    logger.info("\n[4] 成本核算 —— 宁可高估不可算 0")
    a = _adapter([FakeResp(200, _ok_payload(model="未知模型"))])
    r = a.complete("x", model="未知模型")
    check("未配价的模型走兜底价而非 0", r.cost_cny > 0, f"¥{r.cost_cny:.6f}")

    a = _adapter([FakeResp(200, {"model": "deepseek-chat",
                                 "choices": [{"message": {"content": "hi"}}]})])
    r = a.complete("这是一段比较长的提示词" * 20)
    check("网关不回 usage 时按字符粗估", r.prompt_tokens > 0, f"{r.prompt_tokens} tok")
    check("粗估也要算出成本", r.cost_cny > 0, f"¥{r.cost_cny:.6f}")

    tracker = CostTracker(daily_budget_cny=0.005, monthly_budget_cny=100.0)
    tracker.record(LLMResponse(content="", model="m", cost_cny=0.004))
    tracker.check()
    check("未超预算不熔断", tracker.day_cost < 0.005, f"¥{tracker.day_cost:.4f}")
    tracker.record(LLMResponse(content="", model="m", cost_cny=0.004))
    ok = False
    try:
        tracker.check()
    except LLMBudgetExceeded as exc:
        ok = "¥" in str(exc)
    check("超预算熔断且以人民币提示", ok)

    check("默认日预算 30 元", CostTracker().daily_budget_cny == 30.0)
    check("默认月预算 600 元", CostTracker().monthly_budget_cny == 600.0)


def test_cost_tracker_threadsafe() -> None:
    logger.info("\n[5] 成本累加并发安全")
    import threading
    tracker = CostTracker(daily_budget_cny=10_000.0, monthly_budget_cny=10_000.0)

    def worker():
        for _ in range(200):
            tracker.record(LLMResponse(content="", model="m", cost_cny=0.01))

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("并发累加不丢数", abs(tracker.day_cost - 16.0) < 1e-6,
          f"¥{tracker.day_cost:.4f} 期望 ¥16.0000")
    check("history 条数正确", len(tracker.history) == 1600, str(len(tracker.history)))


# ====================================================== 6. 缓存并发
def test_cache_concurrent() -> None:
    logger.info("\n[6] LLM 缓存并发安全（曾把整层 LLM 误判为故障）")
    import threading
    from qmt_trade.brain.llm.cache import LLMCache

    cache = LLMCache()
    errors: list[str] = []

    def worker(i: int):
        try:
            for k in range(60):
                key = f"prompt-{i}-{k}"
                cache.put("m", key, 0.0, LLMResponse(content=f"c{k}", model="m"))
                cache.get("m", key, 0.0)
        except Exception as exc:                      # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("并发读写零异常（无 SQLITE_MISUSE）", not errors, "; ".join(errors[:2]))
    hit = cache.get("m", "prompt-0-0", 0.0)
    check("缓存命中且标记 cached", hit is not None and hit.cached is True)
    cache.close()


# ====================================================== 7. 配置注册表
def test_registry() -> None:
    logger.info("\n[7] 配置注册表（多 provider / 多 model / 多 scene）")
    c = load_llm_config()
    check("加载 ≥4 个平台", len(c.providers) >= 4, str(len(c.providers)))
    check("加载 ≥6 个模型", len(c.models) >= 6, str(len(c.models)))
    check("加载 ≥5 个场景", len(c.scenes) >= 5, str(len(c.scenes)))
    check("默认模型存在", c.get_model(c.default_model) is not None, c.default_model)
    check("按场景取候选", c.get_scene("market_analysis") is not None)
    check("provider 不含明文 key（只存环境变量名）",
          all((p.api_key_env or p.type == "mock") for p in c.providers))


# ====================================================== 8. 场景智能选模
def test_selector() -> None:
    logger.info("\n[8] 场景化智能选模（capability + health + cost 加权）")
    c = load_llm_config()
    s = ModelSelector(c)

    chain = s.select_chain("market_analysis")
    check("market_analysis 首位是推理模型", chain[0] == "deepseek-reasoner", str(chain))

    chain2 = s.select_chain("quick_classify")
    check("quick_classify 靠前的是便宜模型",
          chain2[0] in ("qwen-plus", "deepseek-chat"), str(chain2))

    chain3 = s.select_chain("risk_assess")
    check("risk_assess 首位是推理模型", chain3[0] == "deepseek-reasoner", str(chain3))

    # 熔断模型应出局
    s.health_of("deepseek-reasoner").consecutive_failures = 5
    check("连续失败达阈值 → 出局", "deepseek-reasoner" not in s.select_chain("market_analysis"))
    # 恢复后重新进入
    s.health_of("deepseek-reasoner").consecutive_failures = 0
    check("恢复后重新入选", "deepseek-reasoner" in s.select_chain("market_analysis"))


# ====================================================== 9. 健康度
def test_health() -> None:
    logger.info("\n[9] 模型健康度与熔断")
    h = ModelHealth("m")
    h.record_success(10)
    h.record_success(12)
    check("成功率正确", abs(h.success_rate - 1.0) < 1e-9)
    h.record_failure("err")
    check("连续失败计数", h.consecutive_failures == 1)
    check("未熔断", not h.is_circuit_open())
    h.consecutive_failures = 5
    check("连续失败达阈值 → 不可用时触发熔断", not h.can_use())
    check("熔断后 is_circuit_open", h.is_circuit_open())


# ====================================================== 10. manager 基础 + 场景 + 缓存
def test_manager_basic() -> None:
    logger.info("\n[10] LLMManager 场景选模 + 缓存 + 成本")
    m = LLMManager.from_file()
    r = m.complete("SYMBOL:600000 深度研判", scene="market_analysis", tag="t")
    check("场景选模返回响应", r is not None and bool(r.content), r.model)
    check("场景解析到推理模型", r.model == "deepseek-reasoner", r.model)

    r2 = m.complete("SYMBOL:600000 深度研判", scene="market_analysis", tag="t")
    check("相同请求命中缓存", r2.cached is True)
    check("成本已累计", m.cost.day_cost >= 0, f"¥{m.cost.day_cost:.4f}")

    # 显式 model 优先
    r3 = m.complete("SYMBOL:000001 分类", model="qwen-plus", tag="t")
    check("显式 model 优先", r3.model == "qwen-plus", r3.model)


# ====================================================== 11. manager fallback 链
def test_manager_fallback() -> None:
    logger.info("\n[11] LLMManager 候选链 fallback（主选失败自动下一个）")

    class FlakyAdapter(LLMAdapter):
        name = "flaky"

        def __init__(self, fail_times: int = 1):
            self.fail_times = fail_times
            self.calls = 0

        def complete(self, prompt, *, model=None, temperature=0.0, **kw):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise LLMCallFailed("临时故障")
            return LLMResponse(content="ok", model=model or "m", cost_cny=0.0)

    class OKAdapter(LLMAdapter):
        name = "ok"

        def complete(self, prompt, *, model=None, temperature=0.0, **kw):
            return LLMResponse(content="ok", model=model or "m", cost_cny=0.0)

    cfg = LLMConfig(
        enabled=True, cache_enabled=False,
        providers=[ProviderConfig(id="p1", type="mock")],
        models=[
            ModelConfig(id="deepseek-reasoner", provider="p1", name="deepseek-reasoner",
                        capabilities=("reasoning", "deep")),
            ModelConfig(id="qwen-plus", provider="p1", name="qwen-plus",
                        capabilities=("general", "fast", "cheap")),
        ],
        scenes=[SceneConfig(id="market_analysis", prefer=("reasoning", "deep"),
                             candidates=("deepseek-reasoner", "qwen-plus"))],
    )
    m = LLMManager(cfg)
    seq = {"deepseek-reasoner": FlakyAdapter(fail_times=1), "qwen-plus": OKAdapter()}
    m._adapter_for = lambda mid: seq.get(mid) or MockLLM()
    r = m.complete("x", scene="market_analysis")
    check("主选失败 → 自动 fallback 到次选", r.model == "qwen-plus", r.model)
    check("主选被记失败", m.health["deepseek-reasoner"].consecutive_failures == 1)


# ====================================================== 12. manager 预算熔断
def test_manager_budget() -> None:
    logger.info("\n[12] LLMManager 预算熔断（与体检同源）")

    class PricyAdapter(LLMAdapter):
        name = "pricy"

        def complete(self, prompt, *, model=None, temperature=0.0, **kw):
            return LLMResponse(content="x", model=model or "m", cost_cny=0.01)

    cfg = LLMConfig(
        enabled=True, cache_enabled=False,
        budget={"daily_cny": 0.001, "monthly_cny": 100.0},
        providers=[ProviderConfig(id="p1", type="mock")],
        models=[ModelConfig(id="m1", provider="p1", name="m1")],
    )
    m = LLMManager(cfg)
    m._adapter_for = lambda mid: PricyAdapter()
    r = m.complete("x")
    check("首次调用成功并记录成本", r.cost_cny == 0.01, f"¥{r.cost_cny}")
    ok = False
    try:
        m.complete("x")        # 此时 day_cost=0.01 > 预算 0.001 → 熔断
    except LLMBudgetExceeded:
        ok = True
    check("超预算抛 LLMBudgetExceeded", ok)

    # 与配置同源
    real = load_llm_config()
    rm = LLMManager.from_file()
    check("from_file 日预算==配置", abs(rm.cost.daily_budget_cny
          - float(real.budget.get("daily_cny", 0))) < 1e-9,
          f"¥{rm.cost.daily_budget_cny}")


# ====================================================== 13. 工厂装配
def test_factory_fallback() -> None:
    logger.info("\n[13] 工厂装配 —— 接不上就安静退回 MockLLM")
    # mock 类型
    check("mock provider → MockLLM",
          isinstance(build_adapter(ProviderConfig(id="mock", type="mock"), None), MockLLM))

    p = ProviderConfig(id="deepseek", type="openai_like", api_key_env="DEEPSEEK_API_KEY",
                       base_url="https://api.deepseek.com/v1")
    mdl = ModelConfig(id="deepseek-chat", provider="deepseek", name="deepseek-chat")
    check("无 API Key → MockLLM（系统照常起得来）",
          isinstance(build_adapter(p, mdl), MockLLM))

    os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    try:
        a = build_adapter(p, mdl)
        check("有 Key → 真实 adapter", isinstance(a, OpenAILikeAdapter), type(a).__name__)
        check("base_url 透传", a.base_url == "https://api.deepseek.com/v1", a.base_url)
    finally:
        del os.environ["DEEPSEEK_API_KEY"]

    pc = ProviderConfig(id="custom", type="openai_like", base_url="https://gw/v1",
                        api_key_env="CUSTOM_KEY")
    os.environ["CUSTOM_KEY"] = "sk"
    try:
        a2 = build_adapter(pc, ModelConfig(id="x", provider="custom", name="x"))
        check("custom base_url 生效", a2.base_url == "https://gw/v1", a2.base_url)
    finally:
        del os.environ["CUSTOM_KEY"]


# ====================================================== 14. 降级闭环
def test_agent_degrade() -> None:
    logger.info("\n[14] 模型全挂 → Agent 降级规则路径（P5 闭环）")
    from datetime import date

    from qmt_trade.brain.graph import build_brain
    from qmt_trade.brain.llm.client import LLMClient
    from qmt_trade.brain.state import PortfolioSnapshot
    from qmt_trade.core.config import Settings as S
    from qmt_trade.datahub.manager import DataHub
    from qmt_trade.datahub.providers.mock import MockProvider
    from qmt_trade.selection.pipeline import SelectionPipeline

    settings = S.load()
    hub = DataHub(settings, providers=[MockProvider()])
    day = date(2026, 8, 7)
    cs = SelectionPipeline(settings, hub).run(day)

    class DeadAdapter:
        name = "dead"

        def complete(self, prompt, *, model=None, temperature=0.0, **kw):
            raise LLMCallFailed("模型全挂了")

        def embed(self, text):
            return None

    brain = build_brain(settings, hub, use_llm=True)
    brain.client = LLMClient(DeadAdapter(), cache_enabled=False)
    for agent in getattr(brain, "analysts", []):
        agent.client = brain.client

    snap = PortfolioSnapshot(total_asset=1_000_000, cash=1_000_000,
                             position_weight={}, industry_weight={},
                             max_positions=10, max_position_pct=0.8, drawdown=0.0)
    res = brain.run(cs, snap)
    check("模型全挂仍然跑完，不抛异常", res is not None)
    check("仍然产出 Intent（规则路径兜底）", len(res.intents) > 0,
          f"{len(res.intents)} 个")
    check("成本为 0（一次都没调成）", res.llm_cost_cny == 0.0, f"¥{res.llm_cost_cny}")


def main() -> int:
    logger.info("=" * 64)
    logger.info("LLM 管理层冒烟测试（独立 llm.yaml + 场景智能选模）")
    logger.info("=" * 64)
    test_happy_path()
    test_retry()
    test_no_retry()
    test_cost()
    test_cost_tracker_threadsafe()
    test_cache_concurrent()
    test_registry()
    test_selector()
    test_health()
    test_manager_basic()
    test_manager_fallback()
    test_manager_budget()
    test_factory_fallback()
    test_agent_degrade()
    logger.info("\n" + "=" * 64)
    logger.info(f"结果: {PASS} 通过 / {FAIL} 失败")
    logger.info("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())