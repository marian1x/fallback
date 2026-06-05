from __future__ import annotations

import argparse

from ..downloader import TradingViewDataClient
from strategy import list_strategies


def run_list_data(args: argparse.Namespace, config: dict) -> int:
    data_dir = config.get("data_dir", "data")
    client = TradingViewDataClient(cache_dir=data_dir)
    entries = client.list_cached()
    if not entries:
        print("No cached data found.")
        return 0
    print(
        f"\n{'Exchange':<12} {'Symbol':<10} {'TF':<6} {'Session':<10} {'Adj':<10} "
        f"{'Bars':>7}  {'Start':<18} {'End':<18} {'File'}"
    )
    print("-" * 120)
    for entry in entries:
        print(
            f"{entry['exchange']:<12} {entry['symbol']:<10} {entry['timeframe']:<6} "
            f"{entry.get('session', 'unknown'):<10} {entry.get('adjustment', 'unknown'):<10} "
            f"{entry['bars']:>7}  {entry['start']:<18} {entry['end']:<18} {entry['file']}"
        )
    return 0


def run_list_strategies(args: argparse.Namespace, config: dict) -> int:
    names = list_strategies()
    if not names:
        print("No strategies registered.")
        return 0
    print(f"\nRegistered strategies ({len(names)}):")
    for name in names:
        print(f"  - {name}")
    return 0
