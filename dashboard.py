#!/usr/bin/env python3
import os
import logging
from logging.handlers import RotatingFileHandler
import threading
import time
from datetime import datetime, timedelta
import shutil
import json
from functools import wraps

from flask import (
    Flask, request, render_template, jsonify, session,
    redirect, url_for, flash, g, send_from_directory
)
from flask_babel import Babel, gettext
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import alpaca_trade_api as tradeapi
import requests
from pytz import utc

from models import db, Trade, User
from trade_db import record_open_trade, record_closed_trade
from utils import encrypt_data, decrypt_data

# --- Initialization ---
load_dotenv()

app = Flask(__name__)

# --- Standard Flask Configuration ---
try:
    os.makedirs(app.instance_path)
except OSError:
    pass

app.config.update(
    SQLALCHEMY_DATABASE_URI=os.getenv('DATABASE_URL', 'sqlite:///app.db'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SECRET_KEY=os.getenv('FLASK_SECRET', 'a_very_strong_and_random_secret_key_please_change'),
    UPLOAD_FOLDER=os.path.join(app.instance_path, 'uploads'),
    BACKUP_FOLDER=os.path.join(app.instance_path, 'backups'),
    BABEL_DEFAULT_LOCALE='en',
    LANGUAGES={'en': 'English', 'ro': 'Română'}
)

# --- Environment Variables ---
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('DASHBOARD_PORT', 5050))
ADMIN_USER = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASS = os.getenv('ADMIN_PASSWORD', 'admin')
INTERNAL_API_KEY = os.getenv('INTERNAL_API_KEY', 'your-very-secret-internal-key')
BASE_URL = os.getenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")
BOT_WEBHOOK = os.getenv('TRADING_BOT_URL', 'http://127.0.0.1:5000/webhook')

# --- Enhanced Logging Setup ---
log_handler = RotatingFileHandler('dashboard.log', maxBytes=100000, backupCount=5)
log_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
app.logger.addHandler(log_handler)
app.logger.setLevel(logging.INFO)
logging.getLogger('werkzeug').addHandler(log_handler)
login_logger_handler = RotatingFileHandler('login.log', maxBytes=10000, backupCount=3)
login_logger_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
login_logger = logging.getLogger('login_logger')
login_logger.setLevel(logging.INFO)
login_logger.addHandler(login_logger_handler)

db.init_app(app)

def get_locale():
    return session.get('language') or request.accept_languages.best_match(list(app.config['LANGUAGES'].keys()))

babel = Babel(app, locale_selector=get_locale)

@app.context_processor
def inject_globals():
    return dict(get_locale=get_locale, config=app.config, g=g)

# --- User and Auth Management ---
@app.before_request
def load_logged_in_user():
    user_id = session.get('user_id')
    g.user = db.session.get(User, user_id) if user_id else None

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.user is None:
            flash(gettext("Please log in first."), "warning")
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def superuser_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not g.user or not g.user.is_superuser:
            flash(gettext("Admin access required."), "danger")
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def get_user_api(user):
    key = decrypt_data(user.encrypted_alpaca_key)
    secret = decrypt_data(user.encrypted_alpaca_secret)
    if not key or not secret: return None
    try:
        return tradeapi.REST(key, secret, BASE_URL, api_version='v2')
    except Exception as e:
        app.logger.error(f"[API_FAIL] Failed to initialize Alpaca API for {user.username}: {e}")
        return None

