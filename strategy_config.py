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
STRATEGY_CHOICES = {"keltner", "macd_sma", "rsi_reversion"}
OPTIMIZER_ENGINES = {"random", "tpe"}
ACCELERATOR_CHOICES = {"auto", "cpu", "gpu"}


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").upper().replace("/", "").strip()


def normalize_tester_symbols(raw_entries, fallback_symbols: str | None = None) -> List[Dict]:
    entries = []
    seen = set()
    if isinstance(raw_entries, list):
        for item in raw_entries:
            if isinstance(item, dict):
                symbol = normalize_symbol(item.get("symbol", ""))
                if not symbol or symbol in seen:
                    continue
                entries.append({
                    "symbol": symbol,
                    "selected": bool(item.get("selected", True)),
                })
                seen.add(symbol)
            else:
                symbol = normalize_symbol(item)
                if not symbol or symbol in seen:
                    continue
                entries.append({"symbol": symbol, "selected": True})
                seen.add(symbol)
    if not entries and fallback_symbols:
        normalized = str(fallback_symbols or "").replace(";", ",").replace("\n", ",").replace("\r", ",")
        for chunk in normalized.split(","):
            symbol = normalize_symbol(chunk)
            if not symbol or symbol in seen:
                continue
            entries.append({"symbol": symbol, "selected": True})
            seen.add(symbol)
    return entries


