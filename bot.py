#!/usr/bin/env python3
import os
import logging
from logging.handlers import RotatingFileHandler
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import alpaca_trade_api as tradeapi
import math
import hmac
import threading
import time
from datetime import datetime, timezone

from models import db, User, Trade
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

RECENT_SIGNAL_CACHE = {}
RECENT_SIGNAL_LOCK = threading.Lock()

# --- Logging ---
handler = RotatingFileHandler('trades.log', maxBytes=100000, backupCount=3)
handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
logger = logging.getLogger('trades_logger')
logger.setLevel(logging.INFO)
logger.addHandler(handler)

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

def record_trade_notification(payload):
    try:
        r = requests.post(
            f"{DASHBOARD_INTERNAL_URL}/api/internal/record_trade",
            json=payload,
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

    api = tradeapi.REST(api_key, api_secret, BASE_URL, api_version='v2')
    api_symbol = symbol.replace('/', '')
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
            logger.info(f"[TRADE_EXECUTED] User '{user.username}' CLOSE order {close_order.id} for {api_symbol}")
            close_price = getattr(close_order, "filled_avg_price", None)
            if close_price is not None:
                try:
                    close_price = float(close_price)
                except (TypeError, ValueError):
                    close_price = None
            if close_price is None:
                last_price = get_last_price(api, symbol)
                close_price = last_price if last_price else None
            notification_payload = {
                "result": "closed",
                "symbol": symbol,
                "close_order_id": close_order.id,
                "payload": payload,
                "user_id": user.id,
                "position_obj": {
                    "avg_entry_price": position_to_close.avg_entry_price,
                    "qty": position_to_close.qty,
                    "side": position_to_close.side,
                    "asset_id": position_to_close.asset_id
                }
            }
            if close_price is not None:
                notification_payload["close_price"] = close_price
                notification_payload["close_time"] = datetime.now(timezone.utc).isoformat()
            record_trade_notification(notification_payload)
            return True, 200, {"result": "closed", "close_order_id": close_order.id}, notification_payload
        except tradeapi.rest.APIError as e:
            error_text = str(e).lower()
            no_position_markers = (
                "position not found",
                "position does not exist",
                "no position",
                "insufficient qty",
                "insufficient quantity"
            )
            if any(msg in error_text for msg in no_position_markers):
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
        except tradeapi.rest.APIError:
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
                order_params = {'symbol': api_symbol, 'side': action.lower(), 'type': 'market', 'qty': qty, 'time_in_force': 'gtc'}
            else:
                if action.lower() == 'buy':
                    qty_to_log = amount / last_price
                    order_params = {'symbol': api_symbol, 'side': 'buy', 'type': 'market', 'notional': amount, 'time_in_force': 'day'}
                else:
                    qty = math.floor(amount / last_price)
                    qty_to_log = qty
                    if qty == 0:
                        logger.warning(f"[TRADE_REJECTED] Amount ${amount} too small to short {api_symbol}.")
                        return False, 400, {"error": f"Amount ${amount} too small to short {api_symbol}."}, None
                    order_params = {'symbol': api_symbol, 'side': 'sell', 'type': 'market', 'qty': qty, 'time_in_force': 'day'}

            order = api.submit_order(**order_params)
            logger.info(f"[TRADE_EXECUTED] User '{user.username}' OPEN order {order.id} for {api_symbol}")

            notification_payload = {
                "result": "opened",
                "order_id": order.id,
                "symbol": symbol,
                "side": action.lower(),
                "price": last_price,
                "payload": payload,
                "user_id": user.id,
                "qty": qty_to_log
            }
            record_trade_notification(notification_payload)
            return True, 200, {"result": "opened", "order_id": order.id}, notification_payload
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
            if user:
                account_key = account_key_for_user(user)
                if account_key:
                    target_users = [u for u in User.query.all() if account_key_for_user(u) == account_key]
                else:
                    target_users = [user]
            logger.info(f"[WEBHOOK_DASHBOARD] Targeting {len(target_users)} user(s) for dashboard request.")
        elif ENABLE_TV_BROADCAST and TV_BROADCAST_USER and tradingview_user.lower() == TV_BROADCAST_USER.lower():
            query = User.query
            if not TV_BROADCAST_INCLUDE_SUPERUSER:
                query = query.filter_by(is_superuser=False)
            target_users = query.all()
            logger.info(f"[WEBHOOK_BROADCAST] Using broadcast for '{tradingview_user}'. Users={len(target_users)}")
        else:
            target_users = User.query.filter_by(tradingview_user=tradingview_user).all()

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

if __name__ == "__main__":
    app.run(host=HOST, port=PORT)
