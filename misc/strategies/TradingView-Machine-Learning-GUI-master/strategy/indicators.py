"""TA-Lib indicator wrappers and signal helpers.

Wrappers handle numpy/pandas conversion and bundle multi-output TA-Lib
calls into single functions.  For the 150+ indicators not wrapped here,
call ``talib`` directly in your strategy and use :func:`to_numpy` /
:func:`wrap` for conversion::

    import talib
    from strategy.indicators import to_numpy, wrap

    df["cci"] = wrap(talib.CCI(to_numpy(df["high"]),
                                to_numpy(df["low"]),
                                to_numpy(df["close"]), timeperiod=20), df.index)
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import talib


# ---------------------------------------------------------------------------
# Conversion helpers (public — use these with any talib function)
# ---------------------------------------------------------------------------

def to_numpy(values: pd.Series | list[float] | np.ndarray) -> np.ndarray:
    """Coerce input to a contiguous float64 numpy array (TA-Lib requirement)."""
    if isinstance(values, pd.Series):
        return values.to_numpy(dtype="float64", na_value=np.nan)
    return np.asarray(values, dtype="float64")


def wrap(arr: np.ndarray, index: pd.Index | None = None) -> pd.Series:
    """Wrap a numpy array back into a pandas Series."""
    return pd.Series(arr, index=index, dtype="float64")


def _idx(values: object) -> pd.Index | None:
    return values.index if isinstance(values, pd.Series) else None


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------

def ema(values: pd.Series, length: int) -> pd.Series:
    """Exponential Moving Average."""
    return wrap(talib.EMA(to_numpy(values), timeperiod=length), _idx(values))


def sma(values: pd.Series, length: int) -> pd.Series:
    """Simple Moving Average."""
    return wrap(talib.SMA(to_numpy(values), timeperiod=length), _idx(values))


def wma(values: pd.Series, length: int) -> pd.Series:
    """Weighted Moving Average."""
    return wrap(talib.WMA(to_numpy(values), timeperiod=length), _idx(values))


# ---------------------------------------------------------------------------
# Momentum / oscillators
# ---------------------------------------------------------------------------

def rsi(values: pd.Series, length: int = 14) -> pd.Series:
    """Relative Strength Index."""
    return wrap(talib.RSI(to_numpy(values), timeperiod=length), _idx(values))


def macd(
    values: pd.Series,
    fast_length: int = 12,
    slow_length: int = 26,
    signal_length: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD.  Returns ``(macd_line, signal_line, histogram)``."""
    idx = _idx(values)
    line, signal, hist = talib.MACD(
        to_numpy(values),
        fastperiod=fast_length,
        slowperiod=slow_length,
        signalperiod=signal_length,
    )
    return wrap(line, idx), wrap(signal, idx), wrap(hist, idx)


def stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_length: int = 14,
    k_smoothing: int = 3,
    d_smoothing: int = 3,
) -> tuple[pd.Series, pd.Series]:
    """Stochastic Oscillator.  Returns ``(%K, %D)``."""
    idx = _idx(high)
    slowk, slowd = talib.STOCH(
        to_numpy(high), to_numpy(low), to_numpy(close),
        fastk_period=k_length,
        slowk_period=k_smoothing, slowk_matype=0,
        slowd_period=d_smoothing, slowd_matype=0,
    )
    return wrap(slowk, idx), wrap(slowd, idx)


def stochastic_rsi(
    values: pd.Series,
    length: int = 14,
    k_smoothing: int = 3,
    d_smoothing: int = 3,
) -> tuple[pd.Series, pd.Series]:
    """Stochastic RSI via TA-Lib.  Returns (%K, %D)."""
    idx = _idx(values)
    fastk, fastd = talib.STOCHRSI(
        to_numpy(values),
        timeperiod=length,
        fastk_period=k_smoothing,
        fastd_period=d_smoothing,
        fastd_matype=0,
    )
    return wrap(fastk, idx), wrap(fastd, idx)


def cci(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = 14,
) -> pd.Series:
    """Commodity Channel Index (CCI) via TA-Lib."""
    idx = _idx(high)
    return wrap(talib.CCI(to_numpy(high), to_numpy(low), to_numpy(close), timeperiod=length), idx)


def williams_r(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = 14,
) -> pd.Series:
    """Williams %R via TA-Lib."""
    idx = _idx(high)
    return wrap(talib.WILLR(to_numpy(high), to_numpy(low), to_numpy(close), timeperiod=length), idx)


def momentum(values: pd.Series | list[float] | np.ndarray, length: int = 10) -> pd.Series:
    """Momentum (MOM) via TA-Lib."""
    return wrap(talib.MOM(to_numpy(values), timeperiod=length), _idx(values))


