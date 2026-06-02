#!/usr/bin/env python3
import os
import atexit
import logging
from logging.handlers import RotatingFileHandler
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import math
import hmac
import threading
import time
import sys
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from alpaca_api import AlpacaAPIError, LegacyCompatibleAlpacaClient, TradeUpdatesHub
from local_strategy_engine import LocalStrategyEngine
from models import db, User, Trade
import strategy_config as strategy_store
from utils import decrypt_data

# --- Initialization ---
ENV_PATH = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(ENV_PATH)

app = Flask(__name__)
try:
    os.makedirs(app.instance_path)
except OSError:
    pass

app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///app.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = int(os.getenv('MAX_CONTENT_LENGTH_BYTES', str(512 * 1024)))
db.init_app(app)

BASE_URL = os.getenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")
HOST = os.getenv("BOT_HOST", "127.0.0.1")
PORT = int(os.getenv("BOT_PORT", 5000))
DASHBOARD_INTERNAL_URL = os.getenv("DASHBOARD_INTERNAL_URL", "http://127.0.0.1:5050")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "your-very-secret-internal-key")
ENABLE_TV_BROADCAST = os.getenv("ENABLE_TV_BROADCAST", "false").lower() in ("1", "true", "yes", "y")
TV_BROADCAST_USER = os.getenv("TV_BROADCAST_USER", "Test").strip()
TV_BROADCAST_INCLUDE_SUPERUSER = os.getenv("TV_BROADCAST_INCLUDE_SUPERUSER", "false").lower() in ("1", "true", "yes", "y")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_SECRET_HEADER = os.getenv("WEBHOOK_SECRET_HEADER", "X-Webhook-Secret").strip() or "X-Webhook-Secret"
MIN_TRADE_AMOUNT = float(os.getenv("MIN_TRADE_AMOUNT", "1"))
MAX_TRADE_AMOUNT = float(os.getenv("MAX_TRADE_AMOUNT", "100000"))
MAX_ACCOUNT_ALLOCATION_PCT = float(os.getenv("MAX_ACCOUNT_ALLOCATION_PCT", "0"))
MAX_OPEN_POSITIONS_PER_ACCOUNT = int(os.getenv("MAX_OPEN_POSITIONS_PER_ACCOUNT", "30"))
SIGNAL_DEDUP_WINDOW_SEC = int(os.getenv("SIGNAL_DEDUP_WINDOW_SEC", "8"))
MAX_WEBHOOK_CONTENT_LENGTH = int(os.getenv("MAX_WEBHOOK_CONTENT_LENGTH", str(128 * 1024)))
TRADE_UPDATES_WAIT_SEC = float(os.getenv("TRADE_UPDATES_WAIT_SEC", "12"))
AUTO_EXTENDED_HOURS = os.getenv("AUTO_EXTENDED_HOURS", "true").lower() in ("1", "true", "yes", "y")
AUTO_LIMIT_OUTSIDE_RTH = os.getenv("AUTO_LIMIT_OUTSIDE_RTH", "true").lower() in ("1", "true", "yes", "y")
OUTSIDE_RTH_LIMIT_SLIPPAGE_BPS = float(os.getenv("OUTSIDE_RTH_LIMIT_SLIPPAGE_BPS", "25"))

RECENT_SIGNAL_CACHE = {}
RECENT_SIGNAL_LOCK = threading.Lock()
TRADE_UPDATES = TradeUpdatesHub(logger=logging.getLogger("trades_logger"))
LOCAL_STRATEGY_ENGINE = None

@atexit.register
def _shutdown_streams():
    try:
        TRADE_UPDATES.stop_all()
    except Exception:
        pass
    try:
        if LOCAL_STRATEGY_ENGINE:
            LOCAL_STRATEGY_ENGINE.stop()
    except Exception:
        pass

# --- Logging ---
handler = RotatingFileHandler('trades.log', maxBytes=100000, backupCount=3)
handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
logger = logging.getLogger('trades_logger')
logger.setLevel(logging.INFO)
logger.addHandler(handler)