# --- Core Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
        user_agent = request.user_agent.string
        if user and check_password_hash(user.password_hash, password):
            session.clear()
            session['user_id'] = user.id
            login_logger.info(f"SUCCESSFUL LOGIN | UserID: {user.id}, Username: '{username}', Email: '{user.email}', IP: {ip_address}, Agent: '{user_agent}'")
            flash(gettext("Logged in."), "success")
            return redirect(request.args.get('next') or url_for('dashboard'))
        login_logger.warning(f"FAILED LOGIN ATTEMPT | Attempted Username: '{username}', IP: {ip_address}, Agent: '{user_agent}'")
        flash(gettext("Invalid credentials."), "danger")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if g.user:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        tv_user = request.form['tradingview_user']
        if User.query.filter((User.username == username) | (User.email == email) | (User.tradingview_user == tv_user)).first():
            flash("Username, email, or TradingView user already exists.", "danger")
        else:
            hashed_pw = generate_password_hash(request.form['password'])
            new_user = User(
                username=username, email=email, password_hash=hashed_pw,
                tradingview_user=tv_user, per_trade_amount=1000.0, is_trading_restricted=False
            )
            db.session.add(new_user)
            db.session.commit()
            app.logger.info(f"[USER_ACTION] New user '{username}' registered.")
            flash("Registration successful! Please log in.", "success")
            return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    app.logger.info(f"[USER_ACTION] User '{g.user.username}' logged out.")
    session.clear()
    flash(gettext("Logged out."), "info")
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    if g.user.is_superuser:
        all_users = User.query.filter_by(is_superuser=False).order_by(User.username).all()
        return render_template('admin/dashboard_overview.html', current_user=g.user, all_users=all_users)
    return render_template('dashboard.html', current_user=g.user)

@app.route('/closed_trades')
@login_required
def closed_trades():
    all_users = User.query.all() if g.user.is_superuser else None
    return render_template('closed_trades.html', current_user=g.user, all_users=all_users)

@app.route('/open_analytics')
@login_required
def open_analytics():
    all_users = User.query.all() if g.user.is_superuser else None
    return render_template('open_analytics.html', all_users=all_users, current_user=g.user)

@app.route('/stats')
@login_required
def stats():
    all_users = User.query.all() if g.user.is_superuser else None
    return render_template('stats.html', all_users=all_users, current_user=g.user)

@app.route('/config', methods=['GET', 'POST'])
@login_required
def user_config():
    if request.method == 'POST':
        new_tv_user = request.form['tradingview_user']
        existing_user = User.query.filter(User.tradingview_user == new_tv_user, User.id != g.user.id).first()
        if existing_user:
            flash('That TradingView User ID is already taken. Please choose a different one.', 'danger')
        else:
            g.user.encrypted_alpaca_key = encrypt_data(request.form['alpaca_key'])
            g.user.encrypted_alpaca_secret = encrypt_data(request.form['alpaca_secret'])
            g.user.per_trade_amount = float(request.form['per_trade_amount'])
            g.user.tradingview_user = new_tv_user
            db.session.commit()
            app.logger.info(f"[USER_ACTION] User '{g.user.username}' updated their configuration.")
            flash("Configuration saved successfully!", "success")
        return redirect(url_for('user_config'))
    return render_template('user_config.html', user=g.user, decrypt=decrypt_data, current_user=g.user)

@app.route('/language/<lang>')
def set_language(lang=None):
    session['language'] = lang
    return redirect(request.referrer or url_for('dashboard'))

# --- Admin Routes ---
@app.route('/admin/db_management', methods=['GET', 'POST'])
@superuser_required
def admin_db_management():
    if request.method == 'POST' and request.form.get('action') == 'reinitialize':
        try:
            app.logger.warning(f"[DATABASE] Admin '{g.user.username}' initiated database re-initialization.")
            db.drop_all()
            init_db()
            app.logger.info("[DATABASE] Database reinitialized successfully.")
            flash("Database reinitialized successfully!", "success")
        except Exception as e:
            app.logger.error(f"[DATABASE] Error during DB re-initialization by '{g.user.username}': {e}")
            flash(f"Error reinitializing database: {e}", "danger")
        return redirect(url_for('admin_db_management'))
    return render_template('admin/db_management.html', current_user=g.user)

def update_symbols_task():
    with app.app_context():
        app.logger.info("[SYSTEM] Starting symbol update task.")
        api_user = User.query.filter_by(is_superuser=True).first() or User.query.first()
        if not api_user:
            app.logger.error("[SYSTEM] Cannot update symbols: No users found in database.")
            return
        api = get_user_api(api_user)
        if not api:
            app.logger.error(f"[SYSTEM] Cannot update symbols: Could not initialize API for user '{api_user.username}'.")
            return
        symbols_file = os.path.join(app.instance_path, 'tradable_symbols.json')
        try:
            assets = api.list_assets(status='active')
            symbols = [a.symbol for a in assets if a.tradable]
            with open(symbols_file, 'w') as f: json.dump(sorted(symbols), f)
            app.logger.info(f"[SYSTEM] Successfully updated and saved {len(symbols)} symbols.")
        except Exception as e:
            app.logger.error(f"[SYSTEM] Failed to update symbol list: {e}")

