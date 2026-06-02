#!/usr/bin/env python3
"""
Local optimizer for the Keltner Pine strategy used in this project.

What it does:
- Reads default/config values from a Pine file and an optional TradingView XLSX export.
- Pulls historical OHLC bars from Alpaca (using a user stored in local DB) or from a CSV file.
- Runs many backtests over random parameter combinations and ranks results.
- Saves a machine-readable report (JSON) and top combinations (CSV).

Notes:
- This is a pragmatic emulator for TradingView strategy behavior, not a bit-perfect clone.
- It models the broker emulator intrabar path assumption (open->high->low->close or open->low->high->close)
  and supports fixed SL/TP, forced SL/TP and trailing exits.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from openpyxl import load_workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_PINE = PROJECT_ROOT / "misc" / "keltner.pine"
DEFAULT_XLSX = PROJECT_ROOT / "misc" / "Keltner_channel_strategy_stocks_NYSE_TSM_2026-06-02.xlsx"
DEFAULT_REPORT = PROJECT_ROOT / "misc" / "optimizer_report.json"
DEFAULT_TOP_CSV = PROJECT_ROOT / "misc" / "optimizer_top.csv"


@dataclass
class StrategyParams:
    trade_direction: str
    inner_kc_length: int
    inner_kc_mult: float
    outer_kc_length: int
    outer_kc_mult: float
    fixed_stop_loss_pct: float
    fixed_take_profit_pct: float
    forced_stop_loss_pct: float
    forced_take_profit_pct: float
    trailing_offset_ticks: int
    tick_size: float


@dataclass
class BacktestConfig:
    initial_capital: float
    order_size_usd: float
    commission_pct: float
    timezone_name: str
    market_close_utc_hour: int = 20
    market_close_utc_minute: int = 30
    close_before_minutes: int = 4


@dataclass
class BacktestResult:
    params: StrategyParams
    final_equity: float
    net_profit: float
    return_pct: float
    total_trades: int
    winners: int
    losers: int
    win_rate_pct: float
    gross_profit: float
    gross_loss: float
    profit_factor: float
    sharpe: float
    max_drawdown: float
    max_drawdown_pct: float
    avg_bars_per_trade: float
    score: float


def _safe_float(value: object, default: float) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace("%", "").replace(",", "").strip())
    except Exception:
        return default


def _safe_int(value: object, default: int) -> int:
    try:
        if value is None:
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def parse_pine_defaults(pine_path: Path) -> Dict[str, object]:
    text = pine_path.read_text(encoding="utf-8")

    defaults: Dict[str, object] = {
        "trade_direction": "Both",
        "inner_kc_length": 13,
        "inner_kc_mult": 1.0,
        "outer_kc_length": 13,
        "outer_kc_mult": 2.0,
        "fixed_stop_loss_pct": 3.0,
        "fixed_take_profit_pct": 3.0,
        "forced_stop_loss_pct": 6.0,
        "forced_take_profit_pct": 6.0,
        "trailing_offset_ticks": 4,
        "initial_capital": 1000.0,
        "commission_pct": 0.04,
        "order_size_usd": 2000.0,
        "tick_size": 0.01,
    }

    # Parse strategy(...) line for initial capital and commission.
    strat_match = re.search(r"strategy\((.*?)\)", text, flags=re.S)
    if strat_match:
        s = strat_match.group(1)
        m_cap = re.search(r"initial_capital\s*=\s*([\d\.]+)", s)
        m_comm = re.search(r"commission_value\s*=\s*([\d\.]+)", s)
        if m_cap:
            defaults["initial_capital"] = _safe_float(m_cap.group(1), defaults["initial_capital"])
        if m_comm:
            defaults["commission_pct"] = _safe_float(m_comm.group(1), defaults["commission_pct"])

    def _extract_input_default(title: str, func: str = "input") -> Optional[str]:
        pat = rf"{func}\s*\(\s*([^,\)]+)\s*,.*?title\s*=\s*['\"]{re.escape(title)}['\"]"
        m = re.search(pat, text, flags=re.S)
        if not m:
            return None
        return m.group(1).strip().strip("'\"")

    mapping = [
        ("inner_kc_length", "Inner KC Length", "input", True),
        ("inner_kc_mult", "Inner KC Multiplier", "input", False),
        ("outer_kc_length", "Outer KC Length", "input", True),
        ("outer_kc_mult", "Outer KC Multiplier", "input", False),
        ("fixed_stop_loss_pct", "Fixed Stop Loss (%)", "input.float", False),
        ("fixed_take_profit_pct", "Fixed Take Profit (%)", "input.float", False),
        ("forced_stop_loss_pct", "Forced Stop Loss (%)", "input.float", False),
        ("forced_take_profit_pct", "Forced Take Profit (%)", "input.float", False),
    ]

    for key, title, func, is_int in mapping:
        v = _extract_input_default(title, func=func)
        if v is None:
            continue
        if is_int:
            defaults[key] = _safe_int(v, int(defaults[key]))
        else:
            defaults[key] = _safe_float(v, float(defaults[key]))

    m_trade = re.search(r"trade_direction\s*=\s*input\.string\((.*?)\)", text, flags=re.S)
    if m_trade:
        m_def = re.search(r"defval\s*=\s*\"([^\"]+)\"", m_trade.group(1))
        if m_def:
            defaults["trade_direction"] = m_def.group(1).strip()

    m_trail = re.search(r"trail_offset\s*=\s*([\d\.]+)", text)
    if m_trail:
        defaults["trailing_offset_ticks"] = _safe_int(m_trail.group(1), defaults["trailing_offset_ticks"])

    return defaults


def read_reference_xlsx(xlsx_path: Path) -> Dict[str, object]:
    wb = load_workbook(xlsx_path, data_only=True, read_only=True)
    out: Dict[str, object] = {
        "properties": {},
        "performance": {},
        "trades_analysis": {},
        "risk_adjusted": {},
    }

    if "Properties" in wb.sheetnames:
        ws = wb["Properties"]
        for r in range(2, ws.max_row + 1):
            k = ws.cell(r, 1).value
            v = ws.cell(r, 2).value
            if k:
                out["properties"][str(k).strip()] = v

    def read_metric_sheet(sheet_name: str, target_key: str) -> None:
        if sheet_name not in wb.sheetnames:
            return
        ws = wb[sheet_name]
        # first column metric name, second column "All USD", third "All %"
        for r in range(2, ws.max_row + 1):
            name = ws.cell(r, 1).value
            if not name:
                continue
            usd_val = ws.cell(r, 2).value
            pct_val = ws.cell(r, 3).value
            out[target_key][str(name).strip()] = {"usd": usd_val, "pct": pct_val}

    read_metric_sheet("Performance", "performance")
    read_metric_sheet("Trades analysis", "trades_analysis")
    read_metric_sheet("Risk-adjusted performance", "risk_adjusted")
    return out


def parse_tv_datetime(dt_str: str, tz_name: str) -> datetime:
    # Example: "Jan 03, 2023, 16:30"
    naive = datetime.strptime(dt_str.strip(), "%b %d, %Y, %H:%M")
    # Keep dependency-light: use fixed offset from local timezone today as practical approximation.
    # For DST-accurate conversions, Python zoneinfo is used below when available.
    try:
        from zoneinfo import ZoneInfo

        return naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
    except Exception:
        # Fallback to local +02/+03 style through current system offset.
        local_off = datetime.now().astimezone().utcoffset() or timedelta(0)
        return naive.replace(tzinfo=timezone(local_off)).astimezone(timezone.utc)


def derive_reference_settings(
    pine_defaults: Dict[str, object],
    ref_props: Dict[str, object],
    timezone_name: str,
) -> Tuple[StrategyParams, BacktestConfig, str, str, datetime, datetime, str]:
    prop = ref_props

    # Symbol is usually like NYSE:TSM
    raw_symbol = str(prop.get("Symbol", "NYSE:TSM"))
    symbol = raw_symbol.split(":")[-1].strip().upper()

    timeframe = str(prop.get("Timeframe", "30 minutes")).strip()
    if timeframe.lower().startswith("30"):
        tf = "30Min"
    elif timeframe.lower().startswith("1 hour"):
        tf = "1Hour"
    else:
        tf = "30Min"

    backtest_range = str(prop.get("Backtesting range", "Jan 03, 2023, 16:30 — Jun 01, 2026, 22:30"))
    if "—" in backtest_range:
        start_s, end_s = [x.strip() for x in backtest_range.split("—", 1)]
    elif "-" in backtest_range:
        start_s, end_s = [x.strip() for x in backtest_range.split("-", 1)]
    else:
        start_s, end_s = "Jan 03, 2023, 16:30", "Jun 01, 2026, 22:30"

    start_utc = parse_tv_datetime(start_s, timezone_name)
    end_utc = parse_tv_datetime(end_s, timezone_name)

    params = StrategyParams(
        trade_direction=str(prop.get("Trade Direction", pine_defaults["trade_direction"])),
        inner_kc_length=_safe_int(prop.get("Inner KC Length"), int(pine_defaults["inner_kc_length"])),
        inner_kc_mult=_safe_float(prop.get("Inner KC Multiplier"), float(pine_defaults["inner_kc_mult"])),
        outer_kc_length=_safe_int(prop.get("Outer KC Length"), int(pine_defaults["outer_kc_length"])),
        outer_kc_mult=_safe_float(prop.get("Outer KC Multiplier"), float(pine_defaults["outer_kc_mult"])),
        fixed_stop_loss_pct=_safe_float(prop.get("Fixed Stop Loss (%)"), float(pine_defaults["fixed_stop_loss_pct"])),
        fixed_take_profit_pct=_safe_float(prop.get("Fixed Take Profit (%)"), float(pine_defaults["fixed_take_profit_pct"])),
        forced_stop_loss_pct=_safe_float(prop.get("Forced Stop Loss (%)"), float(pine_defaults["forced_stop_loss_pct"])),
        forced_take_profit_pct=_safe_float(prop.get("Forced Take Profit (%)"), float(pine_defaults["forced_take_profit_pct"])),
        trailing_offset_ticks=int(pine_defaults["trailing_offset_ticks"]),
        tick_size=_safe_float(prop.get("Tick size"), float(pine_defaults["tick_size"])),
    )

    cfg = BacktestConfig(
        initial_capital=_safe_float(prop.get("Initial capital"), float(pine_defaults["initial_capital"])),
        order_size_usd=_safe_float(prop.get("Order size"), float(pine_defaults["order_size_usd"])),
        commission_pct=_safe_float(prop.get("Commission"), float(pine_defaults["commission_pct"])),
        timezone_name=timezone_name,
    )

    return params, cfg, symbol, tf, start_utc, end_utc, raw_symbol


def fetch_bars_alpaca(
    symbol: str,
    timeframe: str,
    start_utc: datetime,
    end_utc: datetime,
    username: Optional[str],
    feed: str,
) -> pd.DataFrame:
    load_dotenv(PROJECT_ROOT / ".env")

    from flask import Flask

    from alpaca_api import LegacyCompatibleAlpacaClient
    from models import User, db
    from utils import decrypt_data

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = __import__("os").getenv("DATABASE_URL", "sqlite:///app.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        if username:
            user = User.query.filter_by(username=username).first()
        else:
            user = User.query.filter_by(is_superuser=True).first() or User.query.first()
        if not user:
            raise RuntimeError("No user found in local DB to decrypt Alpaca credentials.")

        key = decrypt_data(user.encrypted_alpaca_key)
        secret = decrypt_data(user.encrypted_alpaca_secret)
        if not key or not secret:
            raise RuntimeError(f"User '{user.username}' has no usable Alpaca credentials.")

        base_url = __import__("os").getenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")
        api = LegacyCompatibleAlpacaClient(key, secret, base_url)

        bars = api.get_bars(
            symbol,
            timeframe,
            start=start_utc.isoformat().replace("+00:00", "Z"),
            end=end_utc.isoformat().replace("+00:00", "Z"),
            adjustment="raw",
            feed=feed,
        ).df

    if bars.empty:
        raise RuntimeError("No bars returned from Alpaca. Check symbol/timeframe/feed/range.")

    # Normalize columns and ordering.
    bars = bars.copy()
    bars = bars[["open", "high", "low", "close", "volume"]]
    bars = bars.sort_index()
    bars = bars[~bars.index.duplicated(keep="first")]
    bars.index = pd.to_datetime(bars.index, utc=True)
    return bars


def load_bars_csv(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    needed = {"timestamp", "open", "high", "low", "close"}
    if not needed.issubset(set(df.columns)):
        raise ValueError(f"CSV must include columns: {sorted(needed)}")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()
    if "volume" not in df.columns:
        df["volume"] = 0
    return df[["open", "high", "low", "close", "volume"]]


def filter_session(df: pd.DataFrame, mode: str) -> pd.DataFrame:
    mode = (mode or "regular").lower()
    if mode == "all":
        return df
    if len(df.index) > 1:
        min_delta = df.index.to_series().diff().dropna().min()
        if pd.notna(min_delta) and min_delta >= pd.Timedelta(hours=23):
            return df if mode == "regular" else df.iloc[0:0]
    ny = df.tz_convert("America/New_York")
    minutes = ny.index.hour * 60 + ny.index.minute
    regular_mask = (minutes >= (9 * 60 + 30)) & (minutes < (16 * 60))
    if mode == "regular":
        return df[regular_mask]
    if mode == "extended":
        return df[~regular_mask]
    raise ValueError(f"Unknown session mode: {mode}")


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    h_l = df["high"] - df["low"]
    h_pc = (df["high"] - prev_close).abs()
    l_pc = (df["low"] - prev_close).abs()
    return pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)


def keltner_channel(df: pd.DataFrame, length: int, mult: float) -> Tuple[pd.Series, pd.Series, pd.Series]:
    basis = ema(df["close"], length)
    span = ema(true_range(df), length)
    upper = basis + span * mult
    lower = basis - span * mult
    return basis, upper, lower


def infer_path(open_p: float, high_p: float, low_p: float, close_p: float) -> List[float]:
    # TradingView historical emulator heuristic.
    if abs(high_p - open_p) <= abs(open_p - low_p):
        return [open_p, high_p, low_p, close_p]
    return [open_p, low_p, high_p, close_p]


def crosses_up(a: float, b: float, level: float) -> bool:
    return min(a, b) <= level <= max(a, b) and b >= a and b >= level


def crosses_down(a: float, b: float, level: float) -> bool:
    return min(a, b) <= level <= max(a, b) and b <= a and b <= level


def long_intrabar_exit(
    path: List[float],
    activation: float,
    forced_stop: float,
    forced_tp: float,
    trail_offset: float,
    trail_active: bool,
    trail_stop: Optional[float],
    allow_trail: bool,
) -> Tuple[Optional[float], Optional[str], bool, Optional[float]]:
    # Returns: (exit_price, reason, new_trail_active, new_trail_stop)
    active = trail_active
    stop = trail_stop

    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]

        # Gap/segment hit checks for forced orders first (OCA stop/limit style).
        if b >= a:
            # Up segment: forced TP can hit.
            if a <= forced_tp <= b:
                return forced_tp, "Forced TP Long", active, stop
        else:
            # Down segment: forced SL can hit.
            if b <= forced_stop <= a:
                return forced_stop, "Forced SL Long", active, stop

        if not allow_trail:
            continue

        # Trailing activation/update.
        if not active:
            # Activation when price crosses upward the activation level.
            if b >= a and a <= activation <= b:
                active = True
                stop = activation - trail_offset
            elif a >= activation:
                active = True
                stop = a - trail_offset

        if active:
            if b >= a:
                # Favorable move: update stop with new highs reached in segment.
                candidate = b - trail_offset
                stop = candidate if stop is None else max(stop, candidate)
            else:
                # Adverse move: stop may trigger.
                if stop is not None and b <= stop <= a:
                    return stop, "Trailing Exit Long", active, stop

    return None, None, active, stop


def short_intrabar_exit(
    path: List[float],
    activation: float,
    forced_stop: float,
    forced_tp: float,
    trail_offset: float,
    trail_active: bool,
    trail_stop: Optional[float],
    allow_trail: bool,
) -> Tuple[Optional[float], Optional[str], bool, Optional[float]]:
    active = trail_active
    stop = trail_stop

    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]

        if b <= a:
            # Down segment: forced TP can hit.
            if b <= forced_tp <= a:
                return forced_tp, "Forced TP Short", active, stop
        else:
            # Up segment: forced SL can hit.
            if a <= forced_stop <= b:
                return forced_stop, "Forced SL Short", active, stop

        if not allow_trail:
            continue

        if not active:
            # Activation when price crosses downward activation.
            if b <= a and b <= activation <= a:
                active = True
                stop = activation + trail_offset
            elif a <= activation:
                active = True
                stop = a + trail_offset

        if active:
            if b <= a:
                candidate = b + trail_offset
                stop = candidate if stop is None else min(stop, candidate)
            else:
                if stop is not None and a <= stop <= b:
                    return stop, "Trailing Exit Short", active, stop

    return None, None, active, stop


def in_preclose_window(ts_utc: pd.Timestamp, cfg: BacktestConfig) -> bool:
    # Approximation for the Pine time-window logic.
    mc = ts_utc.replace(hour=cfg.market_close_utc_hour, minute=cfg.market_close_utc_minute, second=0, microsecond=0)
    start = mc - timedelta(minutes=cfg.close_before_minutes)
    return start <= ts_utc < mc


def backtest(df: pd.DataFrame, params: StrategyParams, cfg: BacktestConfig, start_utc: datetime, end_utc: datetime) -> BacktestResult:
    df = df[(df.index >= pd.Timestamp(start_utc)) & (df.index <= pd.Timestamp(end_utc))].copy()
    if len(df) < 100:
        raise RuntimeError("Not enough bars for backtest after date filtering.")

    mid_inner, up_inner, low_inner = keltner_channel(df, params.inner_kc_length, params.inner_kc_mult)
    mid_outer, up_outer, low_outer = keltner_channel(df, params.outer_kc_length, params.outer_kc_mult)

    df["mid_inner"] = mid_inner
    df["up_inner"] = up_inner
    df["low_inner"] = low_inner
    df["mid_outer"] = mid_outer
    df["up_outer"] = up_outer
    df["low_outer"] = low_outer
    df = df.dropna()

    if df.empty:
        raise RuntimeError("Indicator warm-up removed all bars; widen date range.")

    idx = df.index
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    mid = df["mid_inner"].to_numpy(dtype=float)
    uin = df["up_inner"].to_numpy(dtype=float)
    lin = df["low_inner"].to_numpy(dtype=float)

    equity = cfg.initial_capital
    equity_marks: List[float] = [equity]

    position = 0  # +1 long, -1 short, 0 flat
    entry_price = 0.0
    qty = 0
    entry_index = -1
    trail_active = False
    trail_stop: Optional[float] = None

    gross_profit = 0.0
    gross_loss = 0.0
    commission_paid = 0.0
    winners = 0
    losers = 0

    trade_returns: List[float] = []
    trade_bars: List[int] = []

    for i in range(1, len(df)):
        ts = idx[i]
        entered_this_bar = False

        # Entry logic first, because Pine evaluates entries before exit blocks.
        if position == 0:
            long_allowed = params.trade_direction in ("Both", "Long Only")
            short_allowed = params.trade_direction in ("Both", "Short Only")

            long_cond = c[i - 1] <= lin[i - 1] and c[i] > lin[i] and c[i] < mid[i]
            short_cond = c[i - 1] >= uin[i - 1] and c[i] < uin[i] and c[i] > mid[i]

            entry_side = 0
            if long_allowed and long_cond:
                entry_side = 1
            elif short_allowed and short_cond:
                entry_side = -1

            if entry_side != 0:
                q = math.floor(cfg.order_size_usd / c[i])
                if q > 0:
                    position = entry_side
                    entry_price = c[i]
                    qty = q
                    entry_index = i
                    trail_active = False
                    trail_stop = None
                    entered_this_bar = True

        # Exit logic for positions that were already open before this bar.
        if position != 0 and not entered_this_bar:
            exit_price: Optional[float] = None
            exit_reason: Optional[str] = None

            fixed_reason: Optional[str] = None
            path = infer_path(o[i], h[i], l[i], c[i])
            trail_offset = params.trailing_offset_ticks * params.tick_size

            if position > 0:
                fixed_stop = entry_price * (1 - params.fixed_stop_loss_pct / 100.0)
                fixed_tp = entry_price * (1 + params.fixed_take_profit_pct / 100.0)
                forced_stop = entry_price * (1 - params.forced_stop_loss_pct / 100.0)
                forced_tp = entry_price * (1 + params.forced_take_profit_pct / 100.0)

                if l[i] <= fixed_stop:
                    fixed_reason = "Fixed Stop Loss (Long)"
                if h[i] >= fixed_tp:
                    fixed_reason = "Fixed Take Profit (Long)"

                ex_p, ex_r, trail_active, trail_stop = long_intrabar_exit(
                    path=path,
                    activation=mid[i],
                    forced_stop=forced_stop,
                    forced_tp=forced_tp,
                    trail_offset=trail_offset,
                    trail_active=trail_active,
                    trail_stop=trail_stop,
                    allow_trail=(fixed_reason is None),
                )
                if ex_p is not None:
                    exit_price, exit_reason = ex_p, ex_r

                if exit_price is None and in_preclose_window(ts, cfg) and c[i] > entry_price:
                    exit_price = c[i]
                    exit_reason = "Forced TP Long"

            else:
                fixed_stop = entry_price * (1 + params.fixed_stop_loss_pct / 100.0)
                fixed_tp = entry_price * (1 - params.fixed_take_profit_pct / 100.0)
                forced_stop = entry_price * (1 + params.forced_stop_loss_pct / 100.0)
                forced_tp = entry_price * (1 - params.forced_take_profit_pct / 100.0)

                if h[i] >= fixed_stop:
                    fixed_reason = "Fixed Stop Loss (Short)"
                if l[i] <= fixed_tp:
                    fixed_reason = "Fixed Take Profit (Short)"

                ex_p, ex_r, trail_active, trail_stop = short_intrabar_exit(
                    path=path,
                    activation=mid[i],
                    forced_stop=forced_stop,
                    forced_tp=forced_tp,
                    trail_offset=trail_offset,
                    trail_active=trail_active,
                    trail_stop=trail_stop,
                    allow_trail=(fixed_reason is None),
                )
                if ex_p is not None:
                    exit_price, exit_reason = ex_p, ex_r

                if exit_price is None and in_preclose_window(ts, cfg) and c[i] < entry_price:
                    exit_price = c[i]
                    exit_reason = "Forced TP Short"

            # strategy.close based fixed exits happen at bar close.
            if exit_price is None and fixed_reason is not None:
                exit_price = c[i]
                exit_reason = fixed_reason

            if exit_price is not None:
                notional_entry = qty * entry_price
                notional_exit = qty * exit_price
                fees = (notional_entry + notional_exit) * (cfg.commission_pct / 100.0)
                pnl = (exit_price - entry_price) * qty * position - fees
                pnl_pct = (pnl / notional_entry) * 100.0 if notional_entry else 0.0

                commission_paid += fees
                if pnl >= 0:
                    gross_profit += pnl
                    winners += 1
                else:
                    gross_loss += abs(pnl)
                    losers += 1

                equity += pnl
                equity_marks.append(equity)
                trade_returns.append(pnl_pct)
                trade_bars.append(i - entry_index)

                position = 0
                entry_price = 0.0
                qty = 0
                entry_index = -1
                trail_active = False
                trail_stop = None

    # Close open position at final close for metric completeness.
    if position != 0 and qty > 0:
        final_price = c[-1]
        notional_entry = qty * entry_price
        notional_exit = qty * final_price
        fees = (notional_entry + notional_exit) * (cfg.commission_pct / 100.0)
        pnl = (final_price - entry_price) * qty * position - fees
        pnl_pct = (pnl / notional_entry) * 100.0 if notional_entry else 0.0
        commission_paid += fees
        if pnl >= 0:
            gross_profit += pnl
            winners += 1
        else:
            gross_loss += abs(pnl)
            losers += 1
        equity += pnl
        equity_marks.append(equity)
        trade_returns.append(pnl_pct)
        trade_bars.append(len(df) - 1 - entry_index)

    total_trades = winners + losers
    net_profit = equity - cfg.initial_capital
    return_pct = (net_profit / cfg.initial_capital) * 100.0 if cfg.initial_capital else 0.0
    win_rate = (winners / total_trades) * 100.0 if total_trades else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)

    eq = np.array(equity_marks, dtype=float)
    running_max = np.maximum.accumulate(eq)
    drawdowns = running_max - eq
    max_dd = float(drawdowns.max()) if len(drawdowns) else 0.0
    max_dd_pct = float(((drawdowns / running_max).max() * 100.0) if len(drawdowns) and np.any(running_max > 0) else 0.0)

    rets = np.array(trade_returns, dtype=float)
    sharpe = 0.0
    if len(rets) > 2 and rets.std(ddof=1) > 1e-12:
        sharpe = float(rets.mean() / rets.std(ddof=1) * math.sqrt(len(rets)))

    avg_bars = float(np.mean(trade_bars)) if trade_bars else 0.0

    score = net_profit * max(profit_factor, 0.01) / (1.0 + max_dd_pct)

    return BacktestResult(
        params=params,
        final_equity=equity,
        net_profit=net_profit,
        return_pct=return_pct,
        total_trades=total_trades,
        winners=winners,
        losers=losers,
        win_rate_pct=win_rate,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        sharpe=sharpe,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        avg_bars_per_trade=avg_bars,
        score=score,
    )


def parse_range(spec: str, is_int: bool) -> List[float]:
    parts = [x.strip() for x in spec.split(":")]
    if len(parts) != 3:
        raise ValueError(f"Range must be min:max:step, got '{spec}'")
    start, end, step = map(float, parts)
    if step <= 0:
        raise ValueError("Step must be > 0")
    values: List[float] = []
    x = start
    while x <= end + 1e-12:
        values.append(round(x if not is_int else int(round(x)), 10))
        x += step
    if is_int:
        values = sorted(set(int(v) for v in values))
    return values


def sample_params(
    rng: random.Random,
    base: StrategyParams,
    ranges: Dict[str, List[float]],
    trade_direction: str,
) -> StrategyParams:
    return StrategyParams(
        trade_direction=trade_direction,
        inner_kc_length=int(rng.choice(ranges["inner_kc_length"])),
        inner_kc_mult=float(rng.choice(ranges["inner_kc_mult"])),
        outer_kc_length=int(rng.choice(ranges["outer_kc_length"])),
        outer_kc_mult=float(rng.choice(ranges["outer_kc_mult"])),
        fixed_stop_loss_pct=float(rng.choice(ranges["fixed_stop_loss_pct"])),
        fixed_take_profit_pct=float(rng.choice(ranges["fixed_take_profit_pct"])),
        forced_stop_loss_pct=float(rng.choice(ranges["forced_stop_loss_pct"])),
        forced_take_profit_pct=float(rng.choice(ranges["forced_take_profit_pct"])),
        trailing_offset_ticks=int(rng.choice(ranges["trailing_offset_ticks"])),
        tick_size=base.tick_size,
    )


def result_row(res: BacktestResult) -> Dict[str, object]:
    row = {
        "score": round(res.score, 6),
        "net_profit": round(res.net_profit, 2),
        "return_pct": round(res.return_pct, 2),
        "total_trades": res.total_trades,
        "win_rate_pct": round(res.win_rate_pct, 2),
        "profit_factor": round(res.profit_factor, 4),
        "sharpe": round(res.sharpe, 4),
        "max_drawdown": round(res.max_drawdown, 2),
        "max_drawdown_pct": round(res.max_drawdown_pct, 2),
    }
    row.update(asdict(res.params))
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description="Optimize Keltner Pine strategy parameters locally.")
    parser.add_argument("--pine", type=Path, default=DEFAULT_PINE)
    parser.add_argument("--reference-xlsx", type=Path, default=DEFAULT_XLSX)
    parser.add_argument("--bars-csv", type=Path, default=None, help="Optional CSV with timestamp,open,high,low,close[,volume]")
    parser.add_argument("--alpaca-user", type=str, default=None, help="Username from local DB to use Alpaca keys")
    parser.add_argument("--feed", type=str, default="iex", help="Alpaca feed (iex/sip). For free plans use iex.")
    parser.add_argument("--symbol", type=str, default=None)
    parser.add_argument("--timeframe", type=str, default=None)
    parser.add_argument("--session", type=str, default="regular", choices=["regular", "extended", "all"])
    parser.add_argument("--start", type=str, default=None, help="UTC ISO start override, e.g. 2023-01-03T00:00:00Z")
    parser.add_argument("--end", type=str, default=None, help="UTC ISO end override")
    parser.add_argument("--timezone", type=str, default="Europe/Bucharest")
    parser.add_argument("--initial-capital", type=float, default=None)
    parser.add_argument("--order-size", type=float, default=None)
    parser.add_argument("--commission-pct", type=float, default=None)
    parser.add_argument("--tick-size", type=float, default=None)
    parser.add_argument("--trials", type=int, default=250)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--trade-direction", type=str, default="Both", choices=["Both", "Long Only", "Short Only"])

    parser.add_argument("--inner-len-range", type=str, default="8:40:1")
    parser.add_argument("--inner-mult-range", type=str, default="0.6:1.8:0.1")
    parser.add_argument("--outer-len-range", type=str, default="8:40:1")
    # Outer multiplier is integer in the current Pine input definition (`input(2, ...)`).
    parser.add_argument("--outer-mult-range", type=str, default="1:4:1")
    parser.add_argument("--fixed-sl-range", type=str, default="1.0:5.0:0.1")
    parser.add_argument("--fixed-tp-range", type=str, default="0.8:4.0:0.1")
    parser.add_argument("--forced-sl-range", type=str, default="3.0:10.0:0.2")
    parser.add_argument("--forced-tp-range", type=str, default="3.0:10.0:0.2")
    # Trailing offset is currently hardcoded in Pine (`trail_offset = 4`), so keep fixed by default.
    parser.add_argument("--trail-offset-range", type=str, default="4:4:1")

    parser.add_argument("--report-json", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--top-csv", type=Path, default=DEFAULT_TOP_CSV)

    args = parser.parse_args()

    if not args.pine.exists():
        raise SystemExit(f"Pine file not found: {args.pine}")

    pine_defaults = parse_pine_defaults(args.pine)

    ref_data = {"properties": {}}
    if args.reference_xlsx and args.reference_xlsx.exists():
        ref_data = read_reference_xlsx(args.reference_xlsx)

    ref_props = ref_data.get("properties", {})
    base_params, cfg, symbol_from_ref, tf_from_ref, start_utc, end_utc, raw_symbol = derive_reference_settings(
        pine_defaults,
        ref_props,
        args.timezone,
    )

    symbol = (args.symbol or symbol_from_ref).split(":")[-1].upper()
    timeframe = args.timeframe or tf_from_ref

    if args.start:
        start_utc = pd.Timestamp(args.start, tz="UTC").to_pydatetime()
    if args.end:
        end_utc = pd.Timestamp(args.end, tz="UTC").to_pydatetime()

    if args.initial_capital is not None:
        cfg.initial_capital = float(args.initial_capital)
    if args.order_size is not None:
        cfg.order_size_usd = float(args.order_size)
    if args.commission_pct is not None:
        cfg.commission_pct = float(args.commission_pct)
    if args.tick_size is not None:
        base_params.tick_size = float(args.tick_size)

    if args.bars_csv:
        bars = load_bars_csv(args.bars_csv)
    else:
        bars = fetch_bars_alpaca(
            symbol=symbol,
            timeframe=timeframe,
            start_utc=start_utc,
            end_utc=end_utc,
            username=args.alpaca_user,
            feed=args.feed,
        )

    bars = filter_session(bars, args.session)
    if bars.empty:
        raise SystemExit(f"No bars left after applying session filter '{args.session}'.")

    ranges = {
        "inner_kc_length": parse_range(args.inner_len_range, is_int=True),
        "inner_kc_mult": parse_range(args.inner_mult_range, is_int=False),
        "outer_kc_length": parse_range(args.outer_len_range, is_int=True),
        "outer_kc_mult": parse_range(args.outer_mult_range, is_int=False),
        "fixed_stop_loss_pct": parse_range(args.fixed_sl_range, is_int=False),
        "fixed_take_profit_pct": parse_range(args.fixed_tp_range, is_int=False),
        "forced_stop_loss_pct": parse_range(args.forced_sl_range, is_int=False),
        "forced_take_profit_pct": parse_range(args.forced_tp_range, is_int=False),
        "trailing_offset_ticks": parse_range(args.trail_offset_range, is_int=True),
    }
    if all(len(values) == 1 for values in ranges.values()):
        base_params = StrategyParams(
            trade_direction=args.trade_direction,
            inner_kc_length=int(ranges["inner_kc_length"][0]),
            inner_kc_mult=float(ranges["inner_kc_mult"][0]),
            outer_kc_length=int(ranges["outer_kc_length"][0]),
            outer_kc_mult=float(ranges["outer_kc_mult"][0]),
            fixed_stop_loss_pct=float(ranges["fixed_stop_loss_pct"][0]),
            fixed_take_profit_pct=float(ranges["fixed_take_profit_pct"][0]),
            forced_stop_loss_pct=float(ranges["forced_stop_loss_pct"][0]),
            forced_take_profit_pct=float(ranges["forced_take_profit_pct"][0]),
            trailing_offset_ticks=int(ranges["trailing_offset_ticks"][0]),
            tick_size=base_params.tick_size,
        )

    rng = random.Random(args.seed)
    seen = set()
    results: List[BacktestResult] = []

    # Evaluate reference config first.
    base_params.trade_direction = args.trade_direction
    base_result = backtest(bars, base_params, cfg, start_utc, end_utc)
    results.append(base_result)
    seen.add(tuple(asdict(base_params).items()))

    print(f"Reference/backbone result: net={base_result.net_profit:.2f} USD | PF={base_result.profit_factor:.3f} | "
          f"trades={base_result.total_trades} | win={base_result.win_rate_pct:.2f}%")

    for i in range(args.trials):
        p = sample_params(rng, base_params, ranges, args.trade_direction)
        key = tuple(asdict(p).items())
        if key in seen:
            continue
        seen.add(key)

        try:
            res = backtest(bars, p, cfg, start_utc, end_utc)
            results.append(res)
        except Exception:
            continue

        if (i + 1) % 25 == 0:
            best = max(results, key=lambda r: r.score)
            print(f"Trial {i+1:4d}/{args.trials}: best score={best.score:.4f}, net={best.net_profit:.2f}, "
                  f"PF={best.profit_factor:.3f}, trades={best.total_trades}")

    results.sort(key=lambda r: r.score, reverse=True)
    top = results[: max(1, args.top_k)]

    args.top_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.top_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(result_row(top[0]).keys()))
        writer.writeheader()
        for r in top:
            writer.writerow(result_row(r))

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "symbol_input": raw_symbol,
        "symbol_used": symbol,
        "timeframe": timeframe,
        "date_range_utc": {"start": start_utc.isoformat(), "end": end_utc.isoformat()},
        "bars_count": int(len(bars)),
        "session_filter": args.session,
        "config": asdict(cfg),
        "reference_properties": {k: str(v) for k, v in ref_props.items()},
        "reference_metrics": ref_data,
        "reference_result": result_row(base_result),
        "best_result": result_row(top[0]),
        "top_results": [result_row(r) for r in top],
    }

    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    best = top[0]
    print("\n=== Best combination ===")
    print(f"Score: {best.score:.4f}")
    print(f"Net profit: {best.net_profit:.2f} USD ({best.return_pct:.2f}%)")
    print(f"Profit factor: {best.profit_factor:.3f} | Sharpe: {best.sharpe:.3f}")
    print(f"Trades: {best.total_trades} | Win rate: {best.win_rate_pct:.2f}% | Max DD: {best.max_drawdown_pct:.2f}%")
    print("Params:")
    for k, v in asdict(best.params).items():
        print(f"  - {k}: {v}")

    print(f"\nSaved top combinations: {args.top_csv}")
    print(f"Saved full report: {args.report_json}")


if __name__ == "__main__":
    main()
