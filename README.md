# Algo Trader (Indian Intraday Bot)

This repository is a starter framework for an **intraday trading bot for the Indian market**.
It is designed for:
- **Broker API:** Zerodha (Kite)
- **Deployment target:** AWS EC2
- **Current mode:** Paper-trade friendly with pluggable live execution

## Project structure

- `main.py` → orchestrates one intraday scan + order flow run
- `Stock.py` → standalone stock scanner using the same momentum strategy
- `config/settings.py` → environment-driven settings
- `data/market_stream.py` → market OHLCV data fetcher
- `strategy/momentum_strategy.py` → EMA + VWAP momentum logic
- `risk/risk_manager.py` → risk-based position sizing
- `execution/order_manager.py` → order placement abstraction
- `broker/kite_client.py` → Zerodha API client wrapper (stub)
- `db/trade_db.py` → SQLite trade logging
- `monitor/position_tracker.py` → in-memory position state tracker
- `utils/logger.py` → file + console logger setup
- `utils/telegram_alert.py` → optional Telegram alert helper

## Strategy (current)

The momentum scan marks a **BUY** signal when:
1. Price is above VWAP
2. Price is above EMA20 and EMA50
3. Current volume is above rolling average volume
4. Price is near intraday high

## Local setup

1. Create virtualenv and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install yfinance pandas ta
```

2. Export environment variables:

```bash
export ZERODHA_API_KEY="your_api_key"
export ZERODHA_API_SECRET="your_api_secret"
export ZERODHA_ACCESS_TOKEN="your_access_token"
export PAPER_TRADE="true"
export CAPITAL="100000"
export RISK_PER_TRADE_PCT="1"
```

Optional Telegram alerts:

```bash
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"
```

3. Run bot:

```bash
python main.py
```

Run scanner only:

```bash
python Stock.py
```

## Tests

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

## EC2 deployment notes

- Use a systemd service or supervisor to keep the bot running.
- Store API secrets in environment variables (or AWS SSM Parameter Store).
- Start in `PAPER_TRADE=true` mode and switch to live only after validation.

## Next improvements

- Integrate official `kiteconnect` SDK in `broker/kite_client.py`
- Add stop-loss / target order management
- Add trading time window & market holiday checks
- Add backtesting and structured logs/alerts