@app.route('/admin/update_symbols')
@superuser_required
def admin_update_symbols():
    app.logger.info(f"[SYSTEM] Admin '{g.user.username}' manually initiated symbol list update.")
    threading.Thread(target=update_symbols_task).start()
    flash("Symbol list update initiated in the background.", 'info')
    return redirect(url_for('admin_db_management'))

@app.route('/admin/logs')
@superuser_required
def admin_logs():
    logfiles = ['dashboard.log', 'trades.log', 'login.log']
    logs_content = {}
    for name in logfiles:
        try:
            if os.path.exists(name):
                with open(name, 'r') as f:
                    logs_content[name] = "".join(f.readlines()[-200:])
            else:
                logs_content[name] = "(File not found)"
        except Exception as e:
            logs_content[name] = f"Failed to read log: {e}"
    return render_template('logs_view.html', logs=logs_content)

@app.route('/admin/backup_db')
@superuser_required
def admin_backup_db():
    try:
        db_path_str = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
        db_path = os.path.join(app.instance_path, db_path_str) if 'instance' not in db_path_str else os.path.join(os.path.dirname(app.instance_path), db_path_str)
        db_dir = os.path.dirname(db_path)
        db_filename = os.path.basename(db_path)
        backup_filename = f"backup-{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.db"
        app.logger.info(f"[DATABASE] Admin '{g.user.username}' created a database backup: {backup_filename}")
        return send_from_directory(directory=db_dir, path=db_filename, as_attachment=True, download_name=backup_filename)
    except Exception as e:
        flash(f'Error creating backup: {e}', 'danger')
        app.logger.error(f"[DATABASE] Backup failed for admin '{g.user.username}': {e}")
        return redirect(url_for('admin_db_management'))

@app.route('/admin/users')
@superuser_required
def admin_user_management():
    users = User.query.order_by(User.id).all()
    return render_template('admin/user_management.html', users=users, current_user=g.user)
    
@app.route('/admin/all_open_trades')
@superuser_required
def admin_all_open_trades():
    users = User.query.order_by(User.username).all()
    return render_template('admin/open_trades.html', current_user=g.user, all_users=users)

@app.route('/admin/health')
@superuser_required
def admin_health_dashboard():
    return render_template('admin/health.html', current_user=g.user)

@app.route('/admin/users/create', methods=['POST'])
@superuser_required
def admin_create_user():
    username = request.form.get('username')
    email = request.form.get('email')
    tv_user = request.form.get('tradingview_user')
    password = request.form.get('password')
    if not all([username, email, tv_user, password]):
        flash('All fields are required.', 'danger')
        return redirect(url_for('admin_user_management'))
    if User.query.filter((User.username == username) | (User.email == email) | (User.tradingview_user == tv_user)).first():
        flash('Username, email, or TradingView user already exists.', 'danger')
        return redirect(url_for('admin_user_management'))
    new_user = User(username=username, email=email, tradingview_user=tv_user, password_hash=generate_password_hash(password), is_trading_restricted=False)
    db.session.add(new_user)
    db.session.commit()
    app.logger.info(f"[USER_ACTION] Admin '{g.user.username}' created new user '{username}'.")
    flash(f'User {username} created successfully.', 'success')
    return redirect(url_for('admin_user_management'))

@app.route('/admin/users/update/<int:user_id>', methods=['POST'])
@superuser_required
def admin_update_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash('User not found.', 'danger')
    else:
        user.username = request.form['username']
        user.email = request.form['email']
        user.tradingview_user = request.form['tradingview_user']
        user.is_trading_restricted = 'is_trading_restricted' in request.form
        db.session.commit()
        app.logger.info(f"[USER_ACTION] Admin '{g.user.username}' updated user '{user.username}'. Restricted status: {user.is_trading_restricted}")
        flash(f'User {user.username} updated successfully.', 'success')
    return redirect(url_for('admin_user_management'))

