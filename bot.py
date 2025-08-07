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
TRADE_NOTIONAL = float(os.getenv("TRADE_NOTIONAL", "2000"))

# Minimums for crypto (as per Alpaca, adjust as needed)
CRYPTO_MIN_QTY = {
    "BTCUSD": 0.0001,
    "ETHUSD": 0.001,
    "DOGEUSD": 10,
    "SOLUSD": 0.01
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
    "trailing exit long", "trailing exit short",
    "forced sl long", "forced sl short", "forced tp long", "forced tp short",
    "fixed take profit (long)", "fixed take profit (short)",
    "fixed stop loss (long)", "fixed stop loss (short)",
    "forced tp (long)", "forced tp (short)",
    "forced sl (long)", "forced sl (short)",
    "fixed take profit long", "fixed take profit short",
    "fixed stop loss long", "fixed stop loss short",
    "close long", "close short",
    "close"
]

def is_close_action(act):
    return act and act.strip().lower() in CLOSE_ACTIONS

def is_crypto(symbol):
    return symbol and symbol.upper().endswith('USD') and symbol.upper() not in ['USD','USDT','USDC']

def get_open_symbols():
    try:
        return set(p.symbol for p in api.list_positions())
    except Exception as e:
        logger.error(f"Failed to fetch open positions: {e}")
        return set()

def get_last_price(symbol):
    try:
        trade = api.get_latest_trade(symbol)
        return float(trade.price)
    except Exception as e:
        logger.error(f"Failed to get price for {symbol}: {e}")
        return 0.0

