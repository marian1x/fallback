#!/usr/bin/env python3
"""Shared local strategy configuration helpers."""

from __future__ import annotations

import json
import os
from typing import Dict, List


PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
DEFAULT_STRATEGY_CONFIG_FILE = os.path.join(PROJECT_ROOT, "instance", "strategy_config.json")
STRATEGY_CONFIG_FILE = os.getenv("STRATEGY_CONFIG_FILE", DEFAULT_STRATEGY_CONFIG_FILE)

SIGNAL_MODES = {"local", "tw", "both", "disabled"}


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").upper().replace("/", "").strip()


def get_default_strategy_config() -> Dict:
    return {
        "enabled": False,
        "alpaca_user": "",
        "symbol": "TSM",
        "timeframe": "30Min",
        "session": "regular",
        "feed": "sip",
        "live_data_source": "alpaca",
        "compute_target": "local",
        "inner_kc_length": 33,
        "inner_kc_mult": 1.7,
        "outer_kc_length": 23,
        "outer_kc_mult": 3.0,
        "backtest_start_date": "2020-01-01",
        "backtest_start_time": "02:00",
        "backtest_end_date": "2030-12-31",
        "backtest_end_time": "02:00",
        "fixed_stop_loss_pct": 4.7,
        "fixed_take_profit_pct": 3.1,
        "forced_stop_loss_pct": 9.0,
        "forced_take_profit_pct": 10.0,
        "initial_capital": 8000,
        "base_currency": "USD",
        "order_size": 2000,
        "pyramiding": 0,
        "commission_pct": 0.04,
        "verify_price_ticks": 0,
        "slippage_ticks": 0,
        "margin_long_pct": 100,
        "margin_short_pct": 100,
        "recalc_after_order_filled": False,
        "recalc_on_every_tick": False,
        "optimize_enabled": False,
        "timeframe_sweep_enabled": False,
        "timeframe_minutes": "5,10,15,30",
        "timeframe_hours_start": 1,
        "timeframe_hours_end": 24,
        "timeframe_hours_step": 1,
        "timeframe_days_start": 1,
        "timeframe_days_end": 5,
        "timeframe_days_step": 1,
        "last_backtest": None,
        "trials": 200,
        "optimizer_jobs": 0,
        "top_k": 20,
        "trade_direction": "Both",
        "inner_len_range": "8:40:1",
        "inner_mult_range": "0.6:1.8:0.1",
        "outer_len_range": "8:40:1",
        "outer_mult_range": "1:4:1",
        "fixed_sl_range": "1.0:5.0:0.1",
        "fixed_tp_range": "0.8:4.0:0.1",
        "forced_sl_range": "3.0:10.0:0.2",
        "forced_tp_range": "3.0:10.0:0.2",
        "trail_offset_range": "4:4:1",
        "universe": [
            {
                "symbol": "TSM",
                "mode": "both",
                "enabled": True,
                "notes": "Baseline comparison",
                "backtest": None,
            }
        ],
    }


def normalize_universe(raw_entries) -> List[Dict]:
    entries = []
    if not isinstance(raw_entries, list):
        return entries
    seen = set()
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        symbol = normalize_symbol(item.get("symbol", ""))
        if not symbol or symbol in seen:
            continue
        mode = str(item.get("mode", "both")).strip().lower()
        if mode not in SIGNAL_MODES:
            mode = "both"
        enabled = bool(item.get("enabled", True))
        notes = str(item.get("notes", "") or "").strip()[:200]
        backtest = item.get("backtest")
        if not isinstance(backtest, dict):
            backtest = None
        entries.append({
            "symbol": symbol,
            "mode": mode,
            "enabled": enabled,
            "notes": notes,
            "backtest": backtest,
        })
        seen.add(symbol)
    return entries


def load_strategy_config() -> Dict:
    data = get_default_strategy_config()
    if not os.path.exists(STRATEGY_CONFIG_FILE):
        return data
    try:
        with open(STRATEGY_CONFIG_FILE, "r", encoding="utf-8") as f:
            file_data = json.load(f)
        if isinstance(file_data, dict):
            data.update(file_data)
    except Exception:
        return data
    data["universe"] = normalize_universe(data.get("universe"))
    return data


def save_strategy_config(cfg: Dict) -> None:
    data = get_default_strategy_config()
    if isinstance(cfg, dict):
        data.update(cfg)
    data["universe"] = normalize_universe(data.get("universe"))
    os.makedirs(os.path.dirname(STRATEGY_CONFIG_FILE), exist_ok=True)
    with open(STRATEGY_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def strategy_mode_for_symbol(symbol: str, cfg: Dict | None = None) -> str:
    cfg = cfg or load_strategy_config()
    target = normalize_symbol(symbol)
    for entry in normalize_universe(cfg.get("universe")):
        if entry["symbol"] == target:
            if not entry.get("enabled", True):
                return "disabled"
            return entry.get("mode", "both")
    return "tw"


def tradingview_allowed_for_symbol(symbol: str, cfg: Dict | None = None) -> bool:
    mode = strategy_mode_for_symbol(symbol, cfg)
    return mode in {"tw", "both"}


def local_allowed_for_symbol(symbol: str, cfg: Dict | None = None) -> bool:
    mode = strategy_mode_for_symbol(symbol, cfg)
    return mode in {"local", "both"}
