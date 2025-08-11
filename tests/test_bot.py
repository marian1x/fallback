import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault('ALPACA_KEY', 'key')
os.environ.setdefault('ALPACA_SECRET', 'secret')

import pytest
from types import SimpleNamespace
import bot

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
    monkeypatch.setattr(bot, 'api', MockAPI())
    assert bot.get_last_price('AAPL') == 150.0

def test_get_last_price_crypto(monkeypatch):
    class MockTrade:
        p = 25000.0
    class MockAPI(SimpleNamespace):
        def get_latest_crypto_trade(self, symbol, exchange):
            assert symbol == 'BTCUSD'
            assert exchange == 'CBSE'
            return MockTrade()
    monkeypatch.setattr(bot, 'api', MockAPI())
    assert bot.get_last_price('BTC/USD') == 25000.0
