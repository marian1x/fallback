import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("ALPACA_KEY", "key")
os.environ.setdefault("ALPACA_SECRET", "secret")
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from flask import Flask

from models import db, Trade, User
import trade_db


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


def test_record_open_trade(app):
    payload = {'symbol': 'AAPL', 'action': 'buy', 'strategy': 'macd_sma', 'strategy_job_id': 'job-123'}
    data = {'order_id': '123', 'side': 'buy', 'qty': 10, 'price': 100, 'open_time': '2026-06-05T13:00:00+00:00'}
    with app.app_context():
        user = User(username='tester', email='tester@example.com', password_hash='hashed')
        db.session.add(user)
        db.session.commit()
        t = trade_db.record_open_trade(data, payload, user.id)
        assert t is not None
        assert t.symbol == 'AAPL'
        assert t.status == 'open'
        assert t.open_time.hour == 13
        assert t.strategy == 'macd_sma'
        assert t.strategy_job_id == 'job-123'

        t2 = trade_db.record_open_trade(data, payload, user.id)
        assert t2 is None
        assert Trade.query.count() == 1


def test_record_closed_trade(app, monkeypatch):
    open_payload = {'symbol': 'AAPL', 'action': 'buy', 'strategy': 'keltner', 'strategy_job_id': 'job-kc'}
    open_data = {'order_id': '1', 'side': 'buy', 'qty': 10, 'price': 100}
    with app.app_context():
        user = User(username='tester', email='tester@example.com', password_hash='hashed')
        db.session.add(user)
        db.session.commit()
        trade_db.record_open_trade(open_data, open_payload, user.id)

        position_obj = SimpleNamespace(
            avg_entry_price=100,
            qty=10,
            side='long',
            asset_id='asset123'
        )

        class MockOrder:
            status = 'filled'
            filled_avg_price = 110
            filled_at = datetime(2023, 1, 1, tzinfo=timezone.utc)

        class MockAPI(SimpleNamespace):
            def get_order(self, order_id):
                assert order_id == 'close123'
                return MockOrder()

        monkeypatch.setattr(trade_db, 'get_api_for_user', lambda user_id: MockAPI())
        monkeypatch.setattr(trade_db.time, 'sleep', lambda s: None)

        close_data = {'close_order_id': 'close123'}
        t = trade_db.record_closed_trade(close_data, {'symbol': 'AAPL', 'action': 'sell'}, user.id, position_obj)
        assert t.status == 'closed'
        assert t.close_price == 110
        assert t.profit_loss == (110 - 100) * 10
        assert t.strategy == 'keltner'
        assert t.strategy_job_id == 'job-kc'
        assert Trade.query.count() == 1


def test_record_closed_trade_fallback_on_unfilled_order(app, monkeypatch):
    open_payload = {'symbol': 'AAPL', 'action': 'buy'}
    open_data = {'order_id': '1', 'side': 'buy', 'qty': 10, 'price': 100}
    with app.app_context():
        user = User(username='tester2', email='tester2@example.com', password_hash='hashed')
        db.session.add(user)
        db.session.commit()
        trade_db.record_open_trade(open_data, open_payload, user.id)

        position_obj = SimpleNamespace(
            avg_entry_price=100,
            qty=10,
            side='long',
            asset_id='asset123'
        )

        class MockOrder:
            status = 'accepted'
            filled_avg_price = None
            filled_at = None

        class MockTrade:
            price = 105

        class MockAPI(SimpleNamespace):
            def get_order(self, order_id):
                assert order_id == 'close123'
                return MockOrder()
            def get_position(self, symbol):
                raise trade_db.AlpacaAPIError("position not found")
            def get_latest_trade(self, symbol):
                return MockTrade()

        monkeypatch.setattr(trade_db, 'get_api_for_user', lambda user_id: MockAPI())
        monkeypatch.setattr(trade_db.time, 'sleep', lambda s: None)

        close_data = {'close_order_id': 'close123'}
        t = trade_db.record_closed_trade(close_data, {'symbol': 'AAPL', 'action': 'sell'}, user.id, position_obj)
        assert t.status == 'closed'
        assert t.close_price == 105


