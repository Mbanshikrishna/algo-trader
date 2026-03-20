Use Ubuntu on EC2 and deploy this repo as a systemd service. The repo already includes a service template at deploy/algo-trader.service and the env vars it expects are shown in algo-trader/.env.example.

Launch an Ubuntu EC2 instance.
Open at least port 22 in the security group so you can SSH in.

SSH into the instance.

ssh -i /path/to/your-key.pem ubuntu@<EC2_PUBLIC_IP>

Install system packages.
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git

Clone your repo and enter it.
git clone <your-repo-url>
cd algo-trader

Create and activate the virtual environment.
python3 -m venv .venv
source .venv/bin/activate

Install Python dependencies from requirements.txt.
pip install -r requirements.txt

Create the env file.
cp .env.example .env
nano .env

Fill in your values in .env.
ANGELONE_API_KEY=your_api_key
ANGELONE_CLIENT_ID=your_client_id
ANGELONE_ACCESS_TOKEN=your_access_token
PAPER_TRADE=true
CAPITAL=100000
RISK_PER_TRADE_PCT=1.0
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

First run it manually to confirm it starts.
python main.py

Install the service file.
sudo cp deploy/algo-trader.service /etc/systemd/system/algo-trader.service
sudo systemctl daemon-reload
sudo systemctl enable algo-trader
sudo systemctl start algo-trader
sudo systemctl status algo-trader

Check logs if needed.

journalctl -u algo-trader -f

A couple of important notes:

The service file assumes the repo is at /home/ubuntu/algo-trader and runs as user ubuntu. If you deploy somewhere else, edit /etc/systemd/system/algo-trader.service accordingly.
Right now algo-trader/broker/angelone_client.py is still a stub, so EC2 deployment works, but real Angel One live trading is not wired up yet. Keep PAPER_TRADE=true unless we add real SmartAPI integration.


# Algo Trader

`algo-trader` is a modular intraday trading bot project for Indian equities. It is structured as a small pipeline where configuration is loaded, market data is fetched, a strategy decides whether a trade exists, risk rules size the trade, an order layer executes it, and supporting modules track positions, log trades, and send alerts.

The codebase is currently paper-trade friendly, with a stubbed Angel One client that can later be replaced with a real `SmartAPI` integration.

## End-to-End Workflow

The core runtime flow of the project is:

1. Config and environment loading
2. Market data collection
3. Strategy signal generation
4. Risk-based position sizing
5. Order creation and execution
6. Monitoring, database logging, and alerts

This is orchestrated mainly from `main.py`.

## How The Bot Runs

When you run `python main.py`, the project follows this sequence:

1. `config/settings.py` reads environment variables such as API credentials, capital, risk percentage, and whether paper trading is enabled.
2. `utils/logger.py` creates a reusable logger for console and file output.
3. `broker/angelone_client.py` builds a broker client instance. Right now this is a placeholder wrapper that returns a mock successful order response.
4. `execution/order_manager.py` receives trade instructions and either simulates a fill in paper mode or forwards the order to the broker client in live mode.
5. `risk/risk_manager.py` calculates how many shares can be traded based on account capital, per-trade risk percentage, entry price, and stop-loss.
6. `data/market_stream.py` downloads intraday OHLCV candle data using `yfinance`.
7. `strategy/momentum_strategy.py` adds indicators like EMA, VWAP, average volume, and intraday high, then decides whether a symbol qualifies for a `BUY` signal.
8. `db/trade_db.py` writes executed trades into a local SQLite database.
9. `monitor/position_tracker.py` updates the in-memory view of currently open positions.
10. `utils/telegram_alert.py` optionally sends a Telegram message after a signal is executed.

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
- Are we paper trading or live trading?
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
- In paper mode, it returns a local order dictionary with status `PAPER_FILLED`
- In live mode, it passes the order to the broker client

