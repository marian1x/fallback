# trade_db.py
from datetime import datetime
from models import db, Trade
import alpaca_trade_api as tradeapi
import os
from dotenv import load_dotenv
import time
import logging

load_dotenv()
API_KEY = os.getenv("ALPACA_KEY")
API_SECRET = os.getenv("ALPACA_SECRET")
BASE_URL = os.getenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")
api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')
load_dotenv()
API_KEY = os.getenv("ALPACA_KEY")
API_SECRET = os.getenv("ALPACA_SECRET")
BASE_URL = os.getenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")
api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')

logger = logging.getLogger(__name__)
def record_open_trade(data, payload):
    symbol = payload.get('symbol')
    existing = Trade.query.filter_by(symbol=symbol, status='open').first()
    if not existing:
        qty = float(data.get('qty', payload.get('qty', 0)) or 0)
        # Fetch live price from Alpaca after opening
        try:
            pos = api.get_position(symbol)
            open_price = float(pos.avg_entry_price)
        except Exception as e:
            logger.warning(f"Could not fetch open price for {symbol} from Alpaca: {e}")
            open_price = float(data.get('price', 0))
        t = Trade(
            trade_id = str(data.get('order_id', '')),
            symbol = symbol,
            side = data.get('side', payload.get('action','')),
            qty = qty,
            open_price = open_price,
            open_time = datetime.utcnow(),
            status = 'open',
            action = payload.get('action','')
        )
        db.session.add(t)
        db.session.commit()
        logger.info(f"DB: Open trade inserted: {symbol} at {open_price}")
        return t
    else:
        logger.warning(f"DB: Open trade for {symbol} already exists, skipping insert.")
    return None

def record_closed_trade(data, payload):
    symbol = payload.get('symbol')
    t = Trade.query.filter_by(symbol=symbol, status='open').order_by(Trade.open_time.desc()).first()
    logger.info(f"[DEBUG] Closing trade for {symbol}: found={bool(t)}")
    if t:
        entry_fill = None
        exit_fill = None
        for _ in range(8):
            acts = api.get_activities(activity_types='FILL', until=datetime.utcnow())
            fills = [a for a in acts if a.symbol == symbol and a.transaction_time.replace(tzinfo=None) >= t.open_time]
            if fills:
                if t.side == 'buy':
                    entry_fill = next((f for f in fills if f.side == 'buy'), None)
                    exit_fill = next((f for f in reversed(fills) if f.side == 'sell'), None)
                else:  # short
                    entry_fill = next((f for f in fills if f.side == 'sell'), None)
                    exit_fill = next((f for f in reversed(fills) if f.side == 'buy'), None)
                if entry_fill and exit_fill:
                    break
            time.sleep(1)
        t.open_price = float(entry_fill.price) if entry_fill else t.open_price
        t.close_price = float(exit_fill.price) if exit_fill else t.open_price
        t.open_time = entry_fill.transaction_time.replace(tzinfo=None) if entry_fill else t.open_time
        t.close_time = exit_fill.transaction_time.replace(tzinfo=None) if exit_fill else datetime.utcnow()
        t.status = 'closed'
        # ====== Fetch realized P/L from Alpaca at close ======
        try:
            # After closing, the position might be gone, so get from activities
            acts = api.get_activities(activity_types='FILL', until=datetime.utcnow())
            fills = [a for a in acts if a.symbol == symbol and a.transaction_time.replace(tzinfo=None) <= t.close_time]
            gross_amount = 0.0
            total_qty = 0.0
            for f in fills:
                q = float(f.qty)
                p = float(f.price)
                gross_amount += q * (p if f.side == 'sell' else -p)
                total_qty += q if f.side == t.side else 0
            t.profit_loss = round(gross_amount, 8)
            if t.open_price:
                t.profit_loss_pct = round((t.close_price - t.open_price) / t.open_price * 100, 6)
            else:
                t.profit_loss_pct = 0
            logger.info(f"DB: Realized P/L for {symbol} fetched from Alpaca activities: {t.profit_loss}")
        except Exception as ex:
            logger.error(f"Could not fetch realized P/L for {symbol} from Alpaca: {ex}")
            # fallback to own calculation
            if t.open_price and t.close_price:
                if t.side == 'buy':
                    t.profit_loss = round((t.close_price - t.open_price) * t.qty, 8)
                else:
                    t.profit_loss = round((t.open_price - t.close_price) * t.qty, 8)
            else:
                t.profit_loss = 0
            t.profit_loss_pct = 0
        t.action = payload.get('action','')
        db.session.commit()
        logger.info(f"DB: Closed trade updated for {symbol}: entry {t.open_price}@{t.open_time}, exit {t.close_price}@{t.close_time}, P/L={t.profit_loss}")
        return t
    else:
        logger.warning(f"DB: No open trade to close for {symbol}.")
    return None

