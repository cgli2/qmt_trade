"""行情质量校验（validate_bars / warn_quality）回归测试。

对应缺陷：日志里反复出现
``行情数据质量问题: low 存在非正价格; 118 条记录涨跌幅超过 35%``。

根因是校验把 A 股两类**合法**极端值和真脏数据混在一个 ``issues`` 列表里：
停牌日 OHLC 记 0、新股期不设涨跌幅限制。后果不止是日志噪音——``data_sync`` 属
``CRITICAL_JOBS``，``not ok`` 会把整个系统降级到 REDUCE_ONLY，一只停牌股就能拉闸。

运行：python tests/test_datahub_quality.py
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from qmt_trade.core.errors import DataQualityError  # noqa: E402
from qmt_trade.datahub import manager as M  # noqa: E402
from qmt_trade.datahub.manager import DataHub, QualityReport, warn_quality  # noqa: E402
from qmt_trade.datahub.types import InstrumentInfo  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger(__name__)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        logger.info(f"  [OK]   {name} {extra}")
    else:
        FAIL += 1
        logger.info(f"  [FAIL] {name} {extra}")


class _FakeSettings:
    """只喂 DataHub.__init__ 真正读到的那几个段。

    刻意**不走 ``get_settings()``**：``Settings.load()`` 会打开
    ``data/db/qmt.duckdb`` 读已发布配置，而后端进程常年持有该库的单进程独占锁，
    测试会直接 PermissionError。质量校验本来也不需要任何真实配置。
    """

    def __init__(self, quality: dict | None = None):
        self.data_dir = Path(sys.argv[0]).resolve().parent.parent / "data"
        self._quality = {"max_missing_ratio": 0.2, "max_abs_return": 0.35,
                         "new_listing_grace_days": 10, "warn_interval": 600}
        if quality:
            self._quality.update(quality)

    def section(self, key: str) -> dict:
        if key == "datahub.quality":
            return dict(self._quality)
        if key == "datahub.priority":
            return {}
        if key == "datahub.circuit_breaker":
            return {"fail_threshold": 3, "cooldown_seconds": 300}
        if key == "datahub.cache":
            return {"max_items": 16, "minute_bar_ttl": 60,
                    "daily_bar_ttl": 86400, "fundamental_ttl": 86400}
        return {}


class _NullStore:
    """占位 store：本测试只走 validate_bars，不该碰 DuckDB（单进程独占锁）。"""


def _hub(**quality) -> DataHub:
    return DataHub(_FakeSettings(quality or None), providers=[], store=_NullStore())


def _frame(rows: list[dict]) -> pd.DataFrame:
    """拼一个已带 prev_close/is_suspended 的帧（模拟 _normalize_bars 的产物）。"""
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    if "prev_close" not in df.columns:
        df["prev_close"] = df.groupby("symbol")["close"].shift(1)
        df["prev_close"] = df["prev_close"].fillna(df["close"])
    if "is_suspended" not in df.columns:
        df["is_suspended"] = df.get("volume", 1) <= 0
    return df.sort_values(["symbol", "date"]).reset_index(drop=True)


def _normal(sym: str, d0: str, n: int = 6, px: float = 10.0) -> list[dict]:
    """n 天正常行情：每天 +1%，volume>0。"""
    out = []
    for i in range(n):
        c = round(px * (1.01 ** i), 2)
        out.append({"symbol": sym, "date": f"2026-09-{int(d0) + i:02d}",
                    "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                    "volume": 10000})
    return out


# ============================================================ [1] 基线：干净数据
logger.info("\n[1] 干净数据不报 issue")
hub = _hub()
df = _frame(_normal("600000.SH", "01") + _normal("000001.SZ", "01"))
rep = hub.validate_bars(df)
check("干净帧 ok=True", rep.ok, str(rep)[:100])
check("干净帧无 notes", not rep.notes, str(rep.notes)[:100])
check("空数据集仍是 issue", not hub.validate_bars(pd.DataFrame()).ok)

# ============================================================ [2] 停牌行豁免
logger.info("\n[2] 停牌行 OHLC=0 → note 而非 issue（本次缺陷主因）")
rows = _normal("600000.SH", "01")
rows[3] = dict(rows[3], open=0.0, high=0.0, low=0.0, close=0.0, volume=0)   # 停牌日
susp_df = _frame(rows)
rep2 = hub.validate_bars(susp_df)
check("停牌帧 ok=True（不拉闸）", rep2.ok, str(rep2)[:160])
check("停牌帧留了 note", any("停牌" in n for n in rep2.notes), str(rep2.notes)[:160])
check("停牌未混进 issues", not any("非正价格" in i for i in rep2.issues), str(rep2.issues)[:160])

# 对照：有成交的行 low=0 必须仍是阻断项，且带条数与标的@日期
bad_rows = _normal("000001.SZ", "01")
bad_rows[2] = dict(bad_rows[2], low=0.0)
rep3 = hub.validate_bars(_frame(bad_rows))
check("非停牌 low=0 → issue", not rep3.ok, str(rep3)[:160])
issue_txt = " ".join(rep3.issues)
check("issue 带条数", "1 条" in issue_txt, issue_txt[:160])
check("issue 带标的@日期", "000001.SZ@2026-09-03" in issue_txt, issue_txt[:160])

# ============================================================ [3] 停牌复牌跳变
logger.info("\n[3] 停牌复牌的大幅跳变 → note")
# 停牌日沿用前收盘价、volume=0（akshare 的常见表达）。若停牌日 close 记 0，复牌日的
# prev_close 也是 0 → 比值为 NA 根本不参与涨跌幅判定，那样就测不到 prev_susp 豁免了。
rows = _normal("600000.SH", "01", n=8)                     # 09-01..09-08
carry = rows[2]["close"]
rows[3] = dict(rows[3], open=carry, high=carry, low=carry, close=carry, volume=0)
for i in range(4, 8):                                        # 复牌腰斩后按新价续接
    c = round(5.0 * (1.01 ** (i - 4)), 2)
    rows[i] = dict(rows[i], open=c, high=round(c * 1.01, 2),
                   low=round(c * 0.99, 2), close=c, volume=9000)
resume_df = _frame(rows)
rep4 = hub.validate_bars(resume_df)
check("复牌跳变 ok=True", rep4.ok, str(rep4)[:220])
check("复牌跳变进了 note", any("停牌复牌" in n for n in rep4.notes), str(rep4.notes)[:220])
check("复牌日样本可定位", any("600000.SH@2026-09-05" in n for n in rep4.notes),
      str(rep4.notes)[:220])

# ============================================================ [4] 新股期豁免
logger.info("\n[4] 新股期无涨跌幅限制 → note（只读 _instrument_cache，不派单取数）")
hub2 = _hub()
hub2._instrument_cache["301999.SZ"] = InstrumentInfo(
    symbol="301999.SZ", name="新股样本", list_date=date(2026, 9, 1))
new_rows = _normal("301999.SZ", "01", n=6, px=20.0)
new_rows[2] = dict(new_rows[2], open=40.0, high=41.0, low=39.0, close=40.0, volume=8000)
rep5 = hub2.validate_bars(_frame(new_rows))
check("新股期跳变 ok=True", rep5.ok, str(rep5)[:200])
check("新股期进了 note", any("新股期" in n for n in rep5.notes), str(rep5.notes)[:200])

# 同一根 K 线放到老股上必须仍是 issue（证明豁免是按 list_date 判定而非一刀切）
hub3 = _hub()
hub3._instrument_cache["600000.SH"] = InstrumentInfo(
    symbol="600000.SH", name="老股样本", list_date=date(2000, 1, 1))
old_rows = _normal("600000.SH", "01", n=6, px=20.0)
old_rows[2] = dict(old_rows[2], open=40.0, high=41.0, low=39.0, close=40.0, volume=8000)
rep6 = hub3.validate_bars(_frame(old_rows))
check("老股同样跳变 → issue", not rep6.ok, str(rep6)[:200])

# grace=0 时豁免关闭（走配置而非直接改属性，确保接线是真的）
hub4 = _hub(new_listing_grace_days=0)
hub4._instrument_cache["301999.SZ"] = InstrumentInfo(
    symbol="301999.SZ", name="新股样本", list_date=date(2026, 9, 1))
check("grace=0 关闭新股豁免", not hub4.validate_bars(_frame(new_rows)).ok)

# ============================================================ [5] 真异常仍阻断
logger.info("\n[5] 无解释的异常涨跌幅必须阻断（保住 smoke_datahub 的既有断言）")
clean = _frame(_normal("600000.SH", "01", n=8) + _normal("000001.SZ", "01", n=8))
tampered = clean.copy()
tampered.loc[tampered.index[10], "close"] = tampered.loc[tampered.index[10], "close"] * 3
rep7 = hub.validate_bars(tampered)
check("close*3 → not ok", not rep7.ok, str(rep7)[:200])
check("涨跌幅 issue 带条数", "条记录涨跌幅超过" in " ".join(rep7.issues),
      " ".join(rep7.issues)[:200])

# high < low 仍阻断且带条数
inv = clean.copy()
inv.loc[inv.index[5], "high"] = 1.0
inv.loc[inv.index[5], "low"] = 99.0
rep8 = hub.validate_bars(inv)
check("high<low → not ok", not rep8.ok, str(rep8)[:200])
check("high<low issue 带条数", "1 条" in " ".join(rep8.issues), " ".join(rep8.issues)[:200])

# require_clean_bars 只被 issues 触发，不被 notes 触发。
# 这里必须自己兜住异常：裸调的话，一旦停牌豁免被改坏，脚本会在此中途 abort，后面的
# 断言与 PASS/FAIL 汇总全部丢失 —— 而"停牌帧被拦下"恰恰是本缺陷最严重的后果
# （data_sync 属 CRITICAL 任务，一次误判就把全系统降级到 REDUCE_ONLY），必须留痕。
try:
    hub.require_clean_bars(susp_df)      # 只有停牌 note → 不该抛
    check("require_clean_bars 放行停牌 note", True)
except DataQualityError as exc:
    check("require_clean_bars 放行停牌 note", False, str(exc)[:160])
try:
    hub.require_clean_bars(tampered)
    check("require_clean_bars 拦截真异常", False)
except DataQualityError:
    check("require_clean_bars 拦截真异常", True)

# ============================================================ [6] 告警限流去重
logger.info("\n[6] warn_quality 签名去重 + 限流")


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


cap = _Cap()
lg = logging.getLogger("qmt_trade.datahub.manager")
lg.addHandler(cap)
lg.setLevel(logging.DEBUG)
lg.propagate = False

M._quality_warn_state.clear()
r_a = QualityReport()
r_a.add("118 条记录涨跌幅超过 35%")
warn_quality(r_a, interval=600)
warn_quality(r_a, interval=600)
r_b = QualityReport()
r_b.add("120 条记录涨跌幅超过 35%")     # 条数变了但种类没变 → 同一签名
warn_quality(r_b, interval=600)
check("同类告警只打一条", len(cap.records) == 1, f"实际 {len(cap.records)} 条")
warn_quality(r_a, interval=0)             # 窗口 0 → 立刻放行，并带上抑制计数
check("窗口过期后放行", len(cap.records) == 2, f"实际 {len(cap.records)} 条")
check("带抑制计数", "被抑制" in cap.records[-1].getMessage(), cap.records[-1].getMessage()[:160])

cap.records.clear()
M._quality_warn_state.clear()
r_note = QualityReport()
r_note.note("3 条停牌记录已豁免校验")
warn_quality(r_note, interval=600)
check("纯 note 不打 WARNING",
      not any(r.levelno >= logging.WARNING for r in cap.records),
      str([r.levelname for r in cap.records]))
check("纯 note 降到 DEBUG",
      any(r.levelno == logging.DEBUG for r in cap.records),
      str([r.levelname for r in cap.records]))

lg.removeHandler(cap)

# ============================================================ [7] 配置接线
logger.info("\n[7] 配置项已接线")
h = _hub()
check("new_listing_grace_days 默认 10", h.new_listing_grace_days == 10,
      str(h.new_listing_grace_days))
check("quality_warn_interval 默认 600", h.quality_warn_interval == 600.0,
      str(h.quality_warn_interval))
h0 = _hub(new_listing_grace_days=0, warn_interval=30, max_abs_return=0.5)
check("datahub.quality 段可覆盖 grace", h0.new_listing_grace_days == 0)
check("datahub.quality 段可覆盖 warn_interval", h0.quality_warn_interval == 30.0)
check("datahub.quality 段可覆盖 max_abs_return", h0.max_abs_return == 0.5)
rep9 = h0.validate_bars(_frame(_normal("600000.SH", "01", n=4, px=10.0)))
check("放宽 max_abs_return 后干净帧仍 ok", rep9.ok, str(rep9)[:160])

print(f"\n===== PASS={PASS} FAIL={FAIL} =====")
sys.exit(1 if FAIL else 0)
