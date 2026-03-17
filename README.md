<<<<<<< HEAD
<<<<<<< HEAD
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
=======
=======
>>>>>>> set-up-bot
# algo-trader

Automated **intraday trading bot** for Indian markets using the **Zerodha Kite API**, with support for running continuously on an **AWS EC2** instance.

## Project Overview

This repository is structured as a modular event-driven trading bot:

- `data/market_stream.py` – consumes live market ticks/candles.
- `strategy/momentum_strategy.py` – generates buy/sell signals.
- `execution/order_manager.py` – places and manages orders.
- `risk/risk_manager.py` – enforces position sizing and limits.
- `monitor/position_tracker.py` – tracks open positions/PnL.
- `db/trade_db.py` – persists trade and bot state.
- `utils/telegram_alert.py` – sends alerts/notifications.
- `main.py` – application entrypoint.

## Zerodha API Setup

1. Create a Kite Connect app from your Zerodha developer account.
2. Collect:
   - `KITE_API_KEY`
   - `KITE_API_SECRET`
3. Implement a secure token flow:
   - Generate `request_token` via login.
   - Exchange for `access_token`.
   - Store token securely (never commit secrets to git).
4. Configure keys/tokens through environment variables or a secure secrets manager.

## AWS EC2 Deployment (Recommended Baseline)

### 1) Launch server

- Use Ubuntu 22.04 LTS (or latest stable).
- Instance type for small setups: `t3.small` / `t3.medium`.
- Attach IAM role (if using AWS services like SSM/CloudWatch/Secrets Manager).

### 2) Install dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git
```

### 3) Clone and configure

```bash
git clone <your-repo-url>
cd algo-trader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt  # if present
```

### 4) Configure environment

Create a `.env` (or equivalent secure config):

```env
KITE_API_KEY=...
KITE_API_SECRET=...
KITE_ACCESS_TOKEN=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### 5) Run bot as a service

Use `systemd` so the bot auto-starts and restarts on failure.

Example unit file `/etc/systemd/system/algo-trader.service`:

```ini
[Unit]
Description=Algo Trader Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/algo-trader
Environment="PATH=/home/ubuntu/algo-trader/.venv/bin"
ExecStart=/home/ubuntu/algo-trader/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable algo-trader
sudo systemctl start algo-trader
sudo systemctl status algo-trader
```

## Production Safety Checklist

- Add strict stop-loss and max daily loss guardrails.
- Enforce max concurrent positions and exposure per symbol.
- Handle API/network retries with cooldown.
- Persist state so reboot does not lose positions/orders context.
- Add market-hours checks and holiday calendar handling.
- Send Telegram alerts for order failures and risk breaches.
- Log every signal, order request/response, and risk decision.

## Important Notes

- Run in **paper/sandbox mode first** until strategy behavior is stable.
- Keep AWS security groups restrictive (SSH only from trusted IP).
- Rotate API tokens and secrets regularly.
- Ensure compliance with broker/exchange rules and your local regulations.

## Disclaimer

This project is for educational purposes only and does not constitute financial advice. Live trading involves substantial risk.
<<<<<<< HEAD
>>>>>>> codex/set-up-intraday-trading-bot-on-aws
=======
=======
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
>>>>>>> origin/codex/setup-aws-ec2-for-trading-bot
>>>>>>> set-up-bot