def get_default_strategy_config() -> Dict:
    return {
        "enabled": False,
        "alpaca_user": "",
        "symbol": "TSM",
        "tester_symbols": [
            {
                "symbol": "TSM",
                "selected": True,
            }
        ],
        "strategy": "keltner",
        "timeframe": "30Min",
        "session": "regular",
        "feed": "sip",
        "live_data_source": "alpaca",
        "compute_target": "local",
        "optimizer_engine": "tpe",
        "accelerator": "auto",
        "validation_enabled": True,
        "validation_train_ratio": 0.70,
        "validation_min_trades": 5,
        "validation_min_win_rate_pct": 45,
        "validation_min_profit_factor": 1.15,
        "validation_max_drawdown_pct": 15,
        "validation_min_net_profit": 0,
        "daily_max_trades_per_symbol": 3,
        "daily_max_losses_per_symbol": 2,
        "daily_max_loss_usd_per_symbol": 100,
        "market_regime_filter_enabled": True,
        "market_regime_symbol": "SPY",
        "market_regime_ttl_seconds": 1800,
        "symbol_killswitch_enabled": True,
        "symbol_killswitch_lookback_trades": 15,
        "symbol_killswitch_min_trades": 6,
        "symbol_killswitch_max_net_loss_usd": 300,
        "symbol_killswitch_min_win_rate_pct": 35,
        "symbol_killswitch_cooldown_days": 14,
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
        "order_size_keltner": 2000,
        "order_size_macd_sma": 5000,
        "macd_priority_enabled": True,
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
        "trailing_offset_pct": 0.0,
        "trail_pct_range": "0.0:0.0:0.1",
        "macd_fast_length": 12,
        "macd_slow_length": 26,
        "macd_signal_length": 9,
        "macd_sma_length": 200,
        "max_intraday_loss_pct": 50,
        "macd_fast_range": "8:20:1",
        "macd_slow_range": "20:40:1",
        "macd_signal_range": "5:15:1",
        "macd_sma_range": "100:250:10",
        "max_intraday_loss_range": "50:50:1",
        "order_size_rsi_reversion": 2000,
        "rsi_length": 2,
        "rsi_oversold": 10.0,
        "rsi_overbought": 90.0,
        "rsi_exit_level": 55.0,
        "rsi_trend_length": 200,
        "rsi_length_range": "2:4:1",
        "rsi_oversold_range": "5:20:5",
        "rsi_overbought_range": "80:95:5",
        "rsi_exit_range": "50:75:5",
        "rsi_trend_range": "100:200:50",
        "universe": [
            {
                "symbol": "TSM",
                "strategy": "keltner",
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
        if not symbol:
            continue
        mode = str(item.get("mode", "both")).strip().lower()
        if mode not in SIGNAL_MODES:
            mode = "both"
        enabled = bool(item.get("enabled", True))
        notes = str(item.get("notes", "") or "").strip()[:200]
        backtest = item.get("backtest")
        if not isinstance(backtest, dict):
            backtest = None
        strategy = str(item.get("strategy") or (backtest or {}).get("strategy") or "keltner").strip().lower()
        if strategy not in STRATEGY_CHOICES:
            strategy = "keltner"
        key = (symbol, strategy)
        if key in seen:
            continue
        entries.append({
            "symbol": symbol,
            "strategy": strategy,
            "mode": mode,
            "enabled": enabled,
            "notes": notes,
            "backtest": backtest,
        })
        seen.add(key)
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
    data["tester_symbols"] = normalize_tester_symbols(data.get("tester_symbols"), data.get("symbol"))
    data["symbol"] = ", ".join(item["symbol"] for item in data["tester_symbols"] if item.get("selected", True))
    if str(data.get("strategy", "")).strip().lower() not in STRATEGY_CHOICES:
        data["strategy"] = "keltner"
    if str(data.get("optimizer_engine", "")).strip().lower() not in OPTIMIZER_ENGINES:
        data["optimizer_engine"] = "tpe"
    if str(data.get("accelerator", "")).strip().lower() not in ACCELERATOR_CHOICES:
        data["accelerator"] = "auto"
    return data


def save_strategy_config(cfg: Dict) -> None:
    data = get_default_strategy_config()
    if isinstance(cfg, dict):
        data.update(cfg)
    data["universe"] = normalize_universe(data.get("universe"))
    data["tester_symbols"] = normalize_tester_symbols(data.get("tester_symbols"), data.get("symbol"))
    data["symbol"] = ", ".join(item["symbol"] for item in data["tester_symbols"] if item.get("selected", True))
    data["strategy"] = str(data.get("strategy", "keltner")).strip().lower()
    if data["strategy"] not in STRATEGY_CHOICES:
        data["strategy"] = "keltner"
    data["optimizer_engine"] = str(data.get("optimizer_engine", "tpe")).strip().lower()
    if data["optimizer_engine"] not in OPTIMIZER_ENGINES:
        data["optimizer_engine"] = "tpe"
    data["accelerator"] = str(data.get("accelerator", "auto")).strip().lower()
    if data["accelerator"] not in ACCELERATOR_CHOICES:
        data["accelerator"] = "auto"
    os.makedirs(os.path.dirname(STRATEGY_CONFIG_FILE), exist_ok=True)
    with open(STRATEGY_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def strategy_mode_for_symbol(symbol: str, cfg: Dict | None = None) -> str:
    cfg = cfg or load_strategy_config()
    target = normalize_symbol(symbol)
    modes = set()
    seen_symbol = False
    enabled_rows = 0
    for entry in normalize_universe(cfg.get("universe")):
        if entry["symbol"] != target:
            continue
        seen_symbol = True
        if not entry.get("enabled", True):
            continue
        enabled_rows += 1
        mode = str(entry.get("mode", "both") or "both").strip().lower()
        if mode == "both":
            return "both"
        if mode in {"local", "tw"}:
            modes.add(mode)
    if "local" in modes and "tw" in modes:
        return "both"
    if "local" in modes:
        return "local"
    if "tw" in modes:
        return "tw"
    if seen_symbol and enabled_rows == 0:
        return "disabled"
    return "tw"


def strategy_entries_for_symbol(symbol: str, cfg: Dict | None = None, *, enabled_only: bool = False) -> List[Dict]:
    cfg = cfg or load_strategy_config()
    target = normalize_symbol(symbol)
    out = []
    for entry in normalize_universe(cfg.get("universe")):
        if entry["symbol"] != target:
            continue
        if enabled_only and not entry.get("enabled", True):
            continue
        out.append(entry)
    return out


def tradingview_allowed_for_symbol(symbol: str, cfg: Dict | None = None) -> bool:
    mode = strategy_mode_for_symbol(symbol, cfg)
    return mode in {"tw", "both"}


def local_allowed_for_symbol(symbol: str, cfg: Dict | None = None) -> bool:
    mode = strategy_mode_for_symbol(symbol, cfg)
    return mode in {"local", "both"}
