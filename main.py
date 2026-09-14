"""
Layer 1 — Continuous Watcher (GitHub Actions scheduled runner)

Features:
- Weekend/Market Hours Guard (prevents unnecessary runs when Gold is closed)
- Multi-Timeframe Confluence (5-min entry aligned with 1-hour EMA50 trend)
- Detailed Reason Reporting (explains exactly why a trade was or wasn't taken)
- High/Low wick-aware trade resolution against TP/SL levels
- Gemini LLM risk validation
- Telegram alerts & daily performance digest
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
  "reasoning": "1-3 sentences explaining why it was accepted or rejected"
}
"""

# ---------------------------------------------------------------------------
# 1. MARKET HOURS GUARD
# ---------------------------------------------------------------------------
def is_market_open():
    """Gold market operates Sun 10:00 PM UTC to Fri 10:00 PM UTC."""
    now = datetime.now(timezone.utc)
    day = now.weekday()  # Mon=0 ... Sat=5, Sun=6
    hour = now.hour

    if day == 5:  # Saturday
        return False
    if day == 6 and hour < 22:  # Sunday before 22:00 UTC
        return False
    if day == 4 and hour >= 22:  # Friday after 22:00 UTC
        return False
    return True

# ---------------------------------------------------------------------------
# DATABASE SETUP & DAILY DIGEST
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

def send_daily_digest_if_scheduled(conn):
    """Sends a trade performance digest around 22:00 UTC daily."""
    now = datetime.now(timezone.utc)
    if now.hour == 22 and now.minute < 15:
        c = conn.cursor()
        c.execute("""
            SELECT result, COUNT(*) 
            FROM history 
            WHERE resolved_at >= datetime('now', '-1 day')
            GROUP BY result
        """)
        stats = dict(c.fetchall())
        wins = stats.get("WIN", 0)
        losses = stats.get("LOSS", 0)
        total = wins + losses
        win_rate = (wins / total * 100) if total > 0 else 0.0

        digest = (
            f"📊 **Daily Performance Digest (24h)**\n"
            f"Resolved Trades: {total}\n"
            f"Wins: {wins} ✅ | Losses: {losses} ❌\n"
            f"Win Rate: {win_rate:.1f}%"
        )
        send_telegram_message(digest)

# ---------------------------------------------------------------------------
# NOTIFICATION (Telegram)
# ---------------------------------------------------------------------------
def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing — console log:")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, data=payload, timeout=10)
        if not resp.ok:
            print("Telegram send failed:", resp.text)
    except requests.RequestException as e:
        print("Telegram request error:", e)

# ---------------------------------------------------------------------------
# DATA FETCH (Twelve Data)
# ---------------------------------------------------------------------------
def fetch_candles(interval="5min", outputsize=100):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
    except Exception as e:
        print(f"Twelve Data error ({interval}):", e)
        return []

    if "values" not in data:
        print(f"Unexpected response ({interval}):", data)
        return []

    return list(reversed(data["values"]))

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
# MULTI-TIMEFRAME CANDIDATE SCANNER (WITH EXPLICIT REASONING)
# ---------------------------------------------------------------------------
def is_candidate(m5_candles, h1_candles, conn):
    if len(m5_candles) < 22 or len(h1_candles) < 50:
        return False, {}, "Insufficient historical candle data."

    # --- 1-Hour Trend Filter ---
    h1_closes = [float(v["close"]) for v in h1_candles]
    h1_ema50 = ema(h1_closes, 50)
    current_price = float(m5_candles[-1]["close"])
    macro_trend = "BULLISH" if current_price > h1_ema50 else "BEARISH"

    # --- 5-Min Entry Signal ---
    m5_closes = [float(v["close"]) for v in m5_candles]
    prev_ema9 = ema(m5_closes[:-1], 9)
    prev_ema21 = ema(m5_closes[:-1], 21)
    curr_ema9 = ema(m5_closes, 9)
    curr_ema21 = ema(m5_closes, 21)
    current_rsi = rsi(m5_closes)

    if None in (prev_ema9, prev_ema21, curr_ema9, curr_ema21, current_rsi, h1_ema50):
        return False, {}, "Indicator calculation error (returned None)."

    bullish_cross = (prev_ema9 <= prev_ema21) and (curr_ema9 > curr_ema21)
    bearish_cross = (prev_ema9 >= prev_ema21) and (curr_ema9 < curr_ema21)
    rsi_extreme = current_rsi > 65 or current_rsi < 35

    # 1. Crossover evaluation
    if not (bullish_cross or bearish_cross):
        return False, {}, f"No 5M EMA 9/21 crossover detected (RSI: {current_rsi:.1f}, 1H Macro Trend: {macro_trend})."

    signal_direction = "BUY" if bullish_cross else "SELL"

    # 2. RSI extreme evaluation
    if not rsi_extreme:
        return False, {}, f"EMA 9/21 cross detected ({signal_direction}), but RSI ({current_rsi:.1f}) is not at an extreme level (>65 or <35)."

    # 3. Multi-timeframe trend confluence evaluation
    if signal_direction == "BUY" and macro_trend != "BULLISH":
        return False, {}, f"5M BUY cross rejected: Conflicts with 1H Bearish Trend (Price {current_price:.2f} < 1H EMA50 {h1_ema50:.2f})."
    elif signal_direction == "SELL" and macro_trend != "BEARISH":
        return False, {}, f"5M SELL cross rejected: Conflicts with 1H Bullish Trend (Price {current_price:.2f} > 1H EMA50 {h1_ema50:.2f})."

    # 4. Active open position evaluation
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM open_calls WHERE direction = ?", (signal_direction,))
    if c.fetchone()[0] > 0:
        return False, {}, f"Valid {signal_direction} setup found, but an active {signal_direction} position is already open."

    context = {
        "ema9": round(curr_ema9, 4),
        "ema21": round(curr_ema21, 4),
        "rsi": round(current_rsi, 2),
        "h1_ema50": round(h1_ema50, 4),
        "macro_trend": macro_trend,
        "last_price": current_price,
        "direction": signal_direction,
    }

    return True, context, "Candidate setup found."

