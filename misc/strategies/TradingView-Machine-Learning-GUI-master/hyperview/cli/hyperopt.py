from __future__ import annotations

import argparse
import logging
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from ..models import CandleRequest, OptimizationRequest
from ..presets import save_best_preset, strategy_preset_path
from .formatting import _resolve, generate_signals, load_candles, print_hyperopt_table, resolve_pairlist
from strategy import list_strategies

from ..hyperopt import run_optimization

_PRESET_METRIC_KEYS = frozenset({
    "net_profit_pct", "max_drawdown_pct", "win_rate_pct", "profit_factor",
    "trade_count", "equity_final", "sharpe_ratio", "calmar_ratio",
    "expectancy_pct", "avg_win_pct", "avg_loss_pct", "worst_trade_pct",
    "max_consec_losses", "sl_exit_pct", "tp_exit_pct", "signal_exit_pct",
})


def _run_single_hyperopt(
    symbol: str,
    exchange: str,
    timeframe: str,
    session_type: str,
    adjustment: str,
    strategy_name: str,
    initial_capital: float,
    data_dir: str,
    output_dir_base: str,
    mode: str,
    start: str | None,
    end: str | None,
    sl_min: float,
    sl_max: float,
    tp_min: float,
    tp_max: float,
    objective: str,
    top_n: int,
    n_trials: int,
) -> tuple[int, tuple[float, float] | None]:
    candle_request = CandleRequest(
        symbol=symbol,
        exchange=exchange,
        timeframe=timeframe,
        start=start,
        end=end,
        session=session_type,
        adjustment=adjustment,
    )

    try:
        candles = load_candles(candle_request, data_dir, compact=True)
        if candles is None or len(candles) == 0:
            print("  Error: No candle data found. Optimization aborted.")
            return 1, None

        signal_frame, strategy = generate_signals(
            strategy_name,
            candles,
            mode,
            start,
            end,
            quiet=True,
        )
        if signal_frame is None or len(signal_frame) == 0:
            print("  Error: No signals generated. Optimization aborted.")
            return 1, None

        buy_count = int(signal_frame["buy_signal"].sum())
        sell_count = int(signal_frame["sell_signal"].sum())
        in_range_bars = int(signal_frame["in_date_range"].sum())
        density_per_1k = (buy_count + sell_count) / max(in_range_bars, 1) * 1000
        density_label = "Low" if density_per_1k < 5 else "Medium" if density_per_1k < 20 else "High"
        print(f"   \u2022 Signals : {buy_count} buy / {sell_count} sell ({density_label} density ({density_per_1k:.2f}/1k bars))")
        request = OptimizationRequest(
            candle_request=candle_request,
            mode=mode,
            objective=objective,
            sl_min=sl_min,
            sl_max=sl_max,
            tp_min=tp_min,
            tp_max=tp_max,
            top_n=top_n,
            n_trials=n_trials,
            initial_equity=initial_capital,
        )

        bundle = run_optimization(
            signal_frame=signal_frame,
            candle_request=candle_request,
            strategy=strategy,
            request=request,
            output_path=strategy_preset_path(output_dir_base, strategy_name),
            initial_equity=initial_capital,
        )

        print_hyperopt_table(bundle.results, top_n)

        if not bundle.results:
            print("  Error: Optimization produced no ranked results.")
            return 1, None

        best = bundle.results[0]
        metrics = {k: v for k, v in best.to_dict().items() if k in _PRESET_METRIC_KEYS}
        preset_path = save_best_preset(
            strategy_preset_path(output_dir_base, strategy_name),
            strategy=strategy_name,
            pair=f"{exchange}:{symbol}",
            timeframe=timeframe,
            session=session_type,
            adjustment=adjustment,
            mode=mode,
            sl=best.sl_pct,
            tp=best.tp_pct,
            objective=objective,
            search_method="bayesian",
            metrics=metrics,
        )
        Console().print(f"   [green]✔[/green] Best preset saved to: {preset_path}")
        return 0, (best.sl_pct, best.tp_pct)

    except Exception as e:
        print(f"\n  Error: Optimization failed ({e})")
        logging.exception(f"Exception during single hyperopt for {exchange}:{symbol}")
        return 1, None


def run_hyperopt(args: argparse.Namespace, config: dict) -> int:
    strategy_name = _resolve(args, config, "strategy")
    available_strategies = list_strategies()

    if not strategy_name or strategy_name not in available_strategies:
        print("Error: Invalid or no strategy specified.")
        print(f"Available strategies: {', '.join(available_strategies) or '(none)'}")
        return 1

    timeframe = _resolve(args, config, "timeframe")
    session_type = _resolve(args, config, "session")
    mode = _resolve(args, config, "mode", "long")
    adjustment = args.adjustment
    initial_capital = config.get("initial_capital", 1000.0)
    data_dir = config.get("data_dir", "./data")

    output_dir_base = config.get("output_dir", "./outputs")
    Path(output_dir_base).mkdir(parents=True, exist_ok=True)

    opt_cfg = config.get("optimization", {})
    sl_range = opt_cfg.get("sl_range", {})
    tp_range = opt_cfg.get("tp_range", {})

    sl_min = args.sl_min if args.sl_min is not None else sl_range.get("min", 0.5)
    sl_max = args.sl_max if args.sl_max is not None else sl_range.get("max", 5.0)
    tp_min = args.tp_min if args.tp_min is not None else tp_range.get("min", 1.0)
    tp_max = args.tp_max if args.tp_max is not None else tp_range.get("max", 10.0)

    objective = args.objective or opt_cfg.get("objective", "net_profit_pct")
    top_n = args.top_n if args.top_n is not None else opt_cfg.get("top_n", 5)
    n_trials = args.n_trials if args.n_trials is not None else opt_cfg.get("n_trials", 100)

    symbols = resolve_pairlist(args, config)
    if not symbols:
        print("Error: No trading pairs resolved. Please check your config or arguments.")
        return 1

    console = Console()

    header_text = (
        f"⚙ HYPERVIEW - BATCH HYPER-OPTIMIZATION\n"
        f"  Strategy : {strategy_name} | Mode: {mode} | Timeframe: {timeframe}\n"
        f"  Targets  : {len(symbols)} pair(s)"
    )
    console.print()
    console.print(Panel(header_text, expand=True))

    rc = 0
    for index, (symbol, pair_exchange) in enumerate(symbols, start=1):
        line = "─" * 72
        console.print(f"\n 📊 [{index}/{len(symbols)}] {pair_exchange}:{symbol}")
        console.print(f" {line}")

        pair_rc, best_values = _run_single_hyperopt(
            symbol=symbol,
            exchange=pair_exchange,
            timeframe=timeframe,
            session_type=session_type,
            adjustment=adjustment,
            strategy_name=strategy_name,
            initial_capital=initial_capital,
            data_dir=data_dir,
            output_dir_base=output_dir_base,
            mode=mode,
            start=args.start,
            end=args.end,
            sl_min=sl_min,
            sl_max=sl_max,
            tp_min=tp_min,
            tp_max=tp_max,
            objective=objective,
            top_n=top_n,
            n_trials=n_trials,
        )
        if pair_rc != 0:
            rc = 1
            continue

    return rc