def roc(values: pd.Series | list[float] | np.ndarray, length: int = 10) -> pd.Series:
    """Rate of Change (ROC) via TA-Lib."""
    return wrap(talib.ROC(to_numpy(values), timeperiod=length), _idx(values))


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------

def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = 14,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Average Directional Index.  Returns ``(ADX, +DI, -DI)``."""
    idx = _idx(high)
    h, l, c = to_numpy(high), to_numpy(low), to_numpy(close)
    return (
        wrap(talib.ADX(h, l, c, timeperiod=length), idx),
        wrap(talib.PLUS_DI(h, l, c, timeperiod=length), idx),
        wrap(talib.MINUS_DI(h, l, c, timeperiod=length), idx),
    )

def aroon(
    high: pd.Series,
    low: pd.Series,
    length: int = 25,
) -> tuple[pd.Series, pd.Series]:
    """Aroon indicator via TA-Lib.  Returns (aroon_down, aroon_up)."""
    idx = _idx(high)
    down, up = talib.AROON(to_numpy(high), to_numpy(low), timeperiod=length)
    return wrap(down, idx), wrap(up, idx)


def psar(
    high: pd.Series,
    low: pd.Series,
    acceleration: float = 0.02,
    maximum: float = 0.2,
) -> pd.Series:
    """Parabolic SAR via TA-Lib."""
    return wrap(talib.SAR(to_numpy(high), to_numpy(low), acceleration=acceleration, maximum=maximum), _idx(high))


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------

def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Average True Range."""
    return wrap(talib.ATR(to_numpy(high), to_numpy(low), to_numpy(close), timeperiod=length), _idx(high))


def bollinger_bands(
    values: pd.Series,
    length: int = 20,
    std_dev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands.  Returns ``(upper, middle, lower)``."""
    idx = _idx(values)
    upper, middle, lower = talib.BBANDS(
        to_numpy(values), timeperiod=length,
        nbdevup=std_dev, nbdevdn=std_dev, matype=0,
    )
    return wrap(upper, idx), wrap(middle, idx), wrap(lower, idx)


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On Balance Volume (OBV) via TA-Lib."""
    idx = _idx(close)
    return wrap(talib.OBV(to_numpy(close), to_numpy(volume)), idx)


def mfi(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    length: int = 14,
) -> pd.Series:
    """Money Flow Index (MFI) via TA-Lib."""
    idx = _idx(high)
    return wrap(talib.MFI(to_numpy(high), to_numpy(low), to_numpy(close), to_numpy(volume), timeperiod=length), idx)


def ad(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Chaikin A/D Line via TA-Lib."""
    idx = _idx(high)
    return wrap(talib.AD(to_numpy(high), to_numpy(low), to_numpy(close), to_numpy(volume)), idx)


def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
) -> pd.Series:
    """Volume Weighted Average Price (cumulative intraday VWAP)."""
    typical_price = (high + low + close) / 3.0
    cum_tp_vol = (typical_price * volume).cumsum()
    cum_vol = volume.cumsum()
    return cum_tp_vol / cum_vol


# ---------------------------------------------------------------------------
# Signal helpers (pure-pandas, no TA-Lib equivalent)
# ---------------------------------------------------------------------------

def barssince(condition: pd.Series | list[bool] | np.ndarray) -> pd.Series:
    """Count bars since *condition* was last True (vectorised, no Python loop)."""
    if not isinstance(condition, pd.Series):
        condition = pd.Series(condition)
    mask = condition.to_numpy(dtype=bool, na_value=False)
    pos = np.arange(len(mask), dtype="float64")
    # Record the bar index at each True position, NaN elsewhere; ffill propagates forward.
    last_true_pos = pd.Series(np.where(mask, pos, np.nan)).ffill().to_numpy()
    result = np.where(np.isnan(last_true_pos), np.nan, pos - last_true_pos)
    return pd.Series(result, index=condition.index, dtype="float64")


def crossed_above(left: pd.Series, right: pd.Series) -> pd.Series:
    """True on bars where *left* crosses above *right*."""
    return (left > right) & (left.shift(1) <= right.shift(1))


def crossed_below(left: pd.Series, right: pd.Series) -> pd.Series:
    """True on bars where *left* crosses below *right*."""
    return (left < right) & (left.shift(1) >= right.shift(1))


def to_unix_timestamp(value: str | None) -> int | None:
    """Convert an ISO date string to a UTC unix timestamp."""
    if value is None:
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return int(math.floor(timestamp.timestamp()))
