#!/usr/bin/env python3
import os
import logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import alpaca_trade_api as tradeapi
import math

# Load .env
load_dotenv()
API_KEY = os.getenv("ALPACA_KEY")
API_SECRET = os.getenv("ALPACA_SECRET")
BASE_URL = os.getenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")
PORT = int(os.getenv("BOT_PORT", 5000))
TRADE_NOTIONAL = float(os.getenv("PER_TRADE_AMOUNT", "2000"))
DASHBOARD_INTERNAL_URL = os.getenv("DASHBOARD_INTERNAL_URL", "http://127.0.0.1:5050")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "your-very-secret-internal-key")


# Logging
logging.basicConfig(filename="trades.log", level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger()

app = Flask(__name__)
api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')

# Action keywords that mean "close this position" - EXPANDED LIST
CLOSE_ACTIONS = [
    "trailing exit long", "trailing exit short",
    "fixed stop loss (long)", "fixed stop loss (short)",
    "fixed take profit (long)", "fixed take profit (short)",
    "forced sl long", "forced sl short", "forced tp long", "forced tp short",
    "close long", "close short",
    "close"
]

def is_close_action(act):
    return act and act.strip().lower() in CLOSE_ACTIONS

def is_crypto(symbol):
    return symbol and (symbol.upper().endswith('USD') or '/' in symbol)

def get_last_price(symbol):
    try:
        api_symbol = symbol.replace('/', '')
        if is_crypto(symbol):
            trade = api.get_latest_crypto_trade(api_symbol, "CBSE")
            return float(trade.p)
        else:
            trade = api.get_latest_trade(api_symbol)
            return float(trade.price)
    except Exception as e:
        logger.error(f"Failed to get price for {symbol}: {e}")
        return 0.0

@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(force=True)
    logger.info(f"Webhook: {payload}")

    symbol = payload.get("symbol")
    action = payload.get("action", "")
    user = payload.get("user", "Unknown")
    amount = float(payload.get("amount", TRADE_NOTIONAL))

    if not symbol or not action:
        return jsonify({"error": "Invalid webhook"}), 400

    api_symbol = symbol.replace('/', '')

    # =========== CLOSE LOGIC ===========
    if is_close_action(action):
        position_to_close = None
        try:
            position_to_close = api.get_position(api_symbol)
        except Exception:
            logger.warning(f"Attempted to close {api_symbol}, but no position exists on Alpaca.")
            return jsonify({"result": "no_position_to_close", "symbol": symbol}), 200
        
        try:
            close_order = api.close_position(api_symbol)
            logger.info(f"[CLOSE] Submitted close order {close_order.id} for {api_symbol}")
            
            # --- Notify Dashboard to Record the Close ---
            if user != "Dashboard":
                try:
                    notification_payload = {
                        "result": "closed",
                        "symbol": symbol,
                        "close_order_id": close_order.id,
                        "payload": payload,
                        "position_obj": {
                            'avg_entry_price': position_to_close.avg_entry_price,
                            'qty': position_to_close.qty,
                            'side': position_to_close.side,
                            'asset_id': position_to_close.asset_id
                        }
                    }
                    headers = {'X-Internal-API-Key': INTERNAL_API_KEY}
                    requests.post(
                        f"{DASHBOARD_INTERNAL_URL}/api/internal/record_close",
                        json=notification_payload,
                        headers=headers,
                        timeout=3 
                    )
                    logger.info(f"Sent close notification to dashboard for order {close_order.id}")
                except requests.exceptions.ReadTimeout:
                    logger.info("Dashboard accepted the close notification (timeout is expected).")
                except Exception as e:
                    logger.error(f"Failed to send close notification to dashboard: {e}")
            # --- End Notification ---

            return jsonify({"result": "closed", "symbol": symbol, "close_order_id": close_order.id}), 200
        except Exception as e:
            logger.error(f"Error closing {api_symbol}: {e}")
            return jsonify({"error": str(e)}), 500

    # =========== OPEN LOGIC ===========
    if action.lower() in ["buy", "sell"]:
        try:
            api.get_position(api_symbol)
            logger.warning(f"Trade rejected: {api_symbol} position already open.")
            return jsonify({"error": f"{api_symbol} position already open."}), 409
        except Exception:
             # Position does not exist, so we can proceed
            pass

        try:
            last_price = get_last_price(symbol)
            if last_price == 0:
                return jsonify({"error": f"Could not fetch a valid price for {symbol}."}), 400

            if is_crypto(symbol):
                qty = amount / last_price
                order_params = {'symbol': api_symbol, 'side': action.lower(), 'type': 'market', 'qty': qty, 'time_in_force': 'gtc'}
            else: # Stocks
                if action.lower() == 'buy':
                    order_params = {'symbol': api_symbol, 'side': 'buy', 'type': 'market', 'notional': amount, 'time_in_force': 'day'}
                else: # Sell (Short) - MUST use qty for shorting stocks
                    qty = math.floor(amount / last_price)
                    if qty == 0:
                        return jsonify({"error": f"Amount ${amount} is too small to short 1 share of {api_symbol} at ${last_price}."}), 400
                    order_params = {'symbol': api_symbol, 'side': 'sell', 'type': 'market', 'qty': qty, 'time_in_force': 'day'}

            order = api.submit_order(**order_params)
            final_qty = order.filled_qty if hasattr(order, 'filled_qty') and order.filled_qty else order_params.get('qty')
            
            return jsonify({
                "result": "opened" if action.lower() == "buy" else "opened_short",
                "order_id": order.id,
                "symbol": symbol,
                "side": action.lower(),
                "qty": final_qty,
                "price": last_price
            }), 200
        except Exception as e:
            logger.error(f"Error handling {symbol} {action.upper()}: {e}")
            return jsonify({"error": str(e)}), 500

    logger.error(f"Unknown action: {action}")
    return jsonify({"error": "Unknown action"}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)