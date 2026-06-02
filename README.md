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
- `--top-k 20`: number of best configurations saved.

Admins can also use the web UI at `Admin Tools -> Admin Strategy Lab` (`/admin/strategy`) to configure strategy runs, manage the signal universe, and compare local vs TradingView signal routing per symbol.

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
  --token "$STRATEGY_WORKER_TOKEN"
```

Set `STRATEGY_WORKER_TOKEN` in the PI5 `.env`. If not set, the dashboard falls back to `INTERNAL_API_KEY`.

For Windows 11, use the standalone agent:

```powershell
cd C:\path\to\fallback
.\venv\Scripts\Activate.ps1
$env:STRATEGY_WORKER_TOKEN="same_value_as_PI5_STRATEGY_WORKER_TOKEN"
python misc\windows_strategy_agent.py --server https://salavat.home.ro/trading --token $env:STRATEGY_WORKER_TOKEN
```

If you want the agent to work through SSH instead of the public HTTPS URL, enable OpenSSH client on Windows and run:

```powershell
python misc\windows_strategy_agent.py `
  --ssh-target pi5@salavat.home.ro `
  --local-port 8765 `
  --token $env:STRATEGY_WORKER_TOKEN
```

This creates an outbound SSH tunnel from Windows to the PI5 and polls `http://127.0.0.1:8765` locally.

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
