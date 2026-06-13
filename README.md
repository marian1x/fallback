# Alpaca Trading Bot & Dashboard

Welcome to the Alpaca Trading Bot, a powerful, self-hosted solution for automating your trading strategies. This project provides a robust Flask-based dashboard and a webhook-driven bot that connects to the Alpaca trading platform.

![Dashboard Screenshot](https://i.imgur.com/your-screenshot-url.png) ## 🚀 Features

- **Real-time Dashboard**: A modern, responsive web interface to monitor your portfolio, view open and closed trades, and analyze performance.
- **Webhook Integration**: Execute trades automatically based on alerts from TradingView or other webhook providers.
- **Alpaca Connectivity**: Seamlessly connect to your Alpaca paper or live trading account.
- **Automated Sync**: Open positions are automatically synced from Alpaca to the local database, ensuring data consistency.
- **Secure Authentication**: The dashboard is protected by a login system, and the database can be re-initialized securely.
- **Themeable UI**: Switch between a clean light mode and a futuristic dark mode.
- **Mobile-Friendly**: The dashboard is fully responsive and optimized for use on mobile devices.
- **Manual Order Controls**: Place market/limit orders, choose time-in-force, and enable extended-hours trading from the dashboard.
- **Admin Strategy Lab**: Configure and run local strategy backtests/optimizations without TradingView.

## 🛠️ Tech Stack

- **Backend**: Python, Flask, Flask-SQLAlchemy
- **Frontend**: HTML, CSS, JavaScript, Bootstrap 5, Chart.js, DataTables
- **Database**: SQLite (default), easily configurable for other databases
- **API**: Alpaca Trade API

## 📋 Prerequisites

Before you begin, ensure you have the following installed:

- Python 3.8+
- `pip` and `venv`
- An account with [Alpaca](https://alpaca.markets/)

## 🔐 Security & Deployment Recommendations

- Keep both Flask services bound to `127.0.0.1` and expose them only through Nginx.
- Use HTTPS only for public access (`https://salavat.home.ro/...`).
- Set a strong `INTERNAL_API_KEY` and (recommended) `WEBHOOK_SECRET`.
- Never commit runtime files (`instance/`, logs, `.env`, `venv/`).
- Rotate `FLASK_SECRET`, `ENCRYPTION_KEY`, and API credentials periodically.

## ⚙️ Installation & Setup

Follow these steps to get your trading bot up and running:

1.  **Clone the Repository**

    ```bash
    git clone [https://github.com/your-username/your-repo-name.git](https://github.com/your-username/your-repo-name.git)
    cd your-repo-name
    ```

2.  **Set up a Virtual Environment**

    It's highly recommended to use a virtual environment to manage dependencies:

    ```bash
    python3 -m venv venv
    source venv/bin/activate  # On Windows, use `.\venv\Scripts\Activate.ps1`
    ```

3.  **Install Dependencies**

    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure Environment Variables**

    Create a `.env` file in the root of the project and add your Alpaca API keys and other settings. You can use the `.env.example` file as a template:

    ```
    # --- Alpaca API Credentials ---
    ALPACA_KEY="YOUR_ALPACA_API_KEY"
    ALPACA_SECRET="YOUR_ALPACA_API_SECRET"
    ALPACA_API_BASE_URL="[https://paper-api.alpaca.markets](https://paper-api.alpaca.markets)" # Use [https://api.alpaca.markets](https://api.alpaca.markets) for live trading

    # --- Application Settings ---
    FLASK_SECRET="a_strong_and_random_secret_key" # Change this to a random string
    ADMIN_USERNAME="admin"
    ADMIN_PASSWORD="a_secure_password" # Change this for production

    # --- Network Configuration ---
    BOT_PORT=5000
    DASHBOARD_PORT=5050

    # --- Security Hardening ---
    SESSION_COOKIE_SECURE=true
    SESSION_LIFETIME_MINUTES=720
    PASSWORD_MIN_LENGTH=10
    LOGIN_RATE_LIMIT_WINDOW_SEC=600
    LOGIN_RATE_LIMIT_MAX_ATTEMPTS=8

    # --- Webhook Security ---
    WEBHOOK_SECRET="your_shared_secret"
    WEBHOOK_SECRET_HEADER="X-Webhook-Secret"

    # --- Risk Controls ---
    MIN_TRADE_AMOUNT=1
    MAX_TRADE_AMOUNT=100000
    MAX_ACCOUNT_ALLOCATION_PCT=0
    MAX_OPEN_POSITIONS_PER_ACCOUNT=30
    SIGNAL_DEDUP_WINDOW_SEC=8

    # --- 24/5 / Extended Hours Controls ---
    AUTO_EXTENDED_HOURS=true
    AUTO_LIMIT_OUTSIDE_RTH=true
    OUTSIDE_RTH_LIMIT_SLIPPAGE_BPS=25
    TRADE_UPDATES_WAIT_SEC=12
    ```

    **Important**: Never commit your `.env` file to version control. The `.gitignore` file is already configured to ignore it.

5.  **Initialize the Database**

    The first time you run the dashboard, it will create the SQLite database file and the necessary tables.

## ▶️ Running the Application

You need to run two separate processes: the trading bot and the dashboard.

-   **Start the Trading Bot**:

    ```bash
    python3 bot.py
    ```

-   **Start the Dashboard**:

    ```bash
    python3 dashboard.py
    ```

You can now access the dashboard at `http://127.0.0.1:5050`.

## 🔎 Local Pine Optimizer

You can optimize the local Pine strategy parameters without TradingView using:

```bash
source venv/bin/activate
python3 misc/pine_optimizer.py \
  --trials 300 \
  --jobs 0 \
  --session regular \
  --feed iex \
  --report-json misc/optimizer_report.json \
  --top-csv misc/optimizer_top.csv
```

Useful flags:

- `--alpaca-user <username>`: use a specific local user for Alpaca credentials.
- `--feed iex|sip`: data feed selection (`iex` usually works on free plans).
- `--bars-csv /path/to/bars.csv`: run backtests from local CSV data.
- `--timeframes 5Min,10Min,15Min,30Min,1Hour,2Hour,1Day`: sweep chart intervals and rank the best global result.
- `--jobs 0`: run optimizer trials in parallel (`0` means auto `cpu_count - 1`; use `1` for single-process).
- `--top-k 20`: number of best configurations saved.
- `--trail-pct-range 0.4:1.2:0.1`: optimize a percentage-based trailing stop instead of fixed ticks
  (`trailing_offset_pct`). `0` keeps the legacy fixed-tick trail. A percentage trail keeps the
  give-back proportional to price so winners on higher-priced names are not cut after a few cents.

### Performance notes

- **Numba JIT (optional).** If `numba` is installed, the Keltner backtest loop is JIT-compiled for a
  ~5-7x speedup; the optimizer falls back to pure Python otherwise. The JIT path is parity-tested
  against the reference implementation (`tests/test_pine_optimizer_numba.py`). Set
  `STRATEGY_DISABLE_NUMBA=1` to force the reference path.
- **Use all cores on the runner.** `pine_optimizer.py --jobs 0` already auto-parallelizes across
  `cpu_count - 1`. The remote/Windows runner additionally accepts `--optimizer-jobs` (default `max`,
  i.e. all logical cores on that machine, e.g. 16 on a Ryzen 9 8945HS) so it uses its own cores
  regardless of the value the PI5 sent. Accepts `max`, `auto`, `inherit`, or an integer.
- **GPU.** The backtest is a sequential, path-dependent state machine and is not GPU-accelerable
  without a full vectorized rewrite; `--accelerator gpu` only reports the detected device and still
  runs the (CPU-parallel, JIT) simulation. Maximizing CPU cores + Numba is the supported fast path.

Admins can also use the web UI at `Admin Tools -> Admin Strategy Lab` (`/admin/strategy`) to configure strategy runs, run a batch of symbols, inspect completed run configuration/trades, add a selected run to Signal Universe, and compare local vs TradingView signal routing per symbol.

### Local Strategy Engine

When `Local strategy enabled` is active in Strategy Lab, the bot service starts a conservative local strategy engine for symbols whose Signal Universe mode is `Local` or `Both`.

Runtime behavior:

- Evaluates entries only on fully closed Alpaca bars for each symbol's saved backtest timeframe.
- Uses the saved optimizer parameters from Signal Universe.
- Checks Alpaca position state before every open or close decision.
- Sends local orders through the same risk-gated execution path used by webhook/manual trading.
- Persists retry/recovery state in `instance/local_strategy_state.json`.
- Logs decisions and recovery attempts to `local_strategy.log`.

Useful env vars:

- `LOCAL_STRATEGY_ENGINE_AUTOSTART=true`: start the local engine with `fallback.service`.
- `LOCAL_STRATEGY_DRY_RUN=false`: log decisions without submitting orders when set to `true`.
- `LOCAL_STRATEGY_POLL_SECONDS=15`: engine polling interval.
- `LOCAL_STRATEGY_ENTRY_REFETCH_SECONDS=60`: when there is no open position, refetch historical bars at this interval; Keltner still uses the full `LOCAL_STRATEGY_BARS_LOOKBACK` window, but old DataFrames are not retained in memory.
- `LOCAL_STRATEGY_BARS_LOOKBACK=260`: max lookback bars fetched for live strategy evaluation.
- `LOCAL_STRATEGY_OPEN_RECOVERY_MAX_ATTEMPTS=3`: max retries for failed opens.
- `LOCAL_STRATEGY_CLOSE_RECOVERY_MAX_ATTEMPTS=0`: close retries; `0` means keep retrying until obsolete/success.
- `LOCAL_STRATEGY_RECOVERY_BASE_SECONDS=15`: first retry delay.
- `LOCAL_STRATEGY_RECOVERY_MAX_SECONDS=300`: max retry delay.
- `TRADE_UPDATES_EVENT_TTL_SEC=3600`: keep Alpaca trade-update events in memory for this many seconds.
- `TRADE_UPDATES_MAX_EVENTS=500`: cap in-memory Alpaca trade-update events per account stream.

### LLM Shadow Validation

The local strategy can optionally ask a remote/local LLM what it would do with a Keltner entry signal while leaving live/paper execution unchanged. This is a shadow-only audit layer: Keltner orders continue through the normal risk-gated path, and LLM decisions are appended to `instance/llm_trade_shadow.jsonl`.

Recommended first-phase `.env` settings for LM Studio on the MiniPC:

```bash
LLM_TRADE_VALIDATION_ENABLED=true
LLM_TRADE_VALIDATION_MODE=shadow
LLM_TRADE_VALIDATION_API_STYLE=lmstudio_native
LLM_TRADE_VALIDATION_BASE_URL=http://192.168.50.110:1234
LLM_TRADE_VALIDATION_MODEL=google/gemma-4-e4b
LLM_TRADE_VALIDATION_API_TOKEN=
LLM_TRADE_VALIDATION_TIMEOUT_SEC=25
LLM_TRADE_VALIDATION_MAX_ATTEMPTS=2
LLM_TRADE_VALIDATION_MAX_WORKERS=1
LLM_TRADE_VALIDATION_NEWS_ENABLED=true
LLM_TRADE_VALIDATION_NEWS_LIMIT=3
LLM_TRADE_VALIDATION_NEWS_TIMEOUT_SEC=5
NEWS_CONTEXT_SOURCES=alpaca,google
NEWS_CONTEXT_LIMIT=3
NEWS_CONTEXT_TIMEOUT_SEC=5
NEWS_CONTEXT_GOOGLE_DAYS=7
```

LM Studio must expose its OpenAI-compatible server to the PI5. Start the server in LM Studio, enable serving on the local network or bind to an address reachable by the PI5, and allow Windows Firewall inbound TCP on port `1234`.

Test from the PI5:

```bash
curl http://192.168.50.110:1234/api/v1/models
curl http://192.168.50.110:1234/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"model":"google/gemma-4-e4b","system_prompt":"Return only valid JSON.","input":"Return {\"decision\":\"approve\",\"confidence\":0.5,\"reason\":\"test\",\"risk_flags\":[]}.","temperature":0.1,"max_output_tokens":120,"store":false}'
```

News context sources:

- `alpaca`: authenticated Alpaca News API; uses the same Alpaca credentials already available to the local strategy request.
- `yahoo`: optional Yahoo Finance search/news endpoint. Keep it disabled by default because the public endpoint can return HTTP 429 rate limits from the PI5 network.
- `google`: Google News RSS query for recent symbol news.
- `stocktwits`: optional investor-message sentiment source. It can be blocked by Cloudflare from some networks; keep it out of `NEWS_CONTEXT_SOURCES` unless it works reliably from the PI5 or you add a paid/authorized sentiment provider.

Test news context from the PI5:

```bash
source venv/bin/activate
python3 misc/news_context_smoke.py --symbol AAPL --sources alpaca,google --limit 3
```

Summarize the shadow period:

```bash
source venv/bin/activate
python3 misc/llm_shadow_report.py --days 30
```

Export shadow events as chat-style examples for manual review or later LoRA/fine-tuning:

```bash
python3 misc/export_llm_shadow_dataset.py --min-confidence 0.7
```

For model specialization, start with prompt/RAG plus the shadow log. Fine-tuning should use curated labels or closed-trade outcomes, not only the model's own first-pass decisions.

### Stock Intelligence Web UI

Logged-in dashboard users can ask the local LM Studio model market questions at `Analytics -> Stock Intelligence`. The dashboard acts as the authenticated proxy: expose the dashboard through Nginx as usual, but keep LM Studio reachable only from the PI5/local network.

Useful `.env` settings:

```bash
STOCK_INTELLIGENCE_ENABLED=true
STOCK_INTELLIGENCE_BASE_URL=http://192.168.50.110:1234
STOCK_INTELLIGENCE_MODEL=google/gemma-4-e4b
STOCK_INTELLIGENCE_TIMEOUT_SEC=45
STOCK_INTELLIGENCE_MAX_ATTEMPTS=2
STOCK_INTELLIGENCE_MAX_TOKENS=900
STOCK_INTELLIGENCE_TEMPERATURE=0.2
```

### Remote Optimizer Worker

For heavier optimization runs, Strategy Lab can queue the job on the PI5 and let another machine run the calculation. The PI5 downloads Alpaca OHLC bars and sends only historical bar data plus optimizer parameters to the worker. Alpaca credentials stay on the PI5.

On the remote machine:

```bash
git clone git@github.com:marian1x/fallback.git
cd fallback
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export STRATEGY_WORKER_TOKEN="same_value_as_PI5_STRATEGY_WORKER_TOKEN"
python misc/remote_optimizer_worker.py \
  --server https://salavat.home.ro/trading \
  --token "$STRATEGY_WORKER_TOKEN" \
  --workers 1
```

Set `STRATEGY_WORKER_TOKEN` in the PI5 `.env`. If not set, the dashboard falls back to `INTERNAL_API_KEY`.

For Windows 11, use the standalone agent:

```powershell
cd C:\path\to\fallback
.\venv\Scripts\Activate.ps1
$env:STRATEGY_WORKER_TOKEN="same_value_as_PI5_STRATEGY_WORKER_TOKEN"
python misc\windows_strategy_agent.py --server https://salavat.home.ro/trading --token $env:STRATEGY_WORKER_TOKEN --workers 1 --optimizer-jobs max
```

`--optimizer-jobs max` (the default) makes the agent run each optimizer job across all logical CPU
cores on the Windows machine (e.g. 16 on a Ryzen 9 8945HS), regardless of the `CPU Jobs` value the
PI5 sent. Use `auto` for `cpu_count - 1`, `inherit` to honor the PI5 value, or an explicit integer.
For the best throughput on the 8945HS install `numba` in the agent's venv (`pip install numba`) to
enable the JIT fast path. Keep `--workers 1` so one job uses all cores rather than oversubscribing.

If you want the agent to work through SSH instead of the public HTTPS URL, enable OpenSSH client on Windows and run:

```powershell
python misc\windows_strategy_agent.py `
  --ssh-target pi5@salavat.home.ro `
  --local-port 8765 `
  --token $env:STRATEGY_WORKER_TOKEN `
  --workers 1
```

This creates an outbound SSH tunnel from Windows to the PI5 and polls `http://127.0.0.1:8765` locally.

Performance note: `CPU Jobs` in Strategy Lab controls parallel processes inside one optimizer run. Agent `--workers` controls how many queued symbols run at the same time. Avoid setting both high at once because that can oversubscribe the mini-PC.

Remote completion payloads can include full optimizer reports and best-trade lists. The dashboard accepts these through `DASHBOARD_MAX_CONTENT_LENGTH_BYTES` (default `16MB`). Keep the trading webhook limit separate and small via `MAX_WEBHOOK_CONTENT_LENGTH` / Nginx.

## 훅 Webhook Configuration

To trigger trades, you need to configure your webhook provider (e.g., TradingView) to send a `POST` request to the bot's webhook URL:

-   **URL**: `http://<your-pi-ip-or-domain>/webhook`
-   **Method**: `POST`
-   **Body (JSON)**:

    ```json
    {
      "symbol": "AAPL",
      "action": "buy",
      "user": "TradingView",
      "price": "173.45"
    }
    ```

Supported `action` values are `buy`, `sell`, and `close`.

## 🤝 Contributing

Contributions are welcome! If you have ideas for new features, improvements, or bug fixes, please open an issue or submit a pull request.

## 📄 License

This project is licensed under the MIT License. See the `LICENSE` file for details.

## Implementation Notes
* The bot uses `alpaca-trade-api` to communicate with the Alpaca paper account.
* Fractional trading is achieved by specifying the `notional` parameter when submitting orders.
* All requests are logged with timestamps in `trades.log` for troubleshooting.
* Designed to run on a Raspberry Pi (or any Linux environment with Python 3).
Usefull commands:
source venv/bin/activate
python3 dashboard.py
python3 bot.py
sudo systemctl status fallback_dashboard.service
sudo lsof -i :5050
sudo systemctl start  fallback.service fallback_dashboard.service
sudo systemctl enable fallback.service fallback_dashboard.service
sudo systemctl daemon-reload
sudo systemctl restart fallback.service 
systemctl --type=service --state=running
sudo nano /etc/apache2/sites-available/trading_bot.conf