local_strategy_handler = RotatingFileHandler('local_strategy.log', maxBytes=200000, backupCount=5)
local_strategy_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
local_strategy_logger = logging.getLogger('local_strategy_logger')
local_strategy_logger.setLevel(logging.INFO)
local_strategy_logger.addHandler(local_strategy_handler)

if INTERNAL_API_KEY == "your-very-secret-internal-key":
    logger.warning("[SECURITY] INTERNAL_API_KEY is using a default value. Set a unique secret in .env.")
if not WEBHOOK_SECRET:
    logger.warning("[SECURITY] WEBHOOK_SECRET not set. Webhook endpoint accepts unsigned payloads.")

# --- Action Keywords ---
CLOSE_ACTIONS = [
    "trailing exit long", "trailing exit short", "fixed stop loss (long)", "fixed stop loss (short)",
    "fixed take profit (long)", "fixed take profit (short)", "forced sl long", "forced sl short",
    "forced tp long", "forced tp short", "close long", "close short", "close"
]

def is_close_action(act):
    return act and act.strip().lower() in CLOSE_ACTIONS

def is_crypto(symbol):
    return symbol and (symbol.upper().endswith('USD') or '/' in symbol)

def _parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")

def is_outside_regular_hours(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    ny = now_utc.astimezone(ZoneInfo("America/New_York"))
    if ny.weekday() >= 5:  # Saturday/Sunday
        return True
    minutes = ny.hour * 60 + ny.minute
    return minutes < (9 * 60 + 30) or minutes >= (16 * 60)

def _round_equity_limit_price(price, side):
    tick = 0.0001 if abs(float(price)) < 1 else 0.01
    units = float(price) / tick
    if side == "buy":
        rounded = math.ceil(units) * tick
    else:
        rounded = math.floor(units) * tick
    decimals = 4 if tick == 0.0001 else 2
    return round(rounded, decimals)

def _build_limit_price(last_price, side, equity=False):
    slip = max(0.0, OUTSIDE_RTH_LIMIT_SLIPPAGE_BPS) / 10000.0
    if side == "buy":
        price = last_price * (1 + slip)
    else:
        price = last_price * (1 - slip)
    if equity:
        return _round_equity_limit_price(price, side)
    return round(price, 4)

def _safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None

def _is_dashboard_request(payload):
    return _parse_bool(payload.get("dashboard_request"), default=False)

def _is_local_strategy_request(payload):
    return _parse_bool(payload.get("local_strategy_request"), default=False)

def get_last_price(api_client, symbol):
    try:
        api_symbol = symbol.replace('/', '')
        if is_crypto(symbol):
            trade = api_client.get_latest_crypto_trade(api_symbol, "CBSE")
            return float(trade.p)
        else:
            trade = api_client.get_latest_trade(api_symbol)
            return float(trade.price)
    except Exception as e:
        logger.error(f"[PRICE_FETCH_FAIL] Failed to get price for {symbol}: {e}")
        return 0.0

def _secure_compare(provided, expected):
    if not provided or not expected:
        return False
    try:
        return hmac.compare_digest(str(provided), str(expected))
    except Exception:
        return False

def _cache_duplicate_signal(user_id, symbol, action):
    if SIGNAL_DEDUP_WINDOW_SEC <= 0:
        return False, 0
    now_ts = time.time()
    key = f"{user_id}:{symbol}:{action.lower()}"
    with RECENT_SIGNAL_LOCK:
        stale_keys = [k for k, ts in RECENT_SIGNAL_CACHE.items() if now_ts - ts > SIGNAL_DEDUP_WINDOW_SEC]
        for stale in stale_keys:
            RECENT_SIGNAL_CACHE.pop(stale, None)
        last_ts = RECENT_SIGNAL_CACHE.get(key)
        if last_ts and (now_ts - last_ts) < SIGNAL_DEDUP_WINDOW_SEC:
            remaining = SIGNAL_DEDUP_WINDOW_SEC - int(now_ts - last_ts)
            return True, max(remaining, 1)
        RECENT_SIGNAL_CACHE[key] = now_ts
    return False, 0

def _validate_trade_risk(api_client, user, symbol, action, amount):
    if amount < MIN_TRADE_AMOUNT:
        return False, 400, f"Amount must be >= {MIN_TRADE_AMOUNT}"
    if amount > MAX_TRADE_AMOUNT:
        return False, 400, f"Amount exceeds configured max trade amount ({MAX_TRADE_AMOUNT})"

    if action.lower() in ("buy", "sell"):
        if MAX_OPEN_POSITIONS_PER_ACCOUNT > 0:
            try:
                open_positions = api_client.list_positions()
            except Exception as e:
                logger.error(f"[RISK_FAIL] Could not fetch open positions for '{user.username}': {e}")
                return False, 500, "Risk verification failed (open positions check)"
            if len(open_positions) >= MAX_OPEN_POSITIONS_PER_ACCOUNT:
                return False, 409, f"Account reached max open positions ({MAX_OPEN_POSITIONS_PER_ACCOUNT})"

        if MAX_ACCOUNT_ALLOCATION_PCT > 0:
            try:
                account = api_client.get_account()
                equity = float(account.equity)
            except Exception as e:
                logger.error(f"[RISK_FAIL] Could not fetch account equity for '{user.username}': {e}")
                return False, 500, "Risk verification failed (equity check)"
            allowed_amount = equity * (MAX_ACCOUNT_ALLOCATION_PCT / 100.0)
            if amount > allowed_amount:
                return False, 400, (
                    f"Amount exceeds allocation rule: max {MAX_ACCOUNT_ALLOCATION_PCT}% of equity "
                    f"({allowed_amount:.2f})"
                )

    return True, 200, None

def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)