# ---------------------------------------------------------------------------
# LLM VALIDATION (Gemini)
# ---------------------------------------------------------------------------
def get_gemini_prediction(context):
    if not GEMINI_API_KEY:
        print("No Gemini Key found — falling back to stub.")
        return {
            "is_signal": True,
            "direction": context["direction"],
            "entry": context["last_price"],
            "take_profit": round(context["last_price"] * (1.01 if context["direction"] == "BUY" else 0.99), 4),
            "stop_loss": round(context["last_price"] * (0.995 if context["direction"] == "BUY" else 1.005), 4),
            "confidence": "test-stub",
            "reasoning": "Fallback stub signal.",
        }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"

    user_prompt = (
        f"{TRADING_GUIDELINES}\n\n"
        f"Indicator Context:\n"
        f"Signal Direction: {context['direction']}\n"
        f"Macro Trend (1H EMA50): {context['macro_trend']} (EMA50: {context['h1_ema50']})\n"
        f"5M EMA9: {context['ema9']} | 5M EMA21: {context['ema21']}\n"
        f"5M RSI: {context['rsi']} | Price: {context['last_price']}\n"
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
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        result = json.loads(raw_text)
    except Exception as e:
        print("Gemini API Error:", e)
        return None

    if not result.get("is_signal"):
        rejection_msg = f"🔍 **Candidate Rejected by Gemini**\nReason: {result.get('reasoning', 'No specific reason provided.')}"
        send_telegram_message(rejection_msg)
        return None

    return result

# ---------------------------------------------------------------------------
# OUTCOME TRACKING (Wick-Aware)
# ---------------------------------------------------------------------------
def resolve_open_calls(conn, latest_candle):
    c = conn.cursor()
    c.execute("SELECT id, timestamp, direction, entry, take_profit, stop_loss FROM open_calls")
    open_rows = c.fetchall()

    if not open_rows:
        return

    high = float(latest_candle["high"])
    low = float(latest_candle["low"])
    close = float(latest_candle["close"])

    for row in open_rows:
        call_id, timestamp, direction, entry, take_profit, stop_loss = row
        result = None

        if direction == "BUY":
            if high >= take_profit:
                result = "WIN"
            elif low <= stop_loss:
                result = "LOSS"
        elif direction == "SELL":
            if low <= take_profit:
                result = "WIN"
            elif high >= stop_loss:
                result = "LOSS"

        if result:
            resolved_at = datetime.now(timezone.utc).isoformat()
            c.execute(
                """INSERT INTO history (timestamp, direction, entry, exit_price, result, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (timestamp, direction, entry, close, result, resolved_at),
            )
            c.execute("DELETE FROM open_calls WHERE id = ?", (call_id,))
            conn.commit()

            emoji = "✅" if result == "WIN" else "❌"
            send_telegram_message(
                f"{emoji} **Trade Resolved ({result})**\n"
                f"Direction: {direction} @ {entry}\n"
                f"Exit: {close} | TP: {take_profit} | SL: {stop_loss}"
            )

# ---------------------------------------------------------------------------
# SINGLE RUN EXECUTION
# ---------------------------------------------------------------------------
def run_once():
    # 1. Market Hours Guard
    if not is_market_open():
        print("Market is closed for the weekend. Exiting run.")
        return

    conn = init_db()

    # 2. Daily Performance Digest
    send_daily_digest_if_scheduled(conn)

    # 3. Fetch Candles (5m for entries, 1h for macro trend)
    m5_candles = fetch_candles(interval="5min", outputsize=100)
    h1_candles = fetch_candles(interval="1h", outputsize=100)

    if not m5_candles or not h1_candles:
        msg = "⚠️ **Scan Skipped**: Unable to retrieve candle data from Twelve Data."
        send_telegram_message(msg)
        print(msg)
        conn.close()
        return

    # 4. Resolve Open Calls
    resolve_open_calls(conn, m5_candles[-1])

    # 5. Scan for MTF Confluence Candidates
    candidate, context, reason = is_candidate(m5_candles, h1_candles, conn)

    if candidate:
        prediction = get_gemini_prediction(context)
        if prediction is None:
            # Gemini rejected trade — message already sent inside get_gemini_prediction()
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

        msg = (
            f"🚀 **New Signal: {prediction['direction']} XAU/USD**\n"
            f"Entry: `{prediction['entry']}`\n"
            f"TP: `{prediction['take_profit']}` | SL: `{prediction['stop_loss']}`\n"
            f"Confidence: {prediction['confidence']}\n"
            f"Reasoning: {prediction['reasoning']}"
        )
        send_telegram_message(msg)
        print(f"[SIGNAL GENERATED] {msg}")
    else:
        # Send no-trade status message with exact reason
        no_trade_msg = f"⚪ **No Trade Signal**\nReason: {reason}"
        send_telegram_message(no_trade_msg)
        print(f"[NO TRADE] {reason}")

    conn.close()

if __name__ == "__main__":
    run_once()
  
