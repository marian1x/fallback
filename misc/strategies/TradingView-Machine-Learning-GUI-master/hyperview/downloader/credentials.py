from __future__ import annotations

import configparser
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TradingViewCredentials:
    auth_token: str
    session_id: str | None
    source: str
    cookies: dict[str, str]


def resolve_credentials() -> TradingViewCredentials:
    auth_token, session_id, source, cookies = _load_from_firefox_profile()

    return TradingViewCredentials(
        auth_token=auth_token or "unauthorized_user_token",
        session_id=session_id,
        source=source or "anonymous",
        cookies=cookies,
    )


# ---------------------------------------------------------------------------
# Firefox profile helpers
# ---------------------------------------------------------------------------

def _load_from_firefox_profile() -> tuple[str | None, str | None, str | None, dict[str, str]]:
    profile_dir = _default_firefox_profile_dir()
    if profile_dir is None:
        return None, None, None, {}

    cookies = _load_cookie_map(profile_dir)
    session_id = _clean(cookies.get("sessionid"))
    auth_token = _clean(cookies.get("auth_token"))
    if auth_token is None:
        auth_token = _read_local_storage(profile_dir, ["auth_token", "tv_auth_token"])
    if auth_token or session_id:
        return auth_token, session_id, "firefox-profile", cookies
    return None, None, None, cookies


def _default_firefox_profile_dir() -> Path | None:
    app_data = os.getenv("APPDATA")
    if not app_data:
        return None

    firefox_root = Path(app_data) / "Mozilla" / "Firefox"
    profiles_ini = firefox_root / "profiles.ini"
    if not profiles_ini.exists():
        return None

    parser = configparser.RawConfigParser()
    parser.read(profiles_ini, encoding="utf-8")

    for section in parser.sections():
        if not section.startswith("Install"):
            continue
        install_default = parser.get(section, "Default", fallback=None)
        if not install_default:
            continue
        resolved_path = firefox_root / install_default
        if resolved_path.exists():
            return resolved_path

    fallback_profile: Path | None = None
    for section in parser.sections():
        if not section.startswith("Profile"):
            continue
        profile_path = parser.get(section, "Path", fallback=None)
        if not profile_path:
            continue
        is_relative = parser.getboolean(section, "IsRelative", fallback=True)
        resolved_path = (firefox_root / profile_path) if is_relative else Path(profile_path)
        if fallback_profile is None and resolved_path.exists():
            fallback_profile = resolved_path
        if parser.getboolean(section, "Default", fallback=False) and resolved_path.exists():
            return resolved_path
    return fallback_profile


def _load_cookie_map(profile_dir: Path) -> dict[str, str]:
    cookies_path = profile_dir / "cookies.sqlite"
    if not cookies_path.exists():
        return {}

    rows = _read_sqlite_rows(
        source_path=cookies_path,
        query="select name, value from moz_cookies where host like ? order by name",
        parameters=("%tradingview.com%",),
    )
    return {str(name): cleaned for name, value in rows if (cleaned := _clean(str(value))) is not None}


def _read_local_storage(profile_dir: Path, keys: list[str]) -> str | None:
    ls_path = profile_dir / "storage" / "default" / "https+++www.tradingview.com" / "ls" / "data.sqlite"
    if not ls_path.exists():
        return None

    placeholders = ", ".join("?" for _ in keys)
    query = f"select value from data where key in ({placeholders}) order by key"
    rows = _read_sqlite_rows(ls_path, query, tuple(keys))
    for row in rows:
        value = _clean(str(row[0]))
        if value is not None:
            return value
    return None


# ---------------------------------------------------------------------------
# SQLite snapshot helpers
# ---------------------------------------------------------------------------

def _read_sqlite_rows(
    source_path: Path,
    query: str,
    parameters: tuple[Any, ...],
) -> list[tuple[Any, ...]]:
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / source_path.name
        _copy_sqlite_snapshot(source_path, snapshot_path)
        try:
            with closing(sqlite3.connect(snapshot_path)) as connection:
                return connection.execute(query, parameters).fetchall()
        except sqlite3.Error:
            return []


def _copy_sqlite_snapshot(source_path: Path, snapshot_path: Path) -> None:
    shutil.copy2(source_path, snapshot_path)
    for suffix in ("-wal", "-shm"):
        sibling = Path(f"{source_path}{suffix}")
        if sibling.exists():
            shutil.copy2(sibling, Path(f"{snapshot_path}{suffix}"))


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
