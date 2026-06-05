#!/usr/bin/env python3
"""Summarize LLM shadow validation logs."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = PROJECT_ROOT / "instance" / "llm_trade_shadow.jsonl"


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def load_events(path: Path, days: int | None):
    cutoff = None
    if days and days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    events = []
    if not path.exists():
        return events
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = parse_time(event.get("created_at_utc"))
            if cutoff and ts and ts < cutoff:
                continue
            events.append(event)
    return events


def summarize(events):
    decisions = Counter()
    statuses = Counter()
    symbols = Counter()
    actions = Counter()
    by_symbol_decision = defaultdict(Counter)
    would_execute = 0
    would_block_or_review = 0

    for event in events:
        llm = event.get("llm") if isinstance(event.get("llm"), dict) else {}
        decision = str(llm.get("decision", "unknown") or "unknown")
        status = str(event.get("status", "unknown") or "unknown")
        symbol = str(event.get("symbol", "UNKNOWN") or "UNKNOWN")
        action = str(event.get("action", "unknown") or "unknown")

        decisions[decision] += 1
        statuses[status] += 1
        symbols[symbol] += 1
        actions[action] += 1
        by_symbol_decision[symbol][decision] += 1
        if bool(event.get("llm_would_execute")):
            would_execute += 1
        else:
            would_block_or_review += 1

    return {
        "total_events": len(events),
        "status_counts": dict(statuses),
        "decision_counts": dict(decisions),
        "action_counts": dict(actions),
        "symbol_counts": dict(symbols),
        "llm_would_execute": would_execute,
        "llm_would_block_or_review": would_block_or_review,
        "by_symbol_decision": {symbol: dict(counts) for symbol, counts in by_symbol_decision.items()},
    }


def print_summary(summary):
    total = summary["total_events"]
    print(f"LLM shadow events: {total}")
    if total == 0:
        return
    print(f"LLM would execute: {summary['llm_would_execute']}")
    print(f"LLM would block/review: {summary['llm_would_block_or_review']}")
    print("")
    print("Decisions:")
    for key, value in sorted(summary["decision_counts"].items()):
        print(f"  {key}: {value}")
    print("")
    print("Statuses:")
    for key, value in sorted(summary["status_counts"].items()):
        print(f"  {key}: {value}")
    print("")
    print("Symbols:")
    for key, value in sorted(summary["symbol_counts"].items()):
        decisions = summary["by_symbol_decision"].get(key, {})
        detail = ", ".join(f"{d}={n}" for d, n in sorted(decisions.items()))
        print(f"  {key}: {value} ({detail})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize LLM trade shadow validation logs.")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG))
    parser.add_argument("--days", type=int, default=0, help="Only include the last N days. 0 means all events.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    events = load_events(Path(args.log_file), args.days if args.days > 0 else None)
    summary = summarize(events)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
