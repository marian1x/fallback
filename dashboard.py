#!/usr/bin/env python3
import os
import re
import logging
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
import shutil
import json

from flask import (
    Flask, request, render_template, jsonify,
    session, redirect, url_for, flash, send_from_directory
)
from flask_babel import Babel, gettext
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
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
PER_TRADE_AMOUNT = os.getenv('PER_TRADE_AMOUNT', '2000')

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
    SECRET_KEY=SECRET_KEY,
    UPLOAD_FOLDER=os.path.join(app.instance_path, 'uploads'),
    BACKUP_FOLDER=os.path.join(app.instance_path, 'backups'),
    BABEL_DEFAULT_LOCALE='en',
    LANGUAGES={'en': 'English', 'ro': 'Română'}
)
os.makedirs(app.instance_path, exist_ok=True)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['BACKUP_FOLDER'], exist_ok=True)

# --- Internationalization Setup ---
def get_locale():
    return session.get('language', request.accept_languages.best_match(list(app.config['LANGUAGES'].keys())))

babel = Babel(app, locale_selector=get_locale)

@app.context_processor
def inject_locale():
    return dict(get_locale=get_locale)


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

# --- Helper function for symbol updates ---
def update_symbols_task():
    with app.app_context():
        symbols_file = os.path.join(app.instance_path, 'tradable_symbols.json')
        try:
            logger.info(f"Updating tradable symbols list from Alpaca to '{symbols_file}'...")
            active_assets = api.list_assets(status='active')
            tradable_symbols = []
            for asset in active_assets:
                if asset.tradable:
                    symbol = asset.symbol
                    if getattr(asset, 'class') == 'crypto' and symbol.endswith('USD'):
                        symbol = f"{symbol[:-3]}/USD"
                    tradable_symbols.append(symbol)
            
            with open(symbols_file, 'w') as f:
                json.dump(sorted(tradable_symbols), f)
            logger.info(f"Successfully updated and saved {len(tradable_symbols)} symbols.")
        except Exception as e:
            logger.error(f"Failed to update tradable symbols list: {e}")
            raise e

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
            flash(gettext("Please log in first."), "warning")
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
            flash(gettext("Logged in."), "success")
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash(gettext("Invalid credentials."), "danger")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    session.pop('user_id', None)
    flash(gettext("Logged out."), "info")
    return redirect(url_for('login'))

# --- Language Switching ---
@app.route('/language/<lang>')
def set_language(lang=None):
    session['language'] = lang
    return redirect(request.referrer)

# --- Main Dashboard Routes ---
@app.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html', per_trade_amount=PER_TRADE_AMOUNT)

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
@app.route('/api/assets')
@login_required
def get_tradable_assets():
    symbols_file = os.path.join(app.instance_path, 'tradable_symbols.json')
    try:
        if not os.path.exists(symbols_file):
            logger.warning(f"Symbols file not found at '{symbols_file}'. Attempting to generate it now.")
            update_symbols_task()
        
        with open(symbols_file, 'r') as f:
            symbols = json.load(f)
        return jsonify(symbols)
    except Exception as e:
        logger.error(f"Could not read tradable_symbols.json: {e}")
        return jsonify({'error': 'Could not load symbol list.'}), 500


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
        open_time_iso = None
        if open_time_utc:
            local_tz = timezone('Europe/Bucharest')
            if open_time_utc.tzinfo is None:
                open_time_utc = utc.localize(open_time_utc)
            open_time_local = open_time_utc.astimezone(local_tz)
            open_time_str = open_time_local.strftime('%Y-%m-%d %H:%M:%S')
            open_time_iso = open_time_utc.isoformat()

        out.append({
            'symbol': p.symbol,
            'side': 'sell' if float(p.qty) < 0 else 'buy',
            'qty': abs(float(p.qty)),
            'open_price': float(p.avg_entry_price),
            'market_value': float(p.market_value),
            'unrealized_pl': float(p.unrealized_pl),
            'open_time_str': open_time_str,
            'open_time_iso': open_time_iso
        })
    return jsonify(out)

@app.route('/api/closed_orders')
@login_required
def api_closed_orders():
    q = Trade.query.filter_by(status='closed')
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
            position_to_close = api.get_position(payload.get('symbol').replace('/', ''))
        except Exception as e:
            logger.warning(f"Could not get position for {payload.get('symbol')} before closing: {e}")

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
            time.sleep(1)
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
    try:
        acct = api.get_account()
        return jsonify({
            'equity': float(acct.equity),
            'cash': float(acct.cash)
        })
    except Exception:
        return jsonify({'equity': 0, 'cash': 0})

# --- Admin and Utility Routes ---
@app.route('/config', methods=['GET', 'POST'])
@login_required
def config():
    user = User.query.get(session.get('user_id'))
    if not user or user.username != ADMIN_USER:
        flash(gettext("Admin access required."), "danger")
        return redirect(url_for('dashboard'))

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    
    if request.method == 'POST':
        try:
            with open(env_path, 'r') as f:
                lines = f.readlines()

            new_lines = []
            for line in lines:
                is_updated = False
                for key, value in request.form.items():
                    if line.strip().startswith(key + '='):
                        new_lines.append(f"{key}={value}\n")
                        is_updated = True
                        break
                if not is_updated:
                    new_lines.append(line)
            
            with open(env_path, 'w') as f:
                f.writelines(new_lines)

            flash(gettext("Configuration saved successfully! The application may need a restart for all changes to take effect."), "success")
        except Exception as e:
            flash(gettext("Error saving configuration: %(error)s", error=e), "danger")
        return redirect(url_for('config'))
    
    config_vars = {}
    try:
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    config_vars[key] = value
    except Exception as e:
        flash(gettext("Could not read .env file: %(error)s", error=e), "warning")

    return render_template('config.html', config=config_vars)

