"""
Layer 1 & Layer 2 — Gold Trading Engine & Interactive Bot

Features:
- Market Hours Guard (prevents unnecessary runs during weekend closures)
- Multi-Timeframe Confluence (5m EMA 9/21 cross aligned with 1h EMA50 trend)
- Detailed Rejection Reporting (sends clear skip reasons to Telegram)
- Gemini LLM Risk Validation (evaluates candle structure & entry setup)
- Paper Trading Engine ($10,000 baseline account tracking real PnL)
- Interactive Telegram Commands (/status, /open, /stats)
"""

import os
import sys
import json
import sqlite3
import requests
from datetime import datetime, timezone

# Optional Telegram bot library for interactive command handling
try:
    from telebot import TeleBot
except ImportError:
    TeleBot = None

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOL = "XAU/USD"
DB_PATH = "signals_test.db"
PAPER_STARTING_BALANCE = 10000.0  # $10,000 starting account balance
RISK_PER_TRADE_PCT = 0.01         # 1% risk per trade

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
# 2. DATABASE & PAPER TRADING SETUP
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
            units REAL,
            risk_usd REAL,
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
            pnl_usd REAL,
            result TEXT,
            resolved_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            balance REAL,
            equity REAL
        )
    """)
    c.execute(
        "INSERT OR IGNORE INTO account (id, balance, equity) VALUES (1, ?, ?)",
        (PAPER_STARTING_BALANCE, PAPER_STARTING_BALANCE),
    )
    conn.commit()
    return conn

# ---------------------------------------------------------------------------
# 3. NOTIFICATIONS & DAILY DIGEST
# ---------------------------------------------------------------------------
def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram configuration missing — standard console output:")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, data=payload, timeout=10)
        if not resp.ok:
            print("Telegram dispatch failed:", resp.text)
    except requests.RequestException as e:
        print("Telegram request error:", e)

def send_daily_digest_if_scheduled(conn):
    """Sends a 24-hour performance digest around 22:00 UTC daily."""
    now = datetime.now(timezone.utc)
    if now.hour == 22 and now.minute < 15:
        c = conn.cursor()
        c.execute("""
            SELECT result, SUM(pnl_usd), COUNT(*) 
            FROM history 
            WHERE resolved_at >= datetime('now', '-1 day')
            GROUP BY result
        """)
        stats = c.fetchall()

        c.execute("SELECT balance FROM account WHERE id = 1")
        balance = c.fetchone()[0]

        wins, losses = 0, 0
        pnl_24h = 0.0

        for row in stats:
            res, pnl, count = row
            if res == "WIN":
                wins = count
                pnl_24h += (pnl or 0.0)
            elif res == "LOSS":
                losses = count
                pnl_24h += (pnl or 0.0)

        total = wins + losses
        win_rate = (wins / total * 100) if total > 0 else 0.0

        digest = f"""📊 **Daily Performance Digest (24h)**
Trades Resolved: {total}
Wins: {wins} ✅ | Losses: {losses} ❌
Win Rate: {win_rate:.1f}%
24h PnL: **${pnl_24h:+.2f}**
Account Balance: **${balance:,.2f}**"""
        send_telegram_message(digest)

# ---------------------------------------------------------------------------
# 4. DATA FETCH (Twelve Data)
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
# 5. TECHNICAL INDICATORS
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
# 6. SCANNER WITH REASONING
# ---------------------------------------------------------------------------
def is_candidate(m5_candles, h1_candles, conn):
    if len(m5_candles) < 22 or len(h1_candles) < 50:
        return False, {}, "Insufficient historical candle data."

    # 1-Hour Trend Filter
    h1_closes = [float(v["close"]) for v in h1_candles]
    h1_ema50 = ema(h1_closes, 50)
    current_price = float(m5_candles[-1]["close"])
    macro_trend = "BULLISH" if current_price > h1_ema50 else "BEARISH"

    # 5-Min Entry Signal
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

    # Evaluation Step 1: Check for EMA Crossover
    if not (bullish_cross or bearish_cross):
        return False, {}, f"No 5M EMA 9/21 crossover detected (RSI: {current_rsi:.1f}, 1H Trend: {macro_trend})."

    signal_direction = "BUY" if bullish_cross else "SELL"

    # Evaluation Step 2: Check for RSI Extreme Condition
    if not rsi_extreme:
        return False, {}, f"EMA 9/21 cross detected ({signal_direction}), but RSI ({current_rsi:.1f}) is not at extreme (>65 or <35)."

    # Evaluation Step 3: Multi-Timeframe Trend Confluence
    if signal_direction == "BUY" and macro_trend != "BULLISH":
        return False, {}, f"5M BUY cross rejected: Conflicts with 1H Bearish Trend (Price {current_price:.2f} < 1H EMA50 {h1_ema50:.2f})."
    elif signal_direction == "SELL" and macro_trend != "BEARISH":
        return False, {}, f"5M SELL cross rejected: Conflicts with 1H Bullish Trend (Price {current_price:.2f} > 1H EMA50 {h1_ema50:.2f})."

    # Evaluation Step 4: Check Active Positions
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
# 7. LLM VALIDATION (Gemini)
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

    user_prompt = f"""{TRADING_GUIDELINES}

