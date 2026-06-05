import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import importlib

# Ensure env vars to satisfy dashboard imports
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ.setdefault('ALPACA_KEY', 'key')
os.environ.setdefault('ALPACA_SECRET', 'secret')

import dashboard
importlib.reload(dashboard)
from datetime import datetime, timezone
import pytest
from flask import Flask
from models import db, Trade, User


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def test_translate_identity():
    assert dashboard.translate("Hello") == "Hello"


def test_translate_format():
    assert dashboard.translate("Hi %(name)s", name="Ana") == "Hi Ana"


def test_visible_closed_trades_query_excludes_synthetic_mirror_rows_by_default(app):
    with app.app_context():
        user = User(username='u1', email='u1@example.com', password_hash='hashed')
        db.session.add(user)
        db.session.commit()
        db.session.add(Trade(
            user_id=user.id,
            trade_id='closed_mirror_AAPL_123',
            symbol='AAPL',
            side='buy',
            qty=1,
            open_price=100,
            close_price=100,
            open_time=datetime.now(timezone.utc),
            close_time=datetime.now(timezone.utc),
            status='closed',
            profit_loss=0,
        ))
        db.session.add(Trade(
            user_id=user.id,
            trade_id='real_order',
            symbol='MSFT',
            side='buy',
            qty=1,
            open_price=100,
            close_price=101,
            open_time=datetime.now(timezone.utc),
            close_time=datetime.now(timezone.utc),
            status='closed',
            profit_loss=1,
        ))
        db.session.commit()

        with app.test_request_context('/api/closed_orders'):
            rows = dashboard.visible_closed_trades_query(Trade.query.filter_by(status='closed')).all()

        assert [row.trade_id for row in rows] == ['real_order']
