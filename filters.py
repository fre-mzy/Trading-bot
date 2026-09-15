from datetime import datetime, timezone
import pandas as pd

def is_rollover_window() -> bool:
    """Checks if current UTC time falls within the 21:45 - 23:00 UTC rollover window."""
    now_utc = datetime.now(timezone.utc).time()
    rollover_start = datetime.strptime("21:45", "%H:%M").time()
    rollover_end = datetime.strptime("23:00", "%H:%M").time()
    
    return rollover_start <= now_utc or now_utc <= rollover_end

def check_spread(spread_points: float, max_allowed: float = 0.35) -> tuple[bool, str]:
    """Validates live bid-ask spread against maximum allowed threshold."""
    if spread_points > max_allowed:
        return False, f"Spread ({spread_points:.2f}) exceeds max allowed ({max_allowed:.2f})."
    return True, "Spread within safe limit."

def check_ema_confluence(df_5m: pd.DataFrame, df_1h: pd.DataFrame) -> tuple[bool, str]:
    """
    Verifies direction alignment across 5m and 1h timeframes.
    BUY: 5m EMA(9) > 5m EMA(21) AND 1h EMA(9) > 1h EMA(21)
    SELL: 5m EMA(9) < 5m EMA(21) AND 1h EMA(9) < 1h EMA(21)
    """
    ema5m_fast = df_5m['ema_fast'].iloc[-1]
    ema5m_slow = df_5m['ema_slow'].iloc[-1]
    
    ema1h_fast = df_1h['ema_fast'].iloc[-1]
    ema1h_slow = df_1h['ema_slow'].iloc[-1]

    is_5m_bullish = ema5m_fast > ema5m_slow
    is_5m_bearish = ema5m_fast < ema5m_slow
    
    is_1h_bullish = ema1h_fast > ema1h_slow
    is_1h_bearish = ema1h_fast < ema1h_slow

    if is_5m_bullish and is_1h_bullish:
        return True, "BUY"
    elif is_5m_bearish and is_1h_bearish:
        return True, "SELL"
    
    return False, "HOLD (Timeframes out of alignment)"

def check_news_spike(df_5m: pd.DataFrame, atr_value: float, threshold_multiplier: float = 3.0) -> tuple[bool, str]:
    """
    Detects abnormal news volatility. Blocks trade if the latest 5m candle 
    range is greater than 3x current ATR (slippage risk).
    """
    latest_candle = df_5m.iloc[-1]
    candle_range = latest_candle['high'] - latest_candle['low']
    max_safe_range = atr_value * threshold_multiplier

    if candle_range > max_safe_range:
        return False, f"Volatile candle spike detected ({candle_range:.2f} pts vs max safe {max_safe_range:.2f} pts)."
    
    return True, "Volatility within normal structural range."

def run_pre_execution_filters(
    df_5m: pd.DataFrame, 
    df_1h: pd.DataFrame, 
    spread_points: float, 
    atr_value: float
) -> dict:
    """
    Executes all pre-flight sanity checks before passing candidates to technical indicator scoring.
    """
    # 1. Rollover Check
    if is_rollover_window():
        return {"passed": False, "signal": "HOLD", "reason": "Market rollover window active (21:45 - 23:00 UTC)."}

    # 2. Spread Check
    spread_ok, spread_msg = check_spread(spread_points)
    if not spread_ok:
        return {"passed": False, "signal": "HOLD", "reason": spread_msg}

    # 3. Confluence Check
    confluence_ok, signal_or_msg = check_ema_confluence(df_5m, df_1h)
    if not confluence_ok:
        return {"passed": False, "signal": "HOLD", "reason": signal_or_msg}

    # 4. News Spike Filter
    spike_ok, spike_msg = check_news_spike(df_5m, atr_value)
    if not spike_ok:
        return {"passed": False, "signal": "HOLD", "reason": spike_msg}

    return {
        "passed": True,
        "signal": signal_or_msg, # "BUY" or "SELL"
        "reason": "All pre-flight execution filters passed."
    }