@app.route('/admin/users/reset_password/<int:user_id>', methods=['POST'])
@superuser_required
def admin_reset_password(user_id):
    user = db.session.get(User, user_id)
    if user:
        new_password = request.form.get('new_password')
        if new_password:
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            app.logger.info(f"[USER_ACTION] Admin '{g.user.username}' reset password for user '{user.username}'.")
            flash(f"Password for {user.username} has been reset.", 'success')
        else:
            flash("Password cannot be empty.", "danger")
    return redirect(url_for('admin_user_management'))

@app.route('/admin/users/delete/<int:user_id>', methods=['POST'])
@superuser_required
def admin_delete_user(user_id):
    user = db.session.get(User, user_id)
    if user and not user.is_superuser:
        trade_count = Trade.query.filter_by(user_id=user.id).count()
        app.logger.warning(f"[USER_ACTION] Admin '{g.user.username}' is deleting user '{user.username}' and their {trade_count} trades.")
        Trade.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
        db.session.commit()
        flash(f"User {user.username} and all their trades have been deleted.", 'success')
    else:
        flash("Cannot delete a superuser or user not found.", 'danger')
    return redirect(url_for('admin_user_management'))

# --- API Endpoints ---
@app.route('/api/tradable_symbols')
@login_required
def api_tradable_symbols():
    symbols_file = os.path.join(app.instance_path, 'tradable_symbols.json')
    try:
        if not os.path.exists(symbols_file):
            app.logger.warning(f"Symbols file not found at '{symbols_file}'. Attempting to generate it now.")
            update_symbols_task()
            # Give the task a moment to run
            time.sleep(2)
        
        with open(symbols_file, 'r') as f:
            symbols = json.load(f)
        return jsonify(symbols)
    except Exception as e:
        app.logger.error(f"Could not read tradable_symbols.json: {e}")
        return jsonify({'error': 'Could not load symbol list.'}), 500

@app.route('/api/admin/health_data')
@superuser_required
def api_admin_health_data():
    def read_filtered_logs(log_file_path, num_lines=100):
        if not os.path.exists(log_file_path): return ["Log file not found."]
        try:
            with open(log_file_path, 'r') as f:
                lines = f.readlines()
                keywords = ['ERROR', 'FAIL', 'REJECTED', 'DATABASE', 'TRADE', 'USER', 'STATUS', 'BOT', 'ACTION', 'SYSTEM', 'WARNING']
                events = [line.strip() for line in lines[-num_lines:] if any(f"[{keyword}]" in line.upper() for keyword in keywords)]
                return events if events else ["No relevant events found in recent log entries."]
        except Exception as e: return [f"Could not read log file: {e}"]

    webhook_log_file = os.path.join(app.instance_path, 'last_webhook.log')
    last_webhook_utc, bot_status = None, 'Unknown'
    if os.path.exists(webhook_log_file):
        try:
            with open(webhook_log_file, 'r') as f: last_webhook_utc = f.read().strip()
            seconds_since = (datetime.now() - datetime.fromisoformat(last_webhook_utc)).total_seconds()
            if seconds_since < 300: bot_status = 'Active'
            elif seconds_since < 3600: bot_status = 'Idle'
            else: bot_status = 'Offline'
        except: bot_status = 'Error Reading Status'
    else: bot_status = 'No Webhooks Received'

    db_size_mb = 0
    try:
        db_path = os.path.join(app.instance_path, 'app.db')
        if os.path.exists(db_path):
            db_size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 2)
    except Exception as e:
        app.logger.error(f"[SYSTEM] Could not calculate DB size: {e}")

    return jsonify({
        'bot_status': bot_status, 'last_webhook_utc': last_webhook_utc, 'db_size_mb': db_size_mb,
        'dashboard_log_events': read_filtered_logs('dashboard.log'),
        'trades_log_events': read_filtered_logs('trades.log')
    })

