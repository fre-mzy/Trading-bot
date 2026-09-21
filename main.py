"""XAU/USD paper-trading signal pipeline. One run = one cycle (GitHub Actions runs it every 5 min).

Cycle:  answer Telegram commands -> skip if market closed -> fetch candles -> settle open paper
trades (TP/SL) -> if flat: rules filters -> technical guardrails -> Gemini veto -> log + alert.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

import db
from ai_engine import evaluate_trade_with_gemini
from bot_commands import process_pending_commands
from config import (ASSUMED_SPREAD, COOLDOWN_MINUTES, MAX_HOLD_CANDLES, MIN_CONFIDENCE, RISK_PCT,
                    SYMBOL)
from filters import market_closed_reason, run_pre_execution_filters
from indicators import calculate_atr, calculate_ema, evaluate_technical_guardrails
from notify import send_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

INTERVAL_MINUTES = {"5min": 5, "1h": 60}


# ----------------------------------------------------------------------------- data
def fetch_market_data(symbol: str = SYMBOL, interval: str = "5min", outputsize: int = 100) -> pd.DataFrame:
    """OHLC candles from Twelve Data, oldest first, timestamps in UTC. The newest row may still be forming."""
    api_key = os.getenv("TWELVE_DATA_API_KEY")
    if not api_key:
        raise ValueError("TWELVE_DATA_API_KEY is not set.")
    resp = requests.get("https://api.twelvedata.com/time_series",
                        params={"symbol": symbol, "interval": interval, "outputsize": outputsize,
                                "timezone": "UTC", "apikey": api_key}, timeout=15)
    data = resp.json()
    if "values" not in data:
        raise ValueError(f"Failed to fetch {interval} data: {data.get('message', 'Unknown error')}")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df[["datetime", "open", "high", "low", "close"]].sort_values("datetime").reset_index(drop=True)


def drop_forming_candle(df: pd.DataFrame, minutes: int, now: datetime) -> pd.DataFrame:
    """Keep only fully closed candles so indicators don't change after the alert fires."""
    closed = df[df["datetime"] + pd.Timedelta(minutes=minutes) <= pd.Timestamp(now)]
    return closed.reset_index(drop=True)


