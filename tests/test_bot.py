import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ.setdefault('ALPACA_KEY', 'key')
os.environ.setdefault('ALPACA_SECRET', 'secret')

import pytest
from types import SimpleNamespace
from flask import Flask
import bot
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

def test_is_close_action_true():
    assert bot.is_close_action('close long')
    assert bot.is_close_action('CLOSE')

def test_is_close_action_false():
    assert not bot.is_close_action('buy')

def test_is_crypto():
    assert bot.is_crypto('BTCUSD')
    assert bot.is_crypto('ETH/USD')
    assert not bot.is_crypto('AAPL')

def test_get_last_price_stock(monkeypatch):
    class MockTrade:
        price = 150.0
    class MockAPI(SimpleNamespace):
        def get_latest_trade(self, symbol):
            assert symbol == 'AAPL'
            return MockTrade()
    assert bot.get_last_price(MockAPI(), 'AAPL') == 150.0

def test_get_last_price_crypto(monkeypatch):
    class MockTrade:
        p = 25000.0
    class MockAPI(SimpleNamespace):
        def get_latest_crypto_trade(self, symbol, exchange):
            assert symbol == 'BTCUSD'
            assert exchange == 'CBSE'
            return MockTrade()
    assert bot.get_last_price(MockAPI(), 'BTC/USD') == 25000.0

def test_equity_limit_price_uses_alpaca_tick_size():
    assert bot._build_limit_price(14.29, 'buy', equity=True) == 14.33
    assert bot._build_limit_price(300.99, 'sell', equity=True) == 300.23

def test_sub_dollar_equity_limit_price_allows_four_decimals():
    assert bot._round_equity_limit_price(0.456789, 'buy') == 0.4568
    assert bot._round_equity_limit_price(0.456789, 'sell') == 0.4567


def test_mirror_close_skips_when_no_db_open_trade(app, monkeypatch):
    with app.app_context():
        user = User(username='mirror', email='mirror@example.com', password_hash='hashed')
        db.session.add(user)
        db.session.commit()

        result = bot.mirror_close_trade(user, {'symbol': 'AAPL', 'action': 'close'}, SimpleNamespace())

        assert result is None
        assert Trade.query.count() == 0
