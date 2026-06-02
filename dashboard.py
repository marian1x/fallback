#!/usr/bin/env python3
import os
import logging
import subprocess
import csv
from logging.handlers import RotatingFileHandler
import threading
import time
from datetime import datetime, timedelta, timezone
import json
import hmac
import secrets
from urllib.parse import urlsplit
from functools import wraps

from flask import (
    Flask, request, render_template, jsonify, session,
    redirect, url_for, flash, g, send_from_directory
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
import requests

from alpaca_api import LegacyCompatibleAlpacaClient
from models import db, Trade, User
import strategy_config as strategy_store
from trade_db import record_open_trade, record_closed_trade
from utils import encrypt_data, decrypt_data

# --- Initialization ---
ENV_PATH = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(ENV_PATH)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

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
    MAX_CONTENT_LENGTH=int(os.getenv('MAX_CONTENT_LENGTH_BYTES', str(512 * 1024))),
    PREFERRED_URL_SCHEME='https',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', 'true').lower() in ('1', 'true', 'yes', 'y'),
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=int(os.getenv('SESSION_LIFETIME_MINUTES', '720'))),
)

# --- Environment Variables ---
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('DASHBOARD_PORT', 5050))
DEBUG = os.getenv('FLASK_DEBUG', 'false').lower() in ('1', 'true', 'yes', 'y')
ADMIN_USER = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASS = os.getenv('ADMIN_PASSWORD', 'admin')
INTERNAL_API_KEY = os.getenv('INTERNAL_API_KEY', 'your-very-secret-internal-key')
BASE_URL = os.getenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")
BOT_WEBHOOK = os.getenv('TRADING_BOT_URL', 'http://127.0.0.1:5000/webhook')
RESTART_COMMAND = os.getenv('RESTART_COMMAND', '').strip()
if not RESTART_COMMAND and os.name == 'posix':
    RESTART_COMMAND = 'sudo systemctl restart fallback_dashboard.service'
BOT_SERVICE_NAME = os.getenv('BOT_SERVICE_NAME', 'fallback.service').strip()
REPO_PATH = os.getenv('TRADINGBOT_REPO_PATH', '').strip()
if not REPO_PATH or not os.path.isdir(REPO_PATH):
    REPO_PATH = os.path.abspath(os.path.dirname(__file__))
LAST_GOOD_COMMIT_FILE = os.path.join(app.instance_path, 'last_good_commit.txt')
VERSION_COUNTER_FILE = os.path.join(app.instance_path, 'version_counter.txt')
STRATEGY_REPORT_FILE = os.path.join(app.instance_path, 'strategy_last_report.json')
STRATEGY_TOP_FILE = os.path.join(app.instance_path, 'strategy_last_top.csv')
LOGIN_RATE_LIMIT_WINDOW_SEC = int(os.getenv('LOGIN_RATE_LIMIT_WINDOW_SEC', '600'))
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = int(os.getenv('LOGIN_RATE_LIMIT_MAX_ATTEMPTS', '8'))
PASSWORD_MIN_LENGTH = int(os.getenv('PASSWORD_MIN_LENGTH', '10'))

LOGIN_ATTEMPTS = {}
LOGIN_ATTEMPTS_LOCK = threading.Lock()
CSRF_EXEMPT_ENDPOINTS = {'webhook', 'record_trade_internal'}

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

if INTERNAL_API_KEY == "your-very-secret-internal-key":
    app.logger.warning("[SECURITY] INTERNAL_API_KEY is using the default placeholder. Configure a strong value in .env.")

db.init_app(app)

def get_client_ip():
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'unknown'

def _purge_old_login_attempts(now_ts):
    stale_keys = []
    for ip, attempts in LOGIN_ATTEMPTS.items():
        recent = [t for t in attempts if (now_ts - t) <= LOGIN_RATE_LIMIT_WINDOW_SEC]
        if recent:
            LOGIN_ATTEMPTS[ip] = recent
        else:
            stale_keys.append(ip)
    for ip in stale_keys:
        LOGIN_ATTEMPTS.pop(ip, None)

def is_login_rate_limited(ip):
    now_ts = time.time()
    with LOGIN_ATTEMPTS_LOCK:
        _purge_old_login_attempts(now_ts)
        attempts = LOGIN_ATTEMPTS.get(ip, [])
        return len(attempts) >= LOGIN_RATE_LIMIT_MAX_ATTEMPTS

def mark_login_failure(ip):
    now_ts = time.time()
    with LOGIN_ATTEMPTS_LOCK:
        _purge_old_login_attempts(now_ts)
        LOGIN_ATTEMPTS.setdefault(ip, []).append(now_ts)

def clear_login_failures(ip):
    with LOGIN_ATTEMPTS_LOCK:
        LOGIN_ATTEMPTS.pop(ip, None)

def csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token

def is_safe_redirect_target(target):
    if not target:
        return False
    host_url = urlsplit(request.host_url)
    redirect_url = urlsplit(target)
    if redirect_url.scheme or redirect_url.netloc:
        return False
    if not target.startswith('/') or target.startswith('//'):
        return False
    return host_url.scheme in ('http', 'https')

def _is_internal_api_key_valid(provided_key):
    if not INTERNAL_API_KEY:
        return False
    if not provided_key:
        return False
    try:
        return hmac.compare_digest(str(provided_key), str(INTERNAL_API_KEY))
    except Exception:
        return False

