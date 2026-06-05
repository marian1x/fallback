#!/usr/bin/env python3
"""Export LLM shadow events as chat-style training examples.

The exported dataset is meant for curation. For real fine-tuning, review or
override labels first; training only on the model's own shadow decisions usually
just reinforces its existing behavior.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "instance" / "llm_trade_shadow.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "instance" / "llm_shadow_training_examples.jsonl"


SYSTEM_PROMPT = (
    "Esti un validator shadow pentru semnale de trading Keltner. "
    "Returneaza strict JSON cu decision, confidence, reason, risk_flags. "
    "Foloseste veto doar pentru risc clar si material; foloseste manual_review "
    "cand informatia este ambigua."
)


def load_events(path: Path):
    if not path.exists():
        return []
    events = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def target_from_event(event: dict, prefer_manual: bool) -> dict | None:
    manual = event.get("manual_label") if isinstance(event.get("manual_label"), dict) else None
    if prefer_manual and not manual:
        return None
    source = manual or event.get("llm")
    if not isinstance(source, dict):
        return None
    decision = str(source.get("decision", "") or "").strip().lower()
    if decision not in {"approve", "veto", "reduce_size", "manual_review"}:
        return None
    try:
        confidence = float(source.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    risk_flags = source.get("risk_flags", [])
    if isinstance(risk_flags, str):
        risk_flags = [risk_flags]
    if not isinstance(risk_flags, list):
        risk_flags = []
    return {
        "decision": decision,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(source.get("reason", "") or "")[:1200],
        "risk_flags": [str(flag)[:80] for flag in risk_flags[:10]],
    }


def example_from_event(event: dict, target: dict) -> dict:
    payload = {
        "signal": {
            "symbol": event.get("symbol"),
            "action": event.get("action"),
            "amount": event.get("amount"),
            "timeframe": event.get("timeframe"),
            "bar_time": event.get("bar_time"),
            "reason": event.get("local_reason"),
        },
        "technical_context": event.get("technical_context", {}),
        "news_context": event.get("news", {}),
        "shadow_mode": True,
    }
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=True, separators=(",", ":"))},
            {"role": "assistant", "content": json.dumps(target, ensure_ascii=True, separators=(",", ":"))},
        ],
        "metadata": {
            "client_order_id": event.get("client_order_id"),
            "created_at_utc": event.get("created_at_utc"),
            "source_status": event.get("status"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export LLM shadow events as chat-style dataset examples.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--min-confidence", type=float, default=0.0)
    parser.add_argument("--prefer-manual-label", action="store_true")
    args = parser.parse_args()

    events = load_events(Path(args.input))
    examples = []
    for event in events:
        target = target_from_event(event, prefer_manual=args.prefer_manual_label)
        if not target:
            continue
        if target["confidence"] < args.min_confidence:
            continue
        examples.append(example_from_event(event, target))

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as fh:
        for example in examples:
            fh.write(json.dumps(example, ensure_ascii=True, separators=(",", ":")) + "\n")
    print(f"Exported {len(examples)} examples to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