@app.route('/api/admin/dashboard_summary')
@superuser_required
def api_admin_dashboard_summary():
    users = User.query.filter_by(is_superuser=False).all()
    summary_data = []
    for user in users:
        api = get_user_api(user)
        user_data = {'username': user.username, 'equity': 'N/A', 'open_pl': 'N/A', 'open_trades_count': 0}
        if api:
            try:
                account = api.get_account()
                positions = api.list_positions()
                total_pl = sum(float(p.unrealized_pl) for p in positions)
                user_data['equity'] = f"${float(account.equity):,.2f}"
                user_data['open_pl'] = f"${total_pl:,.2f}"
                user_data['open_trades_count'] = len(positions)
            except Exception as e:
                app.logger.warning(f"[API_FAIL] Could not fetch account summary for {user.username}: {e}")
                user_data['equity'] = 'Error'
        else:
            user_data['equity'] = 'No API Keys'
        summary_data.append(user_data)
    return jsonify(summary_data)
    
@app.route('/api/admin/performance_leaderboard')
@superuser_required
def api_admin_performance_leaderboard():
    closed_trades = Trade.query.filter(Trade.status == 'closed').all()
    user_stats = {}
    for trade in closed_trades:
        if trade.user_id not in user_stats:
            user_stats[trade.user_id] = {
                'username': trade.user.username, 'total_pl': 0, 
                'wins': 0, 'losses': 0, 'total_trades': 0
            }
        stats = user_stats[trade.user_id]
        stats['total_pl'] += trade.profit_loss or 0
        stats['total_trades'] += 1
        if (trade.profit_loss or 0) > 0:
            stats['wins'] += 1
        elif (trade.profit_loss or 0) < 0:
            stats['losses'] += 1
    leaderboard = []
    for stats in user_stats.values():
        total = stats['wins'] + stats['losses']
        stats['win_rate'] = (stats['wins'] / total * 100) if total > 0 else 0
        leaderboard.append(stats)
    return jsonify(leaderboard)

@app.route('/api/admin/all_open_positions')
@superuser_required
def api_admin_all_open_positions():
    user_filter_id = request.args.get('user_id')
    
    query = User.query
    if user_filter_id:
        query = query.filter(User.id == user_filter_id)
        
    users_with_keys = [u for u in query.all() if u.encrypted_alpaca_key]
    all_positions = []

    for user in users_with_keys:
        api = get_user_api(user)
        if not api:
            continue
        try:
            positions = api.list_positions()
            for p in positions:
                all_positions.append({
                    'user_id': user.id,
                    'username': user.username,
                    'symbol': p.symbol,
                    'side': p.side,
                    'qty': float(p.qty),
                    'open_price': float(p.avg_entry_price),
                    'current_price': float(p.current_price),
                    'unrealized_pl': float(p.unrealized_pl),
                })
        except Exception as e:
            app.logger.error(f"[API_FAIL] Could not fetch positions for {user.username}: {e}")

    return jsonify(all_positions)

@app.route('/api/admin/close_trades', methods=['POST'])
@superuser_required
def api_admin_close_trades():
    data = request.get_json()
    trades_to_close = data.get('trades', []) # Expect a list of {'user_id': X, 'symbol': 'Y'}

    closed_count = 0
    errors = []

    for trade_info in trades_to_close:
        user_id = trade_info.get('user_id')
        symbol = trade_info.get('symbol')
        user = db.session.get(User, user_id)

        if not user or not symbol:
            errors.append(f"Invalid trade data received: {trade_info}")
            continue

        api = get_user_api(user)
        if not api:
            errors.append(f"Could not initialize API for user {user.username}")
            continue
        
        try:
            api.close_position(symbol.replace('/', ''))
            app.logger.info(f"[ADMIN_ACTION] Admin '{g.user.username}' closed position {symbol} for user '{user.username}'.")
            closed_count += 1
        except Exception as e:
            error_msg = f"Failed to close {symbol} for {user.username}: {e}"
            app.logger.error(f"[ADMIN_ACTION] {error_msg}")
            errors.append(error_msg)

    if errors:
        return jsonify({'status': 'partial_success', 'closed': closed_count, 'errors': errors}), 207
    
    return jsonify({'status': 'success', 'closed': closed_count})