def record_trade_notification(payload):
    try:
        safe_payload = _json_safe(payload)
        r = requests.post(
            f"{DASHBOARD_INTERNAL_URL}/api/internal/record_trade",
            json=safe_payload,
            headers={'X-Internal-API-Key': INTERNAL_API_KEY},
            timeout=3
        )
        if not r.ok:
            logger.warning(f"[DASHBOARD_NOTIFY_FAIL] Status={r.status_code} Body={r.text}")
    except Exception as e:
        logger.error(f"[DASHBOARD_NOTIFY_FAIL] {e}")

def mirror_open_trade(user, payload, api_client, position_obj, amount):
    symbol = payload.get("symbol")
    if not symbol:
        return False
    api_symbol = symbol.replace('/', '')
    existing = Trade.query.filter_by(symbol=api_symbol, status='open', user_id=user.id).first()
    if existing:
        return True

    open_price = None
    qty = None
    side = (payload.get("action") or "").lower()
    if position_obj is not None:
        try:
            open_price = float(position_obj.avg_entry_price)
        except (TypeError, ValueError):
            open_price = None
        try:
            qty = abs(float(position_obj.qty))
        except (TypeError, ValueError):
            qty = None
        position_side = getattr(position_obj, "side", "").lower()
        if position_side == "long":
            side = "buy"
        elif position_side == "short":
            side = "sell"

    if not open_price or not qty:
        last_price = get_last_price(api_client, symbol)
        if last_price:
            if not open_price:
                open_price = last_price
            if amount is None:
                amount = user.per_trade_amount
            if is_crypto(symbol):
                qty = amount / last_price
            else:
                if side == "buy":
                    qty = amount / last_price
                else:
                    qty = math.floor(amount / last_price)

    if not open_price or not qty:
        return None

    order_id = f"mirror_{api_symbol}_{int(datetime.now(timezone.utc).timestamp())}"
    record_payload = {
        "result": "opened",
        "order_id": order_id,
        "symbol": symbol,
        "side": side or "buy",
        "price": open_price,
        "payload": payload,
        "user_id": user.id,
        "qty": qty
    }
    record_trade_notification(record_payload)
    logger.info(f"[TRADE_MIRROR] Recorded mirrored open for '{user.username}' {api_symbol}")
    return record_payload

