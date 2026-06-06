#!/usr/bin/env python3
from datetime import datetime, timezone
from models import db, Trade, User
import os
from dotenv import load_dotenv
import time
import logging
from sqlalchemy import inspect, text
from alpaca_api import AlpacaAPIError, LegacyCompatibleAlpacaClient
from utils import decrypt_data

ENV_PATH = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(ENV_PATH)
BASE_URL = os.getenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")

logger = logging.getLogger(__name__)


def ensure_trade_table_columns():
    try:
        inspector = inspect(db.engine)
        columns = {col["name"] for col in inspector.get_columns("trade")}
        changed = False
        if "strategy" not in columns:
            db.session.execute(text("ALTER TABLE trade ADD COLUMN strategy VARCHAR"))
            changed = True
        if "strategy_job_id" not in columns:
            db.session.execute(text("ALTER TABLE trade ADD COLUMN strategy_job_id VARCHAR"))
            changed = True
        if changed:
            db.session.commit()
    except Exception as e:
        logger.warning("[DATABASE] Could not ensure trade metadata columns: %s", e)

def parse_datetime_utc(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value and value.lower() != "none":
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            dt = None
    else:
        dt = None
    if dt and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def is_crypto(symbol):
    return symbol and (symbol.upper().endswith('USD') or '/' in symbol)

def get_latest_price(api, symbol):
    try:
        api_symbol = symbol.replace('/', '')
        if is_crypto(symbol):
            trade = api.get_latest_crypto_trade(api_symbol, "CBSE")
            return float(trade.p)
        trade = api.get_latest_trade(api_symbol)
        return float(trade.price)
    except Exception as e:
        logger.error(f"[PRICE_FETCH_FAIL] Failed to get price for {symbol}: {e}")
        return None

def get_api_for_user(user_id):
    """Initializes and returns an Alpaca API client for a specific user."""
    user = db.session.get(User, user_id)
    if not user:
        raise ValueError(f"User with ID {user_id} not found.")
    
    api_key = decrypt_data(user.encrypted_alpaca_key)
    api_secret = decrypt_data(user.encrypted_alpaca_secret)
    
    if not api_key or not api_secret:
        raise ValueError(f"Alpaca credentials not configured for user {user.username}.")
        
    return LegacyCompatibleAlpacaClient(api_key, api_secret, BASE_URL)

def record_open_trade(data, payload, user_id):
    ensure_trade_table_columns()
    symbol = payload.get('symbol', '').replace('/', '')
    if not symbol:
        logger.error("[DATABASE] Attempted to record an open trade with no symbol.")
        return None
        
    existing = Trade.query.filter_by(symbol=symbol, status='open', user_id=user_id).first()
    if not existing:
        qty_raw = data.get('qty')
        price_raw = data.get('price')

        if qty_raw is None or price_raw is None:
            logger.error(f"[DATABASE] Cannot record open trade for {symbol}: qty or price is missing. Data: {data}")
            return None

        try:
            qty = float(qty_raw)
            open_price = float(price_raw)
        except (ValueError, TypeError) as e:
            logger.error(f"[DATABASE] Invalid data type for qty or price for {symbol}. Error: {e}. Data: {data}")
            return None

        t = Trade(
            user_id=user_id,
            trade_id=str(data.get('order_id', '')),
            symbol=symbol,
            side=data.get('side', payload.get('action', '')),
            qty=qty,
            open_price=open_price,
            open_time=parse_datetime_utc(data.get('open_time')) or datetime.now(timezone.utc),
            status='open',
            action=payload.get('action', ''),
            strategy=str(payload.get('strategy', '') or '').strip().lower() or None,
            strategy_job_id=str(payload.get('strategy_job_id', '') or '').strip() or None,
        )
        db.session.add(t)
        db.session.commit()
        logger.info(f"[DATABASE] Open trade recorded for user_id={user_id}, symbol={symbol}, qty={qty}")
        return t
    else:
        logger.warning(f"[DATABASE] Open trade for symbol={symbol}, user_id={user_id} already exists, skipping insert.")
    return None

def record_closed_trade(data, payload, user_id, position_obj):
    ensure_trade_table_columns()
    symbol = payload.get('symbol', '').replace('/', '')
    if not symbol:
        logger.error("[DATABASE] Attempted to record a closed trade with no symbol.")
        return None
        
    if not position_obj:
        logger.error(f"[DATABASE] Cannot record closed trade for {symbol}, user_id={user_id}: No pre-close position data available.")
        Trade.query.filter_by(symbol=symbol, status='open', user_id=user_id).delete()
        db.session.commit()
        logger.info(f"[DATABASE] Cleaned up orphaned open trades for {symbol}, user_id={user_id}.")
        return None

    try:
        api = get_api_for_user(user_id)
    except ValueError as e:
        logger.error(f"[API_FAIL] Failed to get API client for user {user_id}: {e}")
        return None
        
    avg_entry_price = float(position_obj.avg_entry_price)
    total_qty = abs(float(position_obj.qty))
    side_raw = str(position_obj.side).lower() if position_obj.side is not None else ""
    if side_raw in ("long", "buy"):
        position_side = "long"
    elif side_raw in ("short", "sell"):
        position_side = "short"
    else:
        position_side = "long"
    api_symbol = symbol.replace('/', '')

    close_price = None
    close_time = None
    close_price_override = data.get('close_price')
    close_time_override = data.get('close_time')

    def poll_order_fill(order_id):
        if not order_id:
            return None, None
        terminal_statuses = {'filled', 'partially_filled', 'canceled', 'rejected', 'expired'}
        exit_order = None
        for i in range(30):
            try:
                exit_order = api.get_order(order_id)
                status = (exit_order.status or '').lower()
                if status in terminal_statuses:
                    logger.info(f"Close order {order_id} for {symbol} confirmed as '{exit_order.status}'.")
                    break
                logger.warning(f"Waiting for close order {order_id} to fill... Status: '{exit_order.status}'. Attempt {i+1}/30")
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error fetching close order {order_id}: {e}")
                time.sleep(1)
        if exit_order and exit_order.status in ['filled', 'partially_filled']:
            fill_price = float(exit_order.filled_avg_price) if exit_order.filled_avg_price else None
            return fill_price, parse_datetime_utc(exit_order.filled_at)
        return None, None

    def position_is_still_open():
        try:
            api.get_position(api_symbol)
            return True
        except AlpacaAPIError as e:
            if "position not found" in str(e).lower():
                return False
            raise

    if close_price_override is not None:
        try:
            close_price = float(close_price_override)
        except (TypeError, ValueError):
            close_price = None
        close_time = parse_datetime_utc(close_time_override) or datetime.now(timezone.utc)
        close_order_id = data.get('close_order_id')
        close_price_source = str(data.get('close_price_source', '') or '').lower()
        close_price_authoritative = bool(data.get('close_price_authoritative', True))
        if close_order_id and (not close_price_authoritative or close_price_source.endswith('fallback')):
            polled_price, polled_time = poll_order_fill(close_order_id)
            if polled_price is not None:
                close_price = polled_price
                close_time = polled_time or close_time
                logger.info(f"[DATABASE] Replaced fallback close price for {symbol}, user_id={user_id}, using Alpaca fill for order {close_order_id}.")
            else:
                try:
                    if position_is_still_open():
                        logger.warning(
                            f"[DATABASE] Close order {close_order_id} for {symbol} has no authoritative fill yet "
                            "and the Alpaca position is still open. Skipping close record."
                        )
                        return None
                except Exception as e:
                    logger.error(f"[DATABASE] Error checking position status for {symbol}, user_id={user_id}: {e}")
                    return None
        if close_price is None:
            close_price = get_latest_price(api, symbol)
        if close_price is None:
            logger.warning(f"[DATABASE] Using entry price as fallback close price for {symbol}, user_id={user_id}.")
            close_price = avg_entry_price
    else:
        close_order_id = data.get('close_order_id')
        if not close_order_id:
            logger.error(f"[DATABASE] Cannot record closed trade for {symbol}: No close_order_id was provided.")
            return None

        close_price, close_time = poll_order_fill(close_order_id)
        if close_price is not None:
            pass
        else:
            try:
                exit_order = api.get_order(close_order_id)
                exit_status = (exit_order.status if exit_order else 'unknown')
            except Exception:
                exit_status = 'unknown'
            logger.warning(
                f"[DATABASE] Close order {close_order_id} for {symbol} not filled (status='{exit_status}'). "
                "Checking position status."
            )
            try:
                if position_is_still_open():
                    logger.warning(f"[DATABASE] Position still open for {symbol}, user_id={user_id}. Skipping close record.")
                    return None
            except AlpacaAPIError as e:
                logger.error(f"[DATABASE] Error checking position status for {symbol}, user_id={user_id}: {e}")
                return None
            except Exception as e:
                logger.error(f"[DATABASE] Error checking position status for {symbol}, user_id={user_id}: {e}")
                return None
            close_price = get_latest_price(api, symbol)
            close_time = datetime.now(timezone.utc)
            if close_price is None:
                logger.warning(f"[DATABASE] Using entry price as fallback close price for {symbol}, user_id={user_id}.")
                close_price = avg_entry_price

        if close_time is None:
            close_time = datetime.now(timezone.utc)
        if close_price is None:
            close_price = avg_entry_price

    trade_to_update = Trade.query.filter_by(symbol=symbol, status='open', user_id=user_id).order_by(Trade.open_time.desc()).first()
    
    if not trade_to_update:
        logger.warning(
            f"[DATABASE] Close order recorded for {symbol}, user_id={user_id}, but no matching open trade "
            "exists in the DB. Skipping synthetic closed row."
        )
        return None

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
    trade_to_update.strategy = (
        str(payload.get('strategy', '') or '').strip().lower()
        or trade_to_update.strategy
        or None
    )
    trade_to_update.strategy_job_id = (
        str(payload.get('strategy_job_id', '') or '').strip()
        or trade_to_update.strategy_job_id
        or None
    )

    db.session.commit()
    logger.info(f"[DATABASE] Closed position for user_id={user_id}, symbol={symbol} with P/L={pl:.2f}")
    return trade_to_update
