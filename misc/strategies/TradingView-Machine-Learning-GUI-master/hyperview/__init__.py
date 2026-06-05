"""HyperView — CLI-driven TradingView strategy backtester and hyper-optimizer."""

from .runtime import configure_pycache

configure_pycache()

from .backtest import TradingViewLikeBacktester
from .downloader import TradingViewDataClient
from strategy import get_strategy, list_strategies

__all__ = [
    "TradingViewDataClient",
    "TradingViewLikeBacktester",
    "get_strategy",
    "list_strategies",
]
