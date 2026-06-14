# Release Notes

## Version 2.9.4 - 2026-06-14

### Fix: background analysis overwhelmed the local LLM (no verdicts produced)
- A manual "refresh all" spawned **one LLM request per symbol concurrently**, which overwhelmed LM
  Studio — it evicted in-flight requests (LRU slot reuse), so every call was cancelled and returned
  empty (`"output": []`, `Client disconnected`). No analysis completed.
- Fix: background re-analysis now runs through a **single serialized worker** (one LLM call at a time,
  with de-duplication), uses a **long read timeout** (`LLM_ANALYSIS_TIMEOUT_SEC`, default 180s) instead
  of the 25s gate timeout, and feeds the analyst a **smaller prompt** (18 recent items, trimmed
  summaries). The full news history is still archived; the dossier stays cumulative.

## Version 2.9.3 - 2026-06-14

### More news sources + include all of them
- Added five publisher feeds (verified to return per-symbol results), as `{symbol}`-templated Google
  News site-scoped queries: **Economic Times India, TipRanks, The Motley Fool, GuruFocus, Livemint**.
  Added to the defaults and merged into the live `instance/news_sources.json`.
- **Include all fetched news, not just a few.** The collector now separates per-source fetch count
  (`NEWS_CONTEXT_LIMIT`, default 8) from the total returned (`NEWS_CONTEXT_MAX_ITEMS`, default 60), so
  every reachable source contributes instead of one filling the cap. A real MSFT pass now returns ~60
  items across 9 providers (was 3). The analyst reads up to 50 recent items per re-analysis, and the
  full history is archived regardless. Yahoo (HTTP 429) and credential-less Alpaca surface as errors on
  the News Feeds health page.

## Version 2.9.2 - 2026-06-14

### News sources: fix single-source domination + make them manageable
- **"Only Benzinga" fix.** Three causes: Alpaca's news is Benzinga-backed; Stock Intelligence was
  using the env source list, not the editable `news_sources.json`; and items were truncated to the
  limit after Alpaca had already filled every slot. Now the collector **interleaves across providers**
  before truncating (so the first N span multiple sources), Stock Intelligence uses the editable
  registry, and the default news count was raised 3 → 6.
- **Admin → News Feeds page.** New page to view every configured source, **check health** against a
  test symbol (shows online / down + the error and item count, so you can spot a dead feed or a
  changed URL), enable/disable, add/remove `rss` feeds (`{symbol}` templated), and save back to
  `instance/news_sources.json`.
