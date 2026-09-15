import os
import json
import logging
import pandas as pd
import google.generativeai as genai
from typing import Dict, Any

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Initialize Gemini Client
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    logging.warning("GEMINI_API_KEY environment variable not set.")


def construct_market_payload(
    df_5m: pd.DataFrame, 
    df_1h: pd.DataFrame, 
    signal_type: str, 
    metrics: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Formats technical and price action context into a structured dictionary payload.
    """
    # 1. 1H Macro Trend Context
    ema1h_fast = df_1h['ema_fast'].iloc[-1]
    ema1h_slow = df_1h['ema_slow'].iloc[-1]
    macro_trend = "BULLISH" if ema1h_fast > ema1h_slow else "BEARISH"

    # 2. Key Support and Resistance (24-Hour High/Low estimation from 5m data)
    window_candles = min(len(df_5m), 288)  # ~24 hours of 5m candles
    recent_window = df_5m.iloc[-window_candles:]
    resistance_level = round(recent_window['high'].max(), 2)
    support_level = round(recent_window['low'].min(), 2)

    # 3. Micro Structure: Extract recent 6 candles (30 minutes of price action)
    recent_candles = df_5m.tail(6)[['open', 'high', 'low', 'close', 'volume']].to_dict(orient='records')
    # Round candle values for cleaner payload
    for c in recent_candles:
        for k in ['open', 'high', 'low', 'close']:
            c[k] = round(c[k], 2)

    current_price = round(df_5m['close'].iloc[-1], 2)

    payload = {
        "asset": "XAU/USD",
        "signal_candidate": signal_type,
        "current_price": current_price,
        "macro_context": {
            "1h_trend": macro_trend,
            "24h_support": support_level,
            "24h_resistance": resistance_level
        },
        "technical_indicators": {
            "rsi_14": metrics.get("rsi"),
            "adx_14": metrics.get("adx"),
            "atr_14": metrics.get("atr"),
            "proposed_stop_loss": metrics.get("stop_loss"),
            "proposed_take_profit": metrics.get("take_profit")
        },
        "recent_5m_candles": recent_candles
    }

    return payload


def evaluate_trade_with_gemini(
    df_5m: pd.DataFrame, 
    df_1h: pd.DataFrame, 
    signal_type: str, 
    metrics: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Sends the market context payload to Gemini and parses the structured JSON recommendation.
    """
    if not GEMINI_API_KEY:
        return {
            "decision": "REJECT",
            "confidence_score": 0,
            "reasoning": "Gemini API Key missing.",
            "risk_notes": "Execution halted due to unconfigured API key."
        }

    # Build prompt payload
    payload = construct_market_payload(df_5m, df_1h, signal_type, metrics)

    system_prompt = (
        "You are a Senior Risk Manager for an institutional gold (XAU/USD) trading desk. "
        "Your task is to evaluate a technical trade entry candidate based on structural context, key support/resistance levels, "
        "overbought/oversold indicators, and candle price action.\n\n"
        "REJECT trades that execute directly into major support/resistance, show signs of exhaustion, "
        "or conflict with micro candle structure.\n"
        "EXECUTE trades that show strong structural confluence with low risk of immediate trap.\n\n"
        "Strict Rule: You must respond ONLY with a valid JSON object matching this schema:\n"
        "{\n"
        '  "decision": "EXECUTE" | "REJECT",\n'
        '  "confidence_score": <number between 0 and 100>,\n'
        '  "reasoning": "<1-2 concise sentences explaining structural rationale>",\n'
        '  "risk_notes": "<Key risk or proximity warning to watch>"\n'
        "}"
    )

    user_message = f"Market Payload:\n```json\n{json.dumps(payload, indent=2)}\n```"

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        
        response = model.generate_content(
            f"{system_prompt}\n\n{user_message}",
            generation_config={"response_mime_type": "application/json"}
        )

        # Parse JSON output
        result = json.loads(response.text.strip())
        
        # Guard against unexpected structure
        decision = result.get("decision", "REJECT").upper()
        confidence = float(result.get("confidence_score", 0))
        reasoning = result.get("reasoning", "No reasoning provided.")
        risk_notes = result.get("risk_notes", "None.")

        logging.info(f"Gemini evaluation completed: {decision} (Confidence: {confidence}%)")
        
        return {
            "decision": decision,
            "confidence_score": confidence,
            "reasoning": reasoning,
            "risk_notes": risk_notes
        }

    except json.JSONDecodeError:
        logging.error("Failed to parse Gemini response as JSON.")
        return {
            "decision": "REJECT",
            "confidence_score": 0,
            "reasoning": "Gemini response parsing failed.",
            "risk_notes": "Invalid format returned by AI."
        }
    except Exception as e:
        logging.error(f"Error communicating with Gemini API: {str(e)}")
        return {
            "decision": "REJECT",
            "confidence_score": 0,
            "reasoning": f"Gemini API error: {str(e)}",
            "risk_notes": "Exception caught during AI evaluation."
        }
