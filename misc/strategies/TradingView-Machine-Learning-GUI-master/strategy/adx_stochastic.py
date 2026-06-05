from __future__ import annotations

from typing import Any

import pandas as pd

from .base import BaseStrategy
from . import register_strategy
from .indicators import adx, stochastic, crossed_above, crossed_below, barssince


@register_strategy
class AdxStochasticStrategy(BaseStrategy):
    """ADX + Stochastic Oscillator strategy.

    Enters trades when ADX confirms a strong trend and the Stochastic
    Oscillator signals a reversal from oversold/overbought territory.

    Buy:  ADX > threshold AND Stochastic %K crosses above %D
          while %K was recently in the oversold zone.
    Sell: ADX > threshold AND Stochastic %K crosses below %D
          while %K was recently in the overbought zone.
    """

    strategy_name = "adx_stochastic"

    def default_settings(self) -> dict[str, Any]:
        return {
            "adx_length": 14,
            "adx_threshold": 20,
            "stoch_k_length": 14,
            "stoch_k_smoothing": 3,
            "stoch_d_smoothing": 3,
            "stoch_oversold": 20,
            "stoch_overbought": 80,
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

        # ADX
        dataframe["adx"], dataframe["plus_di"], dataframe["minus_di"] = adx(
            dataframe["high"],
            dataframe["low"],
            dataframe["close"],
            s["adx_length"],
        )

        # Stochastic
        dataframe["stoch_k"], dataframe["stoch_d"] = stochastic(
            dataframe["high"],
            dataframe["low"],
            dataframe["close"],
            s["stoch_k_length"],
            s["stoch_k_smoothing"],
            s["stoch_d_smoothing"],
        )

        # Trend strength filter
        strong_trend = dataframe["adx"] > s["adx_threshold"]

        # Stochastic zone detection
        stoch_oversold_hit = dataframe["stoch_k"] <= s["stoch_oversold"]
        stoch_overbought_hit = dataframe["stoch_k"] >= s["stoch_overbought"]
        dataframe["bars_since_oversold"] = barssince(stoch_oversold_hit)
        dataframe["bars_since_overbought"] = barssince(stoch_overbought_hit)
        was_oversold = dataframe["bars_since_oversold"] <= s["signal_lookback_bars"]
        was_overbought = dataframe["bars_since_overbought"] <= s["signal_lookback_bars"]

        # Stochastic crossovers
        k_cross_above_d = crossed_above(dataframe["stoch_k"], dataframe["stoch_d"])
        k_cross_below_d = crossed_below(dataframe["stoch_k"], dataframe["stoch_d"])

        # Combined signals
        dataframe["buy_signal"] = strong_trend & was_oversold & k_cross_above_d
        dataframe["sell_signal"] = strong_trend & was_overbought & k_cross_below_d

        # Date range filter
        self._apply_date_range(dataframe, s["start"], s["end"])

        dataframe["enable_long"] = s["enable_long"]
        dataframe["enable_short"] = s["enable_short"]
        return dataframe