- **Per-symbol source reachability.** Every news fetch records which sources were reachable vs
  unreachable for that symbol; shown in Stock Intelligence → Show analysis ("Sources reached: N ok ·
  X unreachable").

### Manual "refresh now"
- **Refresh news + analysis button** in Strategy Lab → Bot Routing queues an immediate fetch + LLM
  re-analysis for the whole routing list. The engine picks it up on its next tick (~15s) and runs it
  in the background, bypassing the per-symbol throttle.

### How the analyst is triggered (clarification)
- The LLM is **poll-driven, not push**: the engine fetches the feeds for each routing symbol on a
  throttle (`SYMBOL_MEMORY_REFRESH_SECONDS`, default 30 min); when new deduplicated items appear it
  triggers the background re-analysis. The new button is the manual override of that schedule. News
  ingestion now covers the **whole enabled routing universe**, not just locally-executed symbols.

## Version 2.9.1 - 2026-06-14

### LLM becomes a background analyst (no more waiting on the slow model)
- The local model was too slow to call inline (Stock Intelligence timed out; live signals would have
  had to wait). The LLM now runs **in the background** and keeps a **standing per-symbol verdict** that
  everything else reads instantly.
- **Standing verdict / flags**: the per-symbol dossier now carries an `analysis` block with
  `long_ok` / `short_ok` flags, `bias`, `confidence`, `reason`, `risk_flags`, the time it was decided,
  and how many news items it was based on. The background analyst refreshes it whenever new news is
  ingested for the symbol (it already polls the whole Bot Routing universe).
- **Gate reads the flag, never blocks**: in gate mode the trading path now consults the precomputed
  `long_ok`/`short_ok` flag for the signal's direction — **zero LLM latency on the signal path**. Cold
  start (no verdict yet) fails open and kicks off a background analysis. The old blocking per-signal
  call is still available behind `LLM_GATE_SYNC=true`.
- **Stock Intelligence**:
  - New **"Show analysis (instant)"** button → returns the prepared standing analysis with no LLM call
    (flags, bias/confidence, the time it was produced, the news it was based on, recurring themes and
    notable events, recent archived news). New endpoint `/api/stock_intelligence/analysis`.
  - **"Ask (live answer)"** remains for custom one-off questions (e.g. "why did this fall intraday?");
    its timeout default was raised 45s → 90s (under the 120s gunicorn timeout).
- **Bot Routing news flag**: each symbol row in Strategy Lab → Bot Routing now shows a standing
  verdict badge — **Both** (neutral / no contradiction), **Buy** (shorts blocked by bullish news),
  **Sell** (longs blocked by bearish news), **Blocked**, or **Pending** (not analyzed yet). Hover for
  bias, confidence and the decision time.

### Notes
- Default remains shadow-first (`LLM_GATE_ENFORCE=false`): the flag is computed and logged, but blocks
  nothing until you flip enforce. Fail-open is preserved end-to-end.

## Version 2.9.0 - 2026-06-14

### RSI(2) goes live
- The RSI(2) mean-reversion strategy now runs in the live local execution engine, mirroring the
  MACD+SMA path (`build_rsi_frame`, `evaluate_entry_rsi`, `evaluate_exit_rsi`, plus whitelist,
  min-bars, order-size `order_size_rsi_reversion`). Live signals match the backtest exactly.

### LLM news gatekeeper (promoted from shadow to real gate)
- The local LLM validator gains a **`gate`** mode beside the existing `shadow` mode. In gate mode it
  validates each entry synchronously against aggregated news + per-symbol memory and can
  `veto`/`reduce_size` the trade — e.g. invalidate a short on a name whose own news and analysts are
  bullish even while the broad market falls (and vice-versa for longs).
- **Shadow-first**: gate logs `WOULD veto …` and changes nothing until `LLM_GATE_ENFORCE=true`.
- **Fail-open + alert**: any LLM error/timeout/garbled output executes the trade as normal and emits
  an `entry_llm_failopen` event — an unstable model never halts trading.
- New gate events: `entry_llm_veto`, `entry_llm_would_veto`, `entry_llm_resize`,
  `entry_llm_manual_review`, `entry_llm_approved`, `entry_llm_failopen`.

### Per-symbol memory (knowledge base)
- New `symbol_memory.py`: each symbol gets an inspectable, timestamped, deduplicated news archive
  (`instance/symbol_memory/<SYMBOL>.news.jsonl`) and an LLM-maintained dossier
  (`<SYMBOL>.json`: narrative, key facts, analyst stance, recurring themes, notable events).
- News is **ingested continuously** in the poll loop (throttled per symbol), independent of signals,
  so the model corroborates each new headline against months of prior context **and** against the
  signal's timestamp (every item keeps both `published_at` and `ingested_at`). The dossier roll-up
  runs in the background and is fail-soft.

### Editable multi-source news aggregator
- News sources are now a **user-editable registry** (`instance/news_sources.json`, auto-materialized):
  add/remove/toggle sources without code. New generic **`rss`** source type fetches any RSS/Atom feed
  via a `{symbol}` URL template. Defaults add Yahoo Finance RSS, Nasdaq and Seeking Alpha (plus Bing
  News / Investing.com proxy available, disabled).

### Tests
- Added `tests/test_symbol_memory.py`, `tests/test_news_sources.py`; extended market-news, validator
  and live-engine suites (RSS parsing, gate veto/resize/fail-open mapping, RSI live entry/exit).

### Notes / safety
- Default `.env` keeps `LLM_GATE_ENFORCE=false` (shadow-first) so nothing blocks live trades until you
  flip it after watching the logs. The LLM is never required for a trade to proceed.

## Version 2.8.4 - 2026-06-13

### New strategy: RSI(2) Mean Reversion
- Added a third selectable strategy (`rsi_reversion`) alongside Keltner and MACD+SMA. It trades a
  documented, robust edge — short-term mean reversion in equities (Connors-style RSI(2)):
  - **Long:** price above the trend SMA **and** RSI below the oversold threshold (buy the dip in an uptrend).
  - **Short:** price below the trend SMA **and** RSI above the overbought threshold.
  - **Exit:** RSI mean-reverts past the exit level, or forced/fixed SL/TP, or the pre-close flat.
- Few parameters on purpose (RSI length, oversold, overbought, exit level, trend SMA) so it stays hard
  to overfit. By design it **fires often** — a typical run produces dozens to hundreds of trades, so the
  win rate it reports is backed by a real sample instead of the handful of lucky trades that produce a
  fragile "100% win rate." This is the constructive counterpart to the scoring fix below.
- Fully wired for **testing**: selectable in Strategy Tester with its own Inputs panel and optimizer
  ranges, sweeps under both the random and TPE engines, and validates out-of-sample. Tip: raise **OOS
  Min Trades** for this strategy (e.g. 30+) so the validation gate demands a statistically meaningful
  sample before promotion.
- Note: this release wires RSI(2) for **backtesting/optimization only**. The live local execution
  engine still runs Keltner and MACD+SMA; live routing for RSI(2) is a follow-up once you've validated
  it backtests and passes OOS.

## Version 2.8.3 - 2026-06-13

### Optimizer objective — stop rewarding overfit few-trade combos
- **Sample-size-aware scoring.** The optimizer used to rank combinations with
  `win_rate * 0.35` plus a profit factor that jumps to its cap whenever there are no
  losing trades. That made a fragile "100% win rate over ~10 trades in 5 years" combo
  outrank configs that trade far more and return far more. The win-rate term now uses
  the **Wilson 95% lower bound** of the win proportion (collapses toward 50% as the
  sample shrinks), and an explicit **trade-frequency reward** ramps up to a target.
  Tiny samples (<5, <10 trades) take a significance penalty. Net effect: the optimizer
  now favors configs that trade more often and compound higher real return, instead of
  a brittle few-trade run that won't survive live.
- Applied to **both** strategies (Keltner pure-Python + Numba fast path, and
  MACD+SMA) via a single shared `_compute_score()` helper.

### UI
- **Stay on the Results tab.** Deleting or bulk-deleting optimizer runs submits the
  config form and reloads the page; it no longer bounces you back to Strategy Tester.
  The active top-level Strategy Lab tab is remembered across reloads.

## Version 2.8.2 - 2026-06-13

### MACD+SMA strategy fixes
- **Signal condition bug.** `fast_ma > slow_ma` is mathematically identical to `macd_line > 0` (since
  `macd_line = fast_ma - slow_ma`). The redundant check was replaced with `close > veryslow_ma`
  (current close above the 200-SMA), a genuinely independent trend filter.
- **Lagged SMA filter.** The 200-SMA confirmation used `close.shift(macd_slow_length)` — price 26
  bars ago — which was stale and incoherent. Fixed to use the current bar's close vs. current 200-SMA
  in both the backtest engine and the live strategy engine.
- **Pre-close exit.** MACD trades now force-close N minutes before the configured market close
  (same `close_before_minutes` used by Keltner), preventing overnight gap exposure.

### UI
- **Optimizer Runs sortable columns.** Clicking any column header in the Results → Optimizer Runs
  table sorts the rows by that column (▲ ascending / ▼ descending, click again to reverse).
  Works alongside the existing filters.

## Version 2.8.1 - 2026-06-13

### UI
- **Strategy Lab full-width layout.** Removed the `max-width: 1480px` cap on the Strategy Lab page so it stretches edge-to-edge, consistent with all other dashboard pages.

## Version 2.8.0 - 2026-06-13

### Win-rate / returns
- **Market-regime filter for shorts.** Short entries only fire when the broad market (SPY daily,
  50/200 SMA) is neutral or falling, and are blocked on an uptrend (cached ~30 min). Live data showed
  shorts were the losing side while longs were net positive.
- **Rolling per-symbol kill switch.** A symbol whose recent closed trades in a rolling window are
  deeply net-negative or low win-rate is disabled, then auto-recovers once the streak ages out.
- **Percentage-based trailing stop.** New `trailing_offset_pct` threaded through the backtest engine,
  live engine, and optimizer (`--trail-pct-range`). Defaults to 0 (legacy fixed-tick trail).
- **Stricter quality gates.** Live backtest profit-factor 1.05 -> 1.3; out-of-sample 1.05 -> 1.15.

### Performance
- **Strategy Lab page.** Per-job config and trade lists are now lazy-loaded via
  `/api/admin/strategy/job/<id>`; the listing no longer embeds full reports/trades. The page dropped
  from ~72 MB / 1.6 s to ~1.1 MB / ~0.12 s.
- **Dashboard responsiveness.** Alpaca clients are cached per user (connection reuse), positions /
  account get a short TTL cache, and the all-users loops fan out across a thread pool. Gunicorn
  dashboard threads raised 4 -> 12.
- **Optimizer speed.** Optional Numba JIT fast path for the Keltner backtest (~5-7x, parity-tested);
  remote/Windows runner `--optimizer-jobs max` uses all local cores (16 on a Ryzen 9 8945HS).
- Self-hosted all front-end vendor assets (no remote CDN fetch); dropped animate.css / Google Fonts.

### Fixes
- **Sub-path login redirect.** Logging in at `/trading/` no longer bounces to the site root; the
  post-login `next` target now includes the `X-Forwarded-Prefix` set by the reverse proxy.

## Version 2.7.1 - 2026-06-09

### Strategy Lab
- Reworked **Backtest Stocks** into a persistent preferred list:
  - unchecking a stock now keeps it saved but skips it for the next run,
  - removing a stock is now an explicit action,
  - the saved tester list is preserved in strategy config.
- Added missing field tooltips for strategy-specific optimizer and OOS validation inputs.
- Expanded **Optimizer Runs** filtering with:
  - Romania-local run date,
  - min/max Return %,
  - min/max Win %,
  - min/max Drawdown %,
  - combined filtering behavior across date and metric filters.
- Added a visible **Run Date** column in Optimizer Runs using local Romania time.

### Remote Worker Compatibility
- Added PI5-side compatibility handling for legacy Windows strategy workers that still run an older `pine_optimizer.py`.
- Remote jobs that fail with argparse `returncode=2` because of unsupported validation CLI flags are now automatically requeued once with those flags removed.
- Requeued the broken remote `MACD + SMA` jobs so stale Windows workers can continue processing them without manual recreation.

## Version 2.7.0 - 2026-06-07

### Strategy Lab
- Reworked **Backtest Stocks** into an Alpaca-backed picker that supports:
  - multi-select,
  - symbol search,
  - company-name search,
  - selected-symbol review before run.
- Strategy Tester now shows only the parameter groups relevant to the selected strategy:
  - `Keltner Channel`,
  - `MACD + SMA`.
- Optimizer Runs now keep cross-strategy history visible for the same symbol and add client-side filters for:
  - symbol / job id,
  - strategy,
  - run status,
  - out-of-sample validation result.
- Added **Load** from Optimizer Runs back into Strategy Tester so a completed run can be reviewed or rerun from the tester area.
- Added Optimizer Run deletion from the Results tab.
- Added stronger duplicate active-job detection using a configuration fingerprint so pressing **Run Backtest** twice with the same active setup does not queue a duplicate job.

### Routing / Trade Analytics
- Closed Trades now show the strategy used for each trade and support strategy filtering.
- Strategy Analytics now supports filtering by strategy and adds a strategy-comparison table for side-by-side performance review.
- Recent trades in Strategy Analytics now include the strategy label.
- Trade records now persist `strategy` and `strategy_job_id` metadata for local strategy executions.

### Symbol Search
- Upgraded the tradable-symbol cache from raw symbols to structured Alpaca asset metadata (`symbol`, `name`, `exchange`).
- Manual trade symbol search can now resolve company names to symbols when the match is unambiguous.
- Refreshed `instance/tradable_symbols.json` with named Alpaca assets.

## Version 2.6.0 - 2026-06-02

### Local Strategy Engine
- Added a live local strategy engine in `fallback.service` for Signal Universe symbols set to `Local` or `Both`.
- The engine evaluates entries on fully closed Alpaca bars using each symbol's saved optimizer parameters.
- Added Alpaca position checks before every local open/close decision to avoid duplicate or blind orders.
- Added local order idempotency through deterministic `client_order_id` values for local entry signals.
- Added persistent recovery state for failed order attempts in `instance/local_strategy_state.json`.
- Added close-order recovery retries that continue until the position is no longer open or the order succeeds.
- Added `local_strategy.log` for decisions, skips, order attempts, and recovery outcomes.

### Safety Controls
- Added `LOCAL_STRATEGY_ENGINE_AUTOSTART`, `LOCAL_STRATEGY_DRY_RUN`, `LOCAL_STRATEGY_POLL_SECONDS`, and recovery backoff env controls.
- Local strategy orders reuse the existing bot risk gates and Alpaca order handling path.

## Version 2.5.0 - 2026-06-02

### Strategy Lab
- Strategy Tester now accepts a comma/newline-separated list of stock symbols and creates a separate optimizer run for each symbol.
- Results now show optimizer run history across local and remote jobs, including status, return, win rate, drawdown, and selected action.
- Completed runs can be expanded to inspect the winning configuration and the generated trade list.
- A selected completed run can be added directly to Signal Universe from Bot Routing.

### Optimizer Performance
- Added `--jobs` / `CPU Jobs` for multiprocessing inside a single optimizer run.
- `0` means auto parallelism (`cpu_count - 1`); use `1` to force single-process behavior.
- Updated scoring to rank combinations by return, win rate, profit factor, Sharpe, and explicit drawdown penalty.

### Remote Worker
- Added `--workers` to the Linux remote worker and Windows 11 agent so multiple queued symbols can be processed in parallel.
- Added queue locking in the PI5 API to prevent multiple remote worker threads from claiming the same job.
- Increased the dashboard upload limit for remote optimizer completion payloads through `DASHBOARD_MAX_CONTENT_LENGTH_BYTES` (default `16MB`) to avoid `413` errors for larger reports.

## Version 2.4.0 - 2026-06-02

### Strategy Optimizer
- Added chart interval sweep support for optimizer runs.
- Strategy Lab can now test minute intervals like `5,10,15,30`, hourly ranges like `1Hour` through `24Hour`, and daily ranges like `1Day` through `5Day`.
- Unsupported Alpaca-native intervals such as `24Hour` and multi-day bars are generated by local OHLC resampling.
- Optimizer reports and top CSV rows now include the winning `timeframe`.

### Windows Remote Agent
- Added `misc/windows_strategy_agent.py` for Windows 11 mini-PC usage.
- The agent can run directly over HTTPS or keep an outbound SSH tunnel to PI5 and poll jobs through localhost.

## Version 2.3.0 - 2026-06-02

### Strategy Lab Workflow
- Moved the backtest target into Strategy Tester:
  - stock symbol,
  - chart interval,
  - Alpaca user,
  - session,
  - data feed,
  - local vs remote compute target.
- Added chart intervals from `1Min` through `1Week`.
- Added **Add Latest Result to Signals** to save a validated backtest into Signal Universe.
- Signal Universe rows now show saved backtest performance and keep winning parameters hidden behind an expandable details view.
- Latest Backtest and Remote Jobs status are visible in the Results tab.

### Remote Optimization
- Added a pull-based remote optimizer worker: `misc/remote_optimizer_worker.py`.
- PI5 keeps Alpaca credentials local, fetches historical bars, and sends only OHLC CSV + optimizer parameters to the remote worker.
- Added authenticated remote job endpoints protected by `STRATEGY_WORKER_TOKEN`.

## Version 2.2.1 - 2026-06-02

### UI / Navigation
- Reworked the global navigation into grouped menus: Dashboard, Trades, Analytics, and Admin.
- Removed Bootstrap tooltip bindings from global nav items to avoid dropdown/menu rendering issues.
- Refreshed the global visual theme with stronger card hierarchy, cleaner dropdowns, improved mobile nav spacing, and consistent accent styling.

### Strategy Lab
- Redesigned **Admin -> Strategy Lab** with separate areas for:
  - Strategy Tester,
  - Bot Routing,
  - Results.
- Added TradingView-style Strategy Tester tabs:
  - Inputs,
  - Properties,
  - Optimization.
- Added TradingView-aligned settings for Keltner inputs, backtest range, SL/TP, capital, order size, commission, slippage, margin, and recalculation flags.
- Added contextual tooltips on Strategy Lab tabs, actions, and field labels with examples and tradeoffs.
- Backtest runs now use exact Strategy Tester values by default; optimizer range search runs only when optimization is enabled.

### Routing / Reverse Proxy
- Replaced hardcoded frontend `/api/...` calls with Flask-generated URLs so AJAX requests work correctly when the app is served under `/trading`.

## Version 2.2.0 - 2026-06-02

### Alpaca Integration / Execution
- Migrated trading integration from `alpaca-trade-api` to `alpaca-py`.
- Added trade update stream handling (`trade_updates`) for better order execution tracking.
- Improved order fill price resolution using stream events + fallback order query.

### 24/5 and Manual Trading
- Added manual trade controls in Dashboard:
  - `market` / `limit` order type,
  - `time_in_force` selector,
  - `extended_hours` toggle,
  - optional limit price.
- Added extended-hours / overnight order routing logic for equities:
  - auto conversion to limit orders outside RTH when needed,
  - overnight validation via `overnight_tradable` / `overnight_halted`.

### Admin / UI
- Added **Admin -> Strategy Lab**:
  - strategy configuration storage,
  - local backtest + optimization runs,
  - reference vs best metrics and top combinations table.
- Added Strategy Universe management:
  - per-symbol mode: `Local`, `TradingView`, `Both`, or `Disabled`,
  - Alpaca tradable-symbol validation,
  - live Alpaca price snapshot for symbols configured as `Local` or `Both`.
- TradingView webhooks are now filtered by Strategy Universe mode; symbols marked `Local` are ignored by TW execution.
- Added hover guidance tooltips for navigation/menu items to explain each section.

### Strategy Optimizer Fidelity
- Updated optimizer defaults for better TradingView parity:
  - fixed trailing offset (`4` ticks) by default,
  - integer search range for `outer_kc_mult` by default.
- Updated backtest loop order to match Pine execution flow better (entry block before exit logic).

## Version 2.1.0 - 2026-06-02

### Strategy Research / Optimization
- Added `misc/pine_optimizer.py` to run local backtests and parameter search directly from your Pine strategy logic.
- Optimizer can:
  - read defaults from `misc/keltner.pine`,
  - use TradingView export metadata from XLSX as a baseline,
  - fetch bars from Alpaca (`iex` / `sip`) or local CSV,
  - rank best parameter combinations and export JSON + CSV reports.

### Documentation / Repository Hygiene
- Added optimizer usage documentation:
  - `misc/README_optimizer.md`
  - README section `Local Pine Optimizer`
- Added optimizer/report artifacts to `.gitignore` to keep the repo clean.
- Added required optimizer dependencies to `requirements.txt`:
  - `numpy`
  - `pandas`
  - `openpyxl`

## Version 2.0.1 - 2026-06-01

### Deployment / Runtime
- Migrated both services from Flask dev server to Gunicorn under `systemd`:
  - `fallback.service` -> `bot:app` on `127.0.0.1:5000`
  - `fallback_dashboard.service` -> `dashboard:app` on `127.0.0.1:5050`
- Added `gunicorn` to `requirements.txt`.

### Security / User Management
- Added self-service password change for any logged-in user from **Configuration** page.
- Kept admin password reset flow and added frontend `minlength` validation in admin user management forms.

### UI / Trading UX
- Fixed close-trade notice rendering (`[object Object]`) by parsing nested webhook/proxy responses correctly before display.

## Version 2.0.0 - 2026-06-01

### Security
- Added CSRF protection for all state-changing dashboard endpoints (forms + AJAX `X-CSRF-Token`).
- Hardened session cookies (`HttpOnly`, `SameSite=Lax`, configurable `Secure`) and session lifetime.
- Added Flask security response headers (CSP, Referrer Policy, Frame Options, Permissions Policy, HSTS when HTTPS).
- Added login brute-force protection with configurable in-memory rate limiting per client IP.
- Fixed open redirect risk on login `next` parameter by validating target URL.
- Moved logout to `POST` only.
- Switched internal API key validation to constant-time comparison.

### Trading Bot / Strategy Safety
- Added webhook payload size checks and strict JSON validation.
- Added optional webhook secret validation (`WEBHOOK_SECRET`) via header or payload passphrase.
- Added duplicate signal suppression window to avoid repeated executions from retries.
- Added configurable risk gates:
  - minimum / maximum trade amount,
  - maximum account allocation percent per order,
  - maximum open positions per account.

### UI / Mobile / UX
- Improved login and register screens for mobile and desktop (cleaner auth cards, better spacing, stronger form defaults).
- Added `SweetAlert2` globally (fixes missing `Swal` runtime usage in manual trading flow).
- Refined dashboard layout for responsiveness and readability on smaller screens.
- Improved global visual polish (card depth, navbar interactions, mobile spacing and touch targets).

### Repository Hygiene
- Cleaned `.gitignore` to exclude runtime artifacts and local environments.
- Removed unused templates:
  - `templates/backup_restore.html`
  - `templates/config.html`
  - `templates/logs_list.html`
  - `templates/reinit_db_confirm.html`
- Removed tracked runtime logs from Git index.

### Operational Notes
- Existing reverse-proxy HTTPS setup remains valid.
- For best security, keep dashboard and bot bound to localhost and expose only through Nginx TLS.
- Configure these new env vars as needed:
  - `PASSWORD_MIN_LENGTH`
  - `SESSION_LIFETIME_MINUTES`
  - `LOGIN_RATE_LIMIT_WINDOW_SEC`
  - `LOGIN_RATE_LIMIT_MAX_ATTEMPTS`
  - `SESSION_COOKIE_SECURE`
  - `WEBHOOK_SECRET`
  - `WEBHOOK_SECRET_HEADER`
  - `MIN_TRADE_AMOUNT`
  - `MAX_TRADE_AMOUNT`
  - `MAX_ACCOUNT_ALLOCATION_PCT`
  - `MAX_OPEN_POSITIONS_PER_ACCOUNT`
  - `SIGNAL_DEDUP_WINDOW_SEC`
  - `MAX_WEBHOOK_CONTENT_LENGTH`
