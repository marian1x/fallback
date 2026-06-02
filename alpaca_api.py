#!/usr/bin/env python3
"""
Alpaca integration helpers based on alpaca-py.

This module provides:
- a compatibility client used by the current codebase (REST + market data),
- a lightweight trade-updates stream hub used to wait for order events.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Dict, List, Optional, Set

import pandas as pd
from alpaca.common.exceptions import APIError as AlpacaAPIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoLatestTradeRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import GetAssetsRequest, LimitOrderRequest, MarketOrderRequest
from alpaca.trading.stream import TradingStream


TERMINAL_ORDER_EVENTS: Set[str] = {
    "fill",
    "partial_fill",
    "canceled",
    "rejected",
    "expired",
    "done_for_day",
    "order_cancel_rejected",
    "order_replace_rejected",
}


def is_paper_base_url(base_url: str) -> bool:
    return "paper-api.alpaca.markets" in (base_url or "")


def _parse_order_side(side: str) -> OrderSide:
    side_s = (side or "").strip().lower()
    if side_s == "buy":
        return OrderSide.BUY
    if side_s == "sell":
        return OrderSide.SELL
    raise ValueError(f"Unsupported side: {side}")


def _parse_time_in_force(value: str) -> TimeInForce:
    raw = (value or "day").strip().lower()
    try:
        return TimeInForce(raw)
    except ValueError:
        return TimeInForce.DAY


def _parse_order_type(value: str) -> OrderType:
    raw = (value or "market").strip().lower()
    try:
        return OrderType(raw)
    except ValueError:
        return OrderType.MARKET


def _parse_asset_status(value: str) -> AssetStatus:
    raw = (value or "active").strip().lower()
    try:
        return AssetStatus(raw)
    except ValueError:
        return AssetStatus.ACTIVE


def _parse_stock_feed(value: Optional[str]) -> Optional[DataFeed]:
    if not value:
        return None
    raw = str(value).strip().lower()
    try:
        return DataFeed(raw)
    except ValueError:
        return None


def _parse_adjustment(value: str) -> Adjustment:
    raw = (value or "raw").strip().lower()
    try:
        return Adjustment(raw)
    except ValueError:
        return Adjustment.RAW


def _parse_timeframe(value: str) -> TimeFrame:
    token = str(value or "").strip().lower()
    if token in ("1min", "1m"):
        return TimeFrame(1, TimeFrameUnit.Minute)
    if token in ("5min", "5m"):
        return TimeFrame(5, TimeFrameUnit.Minute)
    if token in ("15min", "15m"):
        return TimeFrame(15, TimeFrameUnit.Minute)
    if token in ("30min", "30m"):
        return TimeFrame(30, TimeFrameUnit.Minute)
    if token in ("1hour", "1h", "60min", "60m"):
        return TimeFrame(1, TimeFrameUnit.Hour)
    if token in ("1day", "1d", "day"):
        return TimeFrame.Day
    if token in ("1week", "1w", "week"):
        return TimeFrame.Week
    raise ValueError(f"Unsupported timeframe: {value}")


class BarsResult:
    def __init__(self, df: pd.DataFrame):
        self.df = df


class LegacyCompatibleAlpacaClient:
    """Compatibility layer matching methods used by the existing app."""

    def __init__(self, api_key: str, api_secret: str, base_url: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.paper = is_paper_base_url(base_url)
        self.trading = TradingClient(api_key=api_key, secret_key=api_secret, paper=self.paper)
        self.stock_data = StockHistoricalDataClient(api_key=api_key, secret_key=api_secret)
        self.crypto_data = CryptoHistoricalDataClient(api_key=api_key, secret_key=api_secret)

    def get_account(self):
        return self.trading.get_account()

    def list_positions(self):
        return self.trading.get_all_positions()

    def get_position(self, symbol_or_asset_id: str):
        return self.trading.get_open_position(symbol_or_asset_id)

    def close_position(self, symbol_or_asset_id: str):
        return self.trading.close_position(symbol_or_asset_id)

    def get_order(self, order_id: str):
        return self.trading.get_order_by_id(order_id)

    def list_assets(self, status: str = "active"):
        req = GetAssetsRequest(status=_parse_asset_status(status))
        return self.trading.get_all_assets(filter=req)

    def get_asset(self, symbol_or_asset_id: str):
        return self.trading.get_asset(symbol_or_asset_id)

    def submit_order(self, **kwargs):
        symbol = kwargs.get("symbol")
        side = _parse_order_side(kwargs.get("side"))
        order_type = _parse_order_type(kwargs.get("type"))
        tif = _parse_time_in_force(kwargs.get("time_in_force"))
        extended_hours = bool(kwargs.get("extended_hours", False))
        qty = kwargs.get("qty")
        notional = kwargs.get("notional")
        limit_price = kwargs.get("limit_price")
        client_order_id = kwargs.get("client_order_id")

        if qty is not None:
            qty = float(qty)
            if qty <= 0:
                raise ValueError("qty must be > 0")
            if math.isfinite(qty):
                qty = float(qty)
        if notional is not None:
            notional = float(notional)
            if notional <= 0:
                raise ValueError("notional must be > 0")

        if order_type == OrderType.LIMIT:
            if limit_price is None:
                raise ValueError("limit_price is required for limit orders")
            order_data = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                notional=notional,
                side=side,
                type=order_type,
                time_in_force=tif,
                limit_price=float(limit_price),
                extended_hours=extended_hours,
                client_order_id=client_order_id,
            )
        else:
            order_data = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                notional=notional,
                side=side,
                type=OrderType.MARKET,
                time_in_force=tif,
                extended_hours=extended_hours,
                client_order_id=client_order_id,
            )
        return self.trading.submit_order(order_data=order_data)

    def get_latest_trade(self, symbol: str):
        req = StockLatestTradeRequest(symbol_or_symbols=[symbol])
        resp = self.stock_data.get_stock_latest_trade(req)
        trade = resp.get(symbol) if isinstance(resp, dict) else None
        if trade is None:
            raise ValueError(f"No latest trade available for symbol {symbol}")
        price = float(getattr(trade, "price", getattr(trade, "p", 0.0)) or 0.0)
        return SimpleNamespace(price=price, p=price)

    def get_latest_crypto_trade(self, symbol: str, _exchange: Optional[str] = None):
        req = CryptoLatestTradeRequest(symbol_or_symbols=[symbol])
        resp = self.crypto_data.get_crypto_latest_trade(req)
        trade = resp.get(symbol) if isinstance(resp, dict) else None
        if trade is None:
            raise ValueError(f"No latest crypto trade available for symbol {symbol}")
        price = float(getattr(trade, "price", getattr(trade, "p", 0.0)) or 0.0)
        return SimpleNamespace(price=price, p=price)

    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        adjustment: str = "raw",
        feed: Optional[str] = None,
    ):
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=_parse_timeframe(timeframe),
            start=pd.Timestamp(start).to_pydatetime() if start else None,
            end=pd.Timestamp(end).to_pydatetime() if end else None,
            adjustment=_parse_adjustment(adjustment),
            feed=_parse_stock_feed(feed),
        )
        bars = self.stock_data.get_stock_bars(req)
        df = bars.df.copy()
        if df.empty:
            return BarsResult(df)
        if isinstance(df.index, pd.MultiIndex):
            # Index format is usually [symbol, timestamp] for multi-symbol responses.
            if "symbol" in df.index.names:
                df = df.xs(symbol, level="symbol")
            else:
                df = df.reset_index().query("symbol == @symbol").set_index("timestamp")
        return BarsResult(df)


@dataclass
class TradeUpdateEvent:
    order_id: str
    event: str
    status: str
    timestamp: Optional[datetime]
    price: Optional[float]
    qty: Optional[float]


class _TradeUpdatesRunner:
    def __init__(self, api_key: str, api_secret: str, base_url: str, logger: logging.Logger):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.paper = is_paper_base_url(base_url)
        self.logger = logger
        self._lock = threading.Lock()
        self._latest_by_order: Dict[str, TradeUpdateEvent] = {}
        self._waiters: Dict[str, List[threading.Event]] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop_signal = threading.Event()
        self._stream_ready = threading.Event()
        self._stream: Optional[TradingStream] = None

    def _build_stream(self) -> TradingStream:
        stream = TradingStream(
            api_key=self.api_key,
            secret_key=self.api_secret,
            paper=self.paper,
        )
        stream.subscribe_trade_updates(self._on_trade_update)
        return stream

    async def _on_trade_update(self, update):
        order = getattr(update, "order", None)
        order_id = str(getattr(order, "id", "") or "")
        if not order_id:
            return

        event_name = str(getattr(update, "event", "") or "").lower()
        status = str(getattr(order, "status", "") or "").lower()
        raw_ts = getattr(update, "timestamp", None)
        raw_price = getattr(update, "price", None)
        raw_qty = getattr(update, "qty", None)

        ts = None
        if raw_ts:
            try:
                ts = pd.Timestamp(raw_ts).to_pydatetime()
            except Exception:
                ts = None
        price = None
        if raw_price is not None:
            try:
                price = float(raw_price)
            except Exception:
                price = None
        qty = None
        if raw_qty is not None:
            try:
                qty = float(raw_qty)
            except Exception:
                qty = None

        payload = TradeUpdateEvent(
            order_id=order_id,
            event=event_name,
            status=status,
            timestamp=ts,
            price=price,
            qty=qty,
        )
        with self._lock:
            self._latest_by_order[order_id] = payload
            waiters = list(self._waiters.get(order_id, []))
        for waiter in waiters:
            waiter.set()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_signal.clear()
            self._stream_ready.clear()
            self._thread = threading.Thread(target=self._run_forever, daemon=True)
            self._thread.start()

    def _run_forever(self) -> None:
        while not self._stop_signal.is_set():
            try:
                self._stream = self._build_stream()
                self._stream_ready.set()
                self._stream.run()
            except Exception as exc:
                self.logger.error(f"[ALPACA_STREAM] trade_updates stream error: {exc}")
            finally:
                self._stream_ready.clear()
                self._stream = None
            if not self._stop_signal.is_set():
                time.sleep(2)

    def stop(self) -> None:
        self._stop_signal.set()
        stream = self._stream
        if stream:
            try:
                stream.stop()
            except Exception:
                pass

    def wait_for_terminal_event(
        self,
        order_id: str,
        timeout_sec: float = 15.0,
        terminal_events: Optional[Set[str]] = None,
    ) -> Optional[TradeUpdateEvent]:
        if not order_id:
            return None

        term = terminal_events or TERMINAL_ORDER_EVENTS
        self.start()
        order_id = str(order_id)
        end_ts = time.time() + max(timeout_sec, 0.1)
        waiter = threading.Event()

        with self._lock:
            latest = self._latest_by_order.get(order_id)
            if latest and latest.event in term:
                return latest
            self._waiters.setdefault(order_id, []).append(waiter)

        try:
            while time.time() < end_ts:
                remaining = end_ts - time.time()
                if remaining <= 0:
                    break
                waiter.wait(timeout=min(remaining, 1.0))
                waiter.clear()
                with self._lock:
                    latest = self._latest_by_order.get(order_id)
                if latest and latest.event in term:
                    return latest
            with self._lock:
                return self._latest_by_order.get(order_id)
        finally:
            with self._lock:
                waiters = self._waiters.get(order_id, [])
                if waiter in waiters:
                    waiters.remove(waiter)
                if not waiters:
                    self._waiters.pop(order_id, None)


class TradeUpdatesHub:
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._runners: Dict[str, _TradeUpdatesRunner] = {}

    def _key(self, api_key: str, base_url: str) -> str:
        return f"{api_key}:{base_url}"

    def _get_runner(self, api_key: str, api_secret: str, base_url: str) -> _TradeUpdatesRunner:
        cache_key = self._key(api_key, base_url)
        with self._lock:
            runner = self._runners.get(cache_key)
            if runner is None:
                runner = _TradeUpdatesRunner(api_key, api_secret, base_url, self.logger)
                self._runners[cache_key] = runner
        return runner

    def wait_for_order(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        order_id: str,
        timeout_sec: float = 15.0,
    ) -> Optional[TradeUpdateEvent]:
        runner = self._get_runner(api_key, api_secret, base_url)
        return runner.wait_for_terminal_event(order_id=order_id, timeout_sec=timeout_sec)

    def stop_all(self) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.stop()
