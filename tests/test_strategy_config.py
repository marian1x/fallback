import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import strategy_config


def test_normalize_universe_allows_same_symbol_with_different_strategies():
    rows = strategy_config.normalize_universe([
        {"symbol": "AAPL", "strategy": "keltner", "mode": "local", "enabled": True},
        {"symbol": "AAPL", "strategy": "macd_sma", "mode": "local", "enabled": True},
        {"symbol": "AAPL", "strategy": "macd_sma", "mode": "both", "enabled": True},
    ])

    assert len(rows) == 2
    assert {(row["symbol"], row["strategy"]) for row in rows} == {
        ("AAPL", "keltner"),
        ("AAPL", "macd_sma"),
    }


def test_strategy_mode_for_symbol_aggregates_duplicate_rows():
    cfg = {
        "universe": [
            {"symbol": "AAPL", "strategy": "keltner", "mode": "local", "enabled": True},
            {"symbol": "AAPL", "strategy": "macd_sma", "mode": "local", "enabled": True},
            {"symbol": "MSFT", "strategy": "keltner", "mode": "tw", "enabled": True},
            {"symbol": "NVDA", "strategy": "keltner", "mode": "local", "enabled": False},
        ]
    }

    assert strategy_config.strategy_mode_for_symbol("AAPL", cfg) == "local"
    assert strategy_config.local_allowed_for_symbol("AAPL", cfg) is True
    assert strategy_config.tradingview_allowed_for_symbol("AAPL", cfg) is False
    assert strategy_config.strategy_mode_for_symbol("MSFT", cfg) == "tw"
    assert strategy_config.strategy_mode_for_symbol("NVDA", cfg) == "disabled"
