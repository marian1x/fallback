import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import importlib

# Ensure env vars to satisfy dashboard imports
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('ALPACA_KEY', 'key')
os.environ.setdefault('ALPACA_SECRET', 'secret')

import dashboard
importlib.reload(dashboard)


def test_translate_identity():
    assert dashboard.translate("Hello") == "Hello"


def test_translate_format():
    assert dashboard.translate("Hi %(name)s", name="Ana") == "Hi Ana"
