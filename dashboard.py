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
from pytz import timezone, utc

from models import db, Trade, User
from trade_db import record_open_trade, record_closed_trade

# Load environment variables
load_dotenv()
API_KEY = os.getenv("ALPACA_KEY")
API_SECRET = os.getenv("ALPACA_SECRET")
BASE_URL = os.getenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")
DB_URL = os.getenv('DATABASE_URL', 'sqlite:///instance/app.db')
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('DASHBOARD_PORT', 5050))
SECRET_KEY = os.getenv('FLASK_SECRET', 'changeme')
BOT_WEBHOOK = os.getenv('TRADING_BOT_URL', 'http://127.0.0.1:5000/webhook')
if not BOT_WEBHOOK.endswith('/webhook'):
    BOT_WEBHOOK = BOT_WEBHOOK.rstrip('/') + '/webhook'
ADMIN_USER = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASS = os.getenv('ADMIN_PASSWORD', 'admin')

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


# --- Flask App Initialization ---
app = Flask(__name__)
app.config.update(
    SQLALCHEMY_DATABASE_URI=DB_URL,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SECRET_KEY=SECRET_KEY
)

# --- Logging Setup ---
logging.basicConfig(
    filename='dashboard.log',
    filemode='a',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)

# --- Database and API Initialization ---
db.init_app(app)
api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')

# --- Database Initialization ---
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

# --- Authentication ---
def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in first.", "warning")
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return wrapped

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = request.form['username']
        p = request.form['password']
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

# --- Main Dashboard Routes ---
@app.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html')

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

# --- API Endpoints ---
@app.route('/api/open_positions')
@login_required
def api_open_positions():
    try:
        positions = api.list_positions()
        db_trades = {t.symbol: t.open_time for t in Trade.query.filter_by(status='open').all()}
    except Exception as e:
        app.logger.error("Error fetching positions: %s", e, exc_info=True)
        return jsonify([])
    
    out = []
    for p in positions:
        open_time_utc = db_trades.get(p.symbol)
        open_time_str = "-"
        if open_time_utc:
            local_tz = timezone('Europe/Bucharest')
            open_time_local = open_time_utc.astimezone(local_tz)
            open_time_str = open_time_local.strftime('%Y-%m-%d %H:%M:%S')

        out.append({
            'symbol': p.symbol,
            'side': 'sell' if float(p.qty) < 0 else 'buy',
            'qty': abs(float(p.qty)),
            'open_price': float(p.avg_entry_price),
            'current_price': float(p.current_price or 0),
            'unrealized_pl': float(p.unrealized_pl),
            'open_time': open_time_str
        })
    return jsonify(out)

@app.route('/api/closed_orders')
@login_required
def api_closed_orders():
    # ... (code unchanged)
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
    
    position_to_close = None
    if is_close_action(payload.get('action')):
        try:
            # Get position info BEFORE closing to pass to the recorder
            position_to_close = api.get_position(payload.get('symbol'))
        except Exception as e:
            logger.warning(f"Could not get position for {payload.get('symbol')} before closing. It may not exist. Error: {e}")

    try:
        r = requests.post(BOT_WEBHOOK, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        detail = str(e)
        if hasattr(e, 'response') and e.response is not None:
            try: detail = e.response.json().get('error', e.response.text)
            except Exception: detail = e.response.text
        app.logger.error("Proxy to bot failed: %s", detail)
        return jsonify({'error': 'proxy_failed', 'detail': detail}), 500

    try:
        if data.get('result') in ['opened', 'opened_short']:
            time.sleep(1) # Give Alpaca a moment to update the position
            t = record_open_trade(data, payload)
            if t: app.logger.info(f"DB: Logged open trade: {t.symbol}")
        elif data.get('result') == 'closed':
            t = record_closed_trade(data, payload, position_to_close)
            if t: app.logger.info(f"DB: Logged closed trade: {t.symbol}, P/L={t.profit_loss:.2f}")
    except Exception as ex:
        app.logger.error(f"DB update error in proxy_trade: {ex}", exc_info=True)
    
    return jsonify(data), r.status_code

@app.route('/api/account')
@login_required
def api_account():
    # ... (code unchanged)
    try:
        acct = api.get_account()
        return jsonify({
            'equity': float(acct.equity),
            'cash': float(acct.cash)
        })
    except Exception:
        return jsonify({'equity': 0, 'cash': 0})

# --- Admin and Utility Routes ---
@app.route('/logs')
@login_required
def logs():
    # ... (code unchanged)
    logfiles = ['dashboard.log', 'trades.log']
    logs_content = {}
    for name in logfiles:
        try:
            if os.path.exists(name):
                with open(name, 'r') as f:
                    lines = f.readlines()
                    logs_content[name] = "".join(lines[-100:])
            else:
                logs_content[name] = "(File not found)"
        except Exception as e:
            logs_content[name] = f"Failed to read log: {e}"
    return render_template('logs_view.html', logs=logs_content)


@app.route('/reinit_db', methods=['GET', 'POST'])
@login_required
def reinit_db():
    # ... (code unchanged)
    user = User.query.get(session.get('user_id'))
    if not user or user.username != ADMIN_USER:
        flash("Admin access required.", "danger")
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        try:
            db.drop_all()
            db.create_all()
            h = generate_password_hash(ADMIN_PASS)
            db.session.add(User(username=ADMIN_USER, password_hash=h))
            db.session.commit()
            flash("Database reinitialized successfully!", "success")
        except Exception as e:
            flash(f"Error reinitializing database: {e}", "danger")
        return redirect(url_for('dashboard'))
    return render_template('reinit_db_confirm.html')


# --- Background Sync Thread ---
def sync_open_trades_to_db():
    # ... (code unchanged)
    with app.app_context():
        try:
            positions = api.list_positions()
            db_symbols = {t.symbol for t in Trade.query.filter_by(status='open').all()}
            added_count = 0
            for p in positions:
                if p.symbol not in db_symbols:
                    t = Trade(
                        trade_id=f"sync_{p.asset_id}_{int(time.time())}",
                        symbol=p.symbol,
                        side='sell' if float(p.qty) < 0 else 'buy',
                        qty=abs(float(p.qty)),
                        open_price=float(p.avg_entry_price),
                        open_time=datetime.now(utc),
                        status='open',
                        action="synced"
                    )
                    db.session.add(t)
                    added_count += 1
            if added_count > 0:
                db.session.commit()
                logger.info(f"Auto-sync: Added {added_count} new open positions from Alpaca.")
        except Exception as e:
            logger.error(f"Auto-sync failed: {e}")

def periodic_sync():
    # ... (code unchanged)
    while True:
        sync_open_trades_to_db()
        time.sleep(30)

if __name__ == "__main__":
    with app.app_context():
        init_db()
    
    sync_thread = threading.Thread(target=periodic_sync, daemon=True)
    sync_thread.start()
    
    app.run(host=HOST, port=PORT)