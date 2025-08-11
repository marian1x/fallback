#!/usr/bin/env python3
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
    symbol = payload.get('symbol').replace('/', '') # Normalize symbol
    existing = Trade.query.filter_by(symbol=symbol, status='open').first()
    if not existing:
        qty = float(data.get('qty', 0))
        open_price = float(data.get('price', 0))

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
    symbol = payload.get('symbol').replace('/', '') # Normalize symbol
    
    if not position_obj:
        logger.error(f"Cannot record closed trade for {symbol}: No pre-close position data available.")
        Trade.query.filter_by(symbol=symbol, status='open').delete()
        db.session.commit()
        logger.info(f"DB: Cleaned up orphaned open trades for {symbol}.")
        return None

    close_order_id = data.get('close_order_id')
    if not close_order_id:
        logger.error(f"Cannot record closed trade for {symbol}: No close_order_id was provided.")
        return None

    # Get data from the position object captured BEFORE closing
    avg_entry_price = float(position_obj.avg_entry_price)
    total_qty = abs(float(position_obj.qty))
    position_side = position_obj.side

    exit_order = None
    # Retry to get the filled close order from Alpaca
    for i in range(15):
        try:
            exit_order = api.get_order(close_order_id)
            if exit_order.status == 'filled':
                logger.info(f"Close order {close_order_id} for {symbol} confirmed as filled.")
                break
            else:
                logger.warning(f"Waiting for close order {close_order_id} to fill... Status is '{exit_order.status}'. Attempt {i+1}/15")
                time.sleep(1)
        except Exception as e:
            logger.error(f"Error fetching close order {close_order_id}: {e}")
            time.sleep(1)

    if not exit_order or exit_order.status != 'filled':
        logger.error(f"Could not confirm fill for close order {close_order_id} for {symbol}. Aborting DB update.")
        return None

    close_price = float(exit_order.filled_avg_price)
    close_time = exit_order.filled_at

    # Find the most recent open trade for this symbol to update it.
    trade_to_update = Trade.query.filter_by(symbol=symbol, status='open').order_by(Trade.open_time.desc()).first()
    
    pl = (close_price - avg_entry_price) * total_qty if position_side == 'long' else (avg_entry_price - close_price) * total_qty
    pl_pct = (pl / (avg_entry_price * total_qty)) * 100 if avg_entry_price > 0 and total_qty > 0 else 0
    
    if not trade_to_update:
        logger.warning(f"A close was recorded for {symbol}, but no open trade was found in DB. Creating a new closed record from Alpaca data.")
        # Create a new record if none is found
        trade_to_update = Trade(
            trade_id=f"closed_{position_obj.asset_id}_{int(time.time())}",
            symbol=symbol,
            side='buy' if position_side == 'long' else 'sell',
            qty=total_qty,
            open_price=avg_entry_price,
            open_time=close_time - timedelta(minutes=5), # Approximate open time
            status='closed',
            close_price=close_price,
            close_time=close_time,
            profit_loss=pl,
            profit_loss_pct=pl_pct,
            action=payload.get('action', '')
        )
        db.session.add(trade_to_update)
    else:
        # Clean up any other (older) open trades for the same symbol
        other_open_trades = Trade.query.filter(Trade.symbol == symbol, Trade.status == 'open', Trade.id != trade_to_update.id).all()
        if other_open_trades:
            logger.warning(f"Found {len(other_open_trades)} older, orphaned open trades for {symbol}. Deleting them now.")
            for trade in other_open_trades:
                db.session.delete(trade)

        # Update the existing trade record
        trade_to_update.status = 'closed'
        trade_to_update.side = 'buy' if position_side == 'long' else 'sell'
        trade_to_update.qty = total_qty
        trade_to_update.open_price = avg_entry_price
        trade_to_update.close_price = close_price
        trade_to_update.close_time = close_time
        trade_to_update.profit_loss = pl
        trade_to_update.profit_loss_pct = pl_pct
        trade_to_update.action = payload.get('action', '')

    db.session.commit()
    logger.info(f"DB: Updated and closed position for {symbol} with P/L={pl}")
    return trade_to_update