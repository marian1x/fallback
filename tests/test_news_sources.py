import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import news_sources
from news_sources import load_news_sources, normalize_source, normalize_sources, save_news_sources


def test_load_materializes_default_file_for_editing(tmp_path):
    sources = load_news_sources(str(tmp_path))
    assert sources, "default sources returned"
    assert os.path.exists(os.path.join(str(tmp_path), "news_sources.json"))
    assert any(s["type"] == "rss" for s in sources)
    assert any(s["type"] == "alpaca" for s in sources)


def test_normalize_source_variants():
    assert normalize_source("alpaca") == {"name": "alpaca", "type": "alpaca", "url": "", "enabled": True}
    # rss without {symbol} is rejected (cannot be made symbol-specific)
    assert normalize_source({"type": "rss", "url": "https://x.com/feed"}) == {}
    ok = normalize_source({"name": "My Feed", "type": "rss", "url": "https://x.com/{symbol}.xml", "enabled": False})
    assert ok["enabled"] is False and ok["name"] == "My Feed"
    # unknown type rejected
    assert normalize_source({"type": "ftp"}) == {}


def test_save_then_load_roundtrip_and_enabled_filter(tmp_path):
    custom = [
        {"name": "Alpaca", "type": "alpaca", "enabled": True},
        {"name": "Disabled RSS", "type": "rss", "url": "https://y.com/{symbol}", "enabled": False},
    ]
    save_news_sources(str(tmp_path), custom)
    all_sources = load_news_sources(str(tmp_path))
    assert len(all_sources) == 2
    enabled = load_news_sources(str(tmp_path), enabled_only=True)
    assert [s["name"] for s in enabled] == ["Alpaca"]
