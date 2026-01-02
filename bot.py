#!/usr/bin/env python3
import os
import logging
from logging.handlers import RotatingFileHandler
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import alpaca_trade_api as tradeapi
import math
from datetime import datetime, timezone

from models import db, User
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
db.init_app(app)

BASE_URL = os.getenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")
PORT = int(os.getenv("BOT_PORT", 5000))
DASHBOARD_INTERNAL_URL = os.getenv("DASHBOARD_INTERNAL_URL", "http://127.0.0.1:5050")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "your-very-secret-internal-key")

# --- Logging ---
handler = RotatingFileHandler('trades.log', maxBytes=100000, backupCount=3)
handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
logger = logging.getLogger('trades_logger')
logger.setLevel(logging.INFO)
logger.addHandler(handler)

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

@app.route("/webhook", methods=["POST"])
def webhook():
    webhook_log_path = os.path.join(app.instance_path, 'last_webhook.log')
    try:
        with open(webhook_log_path, 'w') as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except Exception as e:
        logger.error(f"[BOT_STATUS] Could not write to last_webhook.log: {e}")

    payload = request.get_json(force=True)
    logger.info(f"[WEBHOOK_RECEIVED] Payload: {payload}")
    tradingview_user = payload.get("user")
    
    if not tradingview_user:
        logger.error("[TRADE_REJECTED] 'user' field is missing from webhook.")
        return jsonify({"error": "Webhook missing 'user' identifier"}), 400

    with app.app_context():
        user = User.query.filter_by(tradingview_user=tradingview_user).first()

    if not user:
        logger.error(f"[TRADE_REJECTED] No registered user for TV user='{tradingview_user}'")
        return jsonify({"error": f"User '{tradingview_user}' not registered"}), 403

    if user.is_trading_restricted:
        logger.warning(f"[TRADE_REJECTED] User '{user.username}' is restricted from trading.")
        return jsonify({"error": "Trading for this user is currently restricted"}), 403

    api_key = decrypt_data(user.encrypted_alpaca_key)
    api_secret = decrypt_data(user.encrypted_alpaca_secret)

    if not api_key or not api_secret:
        logger.error(f"[TRADE_REJECTED] User '{user.username}' has no API credentials.")
        return jsonify({"error": "Alpaca credentials not configured"}), 500

    api = tradeapi.REST(api_key, api_secret, BASE_URL, api_version='v2')
    symbol = payload.get("symbol")
    action = payload.get("action", "")
    amount = float(payload.get("amount", user.per_trade_amount))

    if not symbol or not action:
        return jsonify({"error": "Invalid webhook format"}), 400

    api_symbol = symbol.replace('/', '')

    if is_close_action(action):
        try:
            position_to_close = api.get_position(api_symbol)
            close_order = api.close_position(api_symbol)
            logger.info(f"[TRADE_EXECUTED] User '{user.username}' CLOSE order {close_order.id} for {api_symbol}")
            notification_payload = { "result": "closed", "symbol": symbol, "close_order_id": close_order.id, "payload": payload, "user_id": user.id,
                                     "position_obj": { 'avg_entry_price': position_to_close.avg_entry_price, 'qty': position_to_close.qty, 'side': position_to_close.side, 'asset_id': position_to_close.asset_id } }
            requests.post(f"{DASHBOARD_INTERNAL_URL}/api/internal/record_trade", json=notification_payload, headers={'X-Internal-API-Key': INTERNAL_API_KEY}, timeout=3)
            return jsonify({"result": "closed", "close_order_id": close_order.id}), 200
        except tradeapi.rest.APIError as e:
            if "position not found" in str(e).lower():
                logger.warning(f"[TRADE_INFO] User '{user.username}' tried to close {api_symbol}, but no position exists.")
                return jsonify({"result": "no_position_to_close", "symbol": symbol}), 200
            else:
                logger.error(f"[TRADE_FAIL] Error closing {api_symbol} for '{user.username}': {e}")
                return jsonify({"error": str(e)}), 500
        except Exception as e:
            logger.error(f"[TRADE_FAIL] A general error occurred while closing {api_symbol} for '{user.username}': {e}")
            return jsonify({"error": str(e)}), 500

    if action.lower() in ["buy", "sell"]:
        try:
            api.get_position(api_symbol)
            logger.warning(f"[TRADE_REJECTED] User '{user.username}' already has position for {api_symbol}.")
            return jsonify({"error": f"{api_symbol} position already open."}), 409
        except tradeapi.rest.APIError:
            pass
        except Exception as e:
            logger.error(f"[TRADE_FAIL] Could not verify position for '{user.username}' on {api_symbol}: {e}")
            return jsonify({"error": "Failed to verify existing position"}), 500

        try:
            last_price = get_last_price(api, symbol)
            if last_price == 0: return jsonify({"error": f"Could not fetch price for {symbol}."}), 400

            qty_to_log = 0.0

            if is_crypto(symbol):
                qty = amount / last_price
                qty_to_log = qty
                order_params = {'symbol': api_symbol, 'side': action.lower(), 'type': 'market', 'qty': qty, 'time_in_force': 'gtc'}
            else:
                if action.lower() == 'buy':
                    qty_to_log = amount / last_price
                    order_params = {'symbol': api_symbol, 'side': 'buy', 'type': 'market', 'notional': amount, 'time_in_force': 'day'}
                else: # Sell (Short)
                    qty = math.floor(amount / last_price)
                    qty_to_log = qty
                    if qty == 0:
                        logger.warning(f"[TRADE_REJECTED] Amount ${amount} too small to short {api_symbol}.")
                        return jsonify({"error": f"Amount ${amount} too small to short {api_symbol}."}), 400
                    order_params = {'symbol': api_symbol, 'side': 'sell', 'type': 'market', 'qty': qty, 'time_in_force': 'day'}

            order = api.submit_order(**order_params)
            logger.info(f"[TRADE_EXECUTED] User '{user.username}' OPEN order {order.id} for {api_symbol}")
            
            notification_payload = { 
                "result": "opened", "order_id": order.id, "symbol": symbol, "side": action.lower(), 
                "price": last_price, "payload": payload, "user_id": user.id, "qty": qty_to_log
            }
            requests.post(f"{DASHBOARD_INTERNAL_URL}/api/internal/record_trade", json=notification_payload, headers={'X-Internal-API-Key': INTERNAL_API_KEY}, timeout=3)
            return jsonify({"result": "opened", "order_id": order.id}), 200
        except Exception as e:
            logger.error(f"[TRADE_FAIL] Error opening {symbol} for '{user.username}': {e}")
            return jsonify({"error": str(e)}), 500

    logger.error(f"[UNKNOWN_ACTION] Received unknown action: {action}")
    return jsonify({"error": "Unknown action"}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
