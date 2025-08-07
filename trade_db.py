# trade_db.py
from datetime import datetime, timedelta, timezone
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

logger = logging.getLogger(__name__)

def record_open_trade(data, payload):
    symbol = payload.get('symbol')
    # Check if we already have an open trade for this symbol to avoid duplicates
    existing = Trade.query.filter_by(symbol=symbol, status='open').first()
    if not existing:
        qty = float(data.get('qty', payload.get('qty', 0)) or 0)
        open_price = float(data.get('price', 0))

        # We create the trade record immediately with the webhook data.
        # It will be updated with exact fill data upon closing.
        t = Trade(
            trade_id=str(data.get('order_id', '')),
            symbol=symbol,
            side=data.get('side', payload.get('action', '')),
            qty=qty,
            open_price=open_price,
            open_time=datetime.now(timezone.utc),
            status='open',
            action=payload.get('action', '')
        )
        db.session.add(t)
        db.session.commit()
        logger.info(f"DB: Open trade recorded for {symbol} at ~{open_price}")
        return t
    else:
        logger.warning(f"DB: Open trade for {symbol} already exists, skipping insert.")
    return None


def record_closed_trade(data, payload, position_obj):
    symbol = payload.get('symbol')
    
    if not position_obj:
        logger.error(f"Cannot record closed trade for {symbol}: No pre-close position data available from Alpaca.")
        # Mark any open trades as closed with an error status
        open_trades = Trade.query.filter_by(symbol=symbol, status='open').all()
        if not open_trades:
            return None
        for t in open_trades:
            t.status = 'closed_with_error'
            t.action = 'Close signal received but position not found on Alpaca before closing.'
            t.close_time = datetime.now(timezone.utc)
        db.session.commit()
        return None

    # Get data from the position object captured BEFORE closing
    avg_entry_price = float(position_obj.avg_entry_price)
    total_qty = abs(float(position_obj.qty))
    position_side = position_obj.side # 'long' or 'short'

    exit_fill = None
    # Retry to get the exit fill from activities
    for _ in range(15):
        until_timestamp = (datetime.now(timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        activities = api.get_activities(activity_types='FILL', until=until_timestamp, direction='desc')
        symbol_fills = [a for a in activities if a.symbol == symbol]
        
        exit_side = 'sell' if position_side == 'long' else 'buy'
        
        exit_fill = next((f for f in symbol_fills if f.side == exit_side), None)
        if exit_fill:
            logger.info(f"Found exit fill for {symbol} at price {exit_fill.price}")
            break
        logger.warning(f"Waiting for exit fill for {symbol}... Retrying.")
        time.sleep(1)

    if not exit_fill:
        logger.error(f"Could not find exit fill for closed position {symbol}. Aborting DB update.")
        return None

    close_price = float(exit_fill.price)
    close_time = exit_fill.transaction_time

    # Find ALL open trades for this symbol in our DB and consolidate them
    open_trades_in_db = Trade.query.filter_by(symbol=symbol, status='open').all()
    if not open_trades_in_db:
        logger.warning(f"A close was recorded for {symbol}, but no open trades found in our DB. Creating one from scratch.")
        # This case is unlikely but handled for robustness
        open_trades_in_db.append(
            Trade(trade_id=f"missing_{position_obj.asset_id}", open_time=close_time - timedelta(minutes=1))
        )

    # Use the earliest open time for the consolidated record
    first_open_time = min(t.open_time for t in open_trades_in_db)
    
    # Delete the old, now-redundant open trade records
    for trade in open_trades_in_db:
        db.session.delete(trade)
    
    # Calculate final P/L
    pl = (close_price - avg_entry_price) * total_qty if position_side == 'long' else (avg_entry_price - close_price) * total_qty
    pl_pct = (pl / (avg_entry_price * total_qty)) * 100 if avg_entry_price > 0 and total_qty > 0 else 0

    # Create the single, consolidated, and accurate closed trade record
    final_closed_trade = Trade(
        trade_id=open_trades_in_db[0].trade_id, # Reuse ID from the first one
        symbol=symbol,
        side='buy' if position_side == 'long' else 'sell',
        qty=total_qty,
        open_price=avg_entry_price,
        open_time=first_open_time,
        status='closed',
        close_price=close_price,
        close_time=close_time,
        profit_loss=pl,
        profit_loss_pct=pl_pct,
        action=payload.get('action', '')
    )
    db.session.add(final_closed_trade)
    db.session.commit()
    logger.info(f"DB: Consolidated and closed position for {symbol} with P/L={pl}")
    return final_closed_trade