import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import importlib

# Ensure env vars to satisfy dashboard imports
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('ALPACA_KEY', 'key')
os.environ.setdefault('ALPACA_SECRET', 'secret')

import dashboard
importlib.reload(dashboard)


def test_dashboard_is_close_action():
    assert dashboard.is_close_action('close long')
    assert not dashboard.is_close_action('hold')
