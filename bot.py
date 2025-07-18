import os
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, request, jsonify
import requests
import math
import alpaca_trade_api as tradeapi

# —––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# Configuration
# —––––––––––––––––––––––––––––––––––––––––––––––––––––––––

API_KEY      = os.getenv('ALPACA_KEY',    'PK0DBBQMMCVL0AN1ERMC')
API_SECRET   = os.getenv('ALPACA_SECRET', 'qZkdh3gFJQwNxyxCFL17kwe95rj1GcVI6195stBc')
BASE_URL     = 'https://paper-api.alpaca.markets'
TRADE_AMOUNT = 2000  # USD per trade

# Comma-separated list of additional ports to mirror incoming webhooks to:
# e.g. MIRROR_PORTS="5001,5002"
MIRROR_PORTS = os.getenv('MIRROR_PORTS', '')
mirror_ports = [p.strip() for p in MIRROR_PORTS.split(',') if p.strip()]

# Recognized exit-strategy messages from your Pine script (lowercased):
EXIT_ACTIONS = {
    'fixed stop loss (long)',
    'fixed take profit (long)',
    'forced sl (long)',
    'forced tp (long)',
    'trailing exit (long)',
    'trailing exit long',
    'fixed stop loss (short)',
    'fixed take profit (short)',
    'forced sl (short)',
    'forced tp (short)',
    'trailing exit (short)',
    'trailing exit short'
}

# —––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# Flask app & logging
# —––––––––––––––––––––––––––––––––––––––––––––––––––––––––

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# Rotating file for trade events
trade_handler = RotatingFileHandler('trades.log', maxBytes=10*1024*1024, backupCount=5)
trade_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
app.logger.addHandler(trade_handler)

# Simple file for raw/external logging
external_handler = logging.FileHandler('external.log')
external_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
app.logger.addHandler(external_handler)

# —––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# Alpaca client
# —––––––––––––––––––––––––––––––––––––––––––––––––––––––––

api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')

# —––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# Webhook endpoint
# —––––––––––––––––––––––––––––––––––––––––––––––––––––––––

@app.route('/webhook', methods=['POST'])
def webhook():
    data   = request.get_json(force=True)
    symbol = data.get('symbol')
    action = (data.get('action') or '').strip().lower()

    # Log raw incoming webhook
    app.logger.info("Raw webhook: %r", data)

    if not symbol or not action:
        msg = f"Missing symbol or action in payload: {data!r}"
        app.logger.error(msg)
        return jsonify({'error': 'Missing symbol or action'}), 400

    # 1) If it's an exit strategy message → close the position
    if action in EXIT_ACTIONS or action == 'close':
        try:
            api.close_position(symbol)
            app.logger.info("Exit action '%s' received → closed %s", action, symbol)
            result = {'trade': 'closed', 'exit_reason': action}
        except Exception as e:
            app.logger.error("Error closing %s on exit action '%s': %s", symbol, action, e)
            return jsonify({'status': 'error', 'detail': str(e)}), 500

        # Mirror exit webhook to other ports
        mirrors = []
        for port in mirror_ports:
            url = f'http://localhost:{port}/webhook'
            try:
                resp = requests.post(url, json=data, timeout=5)
                app.logger.info("Mirrored exit action to %s (status %s)", url, resp.status_code)
                mirrors.append({'url': url, 'status': resp.status_code})
            except Exception as me:
                app.logger.error("Error mirroring to %s: %s", url, me)
                mirrors.append({'url': url, 'error': str(me)})

        return jsonify({'status': 'ok', **result, 'mirrors': mirrors})

    # 2) Otherwise, handle as buy/sell order
    try:
        side = 'buy' if 'buy' in action else 'sell'

        if side == 'sell':
            # fetch last trade price
            latest = api.get_latest_trade(symbol)
            price  = latest.price
            qty    = int(math.floor(TRADE_AMOUNT / price))
            if qty < 1:
                msg = (
                    f"Not enough notional (${TRADE_AMOUNT}) to sell one share of "
                    f"{symbol} at ${price}"
                )
                app.logger.error(msg)
                return jsonify({'status': 'error', 'detail': msg}), 400

            order = api.submit_order(
                symbol=symbol,
                side='sell',
                type='market',
                time_in_force='day',
                qty=qty
            )
            app.logger.info(
                "Submitted SELL %s shares of %s (order_id=%s)",
                qty, symbol, order.id
            )
            result = {'trade': 'sell', 'order_id': order.id, 'qty': qty}

        else:  # buy
            order = api.submit_order(
                symbol=symbol,
                side='buy',
                type='market',
                time_in_force='day',
                notional=TRADE_AMOUNT
            )
            app.logger.info("Submitted BUY %s (order_id=%s)", symbol, order.id)
            result = {'trade': 'buy', 'order_id': order.id}

    except Exception as e:
        app.logger.error("Trade error for %s (%s): %s", symbol, action, e)
        return jsonify({'status': 'error', 'detail': str(e)}), 500

    # 3) Mirror the order webhook to other ports
    mirrors = []
    for port in mirror_ports:
        url = f'http://localhost:{port}/webhook'
        try:
            resp = requests.post(url, json=data, timeout=5)
            app.logger.info("Mirrored webhook to %s (status %s)", url, resp.status_code)
            mirrors.append({'url': url, 'status': resp.status_code})
        except Exception as me:
            app.logger.error("Error mirroring to %s: %s", url, me)
            mirrors.append({'url': url, 'error': str(me)})

    return jsonify({'status': 'ok', **result, 'mirrors': mirrors})


if __name__ == '__main__':
    # e.g. export MIRROR_PORTS="5001,5002"
    # Listen on port 5000 to match your Apache proxy
    app.run(host='0.0.0.0', port=5000)