Indicator Context:
Signal Direction: {context['direction']}
Macro Trend (1H EMA50): {context['macro_trend']} (EMA50: {context['h1_ema50']})
5M EMA9: {context['ema9']} | 5M EMA21: {context['ema21']}
5M RSI: {context['rsi']} | Price: {context['last_price']}
"""

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
        rejection_msg = f"🔍 **Candidate Rejected by Gemini**\nReason: {result.get('reasoning', 'No reason specified.')}"
        send_telegram_message(rejection_msg)
        return None

    return result

# ---------------------------------------------------------------------------
# 8. PAPER TRADING OUTCOME TRACKER
# ---------------------------------------------------------------------------
def resolve_open_calls(conn, latest_candle):
    c = conn.cursor()
    c.execute("SELECT id, timestamp, direction, entry, take_profit, stop_loss, units, risk_usd FROM open_calls")
    open_rows = c.fetchall()

    if not open_rows:
        return

    high = float(latest_candle["high"])
    low = float(latest_candle["low"])
    close = float(latest_candle["close"])

    c.execute("SELECT balance FROM account WHERE id = 1")
    balance = c.fetchone()[0]

    for row in open_rows:
        call_id, timestamp, direction, entry, take_profit, stop_loss, units, risk_usd = row
        result = None
        pnl_usd = 0.0

        if direction == "BUY":
            if high >= take_profit:
                result = "WIN"
                pnl_usd = abs(take_profit - entry) * units
            elif low <= stop_loss:
                result = "LOSS"
                pnl_usd = -abs(entry - stop_loss) * units
        elif direction == "SELL":
            if low <= take_profit:
                result = "WIN"
                pnl_usd = abs(entry - take_profit) * units
            elif high >= stop_loss:
                result = "LOSS"
                pnl_usd = -abs(stop_loss - entry) * units

        if result:
            new_balance = balance + pnl_usd
            resolved_at = datetime.now(timezone.utc).isoformat()

            c.execute(
                """INSERT INTO history (timestamp, direction, entry, exit_price, pnl_usd, result, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, direction, entry, close, pnl_usd, result, resolved_at),
            )
            c.execute("UPDATE account SET balance = ?, equity = ? WHERE id = 1", (new_balance, new_balance))
            c.execute("DELETE FROM open_calls WHERE id = ?", (call_id,))
            conn.commit()

            emoji = "✅" if result == "WIN" else "❌"
            msg = f"""{emoji} **Paper Trade Resolved ({result})**
Direction: {direction} @ {entry:.2f}
Exit Price: {close:.2f} | PnL: **${pnl_usd:+.2f}**
Updated Balance: **${new_balance:,.2f}**"""
            send_telegram_message(msg)