@app.route('/logs')
@login_required
def logs():
    logfiles = ['dashboard.log', 'trades.log']
    logs_content = {}
    for name in logfiles:
        try:
            if os.path.exists(name):
                with open(name, 'r') as f:
                    lines = f.readlines()
                    logs_content[name] = "".join(lines[-200:])
            else:
                logs_content[name] = "(File not found)"
        except Exception as e:
            logs_content[name] = f"Failed to read log: {e}"
    return render_template('logs_view.html', logs=logs_content)


@app.route('/db_management', methods=['GET', 'POST'])
@login_required
def db_management():
    user = User.query.get(session.get('user_id'))
    if not user or user.username != ADMIN_USER:
        flash(gettext("Admin access required."), "danger")
        return redirect(url_for('dashboard'))
    
    db_path = os.path.join(app.instance_path, 'app.db')

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'restore':
            if 'restore_file' not in request.files:
                flash(gettext('No file part in the request.'), 'warning')
                return redirect(request.url)
            file = request.files['restore_file']
            if file.filename == '':
                flash(gettext('No selected file.'), 'warning')
                return redirect(request.url)
            if file and file.filename.endswith('.db'):
                try:
                    os.makedirs(app.instance_path, exist_ok=True)
                    filename = secure_filename(file.filename)
                    temp_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(temp_path)
                    shutil.copy2(temp_path, db_path)
                    os.remove(temp_path)
                    flash(gettext('Database restored successfully! Please restart the application.'), 'success')
                except Exception as e:
                    flash(gettext('An error occurred during restore: %(error)s', error=e), 'danger')
            else:
                flash(gettext('Invalid file type. Please upload a .db file.'), 'danger')
        elif action == 'reinitialize':
            try:
                db.drop_all()
                db.create_all()
                h = generate_password_hash(ADMIN_PASS)
                db.session.add(User(username=ADMIN_USER, password_hash=h))
                db.session.commit()
                flash(gettext("Database reinitialized successfully!"), "success")
            except Exception as e:
                flash(gettext("Error reinitializing database: %(error)s", error=e), "danger")
        return redirect(url_for('db_management'))

    return render_template('db_management.html')

@app.route('/backup_db')
@login_required
def backup_db():
    user = User.query.get(session.get('user_id'))
    if not user or user.username != ADMIN_USER:
        return "Unauthorized", 403

    try:
        db_dir = os.path.join(app.instance_path)
        db_filename = 'app.db'
        backup_filename = f"backup-{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.db"
        return send_from_directory(directory=db_dir, path=db_filename, as_attachment=True, download_name=backup_filename)
    except Exception as e:
        flash(gettext('Error creating backup: %(error)s', error=e), 'danger')
        return redirect(url_for('db_management'))
        
@app.route('/admin/update_symbols')
@login_required
def update_symbols_manual():
    user = User.query.get(session.get('user_id'))
    if not user or user.username != ADMIN_USER:
        flash(gettext("Admin access required."), "danger")
        return redirect(url_for('dashboard'))
    try:
        update_symbols_task()
        flash(gettext("Tradable symbols list has been updated successfully."), "success")
    except Exception as e:
        flash(gettext("Failed to update symbols list: %(error)s", error=e), "danger")
    return redirect(url_for('db_management'))

# --- Background Sync Threads ---
def run_scheduler(interval, task_func):
    def scheduler_task():
        with app.app_context():
            task_func()
        threading.Timer(interval, scheduler_task).start()
    
    threading.Timer(interval, scheduler_task).start()

def auto_backup_task():
    with app.app_context():
        backup_dir = app.config['BACKUP_FOLDER']
        db_path = os.path.join(app.instance_path, 'app.db')
        if not os.path.exists(db_path):
            return

        backup_filename = f"auto_backup-{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.db"
        shutil.copy2(db_path, os.path.join(backup_dir, backup_filename))
        
        backups = sorted(os.listdir(backup_dir), key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)))
        if len(backups) > 24:
            os.remove(os.path.join(backup_dir, backups[0]))
        logger.info(f"Automatic hourly backup created: {backup_filename}")

def run_daily_at(hour, minute, task_func):
    def daily_task():
        while True:
            now = datetime.now()
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run < now:
                next_run += timedelta(days=1)
            
            sleep_seconds = (next_run - now).total_seconds()
            logger.info(f"Next daily task ({task_func.__name__}) scheduled in {sleep_seconds/3600:.2f} hours.")
            time.sleep(sleep_seconds)

            with app.app_context():
                task_func()
    
    threading.Thread(target=daily_task, daemon=True).start()


if __name__ == "__main__":
    with app.app_context():
        init_db()
        if not os.path.exists(os.path.join(app.instance_path, 'tradable_symbols.json')):
            update_symbols_task()
        auto_backup_task()

    run_scheduler(3600, auto_backup_task)
    run_daily_at(0, 0, update_symbols_task)
    
    app.run(host=HOST, port=PORT)