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
    payload = {'symbol': 'AAPL', 'action': 'buy'}
    data = {'order_id': '123', 'side': 'buy', 'qty': 10, 'price': 100}
    with app.app_context():
        user = User(username='tester', email='tester@example.com', password_hash='hashed')
        db.session.add(user)
        db.session.commit()
        t = trade_db.record_open_trade(data, payload, user.id)
        assert t is not None
        assert t.symbol == 'AAPL'
        assert t.status == 'open'

        t2 = trade_db.record_open_trade(data, payload, user.id)
        assert t2 is None
        assert Trade.query.count() == 1


def test_record_closed_trade(app, monkeypatch):
    open_payload = {'symbol': 'AAPL', 'action': 'buy'}
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
        assert Trade.query.count() == 1
