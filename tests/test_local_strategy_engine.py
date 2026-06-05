import os
import sys
import logging
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from local_strategy_engine import LocalStrategyEngine


def test_local_strategy_payload_has_stable_client_order_id(tmp_path):
    app = SimpleNamespace(instance_path=str(tmp_path))
    engine = LocalStrategyEngine(app, lambda *_: None, "https://paper-api.alpaca.markets", logging.getLogger("test"))

    payload_a = engine.build_payload("AAPL", "buy", 1000, "reason", "15Min", "2026-06-02T15:00:00Z", {"job_id": "job1"})
    payload_b = engine.build_payload("AAPL", "buy", 1000, "reason", "15Min", "2026-06-02T15:00:00Z", {"job_id": "job1"})

    assert payload_a["client_order_id"] == payload_b["client_order_id"]
    assert len(payload_a["client_order_id"]) <= 48
    assert payload_a["local_strategy_request"] is True
    assert payload_a["strategy_job_id"] == "job1"


def test_local_strategy_params_use_backtest_over_config(tmp_path):
    app = SimpleNamespace(instance_path=str(tmp_path))
    engine = LocalStrategyEngine(app, lambda *_: None, "https://paper-api.alpaca.markets", logging.getLogger("test"))

    params = engine.params_from_backtest(
        {"inner_kc_length": 33, "fixed_stop_loss_pct": 4.7},
        {"params": {"inner_kc_length": 12, "fixed_stop_loss_pct": 2.5}},
    )

    assert params["inner_kc_length"] == 12
    assert params["fixed_stop_loss_pct"] == 2.5


def test_llm_shadow_validation_does_not_block_entry_execution(tmp_path):
    app = SimpleNamespace(instance_path=str(tmp_path))
    executed = []

    def execute_trade(_user, payload):
        executed.append(payload)
        return True, 200, {"result": "opened"}, None

    class FakeValidator:
        def __init__(self):
            self.calls = []

        def submit_entry_signal(self, **kwargs):
            self.calls.append(kwargs)

    engine = LocalStrategyEngine(app, execute_trade, "https://paper-api.alpaca.markets", logging.getLogger("test"))
    engine.llm_validator = FakeValidator()
    user = SimpleNamespace(id=3, username="paper", per_trade_amount=1000)
    api = SimpleNamespace(api_key="key", api_secret="secret")
    idx = pd.to_datetime(["2026-06-02T14:45:00Z", "2026-06-02T15:00:00Z"])
    frame = pd.DataFrame(
        [
            {"open": 100, "high": 101, "low": 98, "close": 98.5, "mid_inner": 100, "up_inner": 104, "low_inner": 99},
            {"open": 98.5, "high": 101, "low": 98, "close": 99.5, "mid_inner": 100, "up_inner": 104, "low_inner": 99},
        ],
        index=idx,
    )
    params = {
        "trade_direction": "Both",
        "inner_kc_length": 33,
        "inner_kc_mult": 1.7,
        "fixed_stop_loss_pct": 4.7,
        "fixed_take_profit_pct": 3.1,
        "forced_stop_loss_pct": 9.0,
        "forced_take_profit_pct": 10.0,
    }

    engine.evaluate_entry(
        user=user,
        api=api,
        cfg={"order_size": 500},
        symbol="AAPL",
        latest_price=99.75,
        params=params,
        frame=frame,
        last_bar_ts="2026-06-02T15:00:00Z",
        timeframe="15Min",
        backtest={"job_id": "job1", "timeframe": "15Min", "session": "regular"},
    )

    assert len(engine.llm_validator.calls) == 1
    assert len(executed) == 1
    assert executed[0]["symbol"] == "AAPL"
    assert executed[0]["action"] == "buy"


def test_macd_sma_entry_uses_strategy_payload(tmp_path):
    app = SimpleNamespace(instance_path=str(tmp_path))
    executed = []

    def execute_trade(_user, payload):
        executed.append(payload)
        return True, 200, {"result": "opened"}, None

    engine = LocalStrategyEngine(app, execute_trade, "https://paper-api.alpaca.markets", logging.getLogger("test"))
    user = SimpleNamespace(id=3, username="paper", per_trade_amount=1000)
    api = SimpleNamespace(api_key="key", api_secret="secret")
    idx = pd.to_datetime(["2026-06-02T14:45:00Z", "2026-06-02T15:00:00Z"])
    frame = pd.DataFrame(
        [
            {"open": 100, "high": 101, "low": 99, "close": 100, "long_signal": False, "short_signal": False},
            {"open": 100, "high": 102, "low": 100, "close": 101, "long_signal": True, "short_signal": False},
        ],
        index=idx,
    )
    params = {"strategy": "macd_sma", "trade_direction": "Both"}

    engine.evaluate_entry_macd_sma(
        user=user,
        api=api,
        cfg={"order_size": 500},
        symbol="AAPL",
        latest_price=101,
        params=params,
        frame=frame,
        last_bar_ts="2026-06-02T15:00:00Z",
        timeframe="15Min",
        backtest={"job_id": "job2", "strategy": "macd_sma", "timeframe": "15Min", "session": "regular"},
    )

    assert len(executed) == 1
    assert executed[0]["action"] == "buy"
    assert executed[0]["strategy"] == "macd_sma"
    assert executed[0]["strategy_job_id"] == "job2"


def test_strategy_backtest_mismatch_rejects_entry(tmp_path):
    app = SimpleNamespace(instance_path=str(tmp_path))
    engine = LocalStrategyEngine(app, lambda *_: None, "https://paper-api.alpaca.markets", logging.getLogger("test"))
    params = engine.params_from_backtest(
        {"strategy": "macd_sma"},
        {"strategy": "keltner", "params": {"macd_fast_length": 12}},
    )
    params["strategy"] = "macd_sma"
    assert params["strategy"] != "keltner"


def test_backtest_quality_gate_rejects_weak_configs(tmp_path):
    app = SimpleNamespace(instance_path=str(tmp_path))
    engine = LocalStrategyEngine(app, lambda *_: None, "https://paper-api.alpaca.markets", logging.getLogger("test"))

    weak = {
        "metrics": {
            "net_profit": -10,
            "total_trades": 100,
            "win_rate_pct": 80,
            "profit_factor": 2,
            "max_drawdown_pct": 1,
        }
    }
    strong = {
        "metrics": {
            "net_profit": 100,
            "total_trades": 100,
            "win_rate_pct": 70,
            "profit_factor": 2,
            "max_drawdown_pct": 2,
        }
    }

    assert "net profit" in engine.backtest_entry_rejection_reason(weak)
    assert engine.backtest_entry_rejection_reason(strong) is None
