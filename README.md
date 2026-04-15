# Algo Trader

`algo-trader` is a modular intraday trading bot project for Indian equities. It is structured as a small pipeline where configuration is loaded, market data is fetched, a strategy decides whether a trade exists, risk rules size the trade, an order layer executes it, and supporting modules track positions, log activity, and send alerts.

The codebase is designed around live Angel One execution with SmartAPI-backed market data and order placement.

## End-to-End Workflow

The core runtime flow of the project is:

1. Config and environment loading
2. Market data collection
3. Strategy signal generation
4. Risk-based position sizing
5. Order creation and execution
6. Monitoring and alerts

This is orchestrated mainly from `main.py`.

## How The Bot Runs

When you run `python main.py`, the project follows this sequence:

1. `config/settings.py` reads environment variables such as API credentials, capital, risk percentage, and runtime options.
2. `utils/logger.py` creates a reusable logger for console and file output.
3. `broker/angelone_client.py` builds a broker client instance for live Angel One SmartAPI requests.
4. `execution/order_manager.py` receives trade instructions, builds the Angel One order payload, and forwards it to the broker client.
5. `risk/risk_manager.py` calculates how many shares can be traded based on account capital, per-trade risk percentage, entry price, and stop-loss.
6. `data/market_stream.py` downloads intraday OHLCV candle data using `yfinance`.
7. `strategy/momentum_strategy.py` adds indicators like EMA, VWAP, average volume, and intraday high, then decides whether a symbol qualifies for a `BUY` signal.
8. `monitor/position_tracker.py` updates the in-memory view of currently open positions.
9. `utils/telegram_alert.py` optionally sends a Telegram message after a signal is executed.

## Codebase Structure

### Entry Points

- `main.py`
  Runs one full trading cycle across the hardcoded watchlist. This is the main application entrypoint.
- `Stock.py`
  Runs only the scanning portion of the logic and prints symbols that currently match the momentum criteria.

### Configuration Layer

- `config/settings.py`
  Defines the `Settings` dataclass and loads environment variables into a strongly structured runtime config object.

This layer answers:
- What credentials should be used?
- How much capital is available?
- What percentage of capital can be risked per trade?

### Market Data Layer

- `data/market_stream.py`
  Fetches intraday OHLCV data for a symbol using `yfinance`, then normalizes the returned columns into a plain pandas DataFrame.

This layer answers:
- What is the latest candle data for each stock?
- Is there enough data to evaluate the strategy?

### Strategy Layer

- `strategy/momentum_strategy.py`
  Applies the core trading idea. It calculates:
  - Fast EMA
  - Slow EMA
  - VWAP
  - Average rolling volume
  - Running day high

It then creates a `BUY` signal when the latest candle satisfies all of these conditions:

1. Close is above VWAP
2. Close is above the fast EMA
3. Close is above the slow EMA
4. Current volume is above recent average volume
5. Close is very near the intraday high

If the setup is valid, the strategy returns a signal payload containing:
- `symbol`
- `side`
- `price`
- `stop_loss`

If not, it returns `None`.

### Risk Layer

- `risk/risk_manager.py`
  Converts the strategy signal into a trade size.

It does this by:

1. Calculating risk per share as `entry_price - stop_loss`
2. Calculating allowed total risk as:
   `capital * (risk_per_trade_pct / 100)`
3. Dividing total allowed risk by risk per share
4. Returning a whole-share quantity

If the stop-loss is invalid or risk per share is zero, it returns `0`, which prevents the trade from going forward.

### Execution Layer

- `execution/order_manager.py`
  Builds the order payload used by the bot.

Behavior:
- It builds a live Angel One order payload
- It passes the order to the broker client

- `broker/angelone_client.py`
  Represents the broker integration layer used for live SmartAPI requests, including order placement, instrument lookup, and candle data retrieval.

### Monitoring

- `monitor/position_tracker.py`
  Maintains an in-memory snapshot of open positions during the current session.

Main responsibilities:
- Add a new position when a symbol is bought for the first time
- Recalculate weighted average price if more quantity is added
- Reduce or remove the position when shares are sold
- Return a snapshot of current open positions

### Utilities

- `utils/logger.py`
  Configures logging to both console and a log file. This is useful for debugging, audit trails, and observing runtime behavior.

- `utils/telegram_alert.py`
  Sends Telegram notifications when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are configured in the environment.

## Runtime Flow Inside `main.py`

`main.py` wires the whole system together:

1. Loads settings
2. Sets up logging
3. Validates the Angel One credentials needed for live trading
4. Creates broker, order, risk, strategy, data, and monitoring objects
5. Loops through each symbol in the watchlist
6. Fetches OHLCV data
7. Builds a signal
8. Skips symbols with no valid signal
9. Calculates position size
10. Skips symbols with zero or invalid quantity
11. Places the order
12. Updates the position tracker
13. Logs and sends a Telegram notification
14. Handles per-symbol exceptions so one failure does not stop the whole run
15. Logs a final open-position snapshot

