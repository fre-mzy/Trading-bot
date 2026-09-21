"""Tier-2 sanity check: ask Gemini to veto or approve a candidate the rules already found.

Treat the output as a filter, not an oracle: model 'confidence' is not a calibrated probability."""
import json
import logging
import os
from typing import Any, Dict

import pandas as pd

from config import GEMINI_MODEL, SYMBOL

SYSTEM_PROMPT = (
    "You are a risk manager reviewing a gold (XAU/USD) intraday trade candidate produced by a "
    "rules-based system. Judge structural context: proximity to 24h support/resistance, exhaustion, "
    "and whether the last few 5-minute candles contradict the proposed direction.\n"
    "REJECT trades that run directly into major support/resistance, show exhaustion, or conflict "
    "with recent candle structure. EXECUTE trades with clear structural confluence and room to the target.\n"
    "Respond ONLY with JSON: "
    '{"decision": "EXECUTE" | "REJECT", "confidence_score": <0-100>, '
    '"reasoning": "<1-2 short sentences>", "risk_notes": "<key risk to watch>"}'
)


def _reject(reasoning: str, risk_notes: str) -> Dict[str, Any]:
    return {"decision": "REJECT", "confidence_score": 0.0, "reasoning": reasoning, "risk_notes": risk_notes}


def construct_market_payload(df_5m: pd.DataFrame, df_1h: pd.DataFrame, signal_type: str,
                             metrics: Dict[str, Any]) -> Dict[str, Any]:
    macro = "BULLISH" if df_1h["ema_fast"].iloc[-1] > df_1h["ema_slow"].iloc[-1] else "BEARISH"
    window = df_5m.iloc[-min(len(df_5m), 288):]                       # ~24h of 5m candles
    candles = df_5m.tail(6)[["open", "high", "low", "close"]].round(2).to_dict(orient="records")
    return {
        "asset": SYMBOL,
        "signal_candidate": signal_type,
        "current_price": metrics.get("entry", round(float(df_5m["close"].iloc[-1]), 2)),
        "macro_context": {"1h_trend": macro,
                          "24h_support": round(float(window["low"].min()), 2),
                          "24h_resistance": round(float(window["high"].max()), 2)},
        "technical_indicators": {"rsi_14": metrics.get("rsi"), "adx_14": metrics.get("adx"),
                                 "atr_14": metrics.get("atr"),
                                 "proposed_stop_loss": metrics.get("stop_loss"),
                                 "proposed_take_profit": metrics.get("take_profit")},
        "recent_5m_candles": candles,
    }


def evaluate_trade_with_gemini(df_5m, df_1h, signal_type, metrics) -> Dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return _reject("Gemini API key missing.", "Execution halted: GEMINI_API_KEY not set.")

    payload = construct_market_payload(df_5m, df_1h, signal_type, metrics)
    try:
        from google import genai                     # new SDK: `pip install google-genai`
        from google.genai import types

        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=30_000))
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents="Market payload:\n" + json.dumps(payload, indent=2),
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0,
                                               response_mime_type="application/json"),
        )
        result = json.loads((response.text or "").strip())
        if isinstance(result, list) and result:
            result = result[0]
        decision = str(result.get("decision", "REJECT")).upper()
        confidence = max(0.0, min(100.0, float(result.get("confidence_score", 0))))
        logging.info("Gemini: %s (confidence %.0f)", decision, confidence)
        return {"decision": decision, "confidence_score": confidence,
                "reasoning": result.get("reasoning", "No reasoning provided."),
                "risk_notes": result.get("risk_notes", "None.")}
    except json.JSONDecodeError:
        logging.error("Gemini returned non-JSON output.")
        return _reject("Gemini response could not be parsed.", "Invalid format returned by AI.")
    except Exception as e:
        logging.error("Gemini call failed: %s", type(e).__name__)
        return _reject(f"Gemini API error: {type(e).__name__}", "Exception during AI evaluation.")
