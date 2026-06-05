from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Canonical allowed-value sets (single source of truth)
# ---------------------------------------------------------------------------

ALLOWED_SESSIONS: frozenset[str] = frozenset({"regular", "extended"})
ALLOWED_MODES: frozenset[str] = frozenset({"long", "short", "both"})
ALLOWED_ADJUSTMENTS: frozenset[str] = frozenset({"splits", "dividends", "none"})
ALLOWED_DATAFORMATS: frozenset[str] = frozenset({"csv"})
ALLOWED_OBJECTIVES: frozenset[str] = frozenset({
    "net_profit_pct",
    "profit_factor",
    "win_rate_pct",
    "max_drawdown_pct",
    "trade_count",
})


# ---------------------------------------------------------------------------
# Validation helpers — dict-key style (used by config.py)
# ---------------------------------------------------------------------------

def require_choice(
    config: dict[str, Any],
    key: str,
    allowed: frozenset[str] | set[str],
    *,
    prefix: str = "",
    label: str = "Config value",
) -> None:
    value = config.get(key)
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{label} '{prefix}{key}' must be one of: {choices}")


def require_non_empty_string(
    config: dict[str, Any],
    key: str,
    *,
    prefix: str = "",
    label: str = "Config value",
) -> None:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} '{prefix}{key}' must be a non-empty string")


def require_positive_int(
    config: dict[str, Any],
    key: str,
    *,
    prefix: str = "",
    label: str = "Config value",
) -> None:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} '{prefix}{key}' must be a positive integer")


def require_number(
    config: dict[str, Any],
    key: str,
    *,
    prefix: str = "",
    label: str = "Config value",
) -> None:
    value = config.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} '{prefix}{key}' must be a number")


def require_positive_number(
    config: dict[str, Any],
    key: str,
    *,
    prefix: str = "",
    label: str = "Config value",
) -> None:
    require_number(config, key, prefix=prefix, label=label)
    if float(config[key]) <= 0:
        raise ValueError(f"{label} '{prefix}{key}' must be greater than 0")


def require_pair_string(value: Any, *, key: str, label: str = "Config value") -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} '{key}' must be a non-empty string")
    parts = value.split(":")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(
            f"{label} '{key}' has invalid format '{value}'. "
            f"Expected 'EXCHANGE:SYMBOL' (e.g. 'NASDAQ:AAPL')."
        )


# ---------------------------------------------------------------------------
# Value-style validators (used by presets.py where values are passed directly)
# ---------------------------------------------------------------------------

def check_choice(value: Any, allowed: frozenset[str] | set[str], key: str, label: str = "Preset file value") -> None:
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{label} '{key}' must be one of: {choices}")


def check_non_empty_string(value: Any, key: str, label: str = "Preset file value") -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} '{key}' must be a non-empty string")


def check_positive_number(value: Any, key: str, label: str = "Preset file value") -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        raise ValueError(f"{label} '{key}' must be greater than 0")
