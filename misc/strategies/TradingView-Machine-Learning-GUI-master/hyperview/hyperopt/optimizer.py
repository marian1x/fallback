from __future__ import annotations

import abc
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ..backtest.engine import TradingViewLikeBacktester
from ..utils import format_time
from ..models import (
    BacktestMetrics, CandleRequest, Mode, MultiPairCandidate,
    MultiPairOptimizationBundle, Objective, OptimizationBundle,
    OptimizationRequest, RiskParameters,
)
from strategy import BaseStrategy


def _print_progress(current: int, total: int, start_time: float, phase: str) -> None:
    if total > 100:
        interval = max(1, total // 100)
        if current not in {1, total} and current % interval != 0:
            return
    elapsed = time.time() - start_time
    pct = current / total * 100
    bar_width = 30
    filled = int(bar_width * current // total)
    bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
    time_str = format_time(elapsed)
    label = f"   \u2022 {phase}:"
    sys.stdout.write(
        f"\r{label:<20}|{bar}| {pct:3.0f}% [{current}/{total}] {time_str}   "
    )
    sys.stdout.flush()
    if current == total:
        sys.stdout.write("\n")


# ------------------------------------------------------------------
# Generic ranking / dedup (works for BacktestMetrics & MultiPairCandidate)
# ------------------------------------------------------------------

def _rank(items: list[Any], objective: Objective) -> list[Any]:
    reverse = objective != "max_drawdown_pct"
    ranked = sorted(items, key=lambda item: item.objective_value(objective), reverse=reverse)
    for index, item in enumerate(ranked, start=1):
        item.rank = index
    return ranked


def _deduplicate_and_rank(
    items: list[Any],
    objective: Objective,
    dedup_key: Callable[[Any], tuple],
) -> list[Any]:
    minimize = objective == "max_drawdown_pct"
    deduped: dict[tuple, Any] = {}
    for item in items:
        key = dedup_key(item)
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = item
        elif minimize:
            if item.objective_value(objective) < existing.objective_value(objective):
                deduped[key] = item
        elif item.objective_value(objective) > existing.objective_value(objective):
            deduped[key] = item
    return _rank(list(deduped.values()), objective)


# ------------------------------------------------------------------
# Base optimizer — Bayesian (Optuna TPE) search
# ------------------------------------------------------------------

class _BaseOptimizer(abc.ABC):

    @abc.abstractmethod
    def _evaluate(self, mode: Mode, sl_value: float, tp_value: float, objective: Objective) -> Any: ...

    @abc.abstractmethod
    def _make_bundle(self, request: OptimizationRequest, results: list, output_path: Path) -> Any: ...

    @abc.abstractmethod
    def _dedup_key(self, item: Any) -> tuple: ...

    def optimize(self, request: OptimizationRequest, output_path: Path) -> Any:
        import optuna

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        objective_name = request.objective
        all_results: list[Any] = []
        start_time = time.time()

        def objective(trial: optuna.Trial) -> float:
            sl_value = trial.suggest_float("sl_pct", request.sl_min, request.sl_max, step=0.01)
            tp_value = trial.suggest_float("tp_pct", request.tp_min, request.tp_max, step=0.01)
            result = self._evaluate(request.mode, sl_value, tp_value, objective_name)
            all_results.append(result)
            _print_progress(len(all_results), request.n_trials, start_time, "Optuna TPE")
            return result.objective_value(objective_name)

        direction = "minimize" if objective_name == "max_drawdown_pct" else "maximize"
        study = optuna.create_study(direction=direction, sampler=optuna.samplers.TPESampler(seed=42))
        study.optimize(objective, n_trials=request.n_trials)

        ranked = _deduplicate_and_rank(all_results, objective_name, self._dedup_key)
        return self._make_bundle(request, ranked[: request.top_n], output_path)


# ------------------------------------------------------------------
# Single-pair optimizer
# ------------------------------------------------------------------

def run_optimization(
    signal_frame: pd.DataFrame,
    candle_request: CandleRequest,
    strategy: BaseStrategy,
    request: OptimizationRequest,
    output_path: Path,
    initial_equity: float = 100_000.0,
) -> OptimizationBundle:
    """Top-level entry point for SL/TP optimization."""
    optimizer = _Optimizer(
        signal_frame=signal_frame,
        candle_request=candle_request,
        strategy=strategy,
        initial_equity=initial_equity,
    )
    return optimizer.optimize(request, output_path)


class _Optimizer(_BaseOptimizer):
    def __init__(
        self,
        signal_frame: pd.DataFrame,
        candle_request: CandleRequest,
        strategy: BaseStrategy,
        initial_equity: float = 100_000.0,
    ) -> None:
        self.backtester = TradingViewLikeBacktester(
            candle_request=candle_request,
            initial_equity=initial_equity,
        )
        self.compiled_signal_frame = self.backtester.compile_signal_frame(signal_frame)
        self._metrics_cache: dict[tuple[str, float, float], BacktestMetrics] = {}

    def _evaluate(self, mode: Mode, sl_value: float, tp_value: float, objective: Objective) -> BacktestMetrics:
        key = (mode, round(sl_value, 8), round(tp_value, 8))
        cached = self._metrics_cache.get(key)
        if cached is not None:
            return cached
        risk = RiskParameters.from_mode(mode, sl_value, tp_value)
        metrics = self.backtester.run_metrics(self.compiled_signal_frame, risk, mode)
        self._metrics_cache[key] = metrics
        return metrics

    def _dedup_key(self, item: BacktestMetrics) -> tuple:
        return (item.sl_pct, item.tp_pct, item.mode)

    def _make_bundle(self, request: OptimizationRequest, results: list[BacktestMetrics], output_path: Path) -> OptimizationBundle:
        return OptimizationBundle(request=request, results=results, output_path=output_path)


# ------------------------------------------------------------------
# Multi-pair optimizer
# ------------------------------------------------------------------

PairData = tuple[str, str, pd.DataFrame, BaseStrategy, CandleRequest]
"""(symbol, exchange, signal_frame, strategy, candle_request)"""


def run_multi_optimization(
    pair_data: list[PairData],
    request: OptimizationRequest,
    output_path: Path,
    initial_equity: float = 100_000.0,
) -> MultiPairOptimizationBundle:
    """Optimize SL/TP across all pairs simultaneously."""
    optimizer = _MultiPairOptimizer(
        pair_data=pair_data,
        initial_equity=initial_equity,
    )
    return optimizer.optimize(request, output_path)


class _MultiPairOptimizer(_BaseOptimizer):
    def __init__(
        self,
        pair_data: list[PairData],
        initial_equity: float = 100_000.0,
    ) -> None:
        self.pair_data = pair_data
        self.backtesters = [
            TradingViewLikeBacktester(candle_request=cr, initial_equity=initial_equity)
            for _, _, _, _, cr in pair_data
        ]
        self.compiled_signal_frames = [
            bt.compile_signal_frame(sf)
            for (_, _, sf, _, _), bt in zip(pair_data, self.backtesters)
        ]
        self._candidate_cache: dict[tuple[str, float, float, str], MultiPairCandidate] = {}

    def _evaluate(self, mode: Mode, sl_value: float, tp_value: float, objective: Objective) -> MultiPairCandidate:
        key = (mode, round(sl_value, 8), round(tp_value, 8), objective)
        cached = self._candidate_cache.get(key)
        if cached is not None:
            return cached
        risk = RiskParameters.from_mode(mode, sl_value, tp_value)
        per_pair = [bt.run_metrics(sf, risk, mode) for sf, bt in zip(self.compiled_signal_frames, self.backtesters)]
        candidate = _build_candidate(sl_value, tp_value, per_pair, objective)
        self._candidate_cache[key] = candidate
        return candidate

    def _dedup_key(self, item: MultiPairCandidate) -> tuple:
        return (item.sl_pct, item.tp_pct)

    def _make_bundle(self, request: OptimizationRequest, results: list[MultiPairCandidate], output_path: Path) -> MultiPairOptimizationBundle:
        pairs = [(sym, ex) for sym, ex, _, _, _ in self.pair_data]
        return MultiPairOptimizationBundle(request=request, pairs=pairs, results=results, output_path=output_path)


# ------------------------------------------------------------------
# Multi-pair aggregation helper
# ------------------------------------------------------------------

def _build_candidate(
    sl_value: float,
    tp_value: float,
    per_pair: list[BacktestMetrics],
    request_objective: Objective | None,
) -> MultiPairCandidate:
    """Aggregate per-pair metrics into a single candidate."""
    n = len(per_pair)
    avg_net = sum(m.net_profit_pct for m in per_pair) / n
    worst_dd = max(m.max_drawdown_pct for m in per_pair)

    if request_objective == "max_drawdown_pct":
        agg_obj = worst_dd
    elif request_objective == "profit_factor":
        agg_obj = sum(m.profit_factor for m in per_pair) / n
    elif request_objective == "win_rate_pct":
        agg_obj = sum(m.win_rate_pct for m in per_pair) / n
    elif request_objective == "trade_count":
        agg_obj = sum(m.trade_count for m in per_pair) / n
    else:  # "net_profit_pct" or None → default
        agg_obj = avg_net

    return MultiPairCandidate(
        sl_pct=sl_value,
        tp_pct=tp_value,
        aggregate_net_profit_pct=avg_net,
        aggregate_max_drawdown_pct=worst_dd,
        aggregate_objective=agg_obj,
        per_pair_metrics=per_pair,
    )