def mirror_close_trade(user, payload, api_client):
    symbol = payload.get("symbol")
    if not symbol:
        return None
    api_symbol = symbol.replace('/', '')
    trade = Trade.query.filter_by(symbol=api_symbol, status='open', user_id=user.id).order_by(Trade.open_time.desc()).first()

    close_price = get_last_price(api_client, symbol)
    if close_price == 0:
        close_price = trade.open_price if trade else None
    if close_price is None:
        return None

    if trade:
        qty = trade.qty
        avg_entry_price = trade.open_price
        side_raw = (trade.side or "").lower()
    else:
        side_raw = (payload.get("action") or "").lower()
        amount = user.per_trade_amount
        if is_crypto(symbol):
            qty = amount / close_price
        else:
            qty = amount / close_price
        avg_entry_price = close_price

    position_side = "short" if "short" in side_raw or side_raw == "sell" else "long"
    position_obj = {
        "avg_entry_price": avg_entry_price,
        "qty": qty,
        "side": position_side,
        "asset_id": f"mirror_{api_symbol}"
    }
    record_payload = {
        "result": "closed",
        "symbol": symbol,
        "close_price": close_price,
        "close_time": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
        "user_id": user.id,
        "position_obj": position_obj
    }
    record_trade_notification(record_payload)
    logger.info(f"[TRADE_MIRROR] Recorded mirrored close for '{user.username}' {api_symbol}")
    return record_payload

def account_key_for_user(user):
    api_key = decrypt_data(user.encrypted_alpaca_key)
    api_secret = decrypt_data(user.encrypted_alpaca_secret)
    if not api_key or not api_secret:
        return None
    return f"{api_key}:{api_secret}"

def _order_fill_price(api, order_id):
    if not order_id:
        return None
    try:
        ord_obj = api.get_order(order_id)
        return _safe_float(getattr(ord_obj, "filled_avg_price", None))
    except Exception:
        return None

def _is_no_position_error(err_text):
    normalized = (err_text or "").lower()
    markers = (
        "position not found",
        "position does not exist",
        "no position",
        "insufficient qty",
        "insufficient quantity",
    )
    return any(marker in normalized for marker in markers)

def _pick_close_price_from_updates(api_key, api_secret, close_order_id, close_order, api, fallback_symbol):
    close_price = _safe_float(getattr(close_order, "filled_avg_price", None))
    if close_price is not None:
        return close_price

    try:
        update = TRADE_UPDATES.wait_for_order(
            api_key=api_key,
            api_secret=api_secret,
            base_url=BASE_URL,
            order_id=str(close_order_id),
            timeout_sec=TRADE_UPDATES_WAIT_SEC,
        )
        if update and update.price is not None:
            return float(update.price)
    except Exception as exc:
        logger.warning(f"[ALPACA_STREAM] Could not read trade update for close order {close_order_id}: {exc}")

    close_price = _order_fill_price(api, close_order_id)
    if close_price is not None:
        return close_price

    fallback = get_last_price(api, fallback_symbol)
    return fallback if fallback else None

