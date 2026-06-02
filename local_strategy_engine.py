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
from typing import Callable, Dict, Optional

import pandas as pd

from alpaca_api import AlpacaAPIError, LegacyCompatibleAlpacaClient
from misc.pine_optimizer import (
    choose_fetch_timeframe,
    filter_session,
    keltner_channel,
    normalize_timeframe_token,
    resample_bars,
    timeframe_seconds,
)
from models import User, db
import strategy_config as strategy_store
from utils import decrypt_data


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
        self.recovery_base_seconds = int(os.getenv("LOCAL_STRATEGY_RECOVERY_BASE_SECONDS", "15"))
        self.recovery_max_seconds = int(os.getenv("LOCAL_STRATEGY_RECOVERY_MAX_SECONDS", "300"))
        self.open_recovery_max_attempts = int(os.getenv("LOCAL_STRATEGY_OPEN_RECOVERY_MAX_ATTEMPTS", "3"))
        self.close_recovery_max_attempts = int(os.getenv("LOCAL_STRATEGY_CLOSE_RECOVERY_MAX_ATTEMPTS", "0"))
        self.dry_run = os.getenv("LOCAL_STRATEGY_DRY_RUN", "false").lower() in ("1", "true", "yes", "y")
        self.state_path = os.path.join(self.app.instance_path, "local_strategy_state.json")
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.state_lock = threading.Lock()
        self.state = {"symbols": {}, "recoveries": []}

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

        self.process_recoveries(user, api)

        for entry in strategy_store.normalize_universe(cfg.get("universe")):
            if not entry.get("enabled", True):
                continue
            symbol = strategy_store.normalize_symbol(entry.get("symbol", ""))
            if not symbol or not strategy_store.local_allowed_for_symbol(symbol, cfg):
                continue
            backtest = entry.get("backtest")
            if not isinstance(backtest, dict):
                self.logger.info("[LOCAL_STRATEGY] %s skipped: no saved backtest config.", symbol)
                continue
            try:
                self.evaluate_symbol(user, api, cfg, entry, backtest)
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

    def symbol_state(self, symbol: str) -> Dict:
        with self.state_lock:
            return self.state.setdefault("symbols", {}).setdefault(symbol, {})

    def params_from_backtest(self, cfg: Dict, backtest: Dict) -> Dict:
        params = backtest.get("params") if isinstance(backtest.get("params"), dict) else {}
        return {
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
            "tick_size": float(params.get("tick_size", 0.01)),
        }

    def fetch_closed_bars(self, api: LegacyCompatibleAlpacaClient, symbol: str, timeframe: str, feed: str, session: str) -> pd.DataFrame:
        normalized_tf = normalize_timeframe_token(timeframe)
        fetch_tf = choose_fetch_timeframe([normalized_tf])
        now = datetime.now(timezone.utc)
        lookback_seconds = max(timeframe_seconds(normalized_tf) * self.bars_lookback, 5 * 24 * 3600)
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
        timeframe = normalize_timeframe_token(backtest.get("timeframe") or cfg.get("timeframe", "30Min"))
        feed = str(cfg.get("feed", "iex"))
        session = str(backtest.get("session") or cfg.get("session", "regular"))
        bars = self.fetch_closed_bars(api, symbol, timeframe, feed, session)
        min_bars = max(params["inner_kc_length"], params["outer_kc_length"]) + 3
        if len(bars) < min_bars:
            self.logger.info("[LOCAL_STRATEGY] %s skipped: bars=%s min=%s timeframe=%s", symbol, len(bars), min_bars, timeframe)
            return

        mid, upper, lower = keltner_channel(bars, params["inner_kc_length"], params["inner_kc_mult"])
        frame = bars.assign(mid_inner=mid, up_inner=upper, low_inner=lower).dropna()
        if len(frame) < 2:
            return
        last_bar_ts = frame.index[-1].isoformat()
        latest_price = self.get_latest_price(api, symbol)
        if latest_price <= 0:
            self.logger.warning("[LOCAL_STRATEGY] %s skipped: no latest price.", symbol)
            return

        position = self.get_position(api, symbol)
        if position is not None:
            self.evaluate_exit(user, symbol, position, latest_price, params, frame, last_bar_ts, timeframe)
            return

        self.reset_position_state(symbol)
        self.evaluate_entry(user, cfg, symbol, latest_price, params, frame, last_bar_ts, timeframe, backtest)

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
            raise RuntimeError(f"Alpaca position check failed for {symbol}: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"Position check failed for {symbol}: {exc}") from exc

    def reset_position_state(self, symbol: str) -> None:
        st = self.symbol_state(symbol)
        if st.get("trail_active") or st.get("trail_stop") is not None:
            st["trail_active"] = False
            st["trail_stop"] = None
            self.save_state()

    def evaluate_entry(self, user: User, cfg: Dict, symbol: str, latest_price: float, params: Dict, frame: pd.DataFrame, last_bar_ts: str, timeframe: str, backtest: Dict) -> None:
        st = self.symbol_state(symbol)
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

        payload = self.build_payload(
            symbol=symbol,
            action=action,
            amount=float(cfg.get("order_size", user.per_trade_amount)),
            reason=reason,
            timeframe=timeframe,
            bar_ts=last_bar_ts,
            backtest=backtest,
        )
        self.logger.info("[LOCAL_STRATEGY] %s entry signal action=%s bar=%s price=%s", symbol, action, last_bar_ts, latest_price)
        ok = self.execute_or_recover(user, payload, kind="open")
        st["last_entry_bar"] = last_bar_ts
        st["last_decision"] = f"entry_{action}_{'ok' if ok else 'recovery'}"
        self.save_state()

    def evaluate_exit(self, user: User, symbol: str, position, latest_price: float, params: Dict, frame: pd.DataFrame, last_bar_ts: str, timeframe: str) -> None:
        st = self.symbol_state(symbol)
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
        st["last_exit_bar"] = last_bar_ts
        st["last_exit_price"] = latest_price
        st["last_decision"] = f"exit_{'ok' if ok else 'recovery'}"
        self.save_state()

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
        digest = hashlib.sha1(f"{symbol}:{action}:{bar_ts}".encode("utf-8")).hexdigest()[:16]
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
