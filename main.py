"""
Layer 1 — Continuous Watcher (GitHub Actions scheduled runner)

This script executes once per invocation on a GitHub Actions schedule (e.g., every 5–15 mins).
It fetches candle data from Twelve Data, verifies candle wicks to resolve open TP/SL calls,
scans for new EMA/RSI crossover signals, validates candidate setups via Google Gemini,
logs open calls/history into SQLite, and sends notifications via Telegram.
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOL = "XAU/USD"
DB_PATH = "signals_test.db"

TRADING_GUIDELINES = """You are validating a candidate trade setup for XAU/USD (gold vs USD).
A rule-based filter has flagged this as a POSSIBLE candidate setup. Decide whether
it's a genuine setup, and if so, provide exact trade parameters.

Risk rules:
- Stop loss must be based on a sensible level or ATR-style distance, not a round guess.
- Take profit should reflect at least a 1:1.5 reward-to-risk ratio versus the stop loss.
- If you can't justify a stop loss with reasonable confidence, reject the candidate.

Respond with ONLY a valid JSON object matching this schema exactly:
{
  "is_signal": true or false,
  "direction": "BUY" or "SELL" or null,
  "entry": number or null,
  "take_profit": number or null,
  "stop_loss": number or null,
  "confidence": "low" | "medium" | "high" or null,
  "reasoning": "1-3 sentences"
}
"""

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
# NOTIFICATION (Telegram)
# ---------------------------------------------------------------------------
def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing — printing to console:")
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
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
    except Exception as e:
        print("Twelve Data HTTP request error:", e)
        send_telegram_message(f"⚠️ Twelve Data request exception: {e}")
        return []

    if "values" not in data:
        print("Unexpected response from Twelve Data:", data)
        send_telegram_message(f"⚠️ Twelve Data error: {data}")
        return []

    # Reverse list so array is chronological: oldest -> index 0, latest -> index -1
    candles = list(reversed(data["values"]))
    return candles

# ---------------------------------------------------------------------------
# TECHNICAL INDICATORS
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
# CANDIDATE FILTER WITH DUPLICATE PROTECTION
# ---------------------------------------------------------------------------
def is_candidate(candles, conn):
    if len(candles) < 22:
        return False, {}

    closes = [float(v["close"]) for v in candles]

    # Calculate previous and current EMAs to verify a true cross event
    prev_ema9 = ema(closes[:-1], 9)
    prev_ema21 = ema(closes[:-1], 21)
    curr_ema9 = ema(closes, 9)
    curr_ema21 = ema(closes, 21)
    current_rsi = rsi(closes)

    if None in (prev_ema9, prev_ema21, curr_ema9, curr_ema21, current_rsi):
        return False, {}

    bullish_cross = (prev_ema9 <= prev_ema21) and (curr_ema9 > curr_ema21)
    bearish_cross = (prev_ema9 >= prev_ema21) and (curr_ema9 < curr_ema21)
    rsi_extreme = current_rsi > 65 or current_rsi < 35

    candidate = (bullish_cross or bearish_cross) and rsi_extreme
    direction = "BUY" if bullish_cross else "SELL"

    # Prevent duplicate position generation if an open position already exists
    if candidate:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM open_calls WHERE direction = ?", (direction,))
        if c.fetchone()[0] > 0:
            print(f"Skipping signal: Existing open {direction} call already active in DB.")
            return False, {}

    context = {
        "ema9": round(curr_ema9, 4),
        "ema21": round(curr_ema21, 4),
        "rsi": round(current_rsi, 2),
        "last_price": closes[-1],
        "direction": direction,
    }

    return candidate, context

# ---------------------------------------------------------------------------
# STUBBED PREDICTION (Fallback)
# ---------------------------------------------------------------------------
def get_stub_prediction(context):
    direction = context.get("direction", "BUY")
    entry = context["last_price"]
    take_profit = round(entry * 1.01, 4) if direction == "BUY" else round(entry * 0.99, 4)
    stop_loss = round(entry * 0.995, 4) if direction == "BUY" else round(entry * 1.005, 4)

    return {
        "is_signal": True,
        "direction": direction,
        "entry": entry,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "confidence": "test-stub",
        "reasoning": "Stubbed response for pipeline testing — not a real signal.",
    }

# ---------------------------------------------------------------------------
# GEMINI LLM VALIDATION
# ---------------------------------------------------------------------------
def get_gemini_prediction(context):
    if not GEMINI_API_KEY:
        print("No Gemini API Key found. Falling back to stub prediction.")
        return get_stub_prediction(context)

    # Standard v1beta Flash endpoint
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"

    user_prompt = (
        f"{TRADING_GUIDELINES}\n\n"
        f"Candidate Indicator Context:\n"
        f"Direction Bias: {context['direction']}\n"
        f"EMA9: {context['ema9']}\n"
        f"EMA21: {context['ema21']}\n"
        f"RSI: {context['rsi']}\n"
        f"Last Price: {context['last_price']}\n"
    )

    payload = {
        "contents": [{"parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "response_mime_type": "application/json"
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=20)
        data = resp.json()

        if "candidates" not in data:
            print("Gemini response missing candidates:", data)
            send_telegram_message(f"⚠️ Gemini error/rate limit: {data}")
            return get_stub_prediction(context)

        raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        result = json.loads(raw_text)
    except Exception as e:
        print("Gemini API or JSON parse error:", e)
        send_telegram_message(f"⚠️ Gemini validation failed: {e}")
        return get_stub_prediction(context)

    if not result.get("is_signal"):
        print("Gemini declined candidate signal:", result.get("reasoning"))
        send_telegram_message(
            f"🔍 Candidate rejected by Gemini.\nReasoning: {result.get('reasoning')}"
        )
        return None

    return result

# ---------------------------------------------------------------------------
# OUTCOME TRACKING (Wick-Aware High/Low Resolution)
# ---------------------------------------------------------------------------
def resolve_open_calls(conn, latest_candle):
    c = conn.cursor()
    c.execute("SELECT id, timestamp, direction, entry, take_profit, stop_loss FROM open_calls")
    open_rows = c.fetchall()

    if not open_rows:
        return

    high_price = float(latest_candle["high"])
    low_price = float(latest_candle["low"])
    close_price = float(latest_candle["close"])

    for row in open_rows:
        call_id, timestamp, direction, entry, take_profit, stop_loss = row
        result = None

        if direction == "BUY":
            if high_price >= take_profit:
                result = "WIN"
            elif low_price <= stop_loss:
                result = "LOSS"
        elif direction == "SELL":
            if low_price <= take_profit:
                result = "WIN"
            elif high_price >= stop_loss:
                result = "LOSS"

        if result:
            resolved_at = datetime.now(timezone.utc).isoformat()
            c.execute(
                """INSERT INTO history (timestamp, direction, entry, exit_price, result, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (timestamp, direction, entry, close_price, result, resolved_at),
            )
            c.execute("DELETE FROM open_calls WHERE id = ?", (call_id,))
            conn.commit()

            emoji = "✅" if result == "WIN" else "❌"
            send_telegram_message(
                f"{emoji} {result} — {direction} call resolved\n"
                f"Entry: {entry} | Exit: {close_price}\n"
                f"(TP: {take_profit} | SL: {stop_loss})"
            )
            print(f"Resolved call {call_id} as {result}")

