"""
Layer 1 — Continuous Watcher (GitHub Actions version)

Unlike the original always-running loop, this version runs ONCE per invocation
and exits — GitHub Actions triggers it on a schedule (e.g. every 5-15 minutes)
instead of it looping forever on a server.

Database persistence: since each GitHub Actions run starts on a fresh machine,
signals_test.db is committed back into the repo at the end of each run by the
workflow (see .github/workflows/poll.yml) so history carries over between runs.

API keys are read from environment variables, which GitHub Actions injects
from repo Secrets (never hardcoded, never committed).
"""

import os
import json
import sqlite3
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIG (from environment / GitHub Secrets)
# ---------------------------------------------------------------------------
TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # optional until Layer 2 is wired in
SYMBOL = "XAU/USD"
DB_PATH = "signals_test.db"

# ---------------------------------------------------------------------------
# DATABASE SETUP
# ---------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS open_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            direction TEXT,
            entry REAL,
            take_profit REAL,
            stop_loss REAL,
            confidence TEXT,
            reasoning TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            direction TEXT,
            entry REAL,
            exit_price REAL,
            result TEXT,
            resolved_at TEXT
        )
    """)
    conn.commit()
    return conn

# ---------------------------------------------------------------------------
# DATA FETCH (Twelve Data)
# ---------------------------------------------------------------------------
def fetch_candles():
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": "5min",
        "outputsize": 100,
        "apikey": TWELVE_DATA_API_KEY,
    }
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()

    if "values" not in data:
        print("Unexpected response (rate limit or bad key?):", data)
        send_telegram_message(f"⚠️ Twelve Data error: {data}")
        return []

    # Twelve Data returns newest-first; reverse so oldest is first, newest last
    values = list(reversed(data["values"]))
    closes = [float(v["close"]) for v in values]
    return closes

# ---------------------------------------------------------------------------
# INDICATORS
# ---------------------------------------------------------------------------
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for price in values[period:]:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        change = values[-i] - values[-i - 1]
        if change >= 0:
            gains.append(change)
        else:
            losses.append(abs(change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ---------------------------------------------------------------------------
# CANDIDATE FILTER
# ---------------------------------------------------------------------------
def is_candidate(closes):
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    current_rsi = rsi(closes)

    if ema9 is None or ema21 is None or current_rsi is None:
        return False, {}

    crossover = ema9 > ema21
    rsi_extreme = current_rsi > 65 or current_rsi < 35

    context = {
        "ema9": round(ema9, 4),
        "ema21": round(ema21, 4),
        "rsi": round(current_rsi, 2),
        "last_price": closes[-1],
    }

    return (crossover and rsi_extreme), context

# ---------------------------------------------------------------------------
# STUBBED PREDICTION (use this until Layer 2 / Claude is wired in)
# ---------------------------------------------------------------------------
def get_stub_prediction(context):
    direction = "BUY" if context["ema9"] > context["ema21"] else "SELL"
    entry = context["last_price"]
    take_profit = round(entry * 1.01, 4) if direction == "BUY" else round(entry * 0.99, 4)
    stop_loss = round(entry * 0.995, 4) if direction == "BUY" else round(entry * 1.005, 4)

    return {
        "direction": direction,
        "entry": entry,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "confidence": "test-stub",
        "reasoning": "Stubbed response for pipeline testing — not a real signal.",
    }

# ---------------------------------------------------------------------------
# NOTIFICATION (real Telegram send)
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing — falling back to console print.")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        resp = requests.post(url, data=payload, timeout=10)
        if not resp.ok:
            print("Telegram send failed:", resp.text)
    except requests.RequestException as e:
        print("Telegram send error:", e)

def print_notification(prediction):
    message = (
        f"Found a call -> {prediction['direction']} @ {prediction['entry']}\n"
        f"TP: {prediction['take_profit']} | SL: {prediction['stop_loss']}\n"
        f"Confidence: {prediction['confidence']}\n"
        f"Reasoning: {prediction['reasoning']}"
    )
    send_telegram_message(message)
    print(f"[NOTIFY] {message}")

# ---------------------------------------------------------------------------
# SINGLE RUN (called once per GitHub Actions trigger)
# ---------------------------------------------------------------------------
def run_once():
    conn = init_db()
    c = conn.cursor()

    closes = fetch_candles()
    if not closes:
        print("No data returned this run.")
        send_telegram_message("⚠️ Heartbeat: run completed but no price data returned.")
        return

    candidate, context = is_candidate(closes)
    send_telegram_message(f"✅ Heartbeat: run completed. Candidate found: {candidate}")

    if candidate:
        prediction = get_stub_prediction(context)  # swap for get_claude_prediction(context) later
        c.execute(
            """INSERT INTO open_calls
               (timestamp, direction, entry, take_profit, stop_loss, confidence, reasoning)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                prediction["direction"],
                prediction["entry"],
                prediction["take_profit"],
                prediction["stop_loss"],
                prediction["confidence"],
                prediction["reasoning"],
            ),
        )
        conn.commit()
        print_notification(prediction)
    else:
        print(f"[{datetime.now(timezone.utc).isoformat()}] No candidate this run.")

    conn.close()

if __name__ == "__main__":
    run_once()
    