def _resolve_equity_order_params(symbol, action, amount, last_price, payload):
    requested_type = str(payload.get("order_type", "market")).strip().lower() or "market"
    requested_tif = str(payload.get("time_in_force", "day")).strip().lower() or "day"
    requested_limit = _safe_float(payload.get("limit_price"))
    explicit_eh = _parse_bool(payload.get("extended_hours"), default=False)

    outside_rth = is_outside_regular_hours()
    is_overnight_window = False
    now_ny = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    minutes = now_ny.hour * 60 + now_ny.minute
    is_weekday_overnight = now_ny.weekday() < 5 and (minutes >= 20 * 60 or minutes < 4 * 60)
    is_sunday_open = now_ny.weekday() == 6 and minutes >= 20 * 60
    if is_weekday_overnight or is_sunday_open:
        is_overnight_window = True

    # Extended-hours eligibility: automatic for outside RTH if enabled.
    extended_hours = explicit_eh or (AUTO_EXTENDED_HOURS and outside_rth)
    order_type = requested_type
    tif = requested_tif
    limit_price = requested_limit

    # During extended/overnight, Alpaca accepts limit orders only (DAY/GTC).
    if extended_hours and order_type != "limit":
        if AUTO_LIMIT_OUTSIDE_RTH:
            order_type = "limit"
            if limit_price is None:
                limit_price = _build_limit_price(last_price, action, equity=True)
        else:
            raise ValueError("Extended-hours orders must use limit type. Set order_type=limit.")

    if extended_hours and tif not in ("day", "gtc"):
        tif = "day"

    if is_overnight_window:
        # Explicitly enforce limit-only behavior in overnight window.
        if order_type != "limit":
            raise ValueError("Overnight trading supports limit orders only.")
        if tif not in ("day", "gtc"):
            tif = "day"

    if order_type == "limit" and limit_price is None:
        limit_price = _build_limit_price(last_price, action, equity=True)
    elif order_type == "limit":
        limit_price = _round_equity_limit_price(limit_price, action)

    order_params = {
        "symbol": symbol,
        "side": action,
        "type": order_type,
        "time_in_force": tif,
        "extended_hours": bool(extended_hours),
    }
    client_order_id = str(payload.get("client_order_id", "") or "").strip()
    if client_order_id:
        order_params["client_order_id"] = client_order_id[:48]

    if action == "buy":
        order_params["notional"] = float(amount)
    else:
        qty = math.floor(float(amount) / float(last_price))
        if qty <= 0:
            raise ValueError(f"Amount ${amount} too small to short {symbol}.")
        order_params["qty"] = qty

    if order_type == "limit":
        order_params["limit_price"] = float(limit_price)

    return order_params, outside_rth, is_overnight_window

