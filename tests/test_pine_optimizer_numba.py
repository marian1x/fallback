"""Parity tests: the optional Numba fast path must match the reference backtest.

The optimizer's best parameters drive live trades, so the JIT simulation has to
produce byte-for-byte identical results to the pure-Python reference. These
tests are skipped automatically when numba is not installed.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import misc.pine_optimizer as po

pytestmark = pytest.mark.skipif(not po.NUMBA_AVAILABLE, reason="numba not installed")


def _make_bars(seed: int, n: int = 1200, start_price: float = 100.0) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    idx = pd.date_range("2023-01-03 14:30", periods=n, freq="30min", tz="UTC")
    close = start_price + np.cumsum(rng.normal(0.02, 1.1, n))
    close = np.maximum(close, 1.0)
    spread = np.abs(rng.normal(0.6, 0.3, n)) + 0.05
    high = close + spread
    low = np.maximum(close - spread, 0.5)
    openp = close + rng.normal(0, 0.3, n)
    openp = np.clip(openp, low, high)
    return pd.DataFrame(
        {"open": openp, "high": high, "low": low, "close": close, "volume": 1000},
        index=idx,
    )


def _cfg() -> po.BacktestConfig:
    return po.BacktestConfig(
        initial_capital=8000.0,
        order_size_usd=2000.0,
        commission_pct=0.04,
        timezone_name="Europe/Bucharest",
    )


def _params(**overrides) -> po.StrategyParams:
    base = dict(
        trade_direction="Both",
        inner_kc_length=20,
        inner_kc_mult=1.5,
        outer_kc_length=20,
        outer_kc_mult=3.0,
        fixed_stop_loss_pct=4.7,
        fixed_take_profit_pct=3.1,
        forced_stop_loss_pct=9.0,
        forced_take_profit_pct=10.0,
        trailing_offset_ticks=4,
        tick_size=0.01,
        trailing_offset_pct=0.0,
    )
    base.update(overrides)
    return po.StrategyParams(**base)


def _assert_result_parity(ref: po.BacktestResult, fast: po.BacktestResult):
    assert fast.total_trades == ref.total_trades
    assert fast.winners == ref.winners
    assert fast.losers == ref.losers
    assert fast.gross_profit == pytest.approx(ref.gross_profit, abs=1e-6)
    assert fast.gross_loss == pytest.approx(ref.gross_loss, abs=1e-6)
    assert fast.net_profit == pytest.approx(ref.net_profit, abs=1e-6)
    assert fast.final_equity == pytest.approx(ref.final_equity, abs=1e-6)
    assert fast.win_rate_pct == pytest.approx(ref.win_rate_pct, abs=1e-9)
    assert fast.profit_factor == pytest.approx(ref.profit_factor, abs=1e-9)
    assert fast.max_drawdown_pct == pytest.approx(ref.max_drawdown_pct, abs=1e-9)
    assert fast.sharpe == pytest.approx(ref.sharpe, abs=1e-9)
    assert fast.score == pytest.approx(ref.score, abs=1e-9)
    assert len(fast.trades) == len(ref.trades)
    for a, b in zip(ref.trades, fast.trades):
        assert a == b


@pytest.mark.parametrize("seed", [1, 2, 7, 13, 42])
@pytest.mark.parametrize("direction", ["Both", "Long Only", "Short Only"])
def test_fast_matches_reference(seed, direction):
    df = _make_bars(seed)
    cfg = _cfg()
    params = _params(trade_direction=direction)
    start = df.index[0].to_pydatetime().replace(tzinfo=timezone.utc)
    end = df.index[-1].to_pydatetime().replace(tzinfo=timezone.utc)
    ref = po.backtest(df.copy(), params, cfg, start, end)
    fast = po.backtest_fast(df.copy(), params, cfg, start, end)
    _assert_result_parity(ref, fast)


@pytest.mark.parametrize("trail_pct", [0.3, 0.6, 1.0])
def test_fast_matches_reference_percent_trailing(trail_pct):
    df = _make_bars(5)
    cfg = _cfg()
    params = _params(trailing_offset_pct=trail_pct)
    start = df.index[0].to_pydatetime().replace(tzinfo=timezone.utc)
    end = df.index[-1].to_pydatetime().replace(tzinfo=timezone.utc)
    ref = po.backtest(df.copy(), params, cfg, start, end)
    fast = po.backtest_fast(df.copy(), params, cfg, start, end)
    _assert_result_parity(ref, fast)