@app.route('/api/open_positions')
@login_required
def api_open_positions():
    api = get_user_api(g.user)
    if not api: return jsonify([])
    try:
        positions = api.list_positions()
        db_trades = {t.symbol: t.open_time for t in Trade.query.filter_by(status='open', user_id=g.user.id).all()}
    except Exception as e:
        app.logger.error(f"[API_FAIL] Error fetching positions for user {g.user.username}: {e}")
        return jsonify([])
    out = []
    for p in positions:
        open_time_utc = db_trades.get(p.symbol, datetime.now(utc))
        out.append({'symbol': p.symbol, 'side': 'sell' if float(p.qty) < 0 else 'buy', 'qty': abs(float(p.qty)), 'open_price': float(p.avg_entry_price), 'current_price': float(p.current_price or 0), 'market_value': float(p.market_value), 'unrealized_pl': float(p.unrealized_pl), 'open_time_iso': open_time_utc.isoformat() if open_time_utc else None})
    return jsonify(out)

@app.route('/api/closed_orders')
@login_required
def api_closed_orders():
    user_filter_id = request.args.get('user_id', str(g.user.id))
    if not g.user.is_superuser and user_filter_id != str(g.user.id):
        return jsonify({"error": "Unauthorized"}), 403
    query = Trade.query.filter_by(status='closed')
    if user_filter_id != '0':
        query = query.filter_by(user_id=int(user_filter_id))
    closed_trades = query.order_by(Trade.close_time.desc()).all()
    return jsonify([{'symbol': t.symbol, 'side': t.side, 'open_price': t.open_price, 'close_price': t.close_price, 'profit_loss': t.profit_loss, 'profit_loss_pct': t.profit_loss_pct, 'open_time': t.open_time.isoformat() if t.open_time else None, 'close_time': t.close_time.isoformat() if t.close_time else None, 'action': t.action or ""} for t in closed_trades])

@app.route('/api/account')
@login_required
def api_account():
    api = get_user_api(g.user)
    if not api: return jsonify({'equity': 0, 'cash': 0})
    try:
        acct = api.get_account()
        return jsonify({'equity': float(acct.equity), 'cash': float(acct.cash)})
    except: return jsonify({'equity': 0, 'cash': 0})

@app.route('/api/internal/proxy_trade', methods=['POST'])
@login_required
def proxy_trade_internal():
    payload = request.get_json(force=True)
    payload['user'] = g.user.tradingview_user
    app.logger.info(f"[TRADE] Manual trade proxied by '{g.user.username}': {payload}")
    try:
        r = requests.post(BOT_WEBHOOK, json=payload, timeout=10)
        r.raise_for_status()
        return jsonify(r.json()), r.status_code
    except Exception as e:
        detail = str(e)
        if hasattr(e, 'response') and e.response is not None:
            try: detail = e.response.json().get('error', e.response.text)
            except: detail = e.response.text
        app.logger.error(f"[BOT_PROXY_FAIL] Proxy to bot failed: {detail}")
        return jsonify({'error': 'proxy_failed', 'detail': detail}), 500

@app.route('/api/internal/record_trade', methods=['POST'])
def record_trade_internal():
    if request.headers.get('X-Internal-API-Key') != INTERNAL_API_KEY:
        app.logger.warning("[SECURITY] Unauthorized attempt to access internal record_trade API.")
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    def background_task(data_dict, app_context):
        with app_context:
            try:
                user_id = data_dict.get('user_id')
                payload = data_dict.get('payload')
                app.logger.info(f"[DATABASE] Background task recording trade for user_id={user_id}.")
                if data_dict.get('result') == 'opened':
                    record_open_trade(data_dict, payload, user_id)
                elif data_dict.get('result') == 'closed':
                    pos_data = data_dict.get('position_obj')
                    class MockPosition:
                        # FIX: Added self to the __init__ method
                        def __init__(self, **entries): self.__dict__.update(entries)
                    position_obj = MockPosition(**pos_data) if pos_data else None
                    record_closed_trade(data_dict, payload, user_id, position_obj)
            except Exception as e:
                app.logger.error(f"[DATABASE] Error in background trade recording: {e}", exc_info=True)
    threading.Thread(target=background_task, args=(data, app.app_context())).start()
    return jsonify({"status": "accepted"}), 202

