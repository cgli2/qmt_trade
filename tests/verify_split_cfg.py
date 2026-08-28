import sys, os, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.WARNING)

from qmt_trade.app import build_context
from qmt_trade.strategies.etf_t0 import ETFT0Config, load_config

with build_context("paper") as ctx:
    cfg = load_config(ctx.settings, ETFT0Config, "etf_t0")
    print("symbols:", cfg.symbols)
    print("momentum_mode:", cfg.momentum_mode, "| window:", cfg.momentum_window_min,
          "| thr:", cfg.momentum_threshold)
    print("override:", cfg.momentum_override)
    for sym in cfg.symbols:
        print(f"  {sym} -> {cfg.momentum_params_for(sym)}")
    assert "513310.SH" in cfg.symbols, "513310 未进默认标的池！"
    assert cfg.momentum_params_for("513310.SH") == ("confirm", 15, 0.015), "513310 解析错误"
    assert cfg.momentum_params_for("513100.SH") == ("filter", 15, 0.004), "513100 解析错误"
    assert cfg.momentum_params_for("518880.SH") == ("filter", 15, 0.004), "518880 解析错误"
    print("按标解析断言全部通过 ✓")
