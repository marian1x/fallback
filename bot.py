import os
import logging
from flask import Flask, request, jsonify
import alpaca_trade_api as tradeapi

API_KEY = os.getenv('ALPACA_KEY', 'PK0DBBQMMCVL0AN1ERMC')
API_SECRET = os.getenv('ALPACA_SECRET', 'qZkdh3gFJQwNxyxCFL17kwe95rj1GcVI6195stBc')
BASE_URL = 'https://paper-api.alpaca.markets'
TRADE_AMOUNT = 2000  # USD per trade

app = Flask(__name__)

logging.basicConfig(
    filename='trades.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s'
)

api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    symbol = data.get('symbol')
    action = data.get('action', '').lower()

    if not symbol or not action:
        logging.error('Missing symbol or action in data: %s', data)
        return jsonify({'error': 'Missing symbol or action'}), 400

    try:
        if action == 'close':
            api.close_position(symbol)
            logging.info('Closed position for %s', symbol)
        else:
            side = 'buy' if 'buy' in action else 'sell'
            order = api.submit_order(
                symbol=symbol,
                side=side,
                type='market',
                time_in_force='day',
                notional=TRADE_AMOUNT
            )
            logging.info('Submitted %s order for %s: %s', side, symbol, order.id)
    except Exception as e:
        logging.error('Trade error for %s: %s', symbol, e)
        return jsonify({'status': 'error', 'detail': str(e)}), 500

    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
