"""LIVE smoke test (needs TWELVE_DATA_API_KEY and GEMINI_API_KEY). Not part of `pytest`.

Fetches real gold candles and asks Gemini about a made-up candidate, so you can confirm both keys
work before trusting the scheduled pipeline:   python live_check.py
"""
import logging
from datetime import datetime, timezone

from ai_engine import evaluate_trade_with_gemini
from indicators import calculate_adx, calculate_atr, calculate_dynamic_sl_tp, calculate_rsi
from main import add_emas, drop_forming_candle, fetch_market_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")


def main():
    now = datetime.now(timezone.utc)
    df_5m = add_emas(drop_forming_candle(fetch_market_data(interval="5min", outputsize=200), 5, now))
    df_1h = add_emas(drop_forming_candle(fetch_market_data(interval="1h", outputsize=60), 60, now))
    logging.info("Twelve Data OK: latest closed 5m candle %s, close %.2f",
                 df_5m["datetime"].iloc[-1], df_5m["close"].iloc[-1])

    price = float(df_5m["close"].iloc[-1])
    atr = float(calculate_atr(df_5m).iloc[-1])
    signal = "BUY" if df_5m["ema_fast"].iloc[-1] > df_5m["ema_slow"].iloc[-1] else "SELL"
    sl, tp = calculate_dynamic_sl_tp(price, atr, signal)
    metrics = {"rsi": round(float(calculate_rsi(df_5m).iloc[-1]), 2),
               "adx": round(float(calculate_adx(df_5m).iloc[-1]), 2),
               "atr": round(atr, 2), "stop_loss": sl, "take_profit": tp, "entry": round(price, 2)}

    result = evaluate_trade_with_gemini(df_5m, df_1h, signal, metrics)
    print("\n--- GEMINI RESULT (candidate is synthetic, ignore the decision itself) ---")
    for k, v in result.items():
        print(f"{k}: {v}")
    if result["reasoning"].startswith("Gemini API error") or "key missing" in result["reasoning"]:
        raise SystemExit("Gemini call failed, check GEMINI_API_KEY / GEMINI_MODEL.")


if __name__ == "__main__":
    main()
