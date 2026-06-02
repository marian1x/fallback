import os
import sys
import logging
from types import SimpleNamespace

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
