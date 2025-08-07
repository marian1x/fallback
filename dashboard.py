#!/usr/bin/env python3
import os
import logging
import threading
import time
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, render_template, jsonify,
    session, redirect, url_for, flash
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import alpaca_trade_api as tradeapi
import requests

from models import db, Trade, User
from trade_db import record_open_trade, record_closed_trade
from pytz import timezone, utc

load_dotenv()
API_KEY      = os.getenv("ALPACA_KEY")
API_SECRET   = os.getenv("ALPACA_SECRET")
BASE_URL     = os.getenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")
DB_URL       = os.getenv('DATABASE_URL', 'sqlite:///instance/app.db')
HOST         = os.getenv('HOST', '0.0.0.0')
PORT         = int(os.getenv('DASHBOARD_PORT', 5050))
SECRET_KEY   = os.getenv('FLASK_SECRET', 'changeme')
BOT_WEBHOOK  = os.getenv('TRADING_BOT_URL', 'http://127.0.0.1:5000/webhook')
if not BOT_WEBHOOK.endswith('/webhook'):
    BOT_WEBHOOK = BOT_WEBHOOK.rstrip('/') + '/webhook'
ADMIN_USER   = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASS   = os.getenv('ADMIN_PASSWORD', 'admin')

app = Flask(__name__)
app.config.update(
    SQLALCHEMY_DATABASE_URI        = DB_URL,
    SQLALCHEMY_TRACK_MODIFICATIONS = False,
    SECRET_KEY                     = SECRET_KEY
)

