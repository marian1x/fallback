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
    source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
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