def process_trade_for_user(user, payload):
    if user.is_trading_restricted:
        logger.warning(f"[TRADE_REJECTED] User '{user.username}' is restricted from trading.")
        return False, 403, {"error": "Trading for this user is currently restricted"}, None

    api_key = decrypt_data(user.encrypted_alpaca_key)
    api_secret = decrypt_data(user.encrypted_alpaca_secret)
    if not api_key or not api_secret:
        logger.error(f"[TRADE_REJECTED] User '{user.username}' has no API credentials.")
        return False, 500, {"error": "Alpaca credentials not configured"}, None

    symbol = payload.get("symbol")
    action = payload.get("action", "")
    if not symbol or not action:
        return False, 400, {"error": "Invalid webhook format"}, None

    try:
        amount = float(payload.get("amount", user.per_trade_amount))
    except (TypeError, ValueError):
        return False, 400, {"error": "Invalid amount"}, None

    api_symbol = symbol.replace('/', '')

    if not _is_dashboard_request(payload) and not _is_local_strategy_request(payload):
        cfg = strategy_store.load_strategy_config()
        if not strategy_store.tradingview_allowed_for_symbol(api_symbol, cfg):
            mode = strategy_store.strategy_mode_for_symbol(api_symbol, cfg)
            logger.info(
                f"[TRADE_MODE] Ignored TradingView signal for user='{user.username}' "
                f"symbol='{api_symbol}' because strategy mode is '{mode}'."
            )
            return True, 200, {
                "result": "tw_ignored_by_strategy_mode",
                "symbol": api_symbol,
                "mode": mode,
            }, None

    api = LegacyCompatibleAlpacaClient(api_key, api_secret, BASE_URL)
    duplicate, retry_after = _cache_duplicate_signal(user.id, api_symbol, action)
    if duplicate:
        logger.warning(
            f"[TRADE_DEDUP] Ignored duplicate signal for user='{user.username}' symbol='{api_symbol}' "
            f"action='{action}' retry_after={retry_after}s"
        )
        return True, 200, {"result": "duplicate_ignored", "retry_after_sec": retry_after}, None

    risk_ok, risk_status, risk_error = _validate_trade_risk(api, user, symbol, action, amount)
    if not risk_ok:
        logger.warning(
            f"[TRADE_REJECTED] Risk rule rejected trade for user='{user.username}' symbol='{api_symbol}' "
            f"action='{action}' amount={amount}: {risk_error}"
        )
        return False, risk_status, {"error": risk_error}, None

    if is_close_action(action):
        try:
            position_to_close = api.get_position(api_symbol)
            close_order = api.close_position(api_symbol)
            close_order_id = str(getattr(close_order, "id", ""))
            logger.info(f"[TRADE_EXECUTED] User '{user.username}' CLOSE order {close_order_id} for {api_symbol}")
            close_price = _pick_close_price_from_updates(
                api_key=api_key,
                api_secret=api_secret,
                close_order_id=close_order_id,
                close_order=close_order,
                api=api,
                fallback_symbol=symbol,
            )
            notification_payload = {
                "result": "closed",
                "symbol": symbol,
                "close_order_id": close_order_id,
                "payload": payload,
                "user_id": user.id,
                "position_obj": {
                    "avg_entry_price": position_to_close.avg_entry_price,
                    "qty": position_to_close.qty,
                    "side": position_to_close.side,
                    "asset_id": str(position_to_close.asset_id)
                }
            }
            if close_price is not None:
                notification_payload["close_price"] = close_price
                notification_payload["close_time"] = datetime.now(timezone.utc).isoformat()
            record_trade_notification(notification_payload)
            return True, 200, {"result": "closed", "close_order_id": close_order_id}, notification_payload
        except AlpacaAPIError as e:
            if _is_no_position_error(str(e)):
                mirror_payload = None
                if ENABLE_TV_BROADCAST:
                    mirror_payload = mirror_close_trade(user, payload, api)
                logger.warning(f"[TRADE_INFO] User '{user.username}' tried to close {api_symbol}, but no position exists.")
                return True, 200, {"result": "mirrored_close" if mirror_payload else "no_position_to_close", "symbol": symbol}, mirror_payload
            logger.error(f"[TRADE_FAIL] Error closing {api_symbol} for '{user.username}': {e}")
            return False, 500, {"error": str(e)}, None
        except Exception as e:
            logger.error(f"[TRADE_FAIL] A general error occurred while closing {api_symbol} for '{user.username}': {e}")
            return False, 500, {"error": str(e)}, None

    if action.lower() in ["buy", "sell"]:
        try:
            position_existing = api.get_position(api_symbol)
            logger.warning(f"[TRADE_REJECTED] User '{user.username}' already has position for {api_symbol}.")
            if ENABLE_TV_BROADCAST:
                mirror_payload = mirror_open_trade(user, payload, api, position_existing, amount)
                return True, 200, {"result": "mirrored_open" if mirror_payload else "position_exists", "symbol": symbol}, mirror_payload
            return False, 409, {"error": f"{api_symbol} position already open."}, None
        except AlpacaAPIError:
            pass
        except Exception as e:
            logger.error(f"[TRADE_FAIL] Could not verify position for '{user.username}' on {api_symbol}: {e}")
            return False, 500, {"error": "Failed to verify existing position"}, None

        try:
            last_price = get_last_price(api, symbol)
            if last_price == 0:
                return False, 400, {"error": f"Could not fetch price for {symbol}."}, None

            qty_to_log = 0.0
            if is_crypto(symbol):
                qty = amount / last_price
                qty_to_log = qty
                tif = str(payload.get("time_in_force", "gtc")).strip().lower() or "gtc"
                if tif not in ("gtc", "ioc"):
                    tif = "gtc"
                order_params = {
                    "symbol": api_symbol,
                    "side": action.lower(),
                    "type": str(payload.get("order_type", "market")).strip().lower() or "market",
                    "qty": qty,
                    "time_in_force": tif,
                }
                client_order_id = str(payload.get("client_order_id", "") or "").strip()
                if client_order_id:
                    order_params["client_order_id"] = client_order_id[:48]
                limit_price = _safe_float(payload.get("limit_price"))
                if order_params["type"] == "limit":
                    order_params["limit_price"] = limit_price if limit_price else _build_limit_price(last_price, action.lower())
            else:
                order_params, outside_rth, is_overnight_window = _resolve_equity_order_params(
                    symbol=api_symbol,
                    action=action.lower(),
                    amount=amount,
                    last_price=last_price,
                    payload=payload,
                )
                if outside_rth and order_params.get("extended_hours"):
                    try:
                        asset = api.get_asset(api_symbol)
                        overnight_tradable = bool(getattr(asset, "overnight_tradable", False))
                        overnight_halted = bool(getattr(asset, "overnight_halted", False))
                        if is_overnight_window and not overnight_tradable:
                            raise ValueError(f"{api_symbol} is not overnight_tradable for 24/5 session.")
                        if is_overnight_window and overnight_halted:
                            raise ValueError(f"{api_symbol} is currently overnight_halted.")
                    except AlpacaAPIError:
                        # Keep order flow resilient even if asset metadata lookup fails.
                        pass
                qty_to_log = amount / last_price if action.lower() == "buy" else float(order_params.get("qty", 0))
                if outside_rth:
                    logger.info(
                        f"[TRADE_ROUTE] user='{user.username}' symbol='{api_symbol}' outside_rth={outside_rth} "
                        f"overnight_window={is_overnight_window} order_type={order_params.get('type')} "
                        f"tif={order_params.get('time_in_force')} extended_hours={order_params.get('extended_hours')}"
                    )

            order = api.submit_order(**order_params)
            order_id = str(getattr(order, "id", ""))
            logger.info(f"[TRADE_EXECUTED] User '{user.username}' OPEN order {order_id} for {api_symbol}")

            entry_price = _safe_float(getattr(order, "filled_avg_price", None))
            if entry_price is None:
                try:
                    update = TRADE_UPDATES.wait_for_order(
                        api_key=api_key,
                        api_secret=api_secret,
                        base_url=BASE_URL,
                        order_id=order_id,
                        timeout_sec=TRADE_UPDATES_WAIT_SEC,
                    )
                    if update and update.price is not None:
                        entry_price = float(update.price)
                except Exception as exc:
                    logger.warning(f"[ALPACA_STREAM] Could not read trade update for open order {order_id}: {exc}")
            if entry_price is None:
                entry_price = _order_fill_price(api, order_id) or last_price

            notification_payload = {
                "result": "opened",
                "order_id": order_id,
                "symbol": symbol,
                "side": action.lower(),
                "price": entry_price,
                "payload": payload,
                "user_id": user.id,
                "qty": qty_to_log
            }
            record_trade_notification(notification_payload)
            return True, 200, {"result": "opened", "order_id": order_id}, notification_payload
        except ValueError as e:
            logger.warning(f"[TRADE_REJECTED] {e}")
            return False, 400, {"error": str(e)}, None
        except AlpacaAPIError as e:
            logger.error(f"[TRADE_FAIL] Alpaca API error opening {symbol} for '{user.username}': {e}")
            return False, 500, {"error": str(e)}, None
        except Exception as e:
            logger.error(f"[TRADE_FAIL] Error opening {symbol} for '{user.username}': {e}")
            return False, 500, {"error": str(e)}, None

    logger.error(f"[UNKNOWN_ACTION] Received unknown action: {action}")
    return False, 400, {"error": "Unknown action"}, None