def init_db():
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username=ADMIN_USER).first():
            if not ADMIN_PASS:
                raise ValueError("ADMIN_PASSWORD must be set in .env on first run to create superuser")
            
            superuser = User(
                username=ADMIN_USER, email=f"{ADMIN_USER}@example.com",
                password_hash=generate_password_hash(ADMIN_PASS),
                tradingview_user=f"tv_{ADMIN_USER}", is_superuser=True
            )
            db.session.add(superuser)
            db.session.commit()
            app.logger.info(f"[SYSTEM] Superuser '{ADMIN_USER}' created.")
            
# --- NEW: Investor Payout Feature (File-Based) ---

# Read the toggle from .env file
ENABLE_INVESTOR_VIEW = os.getenv('ENABLE_INVESTOR_VIEW', 'False').lower() in ('true', '1', 't')
INVESTORS_FILE = os.path.join(app.instance_path, 'investors.json')

# Add the toggle to the context so templates can see it
@app.context_processor
def inject_investor_view_flag():
    return dict(ENABLE_INVESTOR_VIEW=ENABLE_INVESTOR_VIEW)

def get_investor_data():
    """Safely reads investor data from the JSON file."""
    if not os.path.exists(INVESTORS_FILE):
        return {}
    try:
        with open(INVESTORS_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

@app.route('/admin/investor_config', methods=['GET', 'POST'])
@superuser_required
def admin_investor_config():
    if not ENABLE_INVESTOR_VIEW:
        flash('Investor view feature is disabled.', 'warning')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        investor_data = {}
        all_users = User.query.order_by(User.username).all()
        for user in all_users:
            amount_str = request.form.get(f'investment_{user.username}')
            if amount_str:
                try:
                    amount = float(amount_str)
                    if amount > 0:
                        investor_data[user.username] = amount
                except ValueError:
                    pass # Ignore invalid numbers
        
        try:
            with open(INVESTORS_FILE, 'w') as f:
                json.dump(investor_data, f, indent=4)
            flash('Investment amounts have been updated.', 'success')
        except IOError as e:
            flash(f'Error saving investor data: {e}', 'danger')

        return redirect(url_for('admin_investor_config'))

    all_users = User.query.order_by(User.is_superuser.desc(), User.username).all()
    investor_data = get_investor_data()
    return render_template('admin/investor_config.html', all_users=all_users, investor_data=investor_data)

@app.route('/investor_payout') # MODIFIED: Now accessible to all logged-in users
@login_required
def investor_payout():
    if not ENABLE_INVESTOR_VIEW:
        flash('Investor view feature is currently disabled.', 'warning')
        return redirect(url_for('dashboard'))
    return render_template('investor_payout.html') # Note: Template is not in 'admin' folder

@app.route('/api/investor_payout_data') # MODIFIED: Now accessible to all logged-in users
@login_required
def api_investor_payout_data():
    if not ENABLE_INVESTOR_VIEW:
        return jsonify({'error': 'Feature not enabled'}), 403

    # CRITICAL: This API must always use the admin's keys to get the total account value.
    admin_user = User.query.filter_by(is_superuser=True).first()
    if not admin_user:
        return jsonify({'error': 'Admin account not found.'}), 500

    api = get_user_api(admin_user)
    if not api:
        return jsonify({'error': 'Admin Alpaca API keys are not configured.'}), 500

    try:
        account = api.get_account()
        live_equity = float(account.equity)
    except Exception as e:
        app.logger.error(f"Could not fetch admin account data for payout report: {e}")
        return jsonify({'error': 'Failed to fetch Alpaca account data.'}), 500

    investor_data = get_investor_data()
    if not investor_data:
        return jsonify({'error': 'No investors have been configured.'}), 404

    total_investment = sum(investor_data.values())
    total_pl = live_equity - total_investment

    payout_data = []
    for username, investment in investor_data.items():
        ownership_percent = (investment / total_investment * 100) if total_investment > 0 else 0
        pl_share = total_pl * (ownership_percent / 100)
        current_equity = investment + pl_share
        payout_data.append({
            'username': username,
            'investment': investment,
            'ownership_percent': ownership_percent,
            'pl_share': pl_share,
            'current_equity': current_equity
        })

    return jsonify({
        'total_investment': total_investment,
        'live_equity': live_equity,
        'total_pl': total_pl,
        'investors': sorted(payout_data, key=lambda x: x['investment'], reverse=True)
    })

if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host=HOST, port=PORT, debug=True)