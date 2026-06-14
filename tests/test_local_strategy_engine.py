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
        cfg={"order_size": 500, "order_size_macd_sma": 5000},
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
    assert executed[0]["amount"] == 5000


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


def test_oos_quality_gate_requires_passed_validation(tmp_path):
    app = SimpleNamespace(instance_path=str(tmp_path))
    engine = LocalStrategyEngine(app, lambda *_: None, "https://paper-api.alpaca.markets", logging.getLogger("test"))

    assert engine.oos_entry_rejection_reason({}) is None
    failed = {"validation": {"enabled": True, "status": {"passed": False, "failed_checks": ["min_profit_factor"]}}}
    passed = {"validation": {"enabled": True, "status": {"passed": True}}}

    assert "min_profit_factor" in engine.oos_entry_rejection_reason(failed)
    assert engine.oos_entry_rejection_reason(passed) is None


def test_oos_quality_gate_recalculates_with_current_thresholds(tmp_path):
    app = SimpleNamespace(instance_path=str(tmp_path))
    engine = LocalStrategyEngine(app, lambda *_: None, "https://paper-api.alpaca.markets", logging.getLogger("test"))

    backtest = {
        "validation": {
            "enabled": True,
            "status": {
                "passed": False,
                "failed_checks": ["min_profit_factor", "max_drawdown_pct"],
                "checks": {
                    "min_trades": {"actual": 8},
                    "min_win_rate_pct": {"actual": 48},
                    "min_profit_factor": {"actual": 1.08},
                    "max_drawdown_pct": {"actual": 14},
                    "min_net_profit": {"actual": 250},
                },
            },
        }
    }

    strict_cfg = {
        "validation_min_trades": 10,
        "validation_min_win_rate_pct": 55,
        "validation_min_profit_factor": 1.2,
        "validation_max_drawdown_pct": 10,
        "validation_min_net_profit": 0,
    }
    relaxed_cfg = {
        "validation_min_trades": 5,
        "validation_min_win_rate_pct": 45,
        "validation_min_profit_factor": 1.05,
        "validation_max_drawdown_pct": 15,
        "validation_min_net_profit": 0,
    }

    assert "min_trades" in engine.oos_entry_rejection_reason(backtest, strict_cfg)
    assert engine.oos_entry_rejection_reason(backtest, relaxed_cfg) is None


def _rsi_frame():
    idx = pd.to_datetime(["2026-06-02T14:45:00Z", "2026-06-02T15:00:00Z"])
    return pd.DataFrame(
        [
            {"open": 100, "high": 101, "low": 99, "close": 100, "long_signal": False, "short_signal": False, "long_exit": False, "short_exit": False, "rsi": 30, "trend_ma": 99},
            {"open": 100, "high": 102, "low": 100, "close": 101, "long_signal": True, "short_signal": False, "long_exit": False, "short_exit": False, "rsi": 8, "trend_ma": 99},
        ],
        index=idx,
    )


class FakeGateValidator:
    def __init__(self, decision="approve", confidence=0.9, failed=False, enforce=True):
        self.mode = "gate"
        self.enforce = enforce
        self.min_confidence = 0.6
        self.reduce_size_factor = 0.5
        self.block_on_manual_review = False
        self._decision = decision
        self._confidence = confidence
        self._failed = failed
        self.calls = []

    def validate_entry_blocking(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "decision": self._decision,
            "confidence": self._confidence,
            "failed": self._failed,
            "status": "ok" if not self._failed else "error",
            "reason": "test",
            "news": {"items": []},
        }

    def simple_chat(self, system_prompt, user_prompt):
        return "{}"


def _make_engine(tmp_path, executed):
    app = SimpleNamespace(instance_path=str(tmp_path))

    def execute_trade(_user, payload):
        executed.append(payload)
        return True, 200, {"result": "opened"}, None

    return LocalStrategyEngine(app, execute_trade, "https://paper-api.alpaca.markets", logging.getLogger("test"))


def test_rsi_entry_uses_strategy_payload(tmp_path):
    executed = []
    engine = _make_engine(tmp_path, executed)
    engine.llm_validator = None  # gate is a no-op
    user = SimpleNamespace(id=3, username="paper", per_trade_amount=1000)
    api = SimpleNamespace(api_key="key", api_secret="secret")
    engine.evaluate_entry_rsi(
        user=user, api=api, cfg={"order_size": 500, "order_size_rsi_reversion": 1500},
        symbol="AAPL", latest_price=101, params={"strategy": "rsi_reversion", "trade_direction": "Both"},
        frame=_rsi_frame(), last_bar_ts="2026-06-02T15:00:00Z", timeframe="15Min",
        backtest={"job_id": "j", "strategy": "rsi_reversion", "timeframe": "15Min", "session": "regular"},
    )
    assert len(executed) == 1
    assert executed[0]["action"] == "buy"
    assert executed[0]["strategy"] == "rsi_reversion"
    assert executed[0]["amount"] == 1500


def test_rsi_exit_fires_on_mean_reversion(tmp_path):
    executed = []
    engine = _make_engine(tmp_path, executed)
    user = SimpleNamespace(id=3, username="paper", per_trade_amount=1000)
    position = SimpleNamespace(avg_entry_price=100.0, side="long")
    frame = _rsi_frame()
    frame.iloc[-1, frame.columns.get_loc("long_exit")] = True
    engine.evaluate_exit_rsi(
        user=user, symbol="AAPL", position=position, latest_price=101,
        params={"fixed_stop_loss_pct": 4.7, "fixed_take_profit_pct": 3.1, "forced_stop_loss_pct": 9.0, "forced_take_profit_pct": 10.0},
        frame=frame, last_bar_ts="2026-06-02T15:00:00Z", timeframe="15Min",
    )
    assert len(executed) == 1
    assert executed[0]["action"] == "close"


