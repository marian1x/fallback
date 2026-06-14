import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from symbol_memory import SymbolMemory


def _item(url, headline, summary="", published_at="2026-06-02T10:00:00+00:00"):
    return {"url": url, "headline": headline, "summary": summary, "published_at": published_at, "provider": "test", "source": "Test"}


def test_append_dedupes_and_keeps_timestamps(tmp_path):
    mem = SymbolMemory(base_dir=str(tmp_path))
    added = mem.append_news("AAPL", [_item("u1", "Apple beats earnings")])
    assert added == 1
    # same url again -> not re-added
    assert mem.append_news("AAPL", [_item("u1", "Apple beats earnings")]) == 0
    assert mem.append_news("AAPL", [_item("u2", "Apple faces probe")]) == 1
    assert mem.archive_count("AAPL") == 2

    recent = mem.recent_archive("AAPL", limit=10)
    assert len(recent) == 2
    assert all(r.get("published_at") and r.get("ingested_at") for r in recent)
    # cheap sentiment label is auto-scored when absent
    assert all(r.get("sentiment_label") for r in recent)


def test_recent_archive_sorted_by_published_desc(tmp_path):
    mem = SymbolMemory(base_dir=str(tmp_path))
    mem.append_news("NVDA", [_item("a", "old", published_at="2026-01-01T00:00:00+00:00")])
    mem.append_news("NVDA", [_item("b", "new", published_at="2026-06-01T00:00:00+00:00")])
    recent = mem.recent_archive("NVDA", limit=10)
    assert recent[0]["headline"] == "new"


def test_build_memory_context_shape(tmp_path):
    mem = SymbolMemory(base_dir=str(tmp_path))
    mem.append_news("TSM", [_item("x", "TSM surges on record demand")])
    ctx = mem.build_memory_context("TSM")
    assert ctx["symbol"] == "TSM"
    assert ctx["archive_count"] == 1
    assert "dossier" in ctx and "recent_news" in ctx and "sentiment_trend" in ctx
    assert ctx["recent_news"][0]["published_at"]


def test_dossier_roundtrip_and_update_with_fake_llm(tmp_path):
    mem = SymbolMemory(base_dir=str(tmp_path))
    mem.append_news("AMD", [_item("n1", "AMD lands major datacenter contract")])

    def fake_llm(system_prompt, user_prompt):
        return (
            '{"narrative_summary":"AMD winning datacenter deals.",'
            '"key_facts":["new contract"],"analyst_stance":"bullish",'
            '"recurring_themes":["datacenter"],'
            '"notable_events":[{"date":"2026-06-02","event":"contract"}]}'
        )

    updated = mem.update_dossier("AMD", fake_llm, force=True)
    assert updated and updated["analyst_stance"] == "bullish"
    reloaded = mem.load_dossier("AMD")
    assert reloaded["narrative_summary"].startswith("AMD winning")
    assert reloaded["last_dossier_llm_at"]
    assert reloaded["news_seen_count"] == 1


def test_update_dossier_failsoft_on_llm_error(tmp_path):
    mem = SymbolMemory(base_dir=str(tmp_path))
    mem.append_news("INTC", [_item("e1", "Intel restructures")])
    prev = mem.load_dossier("INTC")

    def boom(system_prompt, user_prompt):
        raise RuntimeError("llm down")

    result = mem.update_dossier("INTC", boom, force=True)
    assert result is None
    # dossier preserved (still empty narrative), no crash
    assert mem.load_dossier("INTC")["narrative_summary"] == prev["narrative_summary"]


def test_should_update_dossier_thresholds(tmp_path):
    mem = SymbolMemory(base_dir=str(tmp_path))
    # nothing yet
    assert mem.should_update_dossier("X") is False
    mem.append_news("X", [_item("1", "a"), _item("2", "b"), _item("3", "c")])
    # 3 new items, no prior dossier -> update due
    assert mem.should_update_dossier("X") is True


def test_flag_for_cold_then_verdict(tmp_path):
    mem = SymbolMemory(base_dir=str(tmp_path))
    mem.append_news("AAPL", [_item("u1", "AAPL upgraded, analysts bullish", "strong buy")])
    # cold start: no analysis yet
    allowed, analysis = mem.flag_for("AAPL", "sell")
    assert allowed is None and analysis == {}

    def fake_llm(system_prompt, user_prompt):
        return (
            '{"narrative_summary":"AAPL strong.","key_facts":["upgrade"],"analyst_stance":"bullish",'
            '"recurring_themes":["AI"],"notable_events":[],'
            '"trade_verdict":{"bias":"bullish","long_ok":true,"short_ok":false,"confidence":0.82,'
            '"reason":"bullish upgrades contradict a short","risk_flags":["news_conflict"]}}'
        )

    mem.update_dossier("AAPL", fake_llm, force=True)
    an = mem.get_analysis("AAPL")
    assert an["long_ok"] is True and an["short_ok"] is False and an["bias"] == "bullish"
    assert an["based_on_news_count"] == 1 and an["based_on_latest_published_at"]

    sell_allowed, _ = mem.flag_for("AAPL", "sell")
    buy_allowed, _ = mem.flag_for("AAPL", "buy")
    assert sell_allowed is False and buy_allowed is True


def test_refresh_request_roundtrip(tmp_path):
    mem = SymbolMemory(base_dir=str(tmp_path))
    assert mem.pop_refresh_request() is None
    mem.request_refresh(symbols=["aapl", "nvda"])
    req = mem.pop_refresh_request()
    assert req["symbols"] == ["AAPL", "NVDA"] and req["force"] is True
    assert mem.pop_refresh_request() is None  # consumed

    mem.request_refresh(symbols=None)  # all
    assert mem.pop_refresh_request()["symbols"] == "all"


def test_record_sources(tmp_path):
    mem = SymbolMemory(base_dir=str(tmp_path))
    ctx = {"requested_sources": ["Alpaca", "Nasdaq", "Yahoo Finance RSS"],
           "provider_errors": {"Nasdaq": "HTTP 404"}}
    snap = mem.record_sources("AAPL", ctx)
    assert snap["ok"] == ["Alpaca", "Yahoo Finance RSS"]
    assert "Nasdaq" in snap["failed"]
    # persisted and visible in memory context
    mc = mem.build_memory_context("AAPL")
    assert mc["last_sources"]["failed"]["Nasdaq"] == "HTTP 404"
