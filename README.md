# Alpaca Trading Bot

This repository contains a simple Flask based webhook server which sends trade orders to the Alpaca paper trading API. The script logs each opened and closed position to `trades.log`.

## Features
* Receives trade signals from TradingView via HTTP POST on `/webhook` (port `5000`).
* Sends market orders using a fixed notional value of **$2000** per trade (fractional shares enabled).
* Supports `buy`, `sell` and `close` actions.
* Logs order submissions, successful closes and errors.

## Usage
1. Install dependencies (preferably inside a virtual environment):
   ```bash
   pip install -r requirements.txt
   ```
2. Ensure the Alpaca paper trading credentials are available in the environment:
   ```bash
   export ALPACA_KEY=PK0DBBQMMCVL0AN1ERMC
   export ALPACA_SECRET=qZkdh3gFJQwNxyxCFL17kwe95rj1GcVI6195stBc
   ```
3. Start the server:
   ```bash
   python3 bot.py
   ```
   The webhook will listen on `http://<host>:5000/webhook`.

## Webhook Payload
TradingView should send JSON formatted like:
```json
{
  "symbol": "AAPL",
  "action": "buy",
  "user": "Test",
  "price": "173.45"
}
```
The `action` field may contain `buy`, `sell` or `close`. Any error responses are logged to `trades.log`.

## Implementation Notes
* The bot uses `alpaca-trade-api` to communicate with the Alpaca paper account.
* Fractional trading is achieved by specifying the `notional` parameter when submitting orders.
* All requests are logged with timestamps in `trades.log` for troubleshooting.
* Designed to run on a Raspberry Pi (or any Linux environment with Python 3).