def _run_macd_entry_with_validator(tmp_path, validator, gate_sync=True, seed_analysis=None):
    executed = []
    engine = _make_engine(tmp_path, executed)
    engine.llm_validator = validator
    engine.gate_sync = gate_sync
    if seed_analysis is not None:
        dossier = engine.symbol_memory.load_dossier("AAPL")
        dossier["analysis"] = seed_analysis
        engine.symbol_memory.save_dossier("AAPL", dossier)
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
    engine.evaluate_entry_macd_sma(
        user=user, api=api, cfg={"order_size": 500, "order_size_macd_sma": 5000},
        symbol="AAPL", latest_price=101, params={"strategy": "macd_sma", "trade_direction": "Both"},
        frame=frame, last_bar_ts="2026-06-02T15:00:00Z", timeframe="15Min",
        backtest={"strategy": "macd_sma", "timeframe": "15Min", "session": "regular"},
    )
    return executed, validator


# --- Synchronous (LLM_GATE_SYNC=true) gate path -----------------------------
def test_llm_gate_veto_blocks_entry_when_enforced(tmp_path):
    executed, v = _run_macd_entry_with_validator(tmp_path, FakeGateValidator("veto", 0.9, enforce=True), gate_sync=True)
    assert len(v.calls) == 1
    assert executed == []  # veto blocked the entry


def test_llm_gate_would_veto_does_not_block_in_shadow_first(tmp_path):
    executed, v = _run_macd_entry_with_validator(tmp_path, FakeGateValidator("veto", 0.9, enforce=False), gate_sync=True)
    assert len(executed) == 1  # shadow-first: logs but executes


def test_llm_gate_fail_open_executes(tmp_path):
    executed, v = _run_macd_entry_with_validator(tmp_path, FakeGateValidator("unknown", 0.0, failed=True, enforce=True), gate_sync=True)
    assert len(executed) == 1  # fail-open


def test_llm_gate_reduce_size_when_enforced(tmp_path):
    executed, v = _run_macd_entry_with_validator(tmp_path, FakeGateValidator("reduce_size", 0.9, enforce=True), gate_sync=True)
    assert len(executed) == 1
    assert executed[0]["amount"] == 2500  # 5000 * 0.5


# --- Default flag-based gate path (precomputed standing verdict) -------------
def _verdict(long_ok=True, short_ok=True, confidence=0.8):
    return {"updated_at": "2026-06-02T14:00:00+00:00", "bias": "neutral", "long_ok": long_ok,
            "short_ok": short_ok, "confidence": confidence, "reason": "test", "risk_flags": []}


def test_flag_gate_cold_start_fails_open(tmp_path):
    # No standing verdict yet -> execute (fail-open), no blocking LLM call
    executed, v = _run_macd_entry_with_validator(tmp_path, FakeGateValidator(enforce=True), gate_sync=False)
    assert len(executed) == 1
    assert v.calls == []  # flag path never calls the LLM synchronously


def test_flag_gate_veto_blocks_long_when_flag_false_and_enforced(tmp_path):
    executed, v = _run_macd_entry_with_validator(
        tmp_path, FakeGateValidator(enforce=True), gate_sync=False,
        seed_analysis=_verdict(long_ok=False, confidence=0.9),
    )
    assert executed == []  # standing verdict blocks the long, no live LLM call
    assert v.calls == []


def test_flag_gate_allows_long_when_flag_true(tmp_path):
    executed, v = _run_macd_entry_with_validator(
        tmp_path, FakeGateValidator(enforce=True), gate_sync=False,
        seed_analysis=_verdict(long_ok=True, confidence=0.9),
    )
    assert len(executed) == 1


def test_flag_gate_would_veto_in_shadow_first(tmp_path):
    executed, v = _run_macd_entry_with_validator(
        tmp_path, FakeGateValidator(enforce=False), gate_sync=False,
        seed_analysis=_verdict(long_ok=False, confidence=0.9),
    )
    assert len(executed) == 1  # shadow-first: would-veto only, still executes


def test_forced_refresh_bypasses_throttle_and_records_sources(tmp_path):
    from datetime import datetime, timezone, timedelta
    executed = []
    engine = _make_engine(tmp_path, executed)
    engine.llm_validator = None  # no background analysis thread in this test
    engine.news_collector = SimpleNamespace(collect=lambda symbol, api_key=None, api_secret=None: {
        "items": [{"url": "u1", "headline": "AAPL news", "published_at": "2026-06-14T10:00:00+00:00",
                   "provider": "alpaca", "source": "benzinga"}],
        "investor_messages": [], "requested_sources": ["Alpaca", "Nasdaq"],
        "provider_errors": {"Nasdaq": "HTTP 404"}, "aggregate": {},
    })
    cfg = {"universe": [{"symbol": "AAPL", "strategy": "keltner", "mode": "local", "enabled": True}]}
    # Throttle set to the future: a normal pass would skip this symbol.
    engine.symbol_state("AAPL")["next_memory_refresh_utc"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    engine.symbol_memory.request_refresh(symbols=None)  # force all

    user = SimpleNamespace(id=1, username="p", per_trade_amount=1000)
    api = SimpleNamespace(api_key="k", api_secret="s")
    engine.refresh_symbol_memory(user, api, cfg)

    assert engine.symbol_memory.archive_count("AAPL") == 1  # ingested despite throttle (forced)
    snap = engine.symbol_memory.load_dossier("AAPL")["last_sources"]
    assert snap["ok"] == ["Alpaca"] and "Nasdaq" in snap["failed"]