## Runtime Flow Inside `Stock.py`

`Stock.py` is a lighter read-only scanner:

1. Creates a market stream
2. Creates the momentum strategy
3. Loops through the stock list
4. Fetches market data for each symbol
5. Builds a signal
6. Collects matching stocks
7. Prints the results as a pandas DataFrame

This file is useful when you only want to inspect setups without placing orders.

You can also scan the symbols from an Excel workbook instead of the built-in watchlist:

```bash
python Stock.py --excel-path "C:\path\to\pnl.xlsx"
```

## Tests

- `tests/test_core.py`
  Contains lightweight unit tests for the core components:
  - Risk sizing
  - Live order payload creation
  - Position tracking
  - Strategy signal structure

Run tests with:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

## Angel One Data Check

Before enabling live trading, run the read-only connectivity check:

```bash
python check_angelone_data.py --symbol RELIANCE.NS
```

What it verifies:

- Credentials are present
- The symbol resolves to an Angel One tradingsymbol and token
- LTP data can be fetched
- Batch quote data can be fetched
- Historical candles can be fetched for the requested interval and period

This command does not place any orders.

## Telegram Market Summary

To send a Telegram update with the latest daily data for specific stocks, run:

```bash
python send_stock_updates.py SBIN INFY RELIANCE
```

To send the status for all symbols listed in an Excel P&L workbook, run:

```bash
python send_stock_updates.py --excel-path "C:\path\to\pnl.xlsx"
```

This command:

- normalizes symbols like `SBIN` into `SBIN.NS`
- fetches the latest daily OHLCV snapshot plus percentage change versus the previous close
- sends one formatted Telegram message using `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`

By default it uses `yfinance` so you can send read-only updates without Angel One credentials. To use Angel One data instead:

```bash
python send_stock_updates.py SBIN INFY --provider angelone
```

## Environment Variables

The main runtime variables used by the project are:

```env
ANGELONE_API_KEY=your_api_key
ANGELONE_CLIENT_ID=your_client_id
ANGELONE_ACCESS_TOKEN=your_access_token
CAPITAL=100000
RISK_PER_TRADE_PCT=1.0
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Notes:
- Angel One credentials are required for live trading.
- Telegram variables are optional; leave them blank if alerts are not configured yet.

## Local Setup

Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Then run either:

```bash
python main.py
```

or:

```bash
python Stock.py
```

## Deployment Files Included

The repository now includes a few deployment-friendly files:

- `requirements.txt`
  Lists the Python packages needed to run the bot.
- `.env.example`
  Shows the environment variables you should define before running the app.
- `deploy/algo-trader.service`
  A ready-to-adapt `systemd` service file for running the bot on Linux servers such as AWS EC2.

## Suggested EC2 Deployment Workflow

If you want to deploy this application on an Ubuntu EC2 instance, use this order:

1. Launch the server and connect through SSH
2. Install Python, `venv`, and Git
3. Clone this repository
4. Create and activate `.venv`
5. Run `pip install -r requirements.txt`
6. Copy `.env.example` to `.env` and fill in real values
7. Test the bot manually with `python main.py`
8. Copy `deploy/algo-trader.service` into `/etc/systemd/system/`
9. Enable and start the service with `systemctl`

Example:

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
git clone <your-repo-url>
cd algo-trader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
sudo cp deploy/algo-trader.service /etc/systemd/system/algo-trader.service
sudo systemctl daemon-reload
sudo systemctl enable algo-trader
sudo systemctl start algo-trader
sudo systemctl status algo-trader
```

## Current Design Notes

What the project already does well:

- Separates concerns cleanly by module
- Keeps trading, risk, execution, monitoring, and storage logic isolated
- Includes a basic automated test file

What is still intentionally simple:

- Broker integration is lightweight and still needs stronger production hardening
- Watchlist is hardcoded
- Only long-side `BUY` signals are implemented
- Position tracking is in-memory for the current process only
- There is no scheduler, market-hours guard, stop-loss execution, or target management yet

## Suggested Next Improvements

Good next steps for evolving the project:

1. Add session-login and token-refresh support for Angel One `SmartAPI`
2. Add trading window checks and exchange holiday handling
3. Move the watchlist into config or a database
4. Add stop-loss and target exit handling
5. Persist positions and order state across restarts
6. Add retries and stronger error handling around data and network calls
7. Add structured logs and richer Telegram alerts
8. Add backtesting or paper-trade session reports

## Disclaimer

This project is for educational and development purposes only. It is not financial advice. Live trading involves real risk, and any production use should include stronger safeguards, broker-tested execution, and careful validation.
