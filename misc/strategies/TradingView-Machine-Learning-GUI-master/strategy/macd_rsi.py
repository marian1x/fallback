from __future__ import annotations

from typing import Any

import pandas as pd

from .base import BaseStrategy
from . import register_strategy
from .indicators import barssince, crossed_above, crossed_below, macd, rsi


@register_strategy
class MacdRsiStrategy(BaseStrategy):
    """Pure-Python MACD-RSI strategy matching the TradingView Pine Script reference."""

    strategy_name = "macd_rsi"

    def default_settings(self) -> dict[str, Any]:
        return {
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "rsi_length": 14,
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
            "signal_lookback_bars": 10,
            "enable_long": True,
            "enable_short": True,
            "start": None,
            "end": None,
        }

    def required_columns(self) -> list[str]:
        return ["time", "open", "high", "low", "close"]

    def generate_signals(self, candles: pd.DataFrame, settings: dict[str, Any]) -> pd.DataFrame:
        s = {**self.default_settings(), **settings}
        dataframe = self.prepare_candles(candles)

        dataframe["rsi"] = rsi(dataframe["close"], s["rsi_length"])
        dataframe["macd_line"], dataframe["signal_line"], dataframe["hist_line"] = macd(
            dataframe["close"],
            s["macd_fast"],
            s["macd_slow"],
            s["macd_signal"],
        )

        rsi_oversold_hit = dataframe["rsi"] <= s["rsi_oversold"]
        rsi_overbought_hit = dataframe["rsi"] >= s["rsi_overbought"]
        dataframe["bars_since_oversold"] = barssince(rsi_oversold_hit)
        dataframe["bars_since_overbought"] = barssince(rsi_overbought_hit)
        was_oversold = dataframe["bars_since_oversold"] <= s["signal_lookback_bars"]
        was_overbought = dataframe["bars_since_overbought"] <= s["signal_lookback_bars"]
        crossover_bull = crossed_above(dataframe["macd_line"], dataframe["signal_line"])
        crossover_bear = crossed_below(dataframe["macd_line"], dataframe["signal_line"])
        dataframe["buy_signal"] = was_oversold & crossover_bull
        dataframe["sell_signal"] = was_overbought & crossover_bear

        self._apply_date_range(dataframe, s["start"], s["end"])

        dataframe["enable_long"] = s["enable_long"]
        dataframe["enable_short"] = s["enable_short"]
        return dataframe