def test_record_closed_trade_with_override_price(app, monkeypatch):
    open_payload = {'symbol': 'AAPL', 'action': 'buy'}
    open_data = {'order_id': '1', 'side': 'buy', 'qty': 5, 'price': 100}
    with app.app_context():
        user = User(username='tester3', email='tester3@example.com', password_hash='hashed')
        db.session.add(user)
        db.session.commit()
        trade_db.record_open_trade(open_data, open_payload, user.id)

        position_obj = SimpleNamespace(
            avg_entry_price=100,
            qty=5,
            side='long',
            asset_id='asset123'
        )

        class MockAPI(SimpleNamespace):
            pass

        monkeypatch.setattr(trade_db, 'get_api_for_user', lambda user_id: MockAPI())

        close_data = {
            'close_price': 111,
            'close_time': '2023-01-02T12:00:00+00:00'
        }
        t = trade_db.record_closed_trade(close_data, {'symbol': 'AAPL', 'action': 'sell'}, user.id, position_obj)
        assert t.status == 'closed'
        assert t.close_price == 111


def test_record_closed_trade_replaces_fallback_close_price_with_order_fill(app, monkeypatch):
    open_payload = {'symbol': 'AAPL', 'action': 'buy'}
    open_data = {'order_id': '1', 'side': 'buy', 'qty': 5, 'price': 100}
    with app.app_context():
        user = User(username='tester4', email='tester4@example.com', password_hash='hashed')
        db.session.add(user)
        db.session.commit()
        trade_db.record_open_trade(open_data, open_payload, user.id)

        position_obj = SimpleNamespace(
            avg_entry_price=100,
            qty=5,
            side='long',
            asset_id='asset123'
        )

        class MockOrder:
            status = 'filled'
            filled_avg_price = 112
            filled_at = datetime(2023, 1, 3, tzinfo=timezone.utc)

        class MockAPI(SimpleNamespace):
            def get_order(self, order_id):
                assert order_id == 'close123'
                return MockOrder()

        monkeypatch.setattr(trade_db, 'get_api_for_user', lambda user_id: MockAPI())
        monkeypatch.setattr(trade_db.time, 'sleep', lambda s: None)

        close_data = {
            'close_order_id': 'close123',
            'close_price': 100,
            'close_price_source': 'latest_trade_fallback',
            'close_price_authoritative': False,
            'close_time': '2023-01-03T12:00:00+00:00'
        }
        t = trade_db.record_closed_trade(close_data, {'symbol': 'AAPL', 'action': 'sell'}, user.id, position_obj)
        assert t.status == 'closed'
        assert t.close_price == 112
        assert t.profit_loss == 60


def test_record_closed_trade_skips_fallback_when_position_still_open(app, monkeypatch):
    open_payload = {'symbol': 'AAPL', 'action': 'buy'}
    open_data = {'order_id': '1', 'side': 'buy', 'qty': 5, 'price': 100}
    with app.app_context():
        user = User(username='tester5', email='tester5@example.com', password_hash='hashed')
        db.session.add(user)
        db.session.commit()
        trade_db.record_open_trade(open_data, open_payload, user.id)

        position_obj = SimpleNamespace(
            avg_entry_price=100,
            qty=5,
            side='long',
            asset_id='asset123'
        )

        class MockOrder:
            status = 'accepted'
            filled_avg_price = None
            filled_at = None

        class MockAPI(SimpleNamespace):
            def get_order(self, order_id):
                assert order_id == 'close123'
                return MockOrder()

            def get_position(self, symbol):
                assert symbol == 'AAPL'
                return SimpleNamespace(symbol='AAPL')

        monkeypatch.setattr(trade_db, 'get_api_for_user', lambda user_id: MockAPI())
        monkeypatch.setattr(trade_db.time, 'sleep', lambda s: None)

        close_data = {
            'close_order_id': 'close123',
            'close_price': 100,
            'close_price_source': 'latest_trade_fallback',
            'close_price_authoritative': False,
            'close_time': '2023-01-03T12:00:00+00:00'
        }
        assert trade_db.record_closed_trade(close_data, {'symbol': 'AAPL', 'action': 'sell'}, user.id, position_obj) is None
        assert Trade.query.filter_by(status='open').count() == 1


def test_record_closed_trade_skips_when_no_open_trade(app, monkeypatch):
    with app.app_context():
        user = User(username='tester6', email='tester6@example.com', password_hash='hashed')
        db.session.add(user)
        db.session.commit()

        position_obj = SimpleNamespace(
            avg_entry_price=100,
            qty=5,
            side='long',
            asset_id='asset123'
        )

        class MockAPI(SimpleNamespace):
            pass

        monkeypatch.setattr(trade_db, 'get_api_for_user', lambda user_id: MockAPI())

        close_data = {
            'close_price': 111,
            'close_time': '2023-01-02T12:00:00+00:00'
        }
        assert trade_db.record_closed_trade(close_data, {'symbol': 'AAPL', 'action': 'sell'}, user.id, position_obj) is None
        assert Trade.query.count() == 0