def get_crypto_min_qty(symbol):
    return CRYPTO_MIN_QTY.get(symbol.upper(), DEFAULT_CRYPTO_MIN)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    logger.info(f"Webhook: {data}")

    symbol = data.get("symbol")
    action = data.get("action", "")
    user   = data.get("user", "Unknown")
    price  = float(data.get("price") or 0)

    if not symbol or not action:
        logger.error(f"Invalid webhook data: {data}")
        return jsonify({"error": "Invalid webhook"}), 400

    # =========== CLOSE LOGIC (WORKS FOR ALL EXIT/SL/TP SIGNALS) ===========
    if is_close_action(action):
        try:
            api.close_position(symbol)
            logger.info(f"[CLOSE] Flattened {symbol} for user {user} (action={action})")
            return jsonify({
                "result": "closed",
                "symbol": symbol,
                "user": user,
                "action": action
            }), 200
        except Exception as e:
            logger.error(f"Error closing {symbol}: {e}")
            return jsonify({"error": str(e)}), 500

    # =========== CRYPTO LOGIC ===========
    if is_crypto(symbol):
        open_symbols = get_open_symbols()
        if action.lower() == "buy":
            if symbol in open_symbols:
                logger.warning(f"[CRYPTO] Trade rejected: {symbol} already open.")
                return jsonify({"error": f"{symbol} position already open."}), 409
            try:
                last_price = get_last_price(symbol)
                min_qty = get_crypto_min_qty(symbol)
                qty = float(data.get("qty") or 0)
                if not qty:
                    if last_price > 0:
                        raw_qty = TRADE_NOTIONAL / last_price
                        qty = math.floor(raw_qty / min_qty) * min_qty
                        qty = round(qty, 8)
                    else:
                        qty = min_qty
                if qty < min_qty:
                    qty = min_qty
                order = api.submit_order(
                    symbol=symbol,
                    side="buy",
                    type="market",
                    qty=qty,
                    time_in_force="gtc"
                )
                logger.info(f"[CRYPTO] Opened BUY {symbol} ({qty} units at ~${last_price}) (order id={order.id})")
                return jsonify({
                    "result": "opened",
                    "order_id": order.id,
                    "symbol": symbol,
                    "side": "buy",
                    "price": price,
                    "user": user,
                    "action": action,
                    "qty": qty
                }), 200
            except Exception as e:
                logger.error(f"[CRYPTO] Error handling {symbol} BUY: {e}")
                return jsonify({"error": str(e)}), 500
        if action.lower() == "sell":
            open_symbols = get_open_symbols()
            if symbol in open_symbols:
                logger.warning(f"[CRYPTO] Trade rejected: {symbol} already open.")
                return jsonify({"error": f"{symbol} position already open."}), 409
            try:
                last_price = get_last_price(symbol)
                min_qty = get_crypto_min_qty(symbol)
                qty = float(data.get("qty") or 0)
                if not qty:
                    if last_price > 0:
                        raw_qty = TRADE_NOTIONAL / last_price
                        qty = math.floor(raw_qty / min_qty) * min_qty
                        qty = round(qty, 8)
                    else:
                        qty = min_qty
                if qty < min_qty:
                    qty = min_qty
                order = api.submit_order(
                    symbol=symbol,
                    side="sell",
                    type="market",
                    qty=qty,
                    time_in_force="gtc"
                )
                logger.info(f"[CRYPTO] Opened SHORT {symbol} ({qty} units at ~${last_price}) (order id={order.id})")
                return jsonify({
                    "result": "opened_short",
                    "order_id": order.id,
                    "symbol": symbol,
                    "side": "sell",
                    "price": price,
                    "user": user,
                    "action": action,
                    "qty": qty
                }), 200
            except Exception as e:
                logger.error(f"[CRYPTO] Error handling {symbol} SELL: {e}")
                return jsonify({"error": str(e)}), 500

    # =========== STOCK LOGIC ===========
    open_symbols = get_open_symbols()

    if action.lower() == "buy":
        if symbol in open_symbols:
            logger.warning(f"Trade rejected: {symbol} already open.")
            return jsonify({"error": f"{symbol} position already open."}), 409
        try:
            last_price = get_last_price(symbol)
            qty = int(TRADE_NOTIONAL // last_price) if last_price > 0 else 0
            if qty < 1:
                logger.warning(f"Not enough funds to buy 1 share of {symbol} at ${last_price}. Skipping.")
                return jsonify({"error": f"Not enough funds to buy 1 share of {symbol}."}), 400
            order = api.submit_order(
                symbol=symbol,
                side="buy",
                type="market",
                qty=qty,
                time_in_force="day"
            )
            logger.info(f"Opened BUY {symbol} ({qty} shares at ~${last_price}) (order id={order.id})")
            return jsonify({
                "result": "opened",
                "order_id": order.id,
                "symbol": symbol,
                "side": "buy",
                "price": price,
                "user": user,
                "action": action,
                "qty": qty
            }), 200
        except Exception as e:
            logger.error(f"Error handling {symbol} BUY: {e}")
            return jsonify({"error": str(e)}), 500

    if action.lower() == "sell":
        if symbol in open_symbols:
            logger.warning(f"Trade rejected: {symbol} already open.")
            return jsonify({"error": f"{symbol} position already open."}), 409
        try:
            last_price = get_last_price(symbol)
            qty = int(TRADE_NOTIONAL // last_price) if last_price > 0 else 0
            if qty < 1:
                logger.warning(f"Not enough notional to short 1 share of {symbol} at ${last_price}. Skipping.")
                return jsonify({"error": f"Not enough notional to short 1 share of {symbol}."}), 400
            order = api.submit_order(
                symbol=symbol,
                side="sell",
                type="market",
                qty=qty,
                time_in_force="day"
            )
            logger.info(f"Opened SHORT {symbol} ({qty} shares at ~${last_price}) (order id={order.id})")
            return jsonify({
                "result": "opened_short",
                "order_id": order.id,
                "symbol": symbol,
                "side": "sell",
                "user": user,
                "action": action,
                "qty": qty
            }), 200
        except Exception as e:
            logger.error(f"Error opening short {symbol}: {e}")
            return jsonify({"error": str(e)}), 500

    logger.error(f"Unknown action: {action}")
    return jsonify({"error": "Unknown action"}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