def add_emas(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = calculate_ema(df, period=9)
    df["ema_slow"] = calculate_ema(df, period=21)
    return df


# ----------------------------------------------------------------------------- settle trades
def resolve_open_calls(df_5m_full: pd.DataFrame, now: datetime, send=None) -> list:
    """Check open paper trades against candles that happened AFTER the signal.

    If a single candle touched both TP and SL we can't know which came first, so it counts as a
    LOSS (pessimistic on purpose). Costs: the assumed spread widens the stop and shrinks the target."""
    send = send or send_telegram
    resolved = []
    for call in db.get_open_calls():
        start = pd.Timestamp(call.get("candle_time") or call["timestamp"])
        after = df_5m_full[df_5m_full["datetime"] >= start]
        buy = call["direction"] == "BUY"
        entry, tp, sl = call["entry"], call["take_profit"], call["stop_loss"]
        units, risk = call["units"], call["risk_usd"]
        spread = call.get("spread") if call.get("spread") is not None else ASSUMED_SPREAD

        result = exit_price = pnl = None
        for row in after.itertuples():
            hit_sl = row.low <= sl if buy else row.high >= sl
            hit_tp = row.high >= tp if buy else row.low <= tp
            if hit_sl:                                   # includes the both-touched case
                result, exit_price, pnl = "LOSS", sl, -risk
                break
            if hit_tp:
                result, exit_price, pnl = "WIN", tp, units * (abs(tp - entry) - spread)
                break
        if result is None and len(after) >= MAX_HOLD_CANDLES:   # time stop
            exit_price = float(after["close"].iloc[-1])
            move = (exit_price - entry) if buy else (entry - exit_price)
            pnl = units * (move - spread)
            result = "WIN" if pnl > 0 else "LOSS"
        if result is None:
            continue

        balance = db.close_call(call, result, exit_price, pnl, resolved_at=now.isoformat())
        resolved.append((call, result, pnl))
        icon = "✅" if result == "WIN" else "❌"
        send(f"{icon} {result}: {call['direction']} XAU/USD closed at {exit_price:.2f}\n"
             f"Entry {entry} | PnL ${pnl:+,.2f} | Balance ${balance:,.2f}")
        logging.info("Resolved %s %s -> %s (%.2f)", call["direction"], entry, result, pnl)
    return resolved


# ----------------------------------------------------------------------------- pipeline
def run_trading_pipeline(now: Optional[datetime] = None) -> str:
    """Runs one cycle. Returns a short status string (handy for tests and logs)."""
    now = now or datetime.now(timezone.utc)
    logging.info("Starting cycle...")
    db.init_db()

    try:
        process_pending_commands()
    except Exception as e:                                # commands must never block trading
        logging.warning("Command processing failed: %s", type(e).__name__)

    closed = market_closed_reason(now)
    if closed:
        logging.info("Skipped: %s", closed)
        return "market_closed"

    # 1. data (2 API calls per cycle; the free Twelve Data plan allows 800/day)
    try:
        df_5m_full = fetch_market_data(interval="5min", outputsize=500)
        df_1h_full = fetch_market_data(interval="1h", outputsize=60)
    except Exception as e:
        logging.error("Data fetch error: %s", e)
        return "data_error"

    # 2. settle open paper trades first (uses every candle, including the still-forming one)
    resolve_open_calls(df_5m_full, now)

    # 3. one position at a time + cooldown
    if db.get_open_calls():
        logging.info("Skipped: a paper trade is still open.")
        return "position_open"
    last = db.last_signal_time()
    if last and (now - last).total_seconds() < COOLDOWN_MINUTES * 60:
        logging.info("Skipped: cooldown (%d min) active.", COOLDOWN_MINUTES)
        return "cooldown"

    # 4. signals use CLOSED candles only
    df_5m = add_emas(drop_forming_candle(df_5m_full, 5, now))
    df_1h = add_emas(drop_forming_candle(df_1h_full, 60, now))
    if len(df_5m) < 50 or len(df_1h) < 30:
        logging.warning("Not enough candles to evaluate.")
        return "not_enough_data"

    atr_val = float(calculate_atr(df_5m).iloc[-1])
    pre = run_pre_execution_filters(df_5m, df_1h, atr_value=atr_val, now=now)
    if not pre["passed"]:
        logging.info("Stopped at pre-flight: %s", pre["reason"])
        return "preflight_blocked"
    signal = pre["signal"]

    entry_price = round(float(df_5m_full["close"].iloc[-1]), 2)     # latest traded price
    tech = evaluate_technical_guardrails(df_5m, spread_points=ASSUMED_SPREAD, signal_type=signal,
                                         entry_price=entry_price)
    if not tech["passed"]:
        logging.info("Stopped at technical guardrails: %s", tech["reason"])
        return "guardrails_blocked"
    m = tech["metrics"]

    # 5. Gemini veto
    logging.info("Rules passed (%s). Asking Gemini...", signal)
    ai = evaluate_trade_with_gemini(df_5m, df_1h, signal, m)
    if not (ai["decision"] == "EXECUTE" and ai["confidence_score"] >= MIN_CONFIDENCE):
        logging.info("Gemini rejected: %s (%.0f%%). %s", ai["decision"], ai["confidence_score"], ai["reasoning"])
        return "ai_rejected"

    # 6. open the paper trade (risk a fixed % of balance; size so a stop-out costs exactly that)
    balance = db.get_balance()
    risk_usd = round(balance * RISK_PCT / 100, 2)
    sl_dist = abs(entry_price - m["stop_loss"])
    units = round(risk_usd / (sl_dist + ASSUMED_SPREAD), 4)
    first_eligible = pd.Timestamp(now).ceil("5min").isoformat()      # skip the candle the signal fired in
    db.add_open_call(signal, entry_price, m["take_profit"], m["stop_loss"], units, risk_usd,
                     ai["confidence_score"], ai["reasoning"], first_eligible, ASSUMED_SPREAD,
                     timestamp=now.isoformat())

    send_telegram(
        f"🚨 PAPER SIGNAL {signal} XAU/USD\n\n"
        f"Entry: {entry_price}\nStop loss: {m['stop_loss']}\nTake profit: {m['take_profit']}\n"
        f"Risk: ${risk_usd:,.2f} ({RISK_PCT:g}%) | Size: {units} oz\n\n"
        f"RSI {m['rsi']} | ADX {m['adx']} | ATR {m['atr']}\n"
        f"AI confidence: {ai['confidence_score']:.0f}%\nWhy: {ai['reasoning']}\nRisk: {ai['risk_notes']}\n\n"
        f"(Assumes ${ASSUMED_SPREAD:.2f} spread. Paper trade, not financial advice.)")
    logging.info("SIGNAL OPENED: %s @ %s", signal, entry_price)
    return "signal_opened"


if __name__ == "__main__":
    run_trading_pipeline()
