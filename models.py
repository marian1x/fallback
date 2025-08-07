# models.py
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

class Trade(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    trade_id        = db.Column(db.String, unique=True, nullable=False)
    symbol          = db.Column(db.String, nullable=False)
    side            = db.Column(db.String, nullable=False)
    qty             = db.Column(db.Float, nullable=False)
    open_price      = db.Column(db.Float, nullable=False)
    open_time       = db.Column(db.DateTime, nullable=False)
    status          = db.Column(db.String, default='open', nullable=False)
    close_price     = db.Column(db.Float)
    close_time      = db.Column(db.DateTime)
    profit_loss     = db.Column(db.Float)
    profit_loss_pct = db.Column(db.Float)
    action          = db.Column(db.String, nullable=True)
