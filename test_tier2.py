import logging
import pandas as pd
from ai_engine import evaluate_trade_with_gemini
from main import fetch_market_data, fetch_live_spread
from indicators import calculate_ema, calculate_rsi, calculate_adx, calculate_atr, calculate_dynamic_sl_tp

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

def test_tier2_standalone():
    logging.info("Fetching market data for Tier 2 evaluation test...")
    
    # 1. Fetch live market context
    df_5m = fetch_market_data(interval="5min", outputsize=100)
    df_1h = fetch_market_data(interval="1h", outputsize=50)
    
    # Add EMAs
    df_5m["ema_fast"] = calculate_ema(df_5m, 9)
    df_5m["ema_slow"] = calculate_ema(df_5m, 21)
    df_1h["ema_fast"] = calculate_ema(df_1h, 9)
    df_1h["ema_slow"] = calculate_ema(df_1h, 21)

    # Calculate metrics
    rsi = calculate_rsi(df_5m).iloc[-1]
    adx = calculate_adx(df_5m).iloc[-1]
    atr = calculate_atr(df_5m).iloc[-1]
    current_price = df_5m['close'].iloc[-1]

    # Simulate a candidate signal direction based on 5m EMA cross
    signal_candidate = "BUY" if df_5m["ema_fast"].iloc[-1] > df_5m["ema_slow"].iloc[-1] else "SELL"
    sl, tp = calculate_dynamic_sl_tp(current_price, atr, signal_candidate)

    metrics = {
        "rsi": round(rsi, 2),
        "adx": round(adx, 2),
        "atr": round(atr, 2),
        "stop_loss": sl,
        "take_profit": tp
    }

    logging.info(f"Simulating Candidate Signal: {signal_candidate} at ${current_price:.2f}")
    logging.info(f"Metrics: RSI={metrics['rsi']}, ADX={metrics['adx']}, ATR={metrics['atr']}")

    # 2. Query Tier 2 Gemini Risk Engine
    result = evaluate_trade_with_gemini(df_5m, df_1h, signal_candidate, metrics)

    # 3. Print Evaluation Output
    print("\n--- TIER 2 GEMINI EVALUATION RESULT ---")
    print(f"Decision:         {result['decision']}")
    print(f"Confidence Score: {result['confidence_score']}%")
    print(f"Reasoning:        {result['reasoning']}")
    print(f"Risk Notes:       {result['risk_notes']}")
    print("---------------------------------------\n")

if __name__ == "__main__":
    test_tier2_standalone()