# ---------------------------------------------------------------------------
# SINGLE RUN INVOCATION
# ---------------------------------------------------------------------------
def run_once():
    conn = init_db()

    candles = fetch_candles()
    if not candles:
        print("No price candles retrieved this run.")
        send_telegram_message("⚠️ Heartbeat: run completed but no price data was retrieved.")
        conn.close()
        return

    latest_candle = candles[-1]
    
    # 1. Resolve open trades using current candle High / Low wicks
    resolve_open_calls(conn, latest_candle)

    # 2. Check technical scanner for a crossover candidate
    candidate, context = is_candidate(candles, conn)

    if candidate:
        prediction = get_gemini_prediction(context)
        if prediction is None:
            print("Candidate flagged, but LLM validation rejected the setup.")
            conn.close()
            return

        c = conn.cursor()
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

        message = (
            f"🚀 Signal Generated -> {prediction['direction']} @ {prediction['entry']}\n"
            f"TP: {prediction['take_profit']} | SL: {prediction['stop_loss']}\n"
            f"Confidence: {prediction['confidence']}\n"
            f"Reasoning: {prediction['reasoning']}"
        )
        send_telegram_message(message)
        print(f"[SIGNAL ACCEPTED] {message}")
    else:
        print(f"[{datetime.now(timezone.utc).isoformat()}] No candidate detected on this run.")

    conn.close()

if __name__ == "__main__":
    run_once()
    
