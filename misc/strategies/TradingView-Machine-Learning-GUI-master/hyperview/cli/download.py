from __future__ import annotations

import argparse
import time

from ..downloader import TradingViewDataClient
from ..utils import format_time
from .formatting import _resolve, resolve_pairlist


def run_download_data(args: argparse.Namespace, config: dict) -> int:
    timeframes = _resolve_download_timeframes(args, config)
    session = _resolve(args, config, "session")
    adjustment = args.adjustment
    data_dir = config["data_dir"]

    pairs = resolve_pairlist(args, config)

    print(f"\n{'='*60}")
    print("  HyperView - Download Data")
    print(f"  {len(pairs)} pair(s) | {', '.join(timeframes)}")
    for symbol, exch in pairs:
        print(f"    {exch}:{symbol}")
    if args.start:
        print(f"  Range: {args.start} -> {args.end or 'now'}")
    print(f"{'='*60}\n")

    client = TradingViewDataClient(cache_dir=data_dir)
    t0 = time.time()
    total_downloads = 0
    for timeframe in timeframes:
        results = client.download_pairs(
            pairs=pairs,
            timeframe=timeframe,
            start=args.start,
            end=args.end,
            session=session,
            adjustment=adjustment,
        )
        total_downloads += len(results)
    print(f"\nDone - {total_downloads} dataset(s) downloaded ({format_time(time.time() - t0)})")
    return 0


def _resolve_download_timeframes(args: argparse.Namespace, config: dict) -> list[str]:
    cli_timeframes = getattr(args, "timeframe", None)
    if cli_timeframes:
        return [tf.strip() for tf in cli_timeframes if tf and tf.strip()]

    config_timeframe = config.get("timeframe")
    if isinstance(config_timeframe, list):
        return [str(tf).strip() for tf in config_timeframe if str(tf).strip()]
    if isinstance(config_timeframe, str) and config_timeframe.strip():
        return [config_timeframe.strip()]

    raise SystemExit(
        "Error: No timeframe specified. Use --timeframe on the CLI or define 'timeframe' in your config file."
    )
