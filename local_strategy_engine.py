#!/usr/bin/env python3
"""Local live strategy engine for Signal Universe symbols.

The engine is intentionally conservative:
- evaluates entries only on fully closed bars,
- deduplicates one entry decision per symbol/bar,
- uses Alpaca position state before every open/close decision,
- persists failed order attempts for recovery retries.
"""

from __future__ import annotations

import json
import logging
import hashlib
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Callable, Dict, Optional

import pandas as pd

from alpaca_api import AlpacaAPIError, LegacyCompatibleAlpacaClient
from llm_trade_validator import create_llm_trade_validator
from misc.pine_optimizer import (
    choose_fetch_timeframe,
    crossed_above,
    crossed_below,
    filter_session,
    keltner_channel,
    normalize_timeframe_token,
    resample_bars,
    sma,
    timeframe_seconds,
)
from models import Trade, User, db
import strategy_config as strategy_store
from utils import decrypt_data


class TransientStrategyAPIError(RuntimeError):
    pass


class LocalStrategyEngine:
    def __init__(
        self,
        flask_app,
        execute_trade: Callable,
        base_url: str,
        logger: logging.Logger,
    ):
        self.app = flask_app
        self.execute_trade = execute_trade
        self.base_url = base_url
        self.logger = logger
        self.poll_seconds = int(os.getenv("LOCAL_STRATEGY_POLL_SECONDS", "15"))
        self.bars_lookback = int(os.getenv("LOCAL_STRATEGY_BARS_LOOKBACK", "260"))
        self.entry_refetch_seconds = int(os.getenv("LOCAL_STRATEGY_ENTRY_REFETCH_SECONDS", "60"))
        self.recovery_base_seconds = int(os.getenv("LOCAL_STRATEGY_RECOVERY_BASE_SECONDS", "15"))
        self.recovery_max_seconds = int(os.getenv("LOCAL_STRATEGY_RECOVERY_MAX_SECONDS", "300"))
        self.open_recovery_max_attempts = int(os.getenv("LOCAL_STRATEGY_OPEN_RECOVERY_MAX_ATTEMPTS", "3"))
        self.close_recovery_max_attempts = int(os.getenv("LOCAL_STRATEGY_CLOSE_RECOVERY_MAX_ATTEMPTS", "0"))
        self.api_failure_cooldown_seconds = int(os.getenv("LOCAL_STRATEGY_API_FAILURE_COOLDOWN_SECONDS", "120"))
        self.log_throttle_seconds = int(os.getenv("LOCAL_STRATEGY_LOG_THROTTLE_SECONDS", "900"))
        self.min_backtest_trades = int(os.getenv("LOCAL_STRATEGY_MIN_BACKTEST_TRADES", "5"))
        self.min_backtest_win_rate_pct = float(os.getenv("LOCAL_STRATEGY_MIN_BACKTEST_WIN_RATE_PCT", "45"))
        self.min_backtest_profit_factor = float(os.getenv("LOCAL_STRATEGY_MIN_BACKTEST_PROFIT_FACTOR", "1.3"))
        self.max_backtest_drawdown_pct = float(os.getenv("LOCAL_STRATEGY_MAX_BACKTEST_DRAWDOWN_PCT", "15"))
        self.min_backtest_net_profit = float(os.getenv("LOCAL_STRATEGY_MIN_BACKTEST_NET_PROFIT", "0"))
        self.daily_max_trades_per_symbol = int(os.getenv("LOCAL_STRATEGY_DAILY_MAX_TRADES_PER_SYMBOL", "3"))
        self.daily_max_losses_per_symbol = int(os.getenv("LOCAL_STRATEGY_DAILY_MAX_LOSSES_PER_SYMBOL", "2"))
        self.daily_max_loss_usd_per_symbol = float(os.getenv("LOCAL_STRATEGY_DAILY_MAX_LOSS_USD_PER_SYMBOL", "100"))
        self.event_log_path = os.path.join(self.app.instance_path, "local_strategy_events.jsonl")
        self.dry_run = os.getenv("LOCAL_STRATEGY_DRY_RUN", "false").lower() in ("1", "true", "yes", "y")
        self.state_path = os.path.join(self.app.instance_path, "local_strategy_state.json")
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.state_lock = threading.Lock()
        self.state = {"symbols": {}, "recoveries": []}
        self.llm_validator = create_llm_trade_validator(self.app.instance_path, self.logger)
        # Market-regime filter: shorts are only allowed when the broad market is
        # neutral or falling, and blocked while it trends up. The regime barely
        # moves intraday, so it is computed from daily index bars and cached.
        self.market_regime_symbol = strategy_store.normalize_symbol(os.getenv("LOCAL_STRATEGY_REGIME_SYMBOL", "SPY")) or "SPY"
        self.market_regime_sma_fast = int(os.getenv("LOCAL_STRATEGY_REGIME_SMA_FAST", "50"))
        self.market_regime_sma_slow = int(os.getenv("LOCAL_STRATEGY_REGIME_SMA_SLOW", "200"))
        self.market_regime_strong_pct = float(os.getenv("LOCAL_STRATEGY_REGIME_STRONG_PCT", "3.0"))
        self.market_regime_ttl_seconds = int(os.getenv("LOCAL_STRATEGY_REGIME_TTL_SECONDS", "1800"))
        self.market_regime_cache: Dict[str, Dict] = {}

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.load_state()
        self.thread = threading.Thread(target=self.run_forever, name="local-strategy-engine", daemon=True)
        self.thread.start()
        self.logger.info(
            "[LOCAL_STRATEGY] Engine started poll=%ss dry_run=%s state=%s",
            self.poll_seconds,
            self.dry_run,
            self.state_path,
        )

    def stop(self) -> None:
        self.stop_event.set()

    def load_state(self) -> None:
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self.state.update(data)
        except Exception as exc:
            self.logger.error("[LOCAL_STRATEGY] Failed to load state: %s", exc)

    def save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp_path = f"{self.state_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, indent=2)
            os.replace(tmp_path, self.state_path)
        except Exception as exc:
            self.logger.error("[LOCAL_STRATEGY] Failed to save state: %s", exc)

    def emit_event(self, event_type: str, symbol: str, **fields) -> None:
        event = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "symbol": strategy_store.normalize_symbol(symbol),
        }
        event.update(fields)
        try:
            os.makedirs(os.path.dirname(self.event_log_path), exist_ok=True)
            with open(self.event_log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, sort_keys=True, default=str) + "\n")
        except Exception as exc:
            self.logger.warning("[LOCAL_STRATEGY] Failed to write structured event: %s", exc)

    def api_cooldown_active(self) -> bool:
        due = self._parse_state_time(self.state.get("api_cooldown_until_utc"))
        if not due:
            return False
        now = datetime.now(timezone.utc)
        if now < due:
            return True
        with self.state_lock:
            self.state.pop("api_cooldown_until_utc", None)
            self.state.pop("api_cooldown_reason", None)
        self.save_state()
        return False

    def defer_api_cooldown(self, reason: str) -> None:
        until = datetime.now(timezone.utc) + timedelta(seconds=max(1, self.api_failure_cooldown_seconds))
        with self.state_lock:
            self.state["api_cooldown_until_utc"] = until.isoformat()
            self.state["api_cooldown_reason"] = str(reason)[:500]
        self.save_state()

    def log_symbol_throttled(self, symbol: str, key: str, level: int, message: str, *args, interval: Optional[int] = None, strategy: Optional[str] = None) -> None:
        st = self.symbol_state(symbol, strategy)
        now = datetime.now(timezone.utc)
        state_key = f"last_log_{key}_utc"
        last = self._parse_state_time(st.get(state_key))
        if last and (now - last).total_seconds() < (interval or self.log_throttle_seconds):
            return
        st[state_key] = now.isoformat()
        self.logger.log(level, message, *args)
        self.save_state()

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            started = time.time()
            try:
                with self.app.app_context():
                    self.tick()
            except Exception as exc:
                self.logger.exception("[LOCAL_STRATEGY] Engine tick failed: %s", exc)
            elapsed = time.time() - started
            self.stop_event.wait(max(1, self.poll_seconds - elapsed))

    def tick(self) -> None:
        cfg = strategy_store.load_strategy_config()
        if not cfg.get("enabled"):
            return
        user = self.resolve_user(cfg)
        if not user:
            self.logger.warning("[LOCAL_STRATEGY] No Alpaca user resolved; skipping tick.")
            return
        api = self.api_for_user(user)
        if not api:
            return

        if self.api_cooldown_active():
            return

        try:
            self.process_recoveries(user, api)
        except TransientStrategyAPIError as exc:
            self.defer_api_cooldown(str(exc))
            self.logger.warning(
                "[LOCAL_STRATEGY] Alpaca/API transient failure during recovery; cooling down %ss: %s",
                self.api_failure_cooldown_seconds,
                exc,
            )
            return

        entries = []
        for entry in strategy_store.normalize_universe(cfg.get("universe")):
            if not entry.get("enabled", True):
                continue
            symbol = strategy_store.normalize_symbol(entry.get("symbol", ""))
            if not symbol or not strategy_store.local_allowed_for_symbol(symbol, cfg):
                continue
            entries.append(entry)

        entries.sort(
            key=lambda item: (
                strategy_store.normalize_symbol(item.get("symbol", "")),
                self.strategy_priority(item.get("strategy"), cfg),
            )
        )

        for entry in entries:
            symbol = strategy_store.normalize_symbol(entry.get("symbol", ""))
            backtest = entry.get("backtest")
            if not isinstance(backtest, dict):
                self.log_symbol_throttled(
                    symbol,
                    "no_backtest",
                    logging.INFO,
                    "[LOCAL_STRATEGY] %s skipped: no saved backtest config.",
                    symbol,
                    strategy=entry.get("strategy"),
                )
                continue
            try:
                self.evaluate_symbol(user, api, cfg, entry, backtest)
            except TransientStrategyAPIError as exc:
                self.defer_api_cooldown(str(exc))
                self.logger.warning(
                    "[LOCAL_STRATEGY] %s transient Alpaca/API failure; cooling down %ss: %s",
                    symbol,
                    self.api_failure_cooldown_seconds,
                    exc,
                )
                break
            except Exception as exc:
                self.logger.exception("[LOCAL_STRATEGY] %s evaluation failed: %s", symbol, exc)

    def resolve_user(self, cfg: Dict) -> Optional[User]:
        username = str(cfg.get("alpaca_user", "") or "").strip()
        user = User.query.filter_by(username=username, is_superuser=False).first() if username else None
        if user:
            return user
        return User.query.filter_by(is_superuser=False).order_by(User.username).first()

    def api_for_user(self, user: User) -> Optional[LegacyCompatibleAlpacaClient]:
        api_key = decrypt_data(user.encrypted_alpaca_key)
        api_secret = decrypt_data(user.encrypted_alpaca_secret)
        if not api_key or not api_secret:
            self.logger.error("[LOCAL_STRATEGY] User '%s' has no Alpaca credentials.", user.username)
            return None
        return LegacyCompatibleAlpacaClient(api_key, api_secret, self.base_url)

    def state_symbol_key(self, symbol: str, strategy: Optional[str] = None) -> str:
        normalized_symbol = strategy_store.normalize_symbol(symbol)
        normalized_strategy = str(strategy or "").strip().lower()
        if normalized_strategy:
            return f"{normalized_symbol}::{normalized_strategy}"
        return normalized_symbol

    def symbol_state(self, symbol: str, strategy: Optional[str] = None) -> Dict:
        state_key = self.state_symbol_key(symbol, strategy)
        with self.state_lock:
            return self.state.setdefault("symbols", {}).setdefault(state_key, {})

    def _parse_state_time(self, value) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None

    def entry_check_deferred(self, symbol: str, timeframe: str, strategy: Optional[str] = None) -> bool:
        st = self.symbol_state(symbol, strategy)
        due = self._parse_state_time(st.get("next_entry_check_utc"))
        now = datetime.now(timezone.utc)
        max_delay = max(self.poll_seconds, self.entry_refetch_seconds)
        if due and due > (now + timedelta(seconds=max_delay + self.poll_seconds)):
            st["next_entry_check_utc"] = now.isoformat()
            self.save_state()
            return False
        if due and now < due:
            return True
        return False

    def defer_next_entry_check(self, symbol: str, timeframe: str, strategy: Optional[str] = None) -> None:
        st = self.symbol_state(symbol, strategy)
        delay = max(self.poll_seconds, self.entry_refetch_seconds)
        st["next_entry_check_utc"] = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()

    def params_from_backtest(self, cfg: Dict, backtest: Dict) -> Dict:
        params = backtest.get("params") if isinstance(backtest.get("params"), dict) else {}
        return {
            "strategy": str(backtest.get("strategy") or cfg.get("strategy", "keltner")).strip().lower(),
            "trade_direction": params.get("trade_direction") or cfg.get("trade_direction", "Both"),
            "inner_kc_length": int(params.get("inner_kc_length", cfg.get("inner_kc_length", 33))),
            "inner_kc_mult": float(params.get("inner_kc_mult", cfg.get("inner_kc_mult", 1.7))),
            "outer_kc_length": int(params.get("outer_kc_length", cfg.get("outer_kc_length", 23))),
            "outer_kc_mult": float(params.get("outer_kc_mult", cfg.get("outer_kc_mult", 3.0))),
            "fixed_stop_loss_pct": float(params.get("fixed_stop_loss_pct", cfg.get("fixed_stop_loss_pct", 4.7))),
            "fixed_take_profit_pct": float(params.get("fixed_take_profit_pct", cfg.get("fixed_take_profit_pct", 3.1))),
            "forced_stop_loss_pct": float(params.get("forced_stop_loss_pct", cfg.get("forced_stop_loss_pct", 9.0))),
            "forced_take_profit_pct": float(params.get("forced_take_profit_pct", cfg.get("forced_take_profit_pct", 10.0))),
            "trailing_offset_ticks": int(params.get("trailing_offset_ticks", 4)),
            "trailing_offset_pct": float(params.get("trailing_offset_pct", 0.0)),
            "tick_size": float(params.get("tick_size", 0.01)),
            "macd_fast_length": int(params.get("macd_fast_length", cfg.get("macd_fast_length", 12))),
            "macd_slow_length": int(params.get("macd_slow_length", cfg.get("macd_slow_length", 26))),
            "macd_signal_length": int(params.get("macd_signal_length", cfg.get("macd_signal_length", 9))),
            "macd_sma_length": int(params.get("macd_sma_length", cfg.get("macd_sma_length", 200))),
            "max_intraday_loss_pct": float(params.get("max_intraday_loss_pct", cfg.get("max_intraday_loss_pct", 50))),
        }

    def strategy_priority(self, strategy: str, cfg: Optional[Dict] = None) -> int:
        normalized = str(strategy or "").strip().lower()
        if cfg and not bool(cfg.get("macd_priority_enabled", True)):
            return 0
        if normalized == "macd_sma":
            return 0
        if normalized == "keltner":
            return 1
        return 99

    def local_strategies_for_symbol(self, cfg: Dict, symbol: str) -> list[str]:
        strategies = []
        for entry in strategy_store.strategy_entries_for_symbol(symbol, cfg, enabled_only=True):
            mode = str(entry.get("mode", "both") or "both").strip().lower()
            if mode in {"local", "both"}:
                strategy = str(entry.get("strategy") or "keltner").strip().lower()
                if strategy not in strategies:
                    strategies.append(strategy)
        strategies.sort(key=lambda item: self.strategy_priority(item, cfg))
        return strategies

    def order_amount_for_strategy(self, cfg: Dict, strategy: str, user: User) -> float:
        normalized = str(strategy or "").strip().lower()
        fallback = float(cfg.get("order_size", user.per_trade_amount))
        if normalized == "macd_sma":
            return float(cfg.get("order_size_macd_sma", fallback))
        if normalized == "keltner":
            return float(cfg.get("order_size_keltner", fallback))
        return fallback

    def active_open_trade(self, user: User, symbol: str) -> Optional[Trade]:
        return (
            Trade.query
            .filter_by(user_id=user.id, symbol=symbol, status="open")
            .order_by(Trade.open_time.desc(), Trade.id.desc())
            .first()
        )

    def active_open_trade_strategy(self, user: User, symbol: str) -> Optional[str]:
        trade = self.active_open_trade(user, symbol)
        if not trade:
            return None
        strategy = str(trade.strategy or "").strip().lower()
        return strategy or None

    def backtest_entry_rejection_reason(self, backtest: Dict) -> Optional[str]:
        metrics = backtest.get("metrics") if isinstance(backtest.get("metrics"), dict) else {}
        total_trades = self._metric_float(metrics, "total_trades")
        win_rate = self._metric_float(metrics, "win_rate_pct")
        profit_factor = self._metric_float(metrics, "profit_factor")
        max_drawdown = self._metric_float(metrics, "max_drawdown_pct")
        net_profit = self._metric_float(metrics, "net_profit")
        if net_profit is not None and net_profit < self.min_backtest_net_profit:
            return f"backtest net profit {net_profit:.2f} < {self.min_backtest_net_profit:.2f}"
        if total_trades is not None and total_trades < self.min_backtest_trades:
            return f"backtest trades {total_trades:.0f} < {self.min_backtest_trades}"
        if win_rate is not None and win_rate < self.min_backtest_win_rate_pct:
            return f"backtest win rate {win_rate:.2f}% < {self.min_backtest_win_rate_pct:.2f}%"
        if profit_factor is not None and profit_factor < self.min_backtest_profit_factor:
            return f"backtest profit factor {profit_factor:.3f} < {self.min_backtest_profit_factor:.3f}"
        if max_drawdown is not None and max_drawdown > self.max_backtest_drawdown_pct:
            return f"backtest max drawdown {max_drawdown:.2f}% > {self.max_backtest_drawdown_pct:.2f}%"
        return None

    def validation_thresholds(self, cfg: Optional[Dict]) -> Dict[str, float]:
        cfg = cfg or {}
        return {
            "min_net_profit": float(cfg.get("validation_min_net_profit", 0)),
            "min_trades": float(cfg.get("validation_min_trades", 5)),
            "min_win_rate_pct": float(cfg.get("validation_min_win_rate_pct", 45)),
            "min_profit_factor": float(cfg.get("validation_min_profit_factor", 1.15)),
            "max_drawdown_pct": float(cfg.get("validation_max_drawdown_pct", 15)),
        }

    def validation_actuals(self, validation: Dict) -> Dict[str, Optional[float]]:
        status = validation.get("status") if isinstance(validation.get("status"), dict) else {}
        checks = status.get("checks") if isinstance(status.get("checks"), dict) else {}
        test_metrics = validation.get("test_metrics") if isinstance(validation.get("test_metrics"), dict) else {}

        def actual(check_key: str, metric_key: Optional[str] = None) -> Optional[float]:
            metric_name = metric_key or check_key
            if metric_name in test_metrics:
                return self._metric_float(test_metrics, metric_name)
            check = checks.get(check_key) if isinstance(checks.get(check_key), dict) else {}
            return self._metric_float(check, "actual")

        return {
            "min_net_profit": actual("min_net_profit", "net_profit"),
            "min_trades": actual("min_trades", "total_trades"),
            "min_win_rate_pct": actual("min_win_rate_pct", "win_rate_pct"),
            "min_profit_factor": actual("min_profit_factor", "profit_factor"),
            "max_drawdown_pct": actual("max_drawdown_pct", "max_drawdown_pct"),
        }

    def failed_validation_checks(self, actuals: Dict[str, Optional[float]], cfg: Optional[Dict]) -> list[str]:
        thresholds = self.validation_thresholds(cfg)
        failed = []
        if actuals.get("min_net_profit") is not None and actuals["min_net_profit"] < thresholds["min_net_profit"]:
            failed.append("min_net_profit")
        if actuals.get("min_trades") is not None and actuals["min_trades"] < thresholds["min_trades"]:
            failed.append("min_trades")
        if actuals.get("min_win_rate_pct") is not None and actuals["min_win_rate_pct"] < thresholds["min_win_rate_pct"]:
            failed.append("min_win_rate_pct")
        if actuals.get("min_profit_factor") is not None and actuals["min_profit_factor"] < thresholds["min_profit_factor"]:
            failed.append("min_profit_factor")
        if actuals.get("max_drawdown_pct") is not None and actuals["max_drawdown_pct"] > thresholds["max_drawdown_pct"]:
            failed.append("max_drawdown_pct")
        return failed

    def oos_entry_rejection_reason(self, backtest: Dict, cfg: Optional[Dict] = None) -> Optional[str]:
        if cfg is not None and not bool(cfg.get("validation_enabled", True)):
            return None
        validation = backtest.get("validation") if isinstance(backtest.get("validation"), dict) else None
        if not validation or not validation.get("enabled", True):
            return None
        status = validation.get("status") if isinstance(validation.get("status"), dict) else {}
        actuals = self.validation_actuals(validation)
        if any(value is not None for value in actuals.values()):
            failed = self.failed_validation_checks(actuals, cfg)
        elif not status.get("passed"):
            failed = list(status.get("failed_checks") or [])
        else:
            failed = []
        if failed:
            if failed:
                return f"out-of-sample validation failed: {', '.join(map(str, failed))}"
            return "out-of-sample validation failed"
        return None

    def local_day_bounds_utc(self) -> tuple[datetime, datetime]:
        local_tz = ZoneInfo("Europe/Bucharest")
        now_local = datetime.now(local_tz)
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

    def daily_symbol_guard_reason(self, user: User, symbol: str, cfg: Dict) -> Optional[str]:
        max_trades = int(cfg.get("daily_max_trades_per_symbol", self.daily_max_trades_per_symbol))
        max_losses = int(cfg.get("daily_max_losses_per_symbol", self.daily_max_losses_per_symbol))
        max_loss_usd = float(cfg.get("daily_max_loss_usd_per_symbol", self.daily_max_loss_usd_per_symbol))
        day_start, day_end = self.local_day_bounds_utc()
        opened_count = (
            Trade.query
            .filter(
                Trade.user_id == user.id,
                Trade.symbol == symbol,
                Trade.open_time >= day_start,
                Trade.open_time < day_end,
            )
            .count()
        )
        if opened_count >= max_trades:
            return f"daily trade limit {opened_count}/{max_trades}"
        closed = (
            Trade.query
            .filter(
                Trade.user_id == user.id,
                Trade.symbol == symbol,
                Trade.status == "closed",
                Trade.close_time >= day_start,
                Trade.close_time < day_end,
            )
            .all()
        )
        losses = [float(t.profit_loss or 0.0) for t in closed if float(t.profit_loss or 0.0) < 0]
        loss_count = len(losses)
        loss_usd = abs(sum(losses))
        if loss_count >= max_losses:
            return f"daily loss-count limit {loss_count}/{max_losses}"
        if max_loss_usd > 0 and loss_usd >= max_loss_usd:
            return f"daily loss limit {loss_usd:.2f}/{max_loss_usd:.2f} USD"
        return None

    def symbol_killswitch_reason(self, user: User, symbol: str, cfg: Dict) -> Optional[str]:
        """Disable a chronically losing symbol.

        Looks at the symbol's recent closed trades inside a rolling window. If
        there are enough of them and they are either deeply net-negative or have
        a poor win rate, new entries are blocked. The window is time-bounded by
        the cooldown, so once the bad streak ages out the symbol re-enables on
        its own with no manual reset.
        """
        if not bool(cfg.get("symbol_killswitch_enabled", True)):
            return None
        lookback_trades = max(1, int(cfg.get("symbol_killswitch_lookback_trades", 15)))
        min_trades = max(1, int(cfg.get("symbol_killswitch_min_trades", 6)))
        max_net_loss = float(cfg.get("symbol_killswitch_max_net_loss_usd", 300))
        min_win_rate = float(cfg.get("symbol_killswitch_min_win_rate_pct", 35))
        cooldown_days = max(1, int(cfg.get("symbol_killswitch_cooldown_days", 14)))
        window_start = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
        trades = (
            Trade.query
            .filter(
                Trade.user_id == user.id,
                Trade.symbol == symbol,
                Trade.status == "closed",
                Trade.close_time >= window_start,
            )
            .order_by(Trade.close_time.desc())
            .limit(lookback_trades)
            .all()
        )
        if len(trades) < min_trades:
            return None
        pls = [float(t.profit_loss or 0.0) for t in trades]
        net = sum(pls)
        win_rate = (sum(1 for p in pls if p > 0) / len(pls)) * 100.0
        if max_net_loss > 0 and net <= -max_net_loss:
            return f"killswitch net {net:.2f} over last {len(pls)} trades (<= -{max_net_loss:.0f}, {cooldown_days}d cooldown)"
        if win_rate < min_win_rate:
            return f"killswitch win rate {win_rate:.0f}% over last {len(pls)} trades (< {min_win_rate:.0f}%, {cooldown_days}d cooldown)"
        return None

    def _metric_float(self, metrics: Dict, key: str) -> Optional[float]:
        value = metrics.get(key)
        try:
            if value is None or (isinstance(value, float) and math.isnan(value)):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def compute_market_regime(self, api: LegacyCompatibleAlpacaClient, symbol: str, feed: str) -> Optional[str]:
        """Classify the broad market from daily index bars.

        Returns one of: 'strong_bullish', 'bullish', 'neutral', 'bearish', or
        None when data is unavailable. A regime is bullish when price is above
        the slow SMA and the fast SMA is above the slow SMA; 'strong_bullish'
        adds a momentum cushion above the slow SMA.
        """
        fast_len = max(2, self.market_regime_sma_fast)
        slow_len = max(fast_len + 1, self.market_regime_sma_slow)
        try:
            now = datetime.now(timezone.utc)
            start = now - timedelta(days=max(slow_len * 2, 300))
            bars = api.get_bars(
                symbol,
                "1Day",
                start=start.isoformat().replace("+00:00", "Z"),
                end=now.isoformat().replace("+00:00", "Z"),
                adjustment="raw",
                feed=feed,
            ).df
            if bars is None or bars.empty or len(bars) < slow_len + 5:
                return None
            close = bars["close"].astype(float)
            fast_v = float(sma(close, fast_len).iloc[-1])
            slow_v = float(sma(close, slow_len).iloc[-1])
            price = float(close.iloc[-1])
            if any(math.isnan(value) for value in (fast_v, slow_v, price)) or slow_v <= 0:
                return None
            pct_above_slow = (price - slow_v) / slow_v * 100.0
            if price > slow_v and fast_v > slow_v:
                return "strong_bullish" if pct_above_slow >= self.market_regime_strong_pct else "bullish"
            if price < slow_v and fast_v < slow_v:
                return "bearish"
            return "neutral"
        except Exception as exc:
            self.logger.warning("[LOCAL_STRATEGY] Market regime fetch failed for %s: %s", symbol, exc)
            return None

    def market_allows_short(self, api: LegacyCompatibleAlpacaClient, cfg: Dict) -> tuple[bool, str]:
        """Shorts allowed only when the market is neutral or falling.

        On an uptrend (bullish/strong_bullish) shorts are blocked. When the
        regime cannot be determined we block too, because shorts have been the
        losing side and skipping a short on uncertainty is the safe bias.
        """
        if not bool(cfg.get("market_regime_filter_enabled", True)):
            return True, "regime_filter_disabled"
        symbol = strategy_store.normalize_symbol(cfg.get("market_regime_symbol", self.market_regime_symbol)) or self.market_regime_symbol
        feed = str(cfg.get("feed", "iex"))
        ttl = int(cfg.get("market_regime_ttl_seconds", self.market_regime_ttl_seconds))
        now = datetime.now(timezone.utc)
        with self.state_lock:
            cached = self.market_regime_cache.get(symbol)
        if cached and (now - cached["computed_at"]).total_seconds() < ttl:
            regime = cached["regime"]
        else:
            regime = self.compute_market_regime(api, symbol, feed)
            if regime is None:
                return False, "regime_unknown_blocking_short"
            with self.state_lock:
                self.market_regime_cache[symbol] = {"regime": regime, "computed_at": now}
        if regime in ("bullish", "strong_bullish"):
            return False, f"market_uptrend:{regime}"
        return True, f"market_{regime}"

    def fetch_closed_bars(self, api: LegacyCompatibleAlpacaClient, symbol: str, timeframe: str, feed: str, session: str) -> pd.DataFrame:
        normalized_tf = normalize_timeframe_token(timeframe)
        fetch_tf = choose_fetch_timeframe([normalized_tf])
        now = datetime.now(timezone.utc)
        session_multiplier = 4 if str(session or "").lower() == "regular" else 2
        lookback_seconds = max(
            timeframe_seconds(normalized_tf) * self.bars_lookback * session_multiplier,
            10 * 24 * 3600,
        )
        start = now - timedelta(seconds=lookback_seconds)
        bars = api.get_bars(
            symbol,
            fetch_tf,
            start=start.isoformat().replace("+00:00", "Z"),
            end=now.isoformat().replace("+00:00", "Z"),
            adjustment="raw",
            feed=feed,
        ).df
        if bars.empty:
            return bars
        bars = bars.copy().sort_index()
        bars.index = pd.to_datetime(bars.index, utc=True)
        bars = bars[["open", "high", "low", "close", "volume"]]
        bars = resample_bars(bars, normalized_tf)
        bars = filter_session(bars, session)
        cutoff = pd.Timestamp(now - timedelta(seconds=timeframe_seconds(normalized_tf)))
        return bars[bars.index <= cutoff].dropna()

    def evaluate_symbol(self, user: User, api: LegacyCompatibleAlpacaClient, cfg: Dict, entry: Dict, backtest: Dict) -> None:
        symbol = entry["symbol"]
        params = self.params_from_backtest(cfg, backtest)
        row_strategy = str(entry.get("strategy") or "").strip().lower()
        if row_strategy:
            params["strategy"] = row_strategy
        strategy_name = params.get("strategy")
        if params.get("strategy") not in {"keltner", "macd_sma"}:
            self.log_symbol_throttled(
                symbol,
                "unsupported_strategy",
                logging.WARNING,
                "[LOCAL_STRATEGY] %s skipped: strategy '%s' is not implemented in local execution.",
                symbol,
                params.get("strategy"),
                strategy=strategy_name,
            )
            self.defer_next_entry_check(symbol, str(backtest.get("timeframe") or cfg.get("timeframe", "30Min")), strategy_name)
            self.save_state()
            return
        timeframe = normalize_timeframe_token(backtest.get("timeframe") or cfg.get("timeframe", "30Min"))
        feed = str(cfg.get("feed", "iex"))
        session = str(backtest.get("session") or cfg.get("session", "regular"))

        position = self.get_position(api, symbol)
        if position is None and self.entry_check_deferred(symbol, timeframe, strategy_name):
            return
        if position is not None:
            active_strategy = self.active_open_trade_strategy(user, symbol)
            if active_strategy and active_strategy != strategy_name:
                self.log_symbol_throttled(
                    symbol,
                    "position_owned_by_other_strategy",
                    logging.INFO,
                    "[LOCAL_STRATEGY] %s skipped for %s: open position is owned by strategy '%s'.",
                    symbol,
                    strategy_name,
                    active_strategy,
                    strategy=strategy_name,
                )
                return
            if not active_strategy:
                strategies = self.local_strategies_for_symbol(cfg, symbol)
                preferred = strategies[0] if strategies else None
                if preferred and len(strategies) > 1 and strategy_name != preferred:
                    self.log_symbol_throttled(
                        symbol,
                        "position_owner_unknown",
                        logging.INFO,
                        "[LOCAL_STRATEGY] %s skipped for %s: open position owner is unknown and strategy '%s' has priority.",
                        symbol,
                        strategy_name,
                        preferred,
                        strategy=strategy_name,
                    )
                    return
        if position is None:
            saved_strategy = str(backtest.get("strategy") or "keltner").strip().lower()
            if saved_strategy != params.get("strategy"):
                self.log_symbol_throttled(
                    symbol,
                    "strategy_backtest_mismatch",
                    logging.WARNING,
                    "[LOCAL_STRATEGY] %s entry disabled: row strategy '%s' does not match saved backtest strategy '%s'.",
                    symbol,
                    params.get("strategy"),
                    saved_strategy,
                    strategy=strategy_name,
                )
                self.defer_next_entry_check(symbol, timeframe, strategy_name)
                self.save_state()
                self.emit_event("entry_rejected", symbol, reason="strategy_backtest_mismatch", row_strategy=params.get("strategy"), backtest_strategy=saved_strategy)
                return
            daily_rejection = self.daily_symbol_guard_reason(user, symbol, cfg)
            if daily_rejection:
                self.log_symbol_throttled(
                    symbol,
                    "daily_guard_reject",
                    logging.WARNING,
                    "[LOCAL_STRATEGY] %s entry disabled: %s",
                    symbol,
                    daily_rejection,
                    strategy=strategy_name,
                )
                self.defer_next_entry_check(symbol, timeframe, strategy_name)
                self.save_state()
                self.emit_event("entry_rejected", symbol, reason="daily_guard", detail=daily_rejection)
                return
            killswitch_rejection = self.symbol_killswitch_reason(user, symbol, cfg)
            if killswitch_rejection:
                self.log_symbol_throttled(
                    symbol,
                    "killswitch_reject",
                    logging.WARNING,
                    "[LOCAL_STRATEGY] %s entry disabled: %s",
                    symbol,
                    killswitch_rejection,
                    strategy=strategy_name,
                )
                self.defer_next_entry_check(symbol, timeframe, strategy_name)
                self.save_state()
                self.emit_event("entry_rejected", symbol, reason="symbol_killswitch", detail=killswitch_rejection)
                return
            oos_rejection = self.oos_entry_rejection_reason(backtest, cfg)
            if oos_rejection:
                self.log_symbol_throttled(
                    symbol,
                    "oos_reject",
                    logging.WARNING,
                    "[LOCAL_STRATEGY] %s entry disabled: %s",
                    symbol,
                    oos_rejection,
                    strategy=strategy_name,
                )
                self.defer_next_entry_check(symbol, timeframe, strategy_name)
                self.save_state()
                self.emit_event("entry_rejected", symbol, reason="oos_validation", detail=oos_rejection)
                return
            rejection = self.backtest_entry_rejection_reason(backtest)
            if rejection:
                self.log_symbol_throttled(
                    symbol,
                    "quality_reject",
                    logging.INFO,
                    "[LOCAL_STRATEGY] %s entry disabled: %s",
                    symbol,
                    rejection,
                    strategy=strategy_name,
                )
                self.defer_next_entry_check(symbol, timeframe, strategy_name)
                self.save_state()
                self.emit_event("entry_rejected", symbol, reason="backtest_quality", detail=rejection)
                return

        bars = self.fetch_closed_bars(api, symbol, timeframe, feed, session)
        if params.get("strategy") == "macd_sma":
            min_bars = max(
                params["macd_sma_length"] + params["macd_slow_length"] + params["macd_signal_length"] + 5,
                100,
            )
        else:
            min_bars = max(params["inner_kc_length"], params["outer_kc_length"]) + 3
        if len(bars) < min_bars:
            self.log_symbol_throttled(
                symbol,
                "not_enough_bars",
                logging.INFO,
                "[LOCAL_STRATEGY] %s skipped: bars=%s min=%s timeframe=%s",
                symbol,
                len(bars),
                min_bars,
                timeframe,
                strategy=strategy_name,
            )
            if position is None:
                self.defer_next_entry_check(symbol, timeframe, strategy_name)
                self.save_state()
            return

        if params.get("strategy") == "macd_sma":
            frame = self.build_macd_sma_frame(bars, params)
        else:
            mid, upper, lower = keltner_channel(bars, params["inner_kc_length"], params["inner_kc_mult"])
            frame = bars.assign(mid_inner=mid, up_inner=upper, low_inner=lower).dropna()
        if len(frame) < 2:
            if position is None:
                self.defer_next_entry_check(symbol, timeframe, strategy_name)
                self.save_state()
            return
        last_bar_ts = frame.index[-1].isoformat()
        latest_price = self.get_latest_price(api, symbol)
        if latest_price <= 0:
            self.logger.warning("[LOCAL_STRATEGY] %s skipped: no latest price.", symbol)
            if position is None:
                self.defer_next_entry_check(symbol, timeframe, strategy_name)
                self.save_state()
            return

        if position is not None:
            if params.get("strategy") == "macd_sma":
                self.evaluate_exit_macd_sma(user, symbol, position, latest_price, params, frame, last_bar_ts, timeframe)
            else:
                self.evaluate_exit(user, symbol, position, latest_price, params, frame, last_bar_ts, timeframe)
            return

        self.defer_next_entry_check(symbol, timeframe, strategy_name)
        self.reset_position_state(symbol, strategy_name)
        if params.get("strategy") == "macd_sma":
            self.evaluate_entry_macd_sma(user, api, cfg, symbol, latest_price, params, frame, last_bar_ts, timeframe, backtest)
        else:
            self.evaluate_entry(user, api, cfg, symbol, latest_price, params, frame, last_bar_ts, timeframe, backtest)

    def build_macd_sma_frame(self, bars: pd.DataFrame, params: Dict) -> pd.DataFrame:
        frame = bars.copy()
        frame["fast_ma"] = sma(frame["close"], params["macd_fast_length"])
        frame["slow_ma"] = sma(frame["close"], params["macd_slow_length"])
        frame["veryslow_ma"] = sma(frame["close"], params["macd_sma_length"])
        frame["macd_line"] = frame["fast_ma"] - frame["slow_ma"]
        frame["signal_line"] = sma(frame["macd_line"], params["macd_signal_length"])
        frame["hist"] = frame["macd_line"] - frame["signal_line"]
        frame["long_signal"] = (
            crossed_above(frame["hist"], 0.0)
            & (frame["macd_line"] > 0)
            & (frame["fast_ma"] > frame["slow_ma"])
            & (frame["close"].shift(params["macd_slow_length"]) > frame["veryslow_ma"])
        )
        frame["short_signal"] = (
            crossed_below(frame["hist"], 0.0)
            & (frame["macd_line"] < 0)
            & (frame["fast_ma"] < frame["slow_ma"])
            & (frame["close"].shift(params["macd_slow_length"]) < frame["veryslow_ma"])
        )
        return frame.dropna()

    def get_latest_price(self, api: LegacyCompatibleAlpacaClient, symbol: str) -> float:
        try:
            trade = api.get_latest_trade(symbol)
            return float(trade.price)
        except Exception as exc:
            self.logger.error("[LOCAL_STRATEGY] %s latest price failed: %s", symbol, exc)
            return 0.0

    def get_position(self, api: LegacyCompatibleAlpacaClient, symbol: str):
        try:
            return api.get_position(symbol)
        except AlpacaAPIError as exc:
            err = str(exc).lower()
            if any(marker in err for marker in ("position not found", "position does not exist", "no position")):
                return None
            if self.is_transient_api_exception(exc):
                raise TransientStrategyAPIError(f"Alpaca position check failed for {symbol}: {exc}") from exc
            raise RuntimeError(f"Alpaca position check failed for {symbol}: {exc}") from exc
        except Exception as exc:
            if self.is_transient_api_exception(exc):
                raise TransientStrategyAPIError(f"Position check failed for {symbol}: {exc}") from exc
            raise RuntimeError(f"Position check failed for {symbol}: {exc}") from exc

    def is_transient_api_exception(self, exc: Exception) -> bool:
        text = str(exc).lower()
        markers = (
            "temporary failure in name resolution",
            "failed to establish a new connection",
            "max retries exceeded",
            "connection aborted",
            "connection reset",
            "connection refused",
            "read timed out",
            "timeout",
            "temporarily unavailable",
        )
        return any(marker in text for marker in markers)

    def reset_position_state(self, symbol: str, strategy: Optional[str] = None) -> None:
        st = self.symbol_state(symbol, strategy)
        if st.get("trail_active") or st.get("trail_stop") is not None:
            st["trail_active"] = False
            st["trail_stop"] = None
            self.save_state()

    def evaluate_entry(self, user: User, api: LegacyCompatibleAlpacaClient, cfg: Dict, symbol: str, latest_price: float, params: Dict, frame: pd.DataFrame, last_bar_ts: str, timeframe: str, backtest: Dict) -> None:
        strategy_name = str(params.get("strategy") or "keltner").strip().lower()
        st = self.symbol_state(symbol, strategy_name)
        if st.get("last_entry_bar") == last_bar_ts:
            return
        prev = frame.iloc[-2]
        cur = frame.iloc[-1]
        trade_direction = params["trade_direction"]
        action = None
        reason = None
        if trade_direction in ("Both", "Long Only"):
            if prev["close"] <= prev["low_inner"] and cur["close"] > cur["low_inner"] and cur["close"] < cur["mid_inner"]:
                action = "buy"
                reason = "Local KC Long Entry"
        if action is None and trade_direction in ("Both", "Short Only"):
            if prev["close"] >= prev["up_inner"] and cur["close"] < cur["up_inner"] and cur["close"] > cur["mid_inner"]:
                action = "sell"
                reason = "Local KC Short Entry"
        if not action:
            st["last_checked_bar"] = last_bar_ts
            st["last_decision"] = "no_entry"
            self.save_state()
            return

        if action == "sell":
            allowed, regime_reason = self.market_allows_short(api, cfg)
            if not allowed:
                self.log_symbol_throttled(
                    symbol,
                    "short_blocked_regime",
                    logging.INFO,
                    "[LOCAL_STRATEGY] %s short entry blocked by market regime: %s",
                    symbol,
                    regime_reason,
                    strategy=strategy_name,
                )
                st["last_checked_bar"] = last_bar_ts
                st["last_decision"] = f"short_blocked_{regime_reason}"
                self.save_state()
                self.emit_event("entry_rejected", symbol, strategy=strategy_name, reason="market_regime", detail=regime_reason)
                return

        payload = self.build_payload(
            symbol=symbol,
            action=action,
            amount=self.order_amount_for_strategy(cfg, strategy_name, user),
            reason=reason,
            timeframe=timeframe,
            bar_ts=last_bar_ts,
            backtest=backtest,
        )
        self.logger.info("[LOCAL_STRATEGY] %s entry signal action=%s bar=%s price=%s", symbol, action, last_bar_ts, latest_price)
        self.submit_llm_shadow_validation(user, api, payload, latest_price, params, frame, backtest)
        ok = self.execute_or_recover(user, payload, kind="open")
        self.emit_event("entry_submitted", symbol, strategy=payload.get("strategy"), action=action, ok=ok, bar_time=last_bar_ts, reason=reason)
        st["last_entry_bar"] = last_bar_ts
        st["last_decision"] = f"entry_{action}_{'ok' if ok else 'recovery'}"
        self.save_state()

    def evaluate_entry_macd_sma(self, user: User, api: LegacyCompatibleAlpacaClient, cfg: Dict, symbol: str, latest_price: float, params: Dict, frame: pd.DataFrame, last_bar_ts: str, timeframe: str, backtest: Dict) -> None:
        strategy_name = "macd_sma"
        st = self.symbol_state(symbol, strategy_name)
        if st.get("last_entry_bar") == last_bar_ts:
            return
        cur = frame.iloc[-1]
        trade_direction = params["trade_direction"]
        action = None
        reason = None
        if trade_direction in ("Both", "Long Only") and bool(cur.get("long_signal")):
            action = "buy"
            reason = "Local MACD SMA Long Entry"
        elif trade_direction in ("Both", "Short Only") and bool(cur.get("short_signal")):
            action = "sell"
            reason = "Local MACD SMA Short Entry"
        if not action:
            st["last_checked_bar"] = last_bar_ts
            st["last_decision"] = "no_entry"
            self.save_state()
            return

        if action == "sell":
            allowed, regime_reason = self.market_allows_short(api, cfg)
            if not allowed:
                self.log_symbol_throttled(
                    symbol,
                    "short_blocked_regime",
                    logging.INFO,
                    "[LOCAL_STRATEGY] %s MACD/SMA short entry blocked by market regime: %s",
                    symbol,
                    regime_reason,
                    strategy=strategy_name,
                )
                st["last_checked_bar"] = last_bar_ts
                st["last_decision"] = f"short_blocked_{regime_reason}"
                self.save_state()
                self.emit_event("entry_rejected", symbol, strategy=strategy_name, reason="market_regime", detail=regime_reason)
                return

        order_backtest = dict(backtest or {})
        order_backtest["strategy"] = "macd_sma"
        payload = self.build_payload(
            symbol=symbol,
            action=action,
            amount=self.order_amount_for_strategy(cfg, strategy_name, user),
            reason=reason,
            timeframe=timeframe,
            bar_ts=last_bar_ts,
            backtest=order_backtest,
        )
        self.logger.info("[LOCAL_STRATEGY] %s MACD/SMA entry signal action=%s bar=%s price=%s", symbol, action, last_bar_ts, latest_price)
        self.submit_llm_shadow_validation(user, api, payload, latest_price, params, frame, order_backtest)
        ok = self.execute_or_recover(user, payload, kind="open")
        self.emit_event("entry_submitted", symbol, strategy=payload.get("strategy"), action=action, ok=ok, bar_time=last_bar_ts, reason=reason)
        st["last_entry_bar"] = last_bar_ts
        st["last_decision"] = f"entry_{action}_{'ok' if ok else 'recovery'}"
        self.save_state()

    def submit_llm_shadow_validation(
        self,
        user: User,
        api: LegacyCompatibleAlpacaClient,
        payload: Dict,
        latest_price: float,
        params: Dict,
        frame: pd.DataFrame,
        backtest: Dict,
    ) -> None:
        if not self.llm_validator:
            return
        try:
            technical_context = self.build_llm_shadow_context(payload, latest_price, params, frame, backtest)
            self.llm_validator.submit_entry_signal(
                user_snapshot={"id": user.id, "username": user.username},
                payload=payload,
                technical_context=technical_context,
                alpaca_api_key=getattr(api, "api_key", None),
                alpaca_api_secret=getattr(api, "api_secret", None),
            )
        except Exception as exc:
            self.logger.warning("[LLM_SHADOW] submit failed for %s: %s", payload.get("symbol"), exc)

    def build_llm_shadow_context(self, payload: Dict, latest_price: float, params: Dict, frame: pd.DataFrame, backtest: Dict) -> Dict:
        tail_rows = []
        for ts, row in frame.tail(5).iterrows():
            tail_rows.append({
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "open": self._round_for_context(row.get("open")),
                "high": self._round_for_context(row.get("high")),
                "low": self._round_for_context(row.get("low")),
                "close": self._round_for_context(row.get("close")),
                "mid_inner": self._round_for_context(row.get("mid_inner")),
                "up_inner": self._round_for_context(row.get("up_inner")),
                "low_inner": self._round_for_context(row.get("low_inner")),
                "fast_ma": self._round_for_context(row.get("fast_ma")),
                "slow_ma": self._round_for_context(row.get("slow_ma")),
                "veryslow_ma": self._round_for_context(row.get("veryslow_ma")),
                "macd_line": self._round_for_context(row.get("macd_line")),
                "signal_line": self._round_for_context(row.get("signal_line")),
                "hist": self._round_for_context(row.get("hist")),
            })
        backtest_summary = {
            "job_id": backtest.get("job_id"),
            "timeframe": backtest.get("timeframe"),
            "session": backtest.get("session"),
        }
        for key in ("total_return_pct", "win_rate", "max_drawdown_pct", "profit_factor", "trades"):
            if key in backtest:
                backtest_summary[key] = backtest.get(key)
        return {
            "symbol": payload.get("symbol"),
            "action": payload.get("action"),
            "latest_price": self._round_for_context(latest_price),
            "entry_reason": payload.get("local_reason"),
            "timeframe": payload.get("timeframe"),
            "bar_time": payload.get("bar_time"),
            "params": {
                "trade_direction": params.get("trade_direction"),
                "inner_kc_length": params.get("inner_kc_length"),
                "inner_kc_mult": params.get("inner_kc_mult"),
                "fixed_stop_loss_pct": params.get("fixed_stop_loss_pct"),
                "fixed_take_profit_pct": params.get("fixed_take_profit_pct"),
                "forced_stop_loss_pct": params.get("forced_stop_loss_pct"),
                "forced_take_profit_pct": params.get("forced_take_profit_pct"),
                "macd_fast_length": params.get("macd_fast_length"),
                "macd_slow_length": params.get("macd_slow_length"),
                "macd_signal_length": params.get("macd_signal_length"),
                "macd_sma_length": params.get("macd_sma_length"),
            },
            "recent_bars": tail_rows,
            "backtest": backtest_summary,
        }

    def _round_for_context(self, value):
        try:
            if value is None or pd.isna(value):
                return None
            return round(float(value), 6)
        except Exception:
            return value

    def evaluate_exit(self, user: User, symbol: str, position, latest_price: float, params: Dict, frame: pd.DataFrame, last_bar_ts: str, timeframe: str) -> None:
        strategy_name = str(params.get("strategy") or "keltner").strip().lower()
        st = self.symbol_state(symbol, strategy_name)
        if st.get("last_exit_bar") == last_bar_ts and st.get("last_exit_price") == latest_price:
            return
        try:
            entry_price = float(position.avg_entry_price)
        except Exception:
            self.logger.warning("[LOCAL_STRATEGY] %s cannot read avg_entry_price; skip exit.", symbol)
            return
        side = str(getattr(position, "side", "") or "").lower()
        is_short = side == "short"
        reason = self.exit_reason(symbol, is_short, entry_price, latest_price, params, frame, st)
        self.save_state()
        if not reason:
            return
        payload = self.build_payload(
            symbol=symbol,
            action="close",
            amount=user.per_trade_amount,
            reason=reason,
            timeframe=timeframe,
            bar_ts=last_bar_ts,
            backtest=None,
        )
        payload["position_side"] = "short" if is_short else "long"
        self.logger.warning("[LOCAL_STRATEGY] %s exit signal reason='%s' bar=%s price=%s", symbol, reason, last_bar_ts, latest_price)
        ok = self.execute_or_recover(user, payload, kind="close")
        self.emit_event("exit_submitted", symbol, strategy=payload.get("strategy"), ok=ok, bar_time=last_bar_ts, reason=reason)
        st["last_exit_bar"] = last_bar_ts
        st["last_exit_price"] = latest_price
        st["last_decision"] = f"exit_{'ok' if ok else 'recovery'}"
        self.save_state()

    def evaluate_exit_macd_sma(self, user: User, symbol: str, position, latest_price: float, params: Dict, frame: pd.DataFrame, last_bar_ts: str, timeframe: str) -> None:
        st = self.symbol_state(symbol, "macd_sma")
        if st.get("last_exit_bar") == last_bar_ts and st.get("last_exit_price") == latest_price:
            return
        try:
            entry_price = float(position.avg_entry_price)
        except Exception:
            self.logger.warning("[LOCAL_STRATEGY] %s cannot read avg_entry_price; skip MACD/SMA exit.", symbol)
            return
        side = str(getattr(position, "side", "") or "").lower()
        is_short = side == "short"
        reason = self.exit_reason_macd_sma(is_short, entry_price, latest_price, params, frame)
        if not reason:
            return
        payload = self.build_payload(
            symbol=symbol,
            action="close",
            amount=user.per_trade_amount,
            reason=reason,
            timeframe=timeframe,
            bar_ts=last_bar_ts,
            backtest={"strategy": "macd_sma"},
        )
        payload["position_side"] = "short" if is_short else "long"
        self.logger.warning("[LOCAL_STRATEGY] %s MACD/SMA exit signal reason='%s' bar=%s price=%s", symbol, reason, last_bar_ts, latest_price)
        ok = self.execute_or_recover(user, payload, kind="close")
        self.emit_event("exit_submitted", symbol, strategy=payload.get("strategy"), ok=ok, bar_time=last_bar_ts, reason=reason)
        st["last_exit_bar"] = last_bar_ts
        st["last_exit_price"] = latest_price
        st["last_decision"] = f"exit_{'ok' if ok else 'recovery'}"
        self.save_state()

    def exit_reason_macd_sma(self, is_short: bool, entry_price: float, latest_price: float, params: Dict, frame: pd.DataFrame) -> Optional[str]:
        fixed_sl = params["fixed_stop_loss_pct"] / 100.0
        fixed_tp = params["fixed_take_profit_pct"] / 100.0
        forced_sl = params["forced_stop_loss_pct"] / 100.0
        forced_tp = params["forced_take_profit_pct"] / 100.0
        cur = frame.iloc[-1]
        if not is_short:
            if latest_price <= entry_price * (1 - forced_sl):
                return "macd forced sl long"
            if latest_price >= entry_price * (1 + forced_tp):
                return "macd forced tp long"
            if latest_price <= entry_price * (1 - fixed_sl):
                return "macd fixed stop loss long"
            if latest_price >= entry_price * (1 + fixed_tp):
                return "macd fixed take profit long"
            if bool(cur.get("short_signal")):
                return "macd opposite short signal"
        else:
            if latest_price >= entry_price * (1 + forced_sl):
                return "macd forced sl short"
            if latest_price <= entry_price * (1 - forced_tp):
                return "macd forced tp short"
            if latest_price >= entry_price * (1 + fixed_sl):
                return "macd fixed stop loss short"
            if latest_price <= entry_price * (1 - fixed_tp):
                return "macd fixed take profit short"
            if bool(cur.get("long_signal")):
                return "macd opposite long signal"
        return None

    def exit_reason(self, symbol: str, is_short: bool, entry_price: float, latest_price: float, params: Dict, frame: pd.DataFrame, st: Dict) -> Optional[str]:
        fixed_sl = params["fixed_stop_loss_pct"] / 100.0
        fixed_tp = params["fixed_take_profit_pct"] / 100.0
        forced_sl = params["forced_stop_loss_pct"] / 100.0
        forced_tp = params["forced_take_profit_pct"] / 100.0
        if not is_short:
            if latest_price <= entry_price * (1 - forced_sl):
                return "forced sl long"
            if latest_price >= entry_price * (1 + forced_tp):
                return "forced tp long"
            if latest_price <= entry_price * (1 - fixed_sl):
                return "fixed stop loss (long)"
            if latest_price >= entry_price * (1 + fixed_tp):
                return "fixed take profit (long)"
        else:
            if latest_price >= entry_price * (1 + forced_sl):
                return "forced sl short"
            if latest_price <= entry_price * (1 - forced_tp):
                return "forced tp short"
            if latest_price >= entry_price * (1 + fixed_sl):
                return "fixed stop loss (short)"
            if latest_price <= entry_price * (1 - fixed_tp):
                return "fixed take profit (short)"

        activation = float(frame.iloc[-1]["mid_inner"])
        if params.get("trailing_offset_pct", 0.0) > 0:
            offset = latest_price * params["trailing_offset_pct"] / 100.0
        else:
            offset = params["trailing_offset_ticks"] * params["tick_size"]
        trail_active = bool(st.get("trail_active", False))
        trail_stop = st.get("trail_stop")
        if not is_short:
            if not trail_active and latest_price >= activation:
                trail_active = True
                trail_stop = latest_price - offset
            if trail_active:
                trail_stop = max(float(trail_stop), latest_price - offset) if trail_stop is not None else latest_price - offset
                if latest_price <= trail_stop:
                    return "trailing exit long"
        else:
            if not trail_active and latest_price <= activation:
                trail_active = True
                trail_stop = latest_price + offset
            if trail_active:
                trail_stop = min(float(trail_stop), latest_price + offset) if trail_stop is not None else latest_price + offset
                if latest_price >= trail_stop:
                    return "trailing exit short"
        st["trail_active"] = trail_active
        st["trail_stop"] = trail_stop
        return None

    def build_payload(self, symbol: str, action: str, amount: float, reason: str, timeframe: str, bar_ts: str, backtest: Optional[Dict]) -> Dict:
        strategy = str((backtest or {}).get("strategy", "keltner") or "keltner").strip().lower()
        digest = hashlib.sha1(f"{symbol}:{strategy}:{action}:{bar_ts}".encode("utf-8")).hexdigest()[:16]
        client_order_id = f"ls_{symbol}_{action}_{digest}"
        return {
            "symbol": symbol,
            "action": action,
            "amount": amount,
            "local_strategy_request": True,
            "local_reason": reason,
            "timeframe": timeframe,
            "bar_time": bar_ts,
            "client_order_id": client_order_id[:48],
            "strategy": strategy,
            "strategy_job_id": (backtest or {}).get("job_id"),
            "order_type": "market",
            "time_in_force": "day",
        }

    def execute_or_recover(self, user: User, payload: Dict, kind: str) -> bool:
        if self.dry_run:
            self.logger.info("[LOCAL_STRATEGY_DRY_RUN] user=%s payload=%s", user.username, payload)
            return True
        ok, status, result, _record = self.execute_trade(user, payload)
        if ok and status < 400:
            self.logger.info("[LOCAL_STRATEGY_ORDER_OK] user=%s kind=%s payload=%s result=%s", user.username, kind, payload, result)
            return True
        self.logger.error("[LOCAL_STRATEGY_ORDER_FAIL] user=%s kind=%s status=%s payload=%s result=%s", user.username, kind, status, payload, result)
        self.add_recovery(user.id, payload, kind, status, result)
        return False

    def add_recovery(self, user_id: int, payload: Dict, kind: str, status: int, result: Dict) -> None:
        now = datetime.now(timezone.utc)
        item = {
            "id": f"{kind}:{payload.get('symbol')}:{payload.get('action')}:{payload.get('bar_time')}:{int(now.timestamp())}",
            "user_id": user_id,
            "kind": kind,
            "payload": payload,
            "attempts": 0,
            "last_status": status,
            "last_result": result,
            "next_due_utc": (now + timedelta(seconds=self.recovery_base_seconds)).isoformat(),
            "created_at_utc": now.isoformat(),
        }
        with self.state_lock:
            recoveries = self.state.setdefault("recoveries", [])
            if not any(r.get("payload", {}).get("client_order_id") == payload.get("client_order_id") for r in recoveries):
                recoveries.append(item)
        self.save_state()

    def process_recoveries(self, user: User, api: LegacyCompatibleAlpacaClient) -> None:
        now = datetime.now(timezone.utc)
        with self.state_lock:
            recoveries = list(self.state.get("recoveries", []))
        kept = []
        changed = False
        for item in recoveries:
            due_raw = item.get("next_due_utc")
            try:
                due = datetime.fromisoformat(str(due_raw))
            except Exception:
                due = now
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if due > now:
                kept.append(item)
                continue
            if self.recovery_obsolete(api, item):
                self.logger.info("[LOCAL_STRATEGY_RECOVERY_DONE] obsolete item=%s", item.get("id"))
                changed = True
                continue
            max_attempts = self.close_recovery_max_attempts if item.get("kind") == "close" else self.open_recovery_max_attempts
            attempts = int(item.get("attempts", 0)) + 1
            payload = item.get("payload") or {}
            ok, status, result, _record = self.execute_trade(user, payload)
            if ok and status < 400:
                self.logger.info("[LOCAL_STRATEGY_RECOVERY_OK] item=%s attempts=%s result=%s", item.get("id"), attempts, result)
                changed = True
                continue
            if max_attempts > 0 and attempts >= max_attempts:
                self.logger.error("[LOCAL_STRATEGY_RECOVERY_EXHAUSTED] item=%s attempts=%s status=%s result=%s", item.get("id"), attempts, status, result)
                changed = True
                continue
            wait = min(self.recovery_max_seconds, self.recovery_base_seconds * (2 ** min(attempts, 6)))
            item["attempts"] = attempts
            item["last_status"] = status
            item["last_result"] = result
            item["next_due_utc"] = (now + timedelta(seconds=wait)).isoformat()
            kept.append(item)
            changed = True
        if changed:
            with self.state_lock:
                self.state["recoveries"] = kept
            self.save_state()

    def recovery_obsolete(self, api: LegacyCompatibleAlpacaClient, item: Dict) -> bool:
        payload = item.get("payload") or {}
        symbol = strategy_store.normalize_symbol(payload.get("symbol", ""))
        position = self.get_position(api, symbol)
        if item.get("kind") == "close":
            return position is None
        if item.get("kind") == "open":
            return position is not None
        return False