# ---------------------------------------------------------------------------
# 9. SINGLE EXECUTION RUN
# ---------------------------------------------------------------------------
def run_once():
    if not is_market_open():
        print("Market is closed for the weekend. Skipping execution.")
        return

    conn = init_db()
    send_daily_digest_if_scheduled(conn)

    m5_candles = fetch_candles(interval="5min", outputsize=100)
    h1_candles = fetch_candles(interval="1h", outputsize=100)

    if not m5_candles or not h1_candles:
        msg = "⚠️ **Scan Skipped**: Unable to retrieve candle data from Twelve Data."
        send_telegram_message(msg)
        print(msg)
        conn.close()
        return

    resolve_open_calls(conn, m5_candles[-1])

    candidate, context, reason = is_candidate(m5_candles, h1_candles, conn)

    if candidate:
        prediction = get_gemini_prediction(context)
        if prediction is None:
            conn.close()
            return

        c = conn.cursor()
        c.execute("SELECT balance FROM account WHERE id = 1")
        balance = c.fetchone()[0]

        # Lot sizing based on 1% account risk
        entry = float(prediction["entry"])
        stop_loss = float(prediction["stop_loss"])
        sl_distance = abs(entry - stop_loss)
        
        risk_usd = balance * RISK_PER_TRADE_PCT
        units = round(risk_usd / sl_distance, 2) if sl_distance > 0 else 1.0

        c.execute(
            """INSERT INTO open_calls
               (timestamp, direction, entry, take_profit, stop_loss, units, risk_usd, confidence, reasoning)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                prediction["direction"],
                entry,
                prediction["take_profit"],
                stop_loss,
                units,
                risk_usd,
                prediction["confidence"],
                prediction["reasoning"],
            ),
        )
        conn.commit()

        msg = f"""🚀 **New Signal: {prediction['direction']} XAU/USD**
Entry: `{entry:.2f}`
TP: `{prediction['take_profit']:.2f}` | SL: `{stop_loss:.2f}`
Position Size: `{units}` units (Risking ${risk_usd:.2f})
Confidence: {prediction['confidence']}
Reasoning: {prediction['reasoning']}"""
        send_telegram_message(msg)
        print(f"[SIGNAL GENERATED] {msg}")
    else:
        no_trade_msg = f"⚪ **No Trade Signal**\nReason: {reason}"
        send_telegram_message(no_trade_msg)
        print(f"[NO TRADE] {reason}")

    conn.close()

# ---------------------------------------------------------------------------
# 10. INTERACTIVE BOT COMMAND HANDLER
# ---------------------------------------------------------------------------
def start_command_bot():
    if not TELEGRAM_BOT_TOKEN or not TeleBot:
        print("pyTelegramBotAPI or TELEGRAM_BOT_TOKEN not available.")
        return

    bot = TeleBot(TELEGRAM_BOT_TOKEN)

    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        help_text = f"""🤖 **Gold Trading Bot Commands:**

/status - View account balance & open positions count
/open - View currently active open trades
/stats - View all-time performance metrics"""
        bot.reply_to(message, help_text, parse_mode="Markdown")

    @bot.message_handler(commands=['status'])
    def handle_status(message):
        if not os.path.exists(DB_PATH):
            bot.reply_to(message, "⚠️ Database file not found.")
            return

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT balance FROM account WHERE id = 1")
        acc = c.fetchone()
        balance = acc[0] if acc else PAPER_STARTING_BALANCE

        c.execute("SELECT COUNT(*) FROM open_calls")
        open_count = c.fetchone()[0]
        conn.close()

        status_msg = f"""💳 **Account Overview (Paper Trading)**
Balance: **${balance:,.2f}**
Active Open Positions: **{open_count}**"""
        bot.reply_to(message, status_msg, parse_mode="Markdown")

    @bot.message_handler(commands=['open'])
    def handle_open(message):
        if not os.path.exists(DB_PATH):
            bot.reply_to(message, "⚠️ Database file not found.")
            return

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT direction, entry, take_profit, stop_loss, units, timestamp FROM open_calls")
        rows = c.fetchall()
        conn.close()

        if not rows:
            bot.reply_to(message, "🟢 No active open trades right now.")
            return

        response = "📊 **Active Open Trades:**\n\n"
        for r in rows:
            response += f"""• **{r[0]} XAU/USD**
  Entry: `{r[1]:.2f}` | TP: `{r[2]:.2f}` | SL: `{r[3]:.2f}`
  Units: `{r[4]}` | Opened: {r[5][:16]}

"""
        bot.reply_to(message, response, parse_mode="Markdown")

    @bot.message_handler(commands=['stats'])
    def handle_stats(message):
        if not os.path.exists(DB_PATH):
            bot.reply_to(message, "⚠️ Database file not found.")
            return

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT result, SUM(pnl_usd), COUNT(*) FROM history GROUP BY result")
        stats = c.fetchall()

        c.execute("SELECT balance FROM account WHERE id = 1")
        balance = c.fetchone()[0]
        conn.close()

        wins, losses = 0, 0
        total_pnl = 0.0

        for row in stats:
            res, pnl, count = row
            if res == "WIN":
                wins = count
                total_pnl += (pnl or 0.0)
            elif res == "LOSS":
                losses = count
                total_pnl += (pnl or 0.0)

        total_trades = wins + losses
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

        stats_msg = f"""📈 **All-Time Performance Stats**
Total Resolved Trades: {total_trades}
Wins: {wins} ✅ | Losses: {losses} ❌
Win Rate: **{win_rate:.1f}%**
Total PnL: **${total_pnl:+.2f}**
Current Balance: **${balance:,.2f}**"""

        bot.reply_to(message, stats_msg, parse_mode="Markdown")

    print("Interactive Telegram Bot started listening...")
    bot.infinity_polling()

# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # If launched with "--bot", start the interactive command handler.
    # Otherwise, execute a standard single scheduled scan.
    if len(sys.argv) > 1 and sys.argv[1] == "--bot":
        start_command_bot()
    else:
        run_once()