logging.basicConfig(
    filename='dashboard.log',
    filemode='a',  # append mode, or use 'w' for overwrite on each run
    level=logging.INFO,  # log INFO and above
    format='%(asctime)s %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)

db.init_app(app)
api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')

def init_db():
    with app.app_context():
        db.create_all()
        if ADMIN_PASS:
            admin = User.query.filter_by(username=ADMIN_USER).first()
            h = generate_password_hash(ADMIN_PASS)
            if not admin:
                db.session.add(User(username=ADMIN_USER, password_hash=h))
            else:
                admin.password_hash = h
            db.session.commit()

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in first.", "warning")
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return wrapped

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        u = request.form['username']; p = request.form['password']
        user = User.query.filter_by(username=u).first()
        if user and check_password_hash(user.password_hash, p):
            session['user_id'] = user.id
            flash("Logged in.", "success")
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash("Invalid credentials.", "danger")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    session.pop('user_id', None)
    flash("Logged out.", "info")
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/admin/sync_open_trades')
@login_required
def sync_open_trades():
    user = User.query.get(session.get('user_id'))
    if not user or user.username != ADMIN_USER:
        flash("Only admin can sync positions.", "danger")
        return redirect(url_for('dashboard'))
    try:
        positions = api.list_positions()
        symbols_in_db = set([t.symbol for t in Trade.query.filter_by(status='open').all()])
        added = 0
        for p in positions:
            if p.symbol not in symbols_in_db:
                # Create trade record for open position
                t = Trade(
                    trade_id = f"alpaca_{p.symbol}_{p.avg_entry_price}",
                    symbol = p.symbol,
                    side = 'sell' if float(p.qty) < 0 else 'buy',
                    qty = abs(float(p.qty)),
                    open_price = float(p.avg_entry_price),
                    open_time = datetime.utcnow(),  # Will not be exact, but better than missing
                    status = 'open',
                    action = "synced"
                )
                db.session.add(t)
                added += 1
        db.session.commit()
        flash(f"Sync complete. Added {added} open trades from Alpaca positions.", "success")
    except Exception as e:
        flash(f"Sync failed: {e}", "danger")
    return redirect(url_for('dashboard'))

@app.route('/closed_trades')
@login_required
def closed_trades():
    return render_template('closed_trades.html')

@app.route('/open_analytics')
@login_required
def open_analytics():
    return render_template('open_analytics.html')

@app.route('/stats')
@login_required
def stats():
    return render_template('stats.html')

@app.route('/api/open_positions')
@login_required
def api_open_positions():
    try:
        positions = api.list_positions()
    except Exception as e:
        app.logger.error("Error fetching positions: %s", e, exc_info=True)
        return jsonify([])
    out = []
    for p in positions:
        qty  = float(p.qty)
        side = 'sell' if qty < 0 else 'buy'
        # For stocks, use avg_entry_price and latest trade for current price
        # For crypto, avg_entry_price may not be set—use market_price fields if needed
        open_price = None
        current_price = None
        try:
            open_price = float(p.avg_entry_price) if p.avg_entry_price else None
        except Exception:
            open_price = None
        # For current price, try p.current_price, else fallback to get_latest_trade
        try:
            current_price = float(getattr(p, "current_price", None) or 0)
            if not current_price:
                # fallback for stocks
                last_trade = api.get_latest_trade(p.symbol)
                current_price = float(last_trade.price)
        except Exception:
            current_price = None
        out.append({
            'symbol':            p.symbol,
            'side':              side,
            'qty':               qty,
            'open_price':        open_price,
            'current_price':     current_price if current_price else '-',
            'unrealized_pl':     float(p.unrealized_pl),
            'unrealized_pl_pct': float(p.unrealized_plpc)
        })
    return jsonify(out)



@app.route('/api/closed_orders')
@login_required
def api_closed_orders():
    from_date = request.args.get('from')
    to_date = request.args.get('to')
    q = Trade.query.filter_by(status='closed')
    if from_date:
        try:
            from_dt = datetime.strptime(from_date, "%Y-%m-%d")
            q = q.filter(Trade.close_time >= from_dt)
        except Exception: pass
    if to_date:
        try:
            to_dt = datetime.strptime(to_date, "%Y-%m-%d")
            q = q.filter(Trade.close_time <= to_dt)
        except Exception: pass
    closed = q.order_by(Trade.close_time.desc()).all()
    out = []
    for t in closed:
        out.append({
            'symbol': t.symbol,
            'side': t.side,
            'open_price': t.open_price,
            'close_price': t.close_price,
            'profit_loss': t.profit_loss,
            'profit_loss_pct': t.profit_loss_pct,
            'open_time': t.open_time.isoformat() if t.open_time else None,
            'close_time': t.close_time.isoformat() if t.close_time else None,
            'action': t.action or ""
        })
    return jsonify(out)

@app.route('/api/proxy_trade', methods=['POST'])
@login_required
def proxy_trade():
    payload = request.get_json(force=True)
    app.logger.info(f"Proxying trade: {payload}")
    try:
        r = requests.post(BOT_WEBHOOK, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        detail = str(e)
        try:
            if hasattr(e, 'response') and e.response is not None:
                detail = e.response.text
        except Exception:
            pass
        app.logger.error("Proxy to bot failed: %s", e)
        return jsonify({'error':'proxy_failed','detail':detail}), 500

    try:
        if data.get('result') in ['opened', 'opened_short']:
            t = record_open_trade(data, payload)
            if t:
                app.logger.info(f"Logged open trade in DB: {t.symbol} {t.side} {t.qty} at {t.open_price}")
        elif data.get('result') == 'closed':
            t = record_closed_trade(data, payload)
            if t:
                app.logger.info(f"Logged closed trade in DB: {t.symbol} {t.side} {t.qty} at {t.open_price}->{t.close_price}, P/L={t.profit_loss:.2f}")
    except Exception as ex:
        app.logger.error(f"DB update error in proxy_trade: {ex}")

    return jsonify(data), r.status_code

@app.route('/api/account')
@login_required
def api_account():
    try:
        acct = api.get_account()
        return jsonify({
            'equity': acct.equity,
            'cash': acct.cash
        })
    except Exception as e:
        return jsonify({'equity': 0, 'cash': 0})

@app.route('/logs')
@login_required
def logs():
    logfiles = ['dashboard.log', 'trades.log']
    logs = {}
    for name in logfiles:
        try:
            if os.path.exists(name):
                with open(name) as f:
                    logs[name] = f.read()
            else:
                logs[name] = "(File not found)"
        except Exception as e:
            logs[name] = f"Failed to read: {e}"
    return render_template('logs_view.html', logs=logs)
@app.route('/reinit_db', methods=['GET', 'POST'])
@login_required
def reinit_db():
    # Only allow admin user
    user = User.query.get(session.get('user_id'))
    if not user or user.username != ADMIN_USER:
        flash("Only admin can reinitialize the database!", "danger")
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        db.drop_all()
        db.create_all()
        # Re-create admin account
        h = generate_password_hash(ADMIN_PASS)
        db.session.add(User(username=ADMIN_USER, password_hash=h))
        db.session.commit()
        flash("Database has been reinitialized. All trades were deleted.", "success")
        return redirect(url_for('dashboard'))
    return render_template('reinit_db_confirm.html')

def sync_open_trades_to_db():
    try:
        positions = api.list_positions()
        symbols_in_db = set([t.symbol for t in Trade.query.filter_by(status='open').all()])
        added = 0
        for p in positions:
            if p.symbol not in symbols_in_db:
                t = Trade(
                    trade_id = f"alpaca_{p.symbol}_{p.avg_entry_price}",
                    symbol = p.symbol,
                    side = 'sell' if float(p.qty) < 0 else 'buy',
                    qty = abs(float(p.qty)),
                    open_price = float(p.avg_entry_price),
                    open_time = datetime.utcnow(),  # Approximate; real time not available
                    status = 'open',
                    action = "synced"
                )
                db.session.add(t)
                added += 1
        db.session.commit()
        logger.info(f"Auto-sync: Added {added} open trades from Alpaca positions.")
    except Exception as e:
        logger.error(f"Auto-sync failed: {e}")

# Start periodic auto-sync thread when Flask app launches
def periodic_sync():
    while True:
        with app.app_context():
            sync_open_trades_to_db()
        time.sleep(5)  # Sync every 60s

sync_thread = threading.Thread(target=periodic_sync, daemon=True)
sync_thread.start()

if __name__ == "__main__":
    init_db()
    app.run(host=HOST, port=PORT)