@app.route("/webhook", methods=["POST"])
def webhook():
    if request.content_length and request.content_length > MAX_WEBHOOK_CONTENT_LENGTH:
        logger.warning(
            f"[SECURITY] Rejected oversized webhook from ip={request.remote_addr} "
            f"content_length={request.content_length}"
        )
        return jsonify({"error": "payload_too_large"}), 413

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        logger.warning(f"[TRADE_REJECTED] Invalid or missing JSON payload from ip={request.remote_addr}")
        return jsonify({"error": "Invalid JSON payload"}), 400

    if WEBHOOK_SECRET:
        provided_secret = request.headers.get(WEBHOOK_SECRET_HEADER) or payload.get('passphrase') or payload.get('secret')
        if not _secure_compare(provided_secret, WEBHOOK_SECRET):
            logger.warning(f"[SECURITY] Webhook secret mismatch from ip={request.remote_addr}")
            return jsonify({"error": "Unauthorized"}), 401

    webhook_log_path = os.path.join(app.instance_path, 'last_webhook.log')
    try:
        with open(webhook_log_path, 'w') as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except Exception as e:
        logger.error(f"[BOT_STATUS] Could not write to last_webhook.log: {e}")

    logger.info(f"[WEBHOOK_RECEIVED] Payload: {payload}")
    tradingview_user = (payload.get("user") or "").strip()
    dashboard_user_id = payload.get("dashboard_user_id")
    dashboard_username = (payload.get("dashboard_username") or "").strip()
    
    if not tradingview_user and not dashboard_user_id and not dashboard_username:
        logger.error("[TRADE_REJECTED] 'user' field is missing from webhook.")
        return jsonify({"error": "Webhook missing 'user' identifier"}), 400

    with app.app_context():
        if dashboard_user_id or dashboard_username:
            user = None
            if dashboard_user_id:
                try:
                    user = db.session.get(User, int(dashboard_user_id))
                except (TypeError, ValueError):
                    user = None
            if not user and dashboard_username:
                user = User.query.filter_by(username=dashboard_username).first()
            target_users = []
            if user and not user.is_superuser:
                target_users = [user]
            logger.info(f"[WEBHOOK_DASHBOARD] Targeting {len(target_users)} user(s) for dashboard request.")
        elif ENABLE_TV_BROADCAST and TV_BROADCAST_USER and tradingview_user.lower() == TV_BROADCAST_USER.lower():
            query = User.query
            if not TV_BROADCAST_INCLUDE_SUPERUSER:
                query = query.filter_by(is_superuser=False)
            target_users = query.all()
            logger.info(f"[WEBHOOK_BROADCAST] Using broadcast for '{tradingview_user}'. Users={len(target_users)}")
        else:
            target_users = User.query.filter_by(tradingview_user=tradingview_user, is_superuser=False).all()

    if not target_users:
        logger.error(f"[TRADE_REJECTED] No registered user for TV user='{tradingview_user}'")
        return jsonify({"error": f"User '{tradingview_user}' not registered"}), 403

    grouped_users = {}
    for user in target_users:
        account_key = account_key_for_user(user)
        group_key = account_key if account_key else f"missing:{user.id}"
        grouped_users.setdefault(group_key, []).append(user)

    results = []
    success_count = 0
    for group_key, users in grouped_users.items():
        primary = users[0]
        ok, status_code, result, record_payload = process_trade_for_user(primary, payload)
        if ok and status_code < 400:
            success_count += 1
        results.append({
            "user_id": primary.id,
            "username": primary.username,
            "status": status_code,
            "result": result
        })

        if record_payload and len(users) > 1:
            for shadow_user in users[1:]:
                cloned_payload = dict(record_payload)
                cloned_payload["user_id"] = shadow_user.id
                record_trade_notification(cloned_payload)
                results.append({
                    "user_id": shadow_user.id,
                    "username": shadow_user.username,
                    "status": status_code,
                    "result": {
                        "result": "linked_account",
                        "source_user": primary.username,
                        "original_result": result.get("result")
                    }
                })
        elif len(users) > 1:
            for shadow_user in users[1:]:
                results.append({
                    "user_id": shadow_user.id,
                    "username": shadow_user.username,
                    "status": status_code,
                    "result": {
                        "error": "shared_account_no_record",
                        "source_user": primary.username
                    }
                })

    overall_status = 200 if success_count > 0 else 500
    return jsonify({
        "mode": "broadcast" if len(target_users) > 1 else "single",
        "success_count": success_count,
        "results": results
    }), overall_status


def start_local_strategy_engine_once():
    global LOCAL_STRATEGY_ENGINE
    if LOCAL_STRATEGY_ENGINE is not None:
        return
    if os.getenv("LOCAL_STRATEGY_ENGINE_AUTOSTART", "true").lower() not in ("1", "true", "yes", "y"):
        logger.info("[LOCAL_STRATEGY] Autostart disabled by LOCAL_STRATEGY_ENGINE_AUTOSTART.")
        return
    if any("pytest" in arg for arg in sys.argv):
        return
    LOCAL_STRATEGY_ENGINE = LocalStrategyEngine(
        flask_app=app,
        execute_trade=process_trade_for_user,
        base_url=BASE_URL,
        logger=local_strategy_logger,
    )
    LOCAL_STRATEGY_ENGINE.start()


start_local_strategy_engine_once()

if __name__ == "__main__":
    app.run(host=HOST, port=PORT)
