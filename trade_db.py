#!/usr/bin/env python3
from datetime import datetime, timedelta, timezone
from models import db, Trade, User
import alpaca_trade_api as tradeapi
import os
from dotenv import load_dotenv
import time
import logging
from utils import decrypt_data

load_dotenv()
BASE_URL = os.getenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")

logger = logging.getLogger(__name__)

def get_api_for_user(user_id):
    """Initializes and returns an Alpaca API client for a specific user."""
    user = User.query.get(user_id)
    if not user:
        raise ValueError(f"User with ID {user_id} not found.")
    
    api_key = decrypt_data(user.encrypted_alpaca_key)
    api_secret = decrypt_data(user.encrypted_alpaca_secret)
    
    if not api_key or not api_secret:
        raise ValueError(f"Alpaca credentials not configured for user {user.username}.")
        
    return tradeapi.REST(api_key, api_secret, BASE_URL, api_version='v2')

def record_open_trade(data, payload, user_id):
    symbol = payload.get('symbol').replace('/', '')
    
    # Check if this user already has an open trade for this symbol
    existing = Trade.query.filter_by(symbol=symbol, status='open', user_id=user_id).first()
    if not existing:
        qty = float(data.get('qty', 0))
        open_price = float(data.get('price', 0))

        t = Trade(
            user_id=user_id,
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
        logger.info(f"DB: Open trade recorded for user_id={user_id}, symbol={symbol}")
        return t
    else:
        logger.warning(f"DB: Open trade for symbol={symbol}, user_id={user_id} already exists.")
    return None

def record_closed_trade(data, payload, user_id, position_obj):
    symbol = payload.get('symbol').replace('/', '')
    
    if not position_obj:
        logger.error(f"Cannot record closed trade for {symbol}, user_id={user_id}: No pre-close position data.")
        # Clean up any potential orphaned trades for this user/symbol
        Trade.query.filter_by(symbol=symbol, status='open', user_id=user_id).delete()
        db.session.commit()
        logger.info(f"DB: Cleaned up orphaned open trades for {symbol}, user_id={user_id}.")
        return None

    close_order_id = data.get('close_order_id')
    if not close_order_id:
        logger.error(f"Cannot record closed trade for {symbol}: No close_order_id.")
        return None

    try:
        api = get_api_for_user(user_id)
    except ValueError as e:
        logger.error(f"Failed to get API client for user {user_id}: {e}")
        return None
        
    avg_entry_price = float(position_obj.avg_entry_price)
    total_qty = abs(float(position_obj.qty))
    position_side = position_obj.side

    exit_order = None
    for i in range(15): # Retry for up to ~15 seconds
        try:
            exit_order = api.get_order(close_order_id)
            if exit_order.status in ['filled', 'partially_filled']:
                logger.info(f"Close order {close_order_id} confirmed as '{exit_order.status}'.")
                break
            logger.warning(f"Waiting for close order {close_order_id} to fill... Status is '{exit_order.status}'. Attempt {i+1}/15")
            time.sleep(1)
        except Exception as e:
            logger.error(f"Error fetching close order {close_order_id}: {e}")
            time.sleep(1)

    if not exit_order or exit_order.status not in ['filled', 'partially_filled']:
        logger.error(f"Could not confirm fill for close order {close_order_id} for {symbol}. Aborting DB update.")
        return None

    close_price = float(exit_order.filled_avg_price)
    close_time = exit_order.filled_at

    trade_to_update = Trade.query.filter_by(symbol=symbol, status='open', user_id=user_id).order_by(Trade.open_time.desc()).first()
    
    if not trade_to_update:
        logger.warning(f"Close recorded for {symbol}, user_id={user_id}, but no open trade found. Creating new closed record.")
        trade_to_update = Trade(
            user_id=user_id,
            trade_id=f"closed_{position_obj.asset_id}_{int(time.time())}",
            symbol=symbol,
            open_time=close_time - timedelta(minutes=5) # Fallback open time
        )
        db.session.add(trade_to_update)

    pl = (close_price - avg_entry_price) * total_qty if position_side == 'long' else (avg_entry_price - close_price) * total_qty
    pl_pct = (pl / (avg_entry_price * total_qty)) * 100 if avg_entry_price > 0 and total_qty > 0 else 0

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
    logger.info(f"DB: Closed position for user_id={user_id}, symbol={symbol} with P/L={pl:.2f}")
    return trade_to_update