- `broker/angelone_client.py`
  Represents the broker integration layer. It currently acts as a placeholder and returns a fake successful order response. This lets the rest of the system run without requiring live Angel One credentials or a production order flow.

### Monitoring and Persistence

- `monitor/position_tracker.py`
  Maintains an in-memory snapshot of open positions during the current session.

Main responsibilities:
- Add a new position when a symbol is bought for the first time
- Recalculate weighted average price if more quantity is added
- Reduce or remove the position when shares are sold
- Return a snapshot of current open positions

- `db/trade_db.py`
  Manages a local SQLite database and stores executed trades in the `trades` table.

The table stores:
- Symbol
- Side
- Quantity
- Price
- Status
- Timestamp

### Utilities

- `utils/logger.py`
  Configures logging to both console and a log file. This is useful for debugging, audit trails, and observing runtime behavior.

- `utils/telegram_alert.py`
  Sends Telegram notifications when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are configured in the environment.

## Runtime Flow Inside `main.py`

`main.py` wires the whole system together:

1. Loads settings
2. Sets up logging
3. Validates live-trading credentials if paper mode is disabled
4. Creates broker, order, risk, strategy, data, database, and monitoring objects
5. Loops through each symbol in the watchlist
6. Fetches OHLCV data
7. Builds a signal
8. Skips symbols with no valid signal
9. Calculates position size
10. Skips symbols with zero or invalid quantity
11. Places the order
12. Logs the trade to SQLite
13. Updates the position tracker
14. Logs and sends a Telegram notification
15. Handles per-symbol exceptions so one failure does not stop the whole run
16. Logs a final open-position snapshot

## Runtime Flow Inside `Stock.py`

`Stock.py` is a lighter read-only scanner:

1. Creates a market stream
2. Creates the momentum strategy
3. Loops through the stock list
4. Fetches market data for each symbol
5. Builds a signal
6. Collects matching stocks
7. Prints the results as a pandas DataFrame

This file is useful when you only want to inspect setups without placing or simulating orders.

## Tests

- `tests/test_core.py`
  Contains lightweight unit tests for the core components:
  - Risk sizing
  - Paper order creation
  - Position tracking
  - Trade database logging
  - Strategy signal structure

Run tests with:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

## Environment Variables

The main runtime variables used by the project are:

```env
ANGELONE_API_KEY=your_api_key
ANGELONE_CLIENT_ID=your_client_id
ANGELONE_ACCESS_TOKEN=your_access_token
PAPER_TRADE=true
CAPITAL=100000
RISK_PER_TRADE_PCT=1.0
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Notes:
- `PAPER_TRADE=true` is the safe default for development.
- If `PAPER_TRADE=false`, `main.py` requires Angel One credentials to be present.
- Telegram variables are optional.

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
7. Keep `PAPER_TRADE=true` for the first deployment
8. Test the bot manually with `python main.py`
9. Copy `deploy/algo-trader.service` into `/etc/systemd/system/`
10. Enable and start the service with `systemctl`

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
- Supports paper trading without requiring broker connectivity
- Includes a basic automated test file

What is still intentionally simple:

- Broker integration is stubbed
- Watchlist is hardcoded
- Only long-side `BUY` signals are implemented
- Position tracking is in-memory for the current process only
- There is no scheduler, market-hours guard, stop-loss execution, or target management yet

## Suggested Next Improvements

Good next steps for evolving the project:

1. Replace the stubbed Angel One client with real `SmartAPI` authentication and order APIs
2. Add trading window checks and exchange holiday handling
3. Move the watchlist into config or a database
4. Add stop-loss and target exit handling
5. Persist positions and order state across restarts
6. Add retries and stronger error handling around data and network calls
7. Add structured logs and richer Telegram alerts
8. Add backtesting or paper-trade session reports

## Disclaimer

This project is for educational and development purposes only. It is not financial advice. Live trading involves real risk, and any production use should include stronger safeguards, broker-tested execution, and careful validation.