@app.before_request
def enforce_session_security():
    session.permanent = True
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        endpoint = request.endpoint or ''
        if endpoint in CSRF_EXEMPT_ENDPOINTS:
            return None
        expected = session.get('csrf_token')
        supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
        if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
            app.logger.warning(f"[SECURITY] CSRF validation failed on endpoint '{endpoint}' from ip={get_client_ip()}.")
            if request.path.startswith('/api/'):
                return jsonify({'error': 'csrf_failed'}), 400
            flash("Your session token is invalid. Please retry the action.", "danger")
            return redirect(url_for('login'))
    return None

@app.after_request
def add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    if request.is_secure:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    csp = (
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://cdn.datatables.net; "
        "font-src 'self' data: https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
        "script-src 'self' 'unsafe-inline' https://code.jquery.com https://cdn.jsdelivr.net https://cdn.datatables.net https://cdnjs.cloudflare.com; "
        "connect-src 'self'; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers.setdefault('Content-Security-Policy', csp)
    return response

def translate(message, **kwargs):
    if kwargs:
        try:
            return message % kwargs
        except Exception:
            return message
    return message

gettext = translate
app.jinja_env.globals.update(_=translate, csrf_token=csrf_token)

@app.context_processor
def inject_globals():
    return dict(config=app.config, g=g, app_version=get_version_display())

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
            next_path = request.full_path if request.query_string else request.path
            if next_path.endswith('?'):
                next_path = next_path[:-1]
            return redirect(url_for('login', next=next_path))
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
        return LegacyCompatibleAlpacaClient(key, secret, BASE_URL)
    except Exception as e:
        app.logger.error(f"[API_FAIL] Failed to initialize Alpaca API for {user.username}: {e}")
        return None

def get_user_keypair(user):
    key = decrypt_data(user.encrypted_alpaca_key)
    secret = decrypt_data(user.encrypted_alpaca_secret)
    if not key or not secret:
        return None
    return key, secret

def run_command(command, cwd=None, timeout=30):
    try:
        use_shell = isinstance(command, str)
        result = subprocess.run(
            command,
            cwd=cwd,
            shell=use_shell,
            text=True,
            capture_output=True,
            timeout=timeout
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return False, '', str(e)

def is_git_repo():
    ok, out, _ = run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=REPO_PATH)
    return ok and out.strip().lower() == "true"

def get_git_commit_info(commit_ref="HEAD"):
    commit_ref = commit_ref or "HEAD"
    format_token = "%H%x1f%s%x1f%an%x1f%ad"
    ok, out, err = run_command(
        ["git", "log", "-1", f"--pretty=format:{format_token}", commit_ref],
        cwd=REPO_PATH
    )
    if not ok or not out:
        app.logger.warning(f"[UPDATE] Failed to read commit info for {commit_ref}: {err}")
        return None
    parts = out.split("\x1f")
    if len(parts) < 4:
        return None
    commit_hash, subject, author, date = parts
    return {
        "hash": commit_hash,
        "short_hash": commit_hash[:8],
        "subject": subject,
        "author": author,
        "date": date
    }

def read_version_counter():
    if not os.path.exists(VERSION_COUNTER_FILE):
        return None
    try:
        with open(VERSION_COUNTER_FILE, "r") as f:
            value = f.read().strip()
            return int(value)
    except Exception as e:
        app.logger.error(f"[UPDATE] Failed to read version counter: {e}")
        return None

def write_version_counter(value):
    try:
        os.makedirs(os.path.dirname(VERSION_COUNTER_FILE), exist_ok=True)
        with open(VERSION_COUNTER_FILE, "w") as f:
            f.write(str(int(value)))
    except Exception as e:
        app.logger.error(f"[UPDATE] Failed to write version counter: {e}")

def ensure_version_counter():
    if read_version_counter() is None:
        write_version_counter(1)

def increment_version_counter():
    current = read_version_counter()
    if current is None:
        write_version_counter(1)
        return 1
    new_value = current + 1
    write_version_counter(new_value)
    return new_value

def get_version_display():
    ensure_version_counter()
    counter = read_version_counter() or 1
    commit = get_git_commit_info()
    short_hash = commit["short_hash"] if commit else "unknown"
    return f"v{counter} ({short_hash})"

def read_last_good_commit():
    if not os.path.exists(LAST_GOOD_COMMIT_FILE):
        return None
    try:
        with open(LAST_GOOD_COMMIT_FILE, "r") as f:
            value = f.read().strip()
            return value or None
    except Exception as e:
        app.logger.error(f"[UPDATE] Failed to read last good commit: {e}")
        return None

def write_last_good_commit(commit_hash):
    if not commit_hash:
        return
    try:
        os.makedirs(os.path.dirname(LAST_GOOD_COMMIT_FILE), exist_ok=True)
        with open(LAST_GOOD_COMMIT_FILE, "w") as f:
            f.write(commit_hash)
    except Exception as e:
        app.logger.error(f"[UPDATE] Failed to write last good commit: {e}")

def ensure_last_good_commit():
    current = get_git_commit_info()
    if not current:
        return
    if not read_last_good_commit():
        write_last_good_commit(current["hash"])

def get_default_strategy_config():
    return strategy_store.get_default_strategy_config()

def load_strategy_config():
    try:
        return strategy_store.load_strategy_config()
    except Exception as e:
        app.logger.error(f"[STRATEGY] Failed to load strategy config: {e}")
        return get_default_strategy_config()

def save_strategy_config(cfg):
    try:
        strategy_store.save_strategy_config(cfg)
    except Exception as e:
        app.logger.error(f"[STRATEGY] Failed to save strategy config: {e}")

def load_cached_tradable_symbols():
    symbols_file = os.path.join(app.instance_path, 'tradable_symbols.json')
    if not os.path.exists(symbols_file):
        update_symbols_task()
    try:
        with open(symbols_file, 'r', encoding='utf-8') as f:
            symbols = json.load(f)
        if isinstance(symbols, list):
            return sorted({str(s).upper() for s in symbols if s})
    except Exception as e:
        app.logger.warning(f"[STRATEGY] Could not read tradable symbols cache: {e}")
    return []

def parse_strategy_universe_from_form(form):
    symbols = form.getlist('universe_symbol[]')
    modes = form.getlist('universe_mode[]')
    enabled_values = form.getlist('universe_enabled[]')
    notes = form.getlist('universe_notes[]')
    entries = []
    for idx, raw_symbol in enumerate(symbols):
        symbol = strategy_store.normalize_symbol(raw_symbol)
        if not symbol:
            continue
        mode = modes[idx] if idx < len(modes) else 'both'
        enabled_value = enabled_values[idx] if idx < len(enabled_values) else '1'
        note = notes[idx] if idx < len(notes) else ''
        entries.append({
            'symbol': symbol,
            'mode': mode,
            'enabled': str(enabled_value).lower() in ('1', 'true', 'yes', 'on'),
            'notes': note,
        })
    return strategy_store.normalize_universe(entries)

def get_strategy_api_user(config):
    username = str(config.get('alpaca_user', '') or '').strip()
    if username:
        user = User.query.filter_by(username=username).first()
        if user:
            return user
    return User.query.filter_by(is_superuser=True).first() or User.query.first()

def load_strategy_report():
    if not os.path.exists(STRATEGY_REPORT_FILE):
        return None
    try:
        with open(STRATEGY_REPORT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        app.logger.error(f"[STRATEGY] Failed to load strategy report: {e}")
        return None

def load_strategy_top_rows(limit=20):
    if not os.path.exists(STRATEGY_TOP_FILE):
        return []
    rows = []
    try:
        with open(STRATEGY_TOP_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
                if len(rows) >= limit:
                    break
    except Exception as e:
        app.logger.error(f"[STRATEGY] Failed to load strategy top rows: {e}")
        return []
    return rows

def run_strategy_optimizer(config):
    venv_python = os.path.join(REPO_PATH, "venv", "bin", "python")
    report_path = STRATEGY_REPORT_FILE
    top_path = STRATEGY_TOP_FILE
    command = [
        venv_python,
        "misc/pine_optimizer.py",
        "--symbol", str(config.get("symbol", "TSM")),
        "--timeframe", str(config.get("timeframe", "30Min")),
        "--session", str(config.get("session", "regular")),
        "--feed", str(config.get("feed", "iex")),
        "--trials", str(int(config.get("trials", 200))),
        "--top-k", str(int(config.get("top_k", 20))),
        "--trade-direction", str(config.get("trade_direction", "Both")),
        "--inner-len-range", str(config.get("inner_len_range", "8:40:1")),
        "--inner-mult-range", str(config.get("inner_mult_range", "0.6:1.8:0.1")),
        "--outer-len-range", str(config.get("outer_len_range", "8:40:1")),
        "--outer-mult-range", str(config.get("outer_mult_range", "1:4:1")),
        "--fixed-sl-range", str(config.get("fixed_sl_range", "1.0:5.0:0.1")),
        "--fixed-tp-range", str(config.get("fixed_tp_range", "0.8:4.0:0.1")),
        "--forced-sl-range", str(config.get("forced_sl_range", "3.0:10.0:0.2")),
        "--forced-tp-range", str(config.get("forced_tp_range", "3.0:10.0:0.2")),
        "--trail-offset-range", str(config.get("trail_offset_range", "4:4:1")),
        "--report-json", report_path,
        "--top-csv", top_path,
    ]
    alpaca_user = str(config.get("alpaca_user", "") or "").strip()
    if alpaca_user:
        command.extend(["--alpaca-user", alpaca_user])
    return run_command(command, cwd=REPO_PATH, timeout=900)

# --- Core Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(url_for('dashboard'))
    next_url = request.args.get('next') or request.form.get('next')
    safe_next_url = next_url if is_safe_redirect_target(next_url) else None
    if request.method == 'POST':
        client_ip = get_client_ip()
        if is_login_rate_limited(client_ip):
            app.logger.warning(f"[SECURITY] Login rate limit triggered for ip={client_ip}")
            flash("Too many login attempts. Try again in a few minutes.", "danger")
            return render_template('login.html', next_url=safe_next_url), 429
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
        user_agent = request.user_agent.string
        if user and check_password_hash(user.password_hash, password):
            clear_login_failures(client_ip)
            session.clear()
            session['user_id'] = user.id
            session['csrf_token'] = secrets.token_urlsafe(32)
            login_logger.info(f"SUCCESSFUL LOGIN | UserID: {user.id}, Username: '{username}', Email: '{user.email}', IP: {ip_address}, Agent: '{user_agent}'")
            flash(gettext("Logged in."), "success")
            return redirect(safe_next_url or url_for('dashboard'))
        mark_login_failure(client_ip)
        login_logger.warning(f"FAILED LOGIN ATTEMPT | Attempted Username: '{username}', IP: {ip_address}, Agent: '{user_agent}'")
        flash(gettext("Invalid credentials."), "danger")
    return render_template('login.html', next_url=safe_next_url)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if g.user:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        tv_user = request.form['tradingview_user']
        password = request.form.get('password', '')
        if len(password) < PASSWORD_MIN_LENGTH:
            flash(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.", "danger")
            return render_template('register.html')
        if User.query.filter((User.username == username) | (User.email == email) | (User.tradingview_user == tv_user)).first():
            flash("Username, email, or TradingView user already exists.", "danger")
        else:
            hashed_pw = generate_password_hash(password)
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

@app.route('/logout', methods=['POST'])
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
    return render_template(
        'user_config.html',
        user=g.user,
        decrypt=decrypt_data,
        current_user=g.user,
        password_min_length=PASSWORD_MIN_LENGTH
    )

@app.route('/config/password', methods=['POST'])
@login_required
def user_change_password():
    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')

    if not current_password or not new_password or not confirm_password:
        flash("All password fields are required.", "danger")
        return redirect(url_for('user_config'))

    if not check_password_hash(g.user.password_hash, current_password):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for('user_config'))

    if new_password != confirm_password:
        flash("New passwords do not match.", "danger")
        return redirect(url_for('user_config'))

    if len(new_password) < PASSWORD_MIN_LENGTH:
        flash(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.", "danger")
        return redirect(url_for('user_config'))

    if check_password_hash(g.user.password_hash, new_password):
        flash("New password must be different from current password.", "danger")
        return redirect(url_for('user_config'))

    g.user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    app.logger.info(f"[USER_ACTION] User '{g.user.username}' changed their password.")
    flash("Password changed successfully.", "success")
    return redirect(url_for('user_config'))

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

@app.route('/admin/strategy', methods=['GET', 'POST'])
@superuser_required
def admin_strategy():
    config = load_strategy_config()

    if request.method == 'POST':
        action = request.form.get('action', 'save')

        def as_int(value, default):
            try:
                return int(str(value).strip())
            except Exception:
                return default

        config['enabled'] = 'enabled' in request.form
        config['alpaca_user'] = request.form.get('alpaca_user', '').strip()
        config['symbol'] = request.form.get('symbol', config.get('symbol', 'TSM')).strip().upper()
        config['timeframe'] = request.form.get('timeframe', config.get('timeframe', '30Min')).strip()
        config['session'] = request.form.get('session', config.get('session', 'regular')).strip()
        config['feed'] = request.form.get('feed', config.get('feed', 'iex')).strip()
        config['live_data_source'] = 'alpaca'
        config['trade_direction'] = request.form.get('trade_direction', config.get('trade_direction', 'Both')).strip()
        config['trials'] = max(1, as_int(request.form.get('trials', config.get('trials', 200)), 200))
        config['top_k'] = max(1, as_int(request.form.get('top_k', config.get('top_k', 20)), 20))
        config['inner_len_range'] = request.form.get('inner_len_range', config.get('inner_len_range', '8:40:1')).strip()
        config['inner_mult_range'] = request.form.get('inner_mult_range', config.get('inner_mult_range', '0.6:1.8:0.1')).strip()
        config['outer_len_range'] = request.form.get('outer_len_range', config.get('outer_len_range', '8:40:1')).strip()
        config['outer_mult_range'] = request.form.get('outer_mult_range', config.get('outer_mult_range', '1:4:1')).strip()
        config['fixed_sl_range'] = request.form.get('fixed_sl_range', config.get('fixed_sl_range', '1.0:5.0:0.1')).strip()
        config['fixed_tp_range'] = request.form.get('fixed_tp_range', config.get('fixed_tp_range', '0.8:4.0:0.1')).strip()
        config['forced_sl_range'] = request.form.get('forced_sl_range', config.get('forced_sl_range', '3.0:10.0:0.2')).strip()
        config['forced_tp_range'] = request.form.get('forced_tp_range', config.get('forced_tp_range', '3.0:10.0:0.2')).strip()
        config['trail_offset_range'] = request.form.get('trail_offset_range', config.get('trail_offset_range', '4:4:1')).strip()
        universe = parse_strategy_universe_from_form(request.form)
        tradable_symbols = set(load_cached_tradable_symbols())
        invalid_symbols = []
        if tradable_symbols:
            valid_universe = []
            for item in universe:
                if item['symbol'] in tradable_symbols:
                    valid_universe.append(item)
                else:
                    invalid_symbols.append(item['symbol'])
            universe = valid_universe
        config['universe'] = universe

        save_strategy_config(config)
        app.logger.info(
            "[STRATEGY] Admin '%s' saved strategy config (enabled=%s, symbol=%s, timeframe=%s).",
            g.user.username,
            config['enabled'],
            config['symbol'],
            config['timeframe'],
        )
        if invalid_symbols:
            flash(f"Skipped symbols not found in Alpaca tradable list: {', '.join(invalid_symbols[:12])}", "warning")

        if action == 'run':
            ok, out, err = run_strategy_optimizer(config)
            if ok:
                app.logger.info("[STRATEGY] Strategy optimizer run successful. Output=%s", out)
                flash("Strategy run completed. Report updated.", "success")
            else:
                app.logger.error("[STRATEGY] Strategy optimizer failed. Error=%s Output=%s", err, out)
                detail = err or out or "Unknown error"
                flash(f"Strategy run failed: {detail}", "danger")
        else:
            flash("Strategy configuration saved.", "success")
        return redirect(url_for('admin_strategy'))

    report = load_strategy_report()
    top_rows = load_strategy_top_rows(limit=int(config.get('top_k', 20)))
    users = User.query.order_by(User.username).all()
    return render_template(
        'admin/strategy.html',
        config=config,
        report=report,
        top_rows=top_rows,
        users=users,
        tradable_symbols=[],
    )

@app.route('/api/admin/strategy/live_snapshot')
@superuser_required
def api_admin_strategy_live_snapshot():
    config = load_strategy_config()
    api_user = get_strategy_api_user(config)
    if not api_user:
        return jsonify({'error': 'no_api_user'}), 400
    api = get_user_api(api_user)
    if not api:
        return jsonify({'error': 'api_not_configured'}), 400

    rows = []
    for item in strategy_store.normalize_universe(config.get('universe')):
        symbol = item['symbol']
        if not item.get('enabled', True) or not strategy_store.local_allowed_for_symbol(symbol, config):
            continue
        try:
            trade = api.get_latest_trade(symbol)
            price = float(getattr(trade, 'price', getattr(trade, 'p', 0.0)) or 0.0)
            rows.append({
                'symbol': symbol,
                'mode': item.get('mode', 'both'),
                'price': price,
                'source': 'Alpaca',
                'status': 'ok',
            })
        except Exception as e:
            app.logger.warning(f"[STRATEGY] Live snapshot failed for {symbol}: {e}")
            rows.append({
                'symbol': symbol,
                'mode': item.get('mode', 'both'),
                'price': None,
                'source': 'Alpaca',
                'status': str(e),
            })

    return jsonify({
        'api_user': api_user.username,
        'feed': config.get('feed', 'sip'),
        'data_source': 'Alpaca',
        'rows': rows,
    })

def backfill_admin_trades_for_shared_keys():
    admin_user = User.query.filter_by(is_superuser=True).first()
    if not admin_user:
        return {'error': 'admin_missing'}
    admin_keys = get_user_keypair(admin_user)
    if not admin_keys:
        return {'error': 'admin_keys_missing'}

    shared_users = []
    for user in User.query.filter(User.id != admin_user.id).all():
        if get_user_keypair(user) == admin_keys:
            shared_users.append(user)

    if not shared_users:
        return {'shared_users': 0, 'created': 0, 'skipped': 0}

    admin_trades = Trade.query.filter_by(user_id=admin_user.id).order_by(Trade.id).all()
    if not admin_trades:
        return {'shared_users': len(shared_users), 'created': 0, 'skipped': 0}

    created = 0
    skipped = 0
    for user in shared_users:
        for trade in admin_trades:
            copy_trade_id = f"copy_{trade.id}_u{user.id}"
            if Trade.query.filter_by(user_id=user.id, trade_id=copy_trade_id).first():
                skipped += 1
                continue
            if trade.status == 'open':
                if Trade.query.filter_by(user_id=user.id, status='open', symbol=trade.symbol).first():
                    skipped += 1
                    continue
            else:
                existing_match = Trade.query.filter_by(
                    user_id=user.id,
                    symbol=trade.symbol,
                    status=trade.status,
                    open_time=trade.open_time,
                    close_time=trade.close_time
                ).first()
                if existing_match:
                    skipped += 1
                    continue

            cloned_trade = Trade(
                user_id=user.id,
                trade_id=copy_trade_id,
                symbol=trade.symbol,
                side=trade.side,
                qty=trade.qty,
                open_price=trade.open_price,
                open_time=trade.open_time,
                status=trade.status,
                close_price=trade.close_price,
                close_time=trade.close_time,
                profit_loss=trade.profit_loss,
                profit_loss_pct=trade.profit_loss_pct,
                action=trade.action
            )
            db.session.add(cloned_trade)
            created += 1

    db.session.commit()
    return {'shared_users': len(shared_users), 'created': created, 'skipped': skipped}

@app.route('/admin/backfill_trades', methods=['POST'])
@superuser_required
def admin_backfill_trades():
    summary = backfill_admin_trades_for_shared_keys()
    if summary.get('error') == 'admin_missing':
        flash('Backfill failed: no admin user found.', 'danger')
    elif summary.get('error') == 'admin_keys_missing':
        flash('Backfill failed: admin Alpaca keys are missing or invalid.', 'danger')
    else:
        app.logger.info(
            "[ADMIN_ACTION] Backfill complete: created=%s skipped=%s shared_users=%s",
            summary.get('created', 0),
            summary.get('skipped', 0),
            summary.get('shared_users', 0)
        )
        flash(
            f"Backfill complete: {summary.get('created', 0)} trades copied, "
            f"{summary.get('skipped', 0)} skipped across {summary.get('shared_users', 0)} users.",
            'success'
        )
    return redirect(url_for('admin_db_management'))

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
    return render_template(
        'admin/user_management.html',
        users=users,
        current_user=g.user,
        password_min_length=PASSWORD_MIN_LENGTH
    )
    
@app.route('/admin/all_open_trades')
@superuser_required
def admin_all_open_trades():
    users = User.query.order_by(User.username).all()
    return render_template('admin/open_trades.html', current_user=g.user, all_users=users)

@app.route('/admin/health')
@superuser_required
def admin_health_dashboard():
    return render_template('admin/health.html', current_user=g.user)

@app.route('/api/admin/server/version')
@superuser_required
def api_admin_server_version():
    if not is_git_repo():
        return jsonify({'error': 'not_git_repo'}), 500
    ensure_last_good_commit()
    current = get_git_commit_info()
    last_good_hash = read_last_good_commit()
    last_good = get_git_commit_info(last_good_hash) if last_good_hash else None
    return jsonify({'current': current, 'last_good': last_good, 'version': get_version_display()})

def restart_server_async(requested_by):
    if not RESTART_COMMAND:
        app.logger.error("[SYSTEM] Restart command is not configured (RESTART_COMMAND is empty).")
        return False, "restart_not_configured"

    def task():
        app.logger.warning(f"[SYSTEM] Restart requested by admin '{requested_by}'.")
        ok, out, err = run_command(RESTART_COMMAND, cwd=REPO_PATH, timeout=60)
        if ok:
            app.logger.info(f"[SYSTEM] Restart command executed successfully. Output: {out}")
        else:
            app.logger.error(f"[SYSTEM] Restart command failed. Error: {err}")

    threading.Thread(target=task, daemon=True).start()
    return True, "restarting"

@app.route('/api/admin/server/restart', methods=['POST'])
@superuser_required
def api_admin_server_restart():
    success, status = restart_server_async(g.user.username)
    if not success:
        return jsonify({'error': status}), 500
    return jsonify({'status': status})

@app.route('/api/admin/server/pull_updates', methods=['POST'])
@superuser_required
def api_admin_pull_updates():
    if not is_git_repo():
        return jsonify({'error': 'not_git_repo'}), 500
    ensure_last_good_commit()
    before = get_git_commit_info()
    if before:
        write_last_good_commit(before["hash"])

    ok_fetch, fetch_out, fetch_err = run_command(["git", "fetch", "--all"], cwd=REPO_PATH, timeout=60)
    if not ok_fetch:
        app.logger.error(f"[UPDATE] Git fetch failed: {fetch_err}")
        return jsonify({'error': 'fetch_failed', 'detail': fetch_err}), 500

    ok_pull, pull_out, pull_err = run_command(["git", "pull", "--ff-only"], cwd=REPO_PATH, timeout=60)
    if not ok_pull:
        app.logger.error(f"[UPDATE] Git pull failed: {pull_err}")
        return jsonify({'error': 'pull_failed', 'detail': pull_err}), 500

    after = get_git_commit_info()
    changed = bool(before and after and before["hash"] != after["hash"])
    before_label = f"{before['short_hash']} {before['subject']}" if before else 'unknown'
    after_label = f"{after['short_hash']} {after['subject']}" if after else 'unknown'
    app.logger.info(
        f"[UPDATE] Admin '{g.user.username}' pulled updates. "
        f"Before={before_label} After={after_label} Output='{pull_out}'"
    )

    if changed:
        increment_version_counter()
    return jsonify({
        'status': 'success',
        'changed': changed,
        'before': before,
        'after': after,
        'restart_recommended': changed,
        'output': pull_out or fetch_out or '',
        'version': get_version_display()
    })

@app.route('/api/admin/server/rollback', methods=['POST'])
@superuser_required
def api_admin_rollback():
    if not is_git_repo():
        return jsonify({'error': 'not_git_repo'}), 500
    payload = request.get_json(silent=True) or {}
    commit_ref = payload.get('commit') or read_last_good_commit()
    if not commit_ref:
        return jsonify({'error': 'no_last_good_commit'}), 400

    ok_reset, reset_out, reset_err = run_command(["git", "reset", "--hard", commit_ref], cwd=REPO_PATH, timeout=60)
    if not ok_reset:
        app.logger.error(f"[UPDATE] Rollback failed: {reset_err}")
        return jsonify({'error': 'rollback_failed', 'detail': reset_err}), 500

    after = get_git_commit_info()
    app.logger.warning(
        f"[UPDATE] Admin '{g.user.username}' rolled back to {commit_ref}. "
        f"Current={after['short_hash'] if after else 'unknown'} Output='{reset_out}'"
    )

    increment_version_counter()
    return jsonify({
        'status': 'rolled_back',
        'after': after,
        'restart_recommended': True,
        'version': get_version_display()
    })

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
    if len(password) < PASSWORD_MIN_LENGTH:
        flash(f'Password must be at least {PASSWORD_MIN_LENGTH} characters.', 'danger')
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
            if len(new_password) < PASSWORD_MIN_LENGTH:
                flash(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.", "danger")
                return redirect(url_for('admin_user_management'))
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
    def read_log_lines(log_file_path, num_lines=200):
        candidates = [log_file_path] + [f"{log_file_path}.{i}" for i in range(1, 6)]
        combined_lines = []
        for path in reversed(candidates):
            if not os.path.exists(path):
                continue
            try:
                if os.path.getsize(path) == 0:
                    continue
            except OSError:
                continue
            try:
                with open(path, 'r') as f:
                    combined_lines.extend(f.readlines())
            except Exception as e:
                return None, f"Could not read log file: {e}"
        if not combined_lines:
            return None, "Log file not found."
        try:
            lines = [line.strip() for line in combined_lines if line.strip()]
            lines = lines[-num_lines:]
            return lines, None
        except Exception as e:
            return None, f"Could not read log file: {e}"

    def strip_log_suffix(line):
        return line.rsplit(' [in ', 1)[0] if ' [in ' in line else line

    def get_log_summary_and_details(log_file_path):
        keywords = [
            'ERROR', 'WARNING', 'FAIL', 'REJECTED', 'SECURITY', 'DATABASE',
            'API_FAIL', 'TRADE_FAIL', 'TRADE_REJECTED', 'BOT_STATUS', 'SYSTEM',
            'UPDATE', 'UI_EVENT'
        ]
        lines, error = read_log_lines(log_file_path)
        if error:
            return [error], [error]
        if not lines:
            return ["No recent log entries."], ["No recent log entries."]
        summary = []
        for line in lines:
            line_upper = line.upper()
            if any(keyword in line_upper for keyword in keywords):
                summary.append(strip_log_suffix(line))
        if not summary:
            summary = ["No recent issues found."]
        return summary[-100:], lines[-200:]

    def parse_webhook_time(timestamp_str):
        try:
            parsed = datetime.fromisoformat(timestamp_str)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo
            parsed = parsed.replace(tzinfo=local_tz)
        return parsed.astimezone(timezone.utc)

    def infer_bot_status_from_log(log_file_path):
        if not os.path.exists(log_file_path):
            return None
        last_modified = datetime.fromtimestamp(os.path.getmtime(log_file_path), tz=timezone.utc)
        seconds_since = (datetime.now(timezone.utc) - last_modified).total_seconds()
        if seconds_since < 300:
            return 'Active', 'Recent trades log activity'
        if seconds_since < 3600:
            return 'Idle', 'Recent trades log activity'
        return None

    def get_systemd_status(service_name):
        if not service_name or os.name != 'posix':
            return None
        ok, out, _ = run_command(["systemctl", "is-active", service_name], timeout=5)
        if not ok:
            return None
        return out.strip()

    webhook_log_file = os.path.join(app.instance_path, 'last_webhook.log')
    last_webhook_utc, bot_status = None, 'Unknown'
    bot_status_reason = ''
    last_webhook_age_sec = None
    if os.path.exists(webhook_log_file):
        try:
            with open(webhook_log_file, 'r') as f:
                last_webhook_utc = f.read().strip()
            last_webhook_dt = parse_webhook_time(last_webhook_utc)
            if last_webhook_dt:
                last_webhook_age_sec = (datetime.now(timezone.utc) - last_webhook_dt).total_seconds()
                if last_webhook_age_sec < 300:
                    bot_status = 'Active'
                elif last_webhook_age_sec < 3600:
                    bot_status = 'Idle'
                else:
                    bot_status = 'Offline'
                bot_status_reason = f"Last webhook {int(last_webhook_age_sec)}s ago"
            else:
                bot_status = 'Error Reading Status'
                bot_status_reason = 'Invalid timestamp in last_webhook.log'
        except Exception:
            bot_status = 'Error Reading Status'
            bot_status_reason = 'Unable to read last_webhook.log'
    else:
        bot_status = 'No Webhooks Received'
        bot_status_reason = 'last_webhook.log not found'

    fallback_status = infer_bot_status_from_log('trades.log')
    if fallback_status and bot_status in ['Offline', 'No Webhooks Received', 'Error Reading Status']:
        bot_status, bot_status_reason = fallback_status

    service_state = get_systemd_status(BOT_SERVICE_NAME)
    if service_state == 'active' and bot_status in ['Offline', 'No Webhooks Received', 'Error Reading Status', 'Unknown']:
        bot_status = 'Active'
        if bot_status_reason:
            bot_status_reason = f"Service active; {bot_status_reason}"
        else:
            bot_status_reason = "Service active (no recent webhooks)"

    db_size_mb = 0
    try:
        db_path = os.path.join(app.instance_path, 'app.db')
        if os.path.exists(db_path):
            db_size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 2)
    except Exception as e:
        app.logger.error(f"[SYSTEM] Could not calculate DB size: {e}")

    dashboard_summary, dashboard_details = get_log_summary_and_details('dashboard.log')
    trades_summary, trades_details = get_log_summary_and_details('trades.log')

    return jsonify({
        'bot_status': bot_status,
        'bot_status_reason': bot_status_reason,
        'last_webhook_utc': last_webhook_utc,
        'last_webhook_age_sec': last_webhook_age_sec,
        'db_size_mb': db_size_mb,
        'dashboard_log_summary': dashboard_summary,
        'dashboard_log_details': dashboard_details,
        'trades_log_summary': trades_summary,
        'trades_log_details': trades_details
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
            api_symbol = symbol.replace('/', '')
            position_to_close = api.get_position(api_symbol)
            close_order = api.close_position(api_symbol)
            app.logger.info(f"[ADMIN_ACTION] Admin '{g.user.username}' closed position {symbol} for user '{user.username}'.")
            closed_count += 1
            def record_close():
                with app.app_context():
                    payload = {'symbol': symbol, 'action': 'close'}
                    data_dict = {'close_order_id': close_order.id}
                    result = record_closed_trade(data_dict, payload, user.id, position_to_close)
                    if result is None:
                        fallback_price = getattr(position_to_close, 'current_price', None) or getattr(position_to_close, 'avg_entry_price', None)
                        if fallback_price is not None:
                            fallback_data = {
                                'close_price': fallback_price,
                                'close_time': datetime.now(timezone.utc).isoformat()
                            }
                            record_closed_trade(fallback_data, payload, user.id, position_to_close)
            threading.Thread(target=record_close, daemon=True).start()
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
        open_time_utc = db_trades.get(p.symbol, datetime.now(timezone.utc))
        out.append({'symbol': p.symbol, 'side': 'sell' if float(p.qty) < 0 else 'buy', 'qty': abs(float(p.qty)), 'open_price': float(p.avg_entry_price), 'current_price': float(p.current_price or 0), 'market_value': float(p.market_value), 'unrealized_pl': float(p.unrealized_pl), 'open_time_iso': open_time_utc.isoformat() if open_time_utc else None})
    return jsonify(out)

@app.route('/api/closed_orders')
@login_required
def api_closed_orders():
    query = Trade.query.filter_by(status='closed')
    if g.user.is_superuser:
        user_filter_id = request.args.get('user_id', '0')
        if user_filter_id != '0':
            try:
                query = query.filter_by(user_id=int(user_filter_id))
            except ValueError:
                return jsonify({"error": "Invalid user filter"}), 400
    else:
        query = query.filter_by(user_id=g.user.id)
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
    payload = request.get_json(silent=True) or {}
    payload['user'] = g.user.tradingview_user
    payload['dashboard_user_id'] = g.user.id
    payload['dashboard_username'] = g.user.username
    payload['dashboard_request'] = True
    symbol = payload.get('symbol')
    action = payload.get('action')
    amount = payload.get('amount')
    order_type = str(payload.get('order_type', 'market')).strip().lower() or 'market'
    time_in_force = str(payload.get('time_in_force', 'day')).strip().lower() or 'day'
    limit_price = payload.get('limit_price')
    extended_hours_raw = payload.get('extended_hours')
    app.logger.info(
        f"[TRADE_MANUAL_REQUEST] user='{g.user.username}' symbol='{symbol}' action='{action}' amount='{amount}' "
        f"order_type='{order_type}' tif='{time_in_force}' extended_hours='{extended_hours_raw}'"
    )
    if not symbol or not action:
        app.logger.warning(f"[TRADE_MANUAL_REJECTED] Missing symbol/action from user='{g.user.username}'.")
        return jsonify({'error': 'invalid_request', 'detail': 'symbol and action are required'}), 400
    if order_type not in ('market', 'limit'):
        return jsonify({'error': 'invalid_request', 'detail': 'order_type must be market or limit'}), 400
    if time_in_force not in ('day', 'gtc', 'ioc', 'fok', 'opg', 'cls'):
        return jsonify({'error': 'invalid_request', 'detail': 'invalid time_in_force value'}), 400
    if order_type == 'limit':
        try:
            limit_price = float(limit_price)
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid_request', 'detail': 'limit_price is required for limit orders'}), 400
        if limit_price <= 0:
            return jsonify({'error': 'invalid_request', 'detail': 'limit_price must be > 0'}), 400
        payload['limit_price'] = limit_price
    else:
        payload.pop('limit_price', None)
    if extended_hours_raw is not None:
        payload['extended_hours'] = str(extended_hours_raw).strip().lower() in ('1', 'true', 'yes', 'y', 'on')
    payload['order_type'] = order_type
    payload['time_in_force'] = time_in_force
    if amount is not None:
        try:
            amount_val = float(amount)
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid_request', 'detail': 'amount must be numeric'}), 400
        if amount_val <= 0:
            return jsonify({'error': 'invalid_request', 'detail': 'amount must be positive'}), 400
        payload['amount'] = amount_val
    try:
        r = requests.post(BOT_WEBHOOK, json=payload, timeout=10)
        if not r.ok:
            detail = r.text
            try:
                detail = r.json().get('error', detail)
            except ValueError:
                pass
            app.logger.warning(
                f"[TRADE_MANUAL_REJECTED] user='{g.user.username}' symbol='{symbol}' action='{action}' status={r.status_code} detail='{detail}'"
            )
            return jsonify({'error': 'proxy_failed', 'detail': detail}), r.status_code
        try:
            response_payload = r.json()
        except ValueError:
            response_payload = {'detail': r.text}
        app.logger.info(
            f"[TRADE_MANUAL_SUCCESS] user='{g.user.username}' symbol='{symbol}' action='{action}' status={r.status_code}"
        )
        return jsonify(response_payload), r.status_code
    except Exception as e:
        detail = str(e)
        if hasattr(e, 'response') and e.response is not None:
            try:
                detail = e.response.json().get('error', e.response.text)
            except ValueError:
                detail = e.response.text
        app.logger.error(
            f"[BOT_PROXY_FAIL] user='{g.user.username}' symbol='{symbol}' action='{action}' detail='{detail}'",
            exc_info=True
        )
        return jsonify({'error': 'proxy_failed', 'detail': detail}), 500

@app.route('/api/internal/log_client_event', methods=['POST'])
@login_required
def log_client_event():
    payload = request.get_json(silent=True) or {}
    event_type = str(payload.get('event_type', 'ui_event'))[:80]
    message = str(payload.get('message', '')).replace('\n', ' ')[:500]
    detail = payload.get('detail')
    if isinstance(detail, (dict, list)):
        detail_str = json.dumps(detail)[:1000]
    elif detail is None:
        detail_str = ''
    else:
        detail_str = str(detail)[:1000]
    app.logger.warning(
        f"[UI_EVENT] user='{g.user.username}' event='{event_type}' message='{message}' detail='{detail_str}'"
    )
    return jsonify({'status': 'ok'})

@app.route('/api/proxy_trade', methods=['POST'])
@login_required
def proxy_trade_alias():
    return proxy_trade_internal()

@app.route('/api/internal/record_trade', methods=['POST'])
def record_trade_internal():
    if not _is_internal_api_key_valid(request.headers.get('X-Internal-API-Key')):
        app.logger.warning("[SECURITY] Unauthorized attempt to access internal record_trade API.")
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        app.logger.warning("[SECURITY] Invalid internal record_trade payload format.")
        return jsonify({'error': 'invalid_payload'}), 400
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
    app.run(host=HOST, port=PORT, debug=DEBUG)
