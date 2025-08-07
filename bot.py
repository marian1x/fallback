#!/usr/bin/env python3
import os
import logging
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
MIN_CRYPTO_ORDER_SIZE_USD = 10.0 # New variable for minimum crypto order size

# Minimums for crypto (as per Alpaca, adjust as needed)
CRYPTO_MIN_QTY = {
    "BTCUSD": 0.0001,
    "ETHUSD": 0.001,
    "DOGEUSD": 10,
    "SOLUSD": 0.01,
    "PEPEUSD": 1000
}
DEFAULT_CRYPTO_MIN = 0.001

# Logging
logging.basicConfig(filename="trades.log", level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger()

app = Flask(__name__)
api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')

# Action keywords that mean "close this position"
CLOSE_ACTIONS = [
    "trailing exit long", "trailing exit short", "close long", "close short", "close"
]

def is_close_action(act):
    return act and act.strip().lower() in CLOSE_ACTIONS

def is_crypto(symbol):
    return symbol and (symbol.upper().endswith('USD') or '/' in symbol)

def get_open_symbols():
    try:
        return set(p.symbol for p in api.list_positions())
    except Exception as e:
        logger.error(f"Failed to fetch open positions: {e}")
        return set()

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

def get_crypto_min_qty(symbol):
    api_symbol = symbol.replace('/', '')
    return CRYPTO_MIN_QTY.get(api_symbol.upper(), DEFAULT_CRYPTO_MIN)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    logger.info(f"Webhook: {data}")

    symbol = data.get("symbol")
    action = data.get("action", "")
    user   = data.get("user", "Unknown")
    price  = float(data.get("price") or 0)
    # Get amount from webhook, fallback to .env TRADE_NOTIONAL
    amount = float(data.get("amount", TRADE_NOTIONAL))

    if not symbol or not action:
        logger.error(f"Invalid webhook data: {data}")
        return jsonify({"error": "Invalid webhook"}), 400

    api_symbol = symbol.replace('/', '')

    # =========== CLOSE LOGIC ===========
    if is_close_action(action):
        try:
            # This returns the order object used to close the position
            close_order = api.close_position(api_symbol)
            logger.info(f"[CLOSE] Submitted close order {close_order.id} for {api_symbol}")
            return jsonify({
                "result": "closed",
                "symbol": symbol,
                "close_order_id": close_order.id # Return the specific ID of the close order
            }), 200
        except Exception as e:
            if "position not found" in str(e):
                logger.warning(f"Attempted to close {api_symbol}, but no position exists.")
                return jsonify({"result": "no_position_to_close", "symbol": symbol}), 200
            logger.error(f"Error closing {api_symbol}: {e}")
            return jsonify({"error": str(e)}), 500

    open_symbols = get_open_symbols()
    
    if action.lower() in ["buy", "sell"]:
        if api_symbol in open_symbols:
            logger.warning(f"Trade rejected: {api_symbol} position already open.")
            return jsonify({"error": f"{api_symbol} position already open."}), 409
        
        try:
            last_price = get_last_price(symbol)
            if last_price == 0:
                return jsonify({"error": f"Could not fetch a valid price for {symbol}."}), 400

            # Universal order submission logic
            if is_crypto(symbol):
                # Enforce minimum notional value for crypto
                if amount < MIN_CRYPTO_ORDER_SIZE_USD:
                    msg = f"Crypto order for {symbol} rejected. Amount ${amount:.2f} is less than the minimum of ${MIN_CRYPTO_ORDER_SIZE_USD:.2f}."
                    logger.warning(msg)
                    return jsonify({"error": msg}), 400
                
                min_qty = get_crypto_min_qty(symbol)
                raw_qty = amount / last_price
                qty = math.floor(raw_qty / min_qty) * min_qty
                qty = round(qty, 8)
                if qty < min_qty: qty = min_qty
                order_params = {'symbol': api_symbol, 'side': action.lower(), 'type': 'market', 'qty': qty, 'time_in_force': 'gtc'}
            else: # Stocks
                order_params = {'symbol': api_symbol, 'side': action.lower(), 'type': 'market', 'notional': amount, 'time_in_force': 'day'}

            order = api.submit_order(**order_params)
            # Use filled_qty if available, otherwise the requested qty
            final_qty = order.filled_qty if hasattr(order, 'filled_qty') and order.filled_qty else order_params.get('qty', 0)
            
            logger.info(f"Opened {action.upper()} {api_symbol} ({final_qty} units) (order id={order.id})")
            return jsonify({
                "result": "opened" if action.lower() == "buy" else "opened_short",
                "order_id": order.id,
                "symbol": symbol,
                "side": action.lower(),
                "price": price,
                "user": user,
                "action": action,
                "qty": final_qty
            }), 200

        except Exception as e:
            logger.error(f"Error handling {symbol} {action.upper()}: {e}")
            return jsonify({"error": str(e)}), 500

    logger.error(f"Unknown action: {action}")
    return jsonify({"error": "Unknown action"}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)