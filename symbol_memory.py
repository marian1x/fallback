#!/usr/bin/env python3
"""Persistent per-symbol news memory for the local LLM gatekeeper.

Each symbol gets two inspectable files under ``instance/symbol_memory/``:

* ``<SYMBOL>.news.jsonl`` - an append-only, deduplicated archive of every news
  item ever seen for the symbol. Each row keeps both ``published_at`` (when the
  outlet published it) and ``ingested_at`` (when we first stored it), so the LLM
  can corroborate a news item against the date/time of a trade signal.
* ``<SYMBOL>.json`` - a structured "dossier" the LLM maintains over time:
  a rolling narrative, key facts, analyst stance, recurring themes and notable
  timestamped events. Human-readable on purpose so it can be audited.

This module is deliberately decoupled from the LLM client: ``update_dossier``
takes a ``llm_caller`` callable, so there is no import cycle with the validator.
Every public method is fail-soft - it logs and degrades rather than raising into
the trading loop.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from market_news import compact_text, score_text_sentiment, utc_now_iso


DOSSIER_SCHEMA_VERSION = 1


def _safe_symbol(symbol: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9._-]", "", str(symbol or "").upper())
    return cleaned or "UNKNOWN"


def _coerce_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


class SymbolMemory:
    def __init__(self, base_dir: str, logger=None, archive_cap: int = 4000):
        self.base_dir = base_dir
        self.logger = logger
        self.archive_cap = max(100, int(archive_cap))
        self._lock = threading.RLock()
        self._key_cache: Dict[str, set] = {}
        try:
            os.makedirs(self.base_dir, exist_ok=True)
        except Exception as exc:
            self._warn("failed to create memory dir %s: %s", self.base_dir, exc)

    # -- paths -----------------------------------------------------------------
    def _news_path(self, symbol: str) -> str:
        return os.path.join(self.base_dir, f"{_safe_symbol(symbol)}.news.jsonl")

    def _dossier_path(self, symbol: str) -> str:
        return os.path.join(self.base_dir, f"{_safe_symbol(symbol)}.json")

    def _warn(self, msg, *args):
        if self.logger:
            self.logger.warning("[SYMBOL_MEMORY] " + msg, *args)

    # -- news archive ----------------------------------------------------------
    @staticmethod
    def _item_key(item: Dict) -> str:
        return str(item.get("url") or item.get("id") or item.get("headline") or "").strip()

    def _load_keys(self, symbol: str) -> set:
        sym = _safe_symbol(symbol)
        if sym in self._key_cache:
            return self._key_cache[sym]
        keys: set = set()
        path = self._news_path(symbol)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        key = self._item_key(row)
                        if key:
                            keys.add(key)
            except Exception as exc:
                self._warn("failed to read archive %s: %s", path, exc)
        self._key_cache[sym] = keys
        return keys

    def append_news(self, symbol: str, items: List[Dict]) -> int:
        """Append new, unseen news items to the symbol archive. Returns count added."""
        if not items:
            return 0
        sym = _safe_symbol(symbol)
        added = 0
        now = utc_now_iso()
        with self._lock:
            keys = self._load_keys(symbol)
            rows = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = self._item_key(item)
                if not key or key in keys:
                    continue
                sentiment = item.get("sentiment") if isinstance(item.get("sentiment"), dict) else {}
                if not sentiment:
                    sentiment = score_text_sentiment(
                        f"{item.get('headline', '')} {item.get('summary', '')}"
                    )
                rows.append({
                    "key": key,
                    "ingested_at": now,
                    "published_at": item.get("published_at") or item.get("created_at"),
                    "provider": item.get("provider"),
                    "source": compact_text(item.get("source"), 120),
                    "headline": compact_text(item.get("headline") or item.get("title"), 300),
                    "summary": compact_text(item.get("summary"), 700),
                    "url": compact_text(item.get("url") or item.get("link"), 400),
                    "sentiment_label": sentiment.get("label"),
                    "sentiment_score": sentiment.get("score"),
                })
                keys.add(key)
                added += 1
            if not rows:
                return 0
            path = self._news_path(symbol)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n")
            except Exception as exc:
                self._warn("failed to append archive %s: %s", path, exc)
                return 0
            self._maybe_trim(symbol)
        return added

    def _maybe_trim(self, symbol: str) -> None:
        path = self._news_path(symbol)
        try:
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            if len(lines) <= self.archive_cap:
                return
            keep = lines[-self.archive_cap:]
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(keep)
            # rebuild key cache from trimmed file
            self._key_cache.pop(_safe_symbol(symbol), None)
        except Exception as exc:
            self._warn("failed to trim archive %s: %s", path, exc)

    def recent_archive(self, symbol: str, limit: int = 20) -> List[Dict]:
        path = self._news_path(symbol)
        if not os.path.exists(path):
            return []
        rows: List[Dict] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as exc:
            self._warn("failed to read archive %s: %s", path, exc)
            return []
        rows.sort(
            key=lambda r: (_coerce_dt(r.get("published_at")) or _coerce_dt(r.get("ingested_at")) or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )
        return rows[: max(0, int(limit))]

    def archive_count(self, symbol: str) -> int:
        return len(self._load_keys(symbol))

    # -- dossier ---------------------------------------------------------------
    def load_dossier(self, symbol: str) -> Dict:
        path = self._dossier_path(symbol)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    return data
            except Exception as exc:
                self._warn("failed to read dossier %s: %s", path, exc)
        return self._empty_dossier(symbol)

    def _empty_dossier(self, symbol: str) -> Dict:
        return {
            "symbol": _safe_symbol(symbol),
            "schema_version": DOSSIER_SCHEMA_VERSION,
            "updated_at": None,
            "narrative_summary": "",
            "key_facts": [],
            "analyst_stance": "unknown",
            "recurring_themes": [],
            "notable_events": [],
            "rolling_sentiment_trend": "unknown",
            "news_seen_count": 0,
            "last_dossier_llm_at": None,
            # Standing trade verdict the background analyst keeps up to date so the
            # trading path can read a flag instead of waiting for the slow LLM.
            "analysis": {},
            # Which news sources were reachable on the most recent fetch.
            "last_sources": {},
        }

    def get_analysis(self, symbol: str) -> Dict:
        analysis = self.load_dossier(symbol).get("analysis")
        return analysis if isinstance(analysis, dict) else {}

    # -- cross-process manual refresh trigger ---------------------------------
    def _refresh_request_path(self) -> str:
        return os.path.join(self.base_dir, "refresh_request.json")

    def request_refresh(self, symbols: Optional[List[str]] = None, force: bool = True) -> Dict:
        """Queue a manual 'check news + re-analyze now' request for the engine.

        The dashboard process writes this; the engine process picks it up on its
        next tick and refreshes the named symbols (or all) immediately, bypassing
        the per-symbol throttle."""
        payload = {
            "requested_at": utc_now_iso(),
            "symbols": [_safe_symbol(s) for s in symbols] if symbols else "all",
            "force": bool(force),
        }
        path = self._refresh_request_path()
        with self._lock:
            try:
                os.makedirs(self.base_dir, exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                os.replace(tmp, path)
            except Exception as exc:
                self._warn("failed to write refresh request: %s", exc)
        return payload

    def pop_refresh_request(self) -> Optional[Dict]:
        path = self._refresh_request_path()
        if not os.path.exists(path):
            return None
        with self._lock:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                os.remove(path)
                return data if isinstance(data, dict) else None
            except Exception as exc:
                self._warn("failed to read refresh request: %s", exc)
                try:
                    os.remove(path)
                except Exception:
                    pass
                return None

    # -- per-symbol source reachability ---------------------------------------
    def record_sources(self, symbol: str, news_context: Dict) -> Dict:
        """Persist which news sources were reachable on the last fetch for a symbol."""
        if not isinstance(news_context, dict):
            return {}
        requested = list(news_context.get("requested_sources") or [])
        failed = news_context.get("provider_errors") or {}
        ok = [s for s in requested if s not in failed]
        snapshot = {
            "checked_at": utc_now_iso(),
            "requested": requested,
            "ok": ok,
            "failed": {str(k): compact_text(v, 200) for k, v in failed.items()},
        }
        with self._lock:
            dossier = self.load_dossier(symbol)
            dossier["last_sources"] = snapshot
            self.save_dossier(symbol, dossier)
        return snapshot

    def flag_for(self, symbol: str, action: str):
        """Return (allowed, analysis) for an entry action using the standing verdict.

        ``allowed`` is None when no analysis exists yet (cold start) so the caller
        can fail-open and trigger a background refresh; True/False otherwise."""
        analysis = self.get_analysis(symbol)
        if not analysis or analysis.get("updated_at") is None:
            return None, {}
        act = str(action or "").strip().lower()
        if act in ("buy", "long"):
            allowed = bool(analysis.get("long_ok", True))
        elif act in ("sell", "short"):
            allowed = bool(analysis.get("short_ok", True))
        else:
            allowed = True
        return allowed, analysis

    def save_dossier(self, symbol: str, data: Dict) -> None:
        path = self._dossier_path(symbol)
        payload = dict(data or {})
        payload["symbol"] = _safe_symbol(symbol)
        payload["schema_version"] = DOSSIER_SCHEMA_VERSION
        payload["updated_at"] = utc_now_iso()
        with self._lock:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception as exc:
                self._warn("failed to write dossier %s: %s", path, exc)

    # -- sentiment trend -------------------------------------------------------
    def _sentiment_trend(self, recent: List[Dict]) -> Dict:
        scores = [float(r.get("sentiment_score") or 0.0) for r in recent if r.get("sentiment_score") is not None]
        if not scores:
            return {"label": "unknown", "recent_avg": 0.0, "prior_avg": 0.0, "sample": 0}
        half = max(1, len(scores) // 2)
        recent_avg = sum(scores[:half]) / half
        prior_scores = scores[half:]
        prior_avg = sum(prior_scores) / len(prior_scores) if prior_scores else recent_avg
        delta = recent_avg - prior_avg
        label = "stable"
        if delta >= 0.15:
            label = "improving"
        elif delta <= -0.15:
            label = "deteriorating"
        return {
            "label": label,
            "recent_avg": round(recent_avg, 3),
            "prior_avg": round(prior_avg, 3),
            "sample": len(scores),
        }

    # -- prompt context --------------------------------------------------------
    def build_memory_context(self, symbol: str, recent_n: int = 12) -> Dict:
        """Compact, timestamp-rich memory snapshot for the gate prompt."""
        dossier = self.load_dossier(symbol)
        recent = self.recent_archive(symbol, limit=recent_n)
        trend = self._sentiment_trend(recent)
        return {
            "symbol": _safe_symbol(symbol),
            "now_utc": utc_now_iso(),
            "archive_count": self.archive_count(symbol),
            "dossier": {
                "narrative_summary": dossier.get("narrative_summary"),
                "key_facts": dossier.get("key_facts", [])[:12],
                "analyst_stance": dossier.get("analyst_stance"),
                "recurring_themes": dossier.get("recurring_themes", [])[:12],
                "notable_events": dossier.get("notable_events", [])[:12],
                "updated_at": dossier.get("updated_at"),
            },
            "sentiment_trend": trend,
            "last_sources": dossier.get("last_sources", {}),
            "recent_news": [
                {
                    "published_at": r.get("published_at"),
                    "ingested_at": r.get("ingested_at"),
                    "source": r.get("source"),
                    "provider": r.get("provider"),
                    "headline": r.get("headline"),
                    "summary": compact_text(r.get("summary"), 240),
                    "sentiment_label": r.get("sentiment_label"),
                    "sentiment_score": r.get("sentiment_score"),
                }
                for r in recent
            ],
        }

    # -- LLM-maintained dossier roll-up ---------------------------------------
    def should_update_dossier(self, symbol: str, *, min_new_items: int = 1, min_interval_sec: int = 21600) -> bool:
        dossier = self.load_dossier(symbol)
        last = _coerce_dt(dossier.get("last_dossier_llm_at"))
        seen = int(dossier.get("news_seen_count") or 0)
        archive = self.archive_count(symbol)
        if last is None and archive > 0:
            return True
        if archive - seen >= min_new_items:
            return True
        if last is not None:
            age = (datetime.now(timezone.utc) - last).total_seconds()
            if age >= min_interval_sec and archive - seen > 0:
                return True
        return False

    def update_dossier(
        self,
        symbol: str,
        llm_caller: Callable[[str, str], str],
        *,
        force: bool = False,
        recent_n: int = 18,
    ) -> Optional[Dict]:
        """Fold the latest news into the structured dossier via the LLM.

        ``llm_caller(system_prompt, user_prompt) -> str`` is supplied by the
        caller (engine/validator) so this module stays client-agnostic. On any
        failure the previous dossier is preserved (fail-soft).
        """
        if not force and not self.should_update_dossier(symbol):
            return None
        prev = self.load_dossier(symbol)
        recent = self.recent_archive(symbol, limit=recent_n)
        if not recent:
            return None
        system_prompt = (
            "You maintain a long-running research dossier about a single stock symbol AND a standing "
            "trade verdict for an automated bot. You receive the previous dossier (JSON) and the most "
            "recent news items, each with timestamps. Update the durable narrative, key facts, analyst "
            "stance, recurring themes and timestamped notable events; preserve still-relevant prior "
            "facts, drop stale ones. "
            "Then set a trade_verdict the bot will read instead of asking you live: "
            "long_ok=false ONLY when recent material news clearly contradicts opening a LONG "
            "(e.g. fresh bad news / risk event); short_ok=false ONLY when recent material news or a "
            "clearly bullish analyst stance contradicts opening a SHORT (e.g. the name's own news is "
            "strong even if the market is weak). When news is absent or immaterial, both flags are "
            "true. bias is one of bullish|bearish|neutral. confidence is 0..1. "
            "Return ONLY one valid JSON object with keys: narrative_summary (string), key_facts "
            "(string array), analyst_stance (string), recurring_themes (string array), "
            "notable_events (array of {date, event}), trade_verdict ({bias, long_ok, short_ok, "
            "confidence, reason, risk_flags}). No markdown, no commentary."
        )
        payload = {
            "symbol": _safe_symbol(symbol),
            "now_utc": utc_now_iso(),
            "previous_dossier": {
                "narrative_summary": prev.get("narrative_summary"),
                "key_facts": prev.get("key_facts", []),
                "analyst_stance": prev.get("analyst_stance"),
                "recurring_themes": prev.get("recurring_themes", []),
                "notable_events": prev.get("notable_events", []),
            },
            "recent_news": [
                {
                    "published_at": r.get("published_at"),
                    "ingested_at": r.get("ingested_at"),
                    "source": r.get("source"),
                    "headline": r.get("headline"),
                    "summary": compact_text(r.get("summary"), 200),
                    "sentiment_label": r.get("sentiment_label"),
                }
                for r in recent
            ],
        }
        user_prompt = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        try:
            raw = llm_caller(system_prompt, user_prompt)
            parsed = self._extract_json(raw)
        except Exception as exc:
            self._warn("dossier LLM update failed for %s: %s", symbol, exc)
            return None
        if not parsed:
            self._warn("dossier LLM returned no JSON for %s", symbol)
            return None

        merged = dict(prev)
        merged["narrative_summary"] = compact_text(parsed.get("narrative_summary", prev.get("narrative_summary", "")), 2000)
        merged["key_facts"] = [compact_text(f, 240) for f in (parsed.get("key_facts") or [])[:20] if str(f or "").strip()]
        merged["analyst_stance"] = compact_text(parsed.get("analyst_stance", prev.get("analyst_stance", "unknown")), 120)
        merged["recurring_themes"] = [compact_text(t, 120) for t in (parsed.get("recurring_themes") or [])[:20] if str(t or "").strip()]
        events = []
        for ev in (parsed.get("notable_events") or [])[:30]:
            if isinstance(ev, dict) and str(ev.get("event") or "").strip():
                events.append({"date": compact_text(ev.get("date"), 40), "event": compact_text(ev.get("event"), 240)})
        merged["notable_events"] = events
        merged["rolling_sentiment_trend"] = self._sentiment_trend(recent).get("label", "unknown")
        merged["news_seen_count"] = self.archive_count(symbol)
        merged["last_dossier_llm_at"] = utc_now_iso()

        verdict = parsed.get("trade_verdict") if isinstance(parsed.get("trade_verdict"), dict) else {}
        try:
            confidence = min(1.0, max(0.0, float(verdict.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        risk_flags = verdict.get("risk_flags", [])
        if isinstance(risk_flags, str):
            risk_flags = [risk_flags]
        if not isinstance(risk_flags, list):
            risk_flags = []
        latest_pub = recent[0].get("published_at") if recent else None
        merged["analysis"] = {
            "updated_at": utc_now_iso(),
            "bias": compact_text(verdict.get("bias", "neutral"), 40) or "neutral",
            "long_ok": bool(verdict.get("long_ok", True)),
            "short_ok": bool(verdict.get("short_ok", True)),
            "confidence": confidence,
            "summary": compact_text(merged.get("narrative_summary", ""), 600),
            "reason": compact_text(verdict.get("reason", ""), 600),
            "risk_flags": [compact_text(f, 80) for f in risk_flags[:10] if str(f or "").strip()],
            "based_on_news_count": len(recent),
            "based_on_latest_published_at": latest_pub,
        }
        self.save_dossier(symbol, merged)
        return merged

    @staticmethod
    def _extract_json(text: str) -> Dict:
        raw = (text or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(raw[start : end + 1])
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}


def create_symbol_memory(instance_path: str, logger=None) -> SymbolMemory:
    base_dir = os.getenv(
        "SYMBOL_MEMORY_DIR",
        os.path.join(instance_path, "symbol_memory"),
    )
    archive_cap = 4000
    try:
        archive_cap = max(100, int(os.getenv("SYMBOL_MEMORY_ARCHIVE_CAP", "4000")))
    except (TypeError, ValueError):
        archive_cap = 4000
    return SymbolMemory(base_dir=base_dir, logger=logger, archive_cap=archive_cap)
