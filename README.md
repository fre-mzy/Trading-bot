# XAU/USD paper-trading signal bot

Every 5 minutes (GitHub Actions) the bot checks gold, and if a setup passes every filter and Gemini
approves it, it logs a **paper** trade and sends a Telegram alert. When TP or SL is hit it records
the WIN/LOSS, updates a virtual $10,000 account and tells you.

Pipeline: market hours -> data freshness -> 5m+1h EMA alignment -> news-spike filter -> ADX/RSI/spread
guardrails -> Gemini veto (confidence >= 75) -> paper trade + alert.

## Setup (10 minutes)
1. Repo **Settings -> Secrets and variables -> Actions -> New repository secret**, add:
   `TWELVE_DATA_API_KEY`, `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
2. (Optional) add a repository **variable** `ASSUMED_SPREAD` = what your broker charges on gold, in dollars.
3. **Actions -> Tests -> Run workflow**: runs the offline tests plus a live check of both API keys.
4. **Actions -> XAU/USD Paper Trading Pipeline -> Run workflow** once by hand and read the log.
5. Message your bot `/status`, `/open` or `/stats` (answered within ~5 minutes, on the next cycle).

## Things to know
- All results are **paper**. The spread is assumed (Twelve Data does not reliably give gold bid/ask).
- Free Twelve Data allows 800 requests/day; the bot uses 2 per cycle and none while the market is closed.
- GitHub cron can run late (10+ min is common) and is not guaranteed.
- Trades are settled on candles after the signal; a candle touching both TP and SL counts as a loss.
- Change settings with env vars / repo variables (see `config.py`). Run `pytest -q` before editing rules.
- Do not risk real money until you have a few hundred settled paper trades and know the win rate and drawdown.
