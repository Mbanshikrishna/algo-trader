# Chanakya Trading Engine

AI-powered intraday trading bot using Python, Zerodha API, and AWS.

## Project Overview

Chanakya Trading Engine is a modular intraday trading framework for Indian markets. It is designed for strategy experimentation in paper-trading mode first, and supports a path to live execution with Zerodha Kite APIs.

## Repository Structure

- `main.py` - Orchestrates one intraday scan and execution cycle
- `Stock.py` - Standalone scanner entrypoint
- `config/settings.py` - Environment-driven runtime settings
- `data/market_stream.py` - Market data fetch and normalization
- `strategy/momentum_strategy.py` - Signal generation logic
- `risk/risk_manager.py` - Position sizing and guardrails
- `execution/order_manager.py` - Order placement abstraction
- `broker/kite_client.py` - Zerodha API wrapper
- `monitor/position_tracker.py` - Position state tracking
- `db/trade_db.py` - Trade logging (SQLite)
- `utils/logger.py` - Console/file logging utilities
- `utils/telegram_alert.py` - Telegram notification helper

## Quick Start

1. Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

If `requirements.txt` is not available, install the core libraries manually:

```bash
pip install yfinance pandas ta
```

3. Configure environment variables:

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

4. Run the bot:

```bash
python main.py
```

Run scanner only:

```bash
python Stock.py
```

## Testing

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

## Deployment Notes (AWS EC2)

- Start with `PAPER_TRADE=true` until strategies and risk controls are validated.
- Use `systemd` or `supervisor` to keep the process alive.
- Keep API keys/secrets in environment variables or a secrets manager (e.g., AWS SSM/Secrets Manager).
- Add monitoring/alerts for bot health, order failures, and risk-limit breaches.

## Risk Disclaimer

This project is for educational and research purposes. Live trading involves substantial financial risk.
