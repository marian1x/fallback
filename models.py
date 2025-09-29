# models.py
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    tradingview_user = db.Column(db.String(80), unique=True, nullable=True)
    
    encrypted_alpaca_key = db.Column(db.String(512))
    encrypted_alpaca_secret = db.Column(db.String(512))
    
    per_trade_amount = db.Column(db.Float, default=1000.0)
    
    is_superuser = db.Column(db.Boolean, default=False, nullable=False)
    
    # FIX: New fields for admin control
    is_trading_restricted = db.Column(db.Boolean, default=False, nullable=False)
    
    trades = db.relationship('Trade', backref='user', lazy=True)

class Trade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    
    trade_id = db.Column(db.String, nullable=False)
    symbol = db.Column(db.String, nullable=False)
    side = db.Column(db.String, nullable=False)
    qty = db.Column(db.Float, nullable=False)
    open_price = db.Column(db.Float, nullable=False)
    open_time = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    status = db.Column(db.String, default='open', nullable=False)
    close_price = db.Column(db.Float)
    close_time = db.Column(db.DateTime(timezone=True))
    profit_loss = db.Column(db.Float)
    profit_loss_pct = db.Column(db.Float)
    action = db.Column(db.String, nullable=True)