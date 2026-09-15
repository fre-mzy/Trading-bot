import os
import sqlite3
import logging
import pandas as pd
import requests
from datetime import datetime, timezone

# Import custom pipeline modules
from filters import run_pre_execution_filters
from indicators import evaluate_technical_guardrails, calculate_ema
from ai_engine import evaluate_trade_with_gemini

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Environment configuration
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_PATH = "signals_test.db"

def send_telegram_alert(message: str):
    """Sends notification to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram credentials missing. Skipping notification.")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Failed to send Telegram alert: {e}")

def fetch_market_data(symbol: str = "XAU/USD", interval: str = "5min", outputsize: int = 100) -> pd.DataFrame:
    """Fetches OHLCV candle data from Twelve Data."""
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&outputsize={outputsize}&apikey={TWELVE_DATA_API_KEY}"
    response = requests.get(url, timeout=15)
    data = response.json()

    if "values" not in data:
        raise ValueError(f"Failed to fetch {interval} data: {data.get('message', 'Unknown error')}")

    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])

    # Handle missing volume key from XAU/USD API payload
    if "volume" not in df.columns:
        df["volume"] = 0.0

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df = df.sort_values("datetime").reset_index(drop=True)
    return df


def fetch_live_spread(symbol: str = "XAU/USD") -> float:
    """Fetches live bid/ask spread in points."""
    url = f"https://api.twelvedata.com/quote?symbol={symbol}&apikey={TWELVE_DATA_API_KEY}"
    try:
        res = requests.get(url, timeout=10).json()
        bid = float(res.get("bid", 0))
        ask = float(res.get("ask", 0))
        return round(ask - bid, 2) if (ask and bid) else 0.15
    except Exception:
        return 0.15  # Fallback baseline spread

def log_trade_to_db(signal: str, price: float, sl: float, tp: float, reasoning: str, score: float):
    """Logs executed trade to SQLite."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            signal TEXT,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            ai_score REAL,
            reasoning TEXT
        )
    """)
    cursor.execute("""
        INSERT INTO trades (timestamp, signal, entry_price, stop_loss, take_profit, ai_score, reasoning)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now(timezone.utc).isoformat(), signal, price, sl, tp, score, reasoning))
    conn.commit()
    conn.close()

def run_trading_pipeline():
    logging.info("Starting automated market execution cycle...")

    # 1. Fetch Market Data
    try:
        df_5m = fetch_market_data(interval="5min", outputsize=100)
        df_1h = fetch_market_data(interval="1h", outputsize=50)
        spread = fetch_live_spread()
    except Exception as e:
        logging.error(f"Data fetch error: {e}")
        return

    # Calculate required trend indicators
    df_5m["ema_fast"] = calculate_ema(df_5m, period=9)
    df_5m["ema_slow"] = calculate_ema(df_5m, period=21)
    df_1h["ema_fast"] = calculate_ema(df_1h, period=9)
    df_1h["ema_slow"] = calculate_ema(df_1h, period=21)

    # 2. Run Pre-Flight Filters
    from indicators import calculate_atr
    atr_val = calculate_atr(df_5m).iloc[-1]
    
    pre_check = run_pre_execution_filters(df_5m, df_1h, spread_points=spread, atr_value=atr_val)
    if not pre_check["passed"]:
        logging.info(f"Pipeline Stopped at Pre-Flight: {pre_check['reason']}")
        return

    signal_type = pre_check["signal"]

    # 3. Run Technical Indicator Guardrails (RSI, ADX, Dynamic SL/TP)
    tech_eval = evaluate_technical_guardrails(df_5m, spread_points=spread, signal_type=signal_type)
    if not tech_eval["passed"]:
        logging.info(f"Pipeline Stopped at Technical Guardrails: {tech_eval['reason']}")
        return

    metrics = tech_eval["metrics"]

    # 4. Gemini AI Decision Engine Evaluation
    logging.info("Pre-flight & Technicals passed. Querying Gemini AI Risk Engine...")
    ai_result = evaluate_trade_with_gemini(df_5m, df_1h, signal_type, metrics)

    decision = ai_result["decision"]
    score = ai_result["confidence_score"]
    reasoning = ai_result["reasoning"]
    risk_notes = ai_result["risk_notes"]

    # 5. Execution Gate: Requires AI 'EXECUTE' status and >= 75 Confidence
    if decision == "EXECUTE" and score >= 75:
        current_price = round(df_5m["close"].iloc[-1], 2)
        sl = metrics["stop_loss"]
        tp = metrics["take_profit"]

        # Log to Database
        log_trade_to_db(signal_type, current_price, sl, tp, reasoning, score)

        # Telegram Notification
        msg = (
            f"🚨 *NEW XAU/USD SIGNAL: {signal_type}*\n\n"
            f"📍 *Entry Price:* `${current_price}`\n"
            f"🛡️ *Stop Loss:* `${sl}`\n"
            f"🎯 *Take Profit:* `${tp}`\n\n"
            f"📊 *Metrics:* RSI `{metrics['rsi']}` | ADX `{metrics['adx']}` | ATR `{metrics['atr']}`\n"
            f"🧠 *AI Confidence:* `{score}%`\n"
            f"💬 *AI Rationale:* {reasoning}\n"
            f"⚠️ *Risk Notes:* {risk_notes}"
        )
        send_telegram_alert(msg)
        logging.info(f"TRADE EXECUTED AND NOTIFIED: {signal_type} @ {current_price}")
    else:
        logging.info(f"Trade Rejected by Gemini: {decision} (Score: {score}%). Reason: {reasoning}")

if __name__ == "__main__":
    run_trading_pipeline()
    
