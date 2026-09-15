import pandas as pd
import numpy as np
from datetime import datetime, timezone

def calculate_ema(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    """Calculates Exponential Moving Average (EMA)."""
    return df[column].ewm(span=period, adjust=False).mean()

def calculate_rsi(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    """Calculates Relative Strength Index (RSI)."""
    delta = df[column].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculates Average True Range (ATR)."""
    high = df['high']
    low = df['low']
    close = df['close'].shift(1)
    
    tr1 = high - low
    tr2 = (high - close).abs()
    tr3 = (low - close).abs()
    
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(alpha=1/period, adjust=False).mean()

def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculates Average Directional Index (ADX) for regime detection."""
    df = df.copy()
    df['up_move'] = df['high'] - df['high'].shift(1)
    df['down_move'] = df['low'].shift(1) - df['low']
    
    df['plus_dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0)
    df['minus_dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0)
    
    tr = calculate_atr(df, period)
    plus_di = 100 * (pd.Series(df['plus_dm']).ewm(alpha=1/period, adjust=False).mean() / tr)
    minus_di = 100 * (pd.Series(df['minus_dm']).ewm(alpha=1/period, adjust=False).mean() / tr)
    
    dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di))
    return dx.ewm(alpha=1/period, adjust=False).mean()

def calculate_dynamic_sl_tp(current_price: float, atr_value: float, signal_type: str):
    """
    Computes ATR-based Stop-Loss and Take-Profit values.
    SL = 1.5 * ATR | TP = 2.5 * ATR
    """
    sl_distance = round(atr_value * 1.5, 2)
    tp_distance = round(atr_value * 2.5, 2)
    
    if signal_type == "BUY":
        stop_loss = round(current_price - sl_distance, 2)
        take_profit = round(current_price + tp_distance, 2)
    elif signal_type == "SELL":
        stop_loss = round(current_price + sl_distance, 2)
        take_profit = round(current_price - tp_distance, 2)
    else:
        return None, None
        
    return stop_loss, take_profit

def evaluate_technical_guardrails(df_5m: pd.DataFrame, spread_points: float, signal_type: str) -> dict:
    """
    Evaluates 5-minute technical indicators against hard guardrails.
    Returns pass/fail status and metric context.
    """
    # 1. Rollover Noise Guardout (21:45 - 23:00 UTC)
    now_utc = datetime.now(timezone.utc).time()
    rollover_start = datetime.strptime("21:45", "%H:%M").time()
    rollover_end = datetime.strptime("23:00", "%H:%M").time()
    
    if rollover_start <= now_utc or now_utc <= rollover_end:
        return {"passed": False, "reason": "Execution blocked: Market rollover window (21:45-23:00 UTC)."}

    # 2. Maximum Spread Guardrail
    MAX_SPREAD = 0.35  # $0.35 max spread for XAU/USD
    if spread_points > MAX_SPREAD:
        return {"passed": False, "reason": f"Execution blocked: Spread ({spread_points}) exceeds max threshold ({MAX_SPREAD})."}

    # Calculate indicators on the latest candle
    rsi_series = calculate_rsi(df_5m)
    adx_series = calculate_adx(df_5m)
    atr_series = calculate_atr(df_5m)

    latest_rsi = rsi_series.iloc[-1]
    latest_adx = adx_series.iloc[-1]
    latest_atr = atr_series.iloc[-1]
    current_price = df_5m['close'].iloc[-1]

    # 3. ADX Regime Guardrail (Block trade if market is ranging)
    if latest_adx < 20:
        return {"passed": False, "reason": f"Execution blocked: ADX is {latest_adx:.2f} (< 20). Market is ranging."}

    # 4. RSI Overbought/Oversold Guardrail
    if signal_type == "BUY" and latest_rsi > 68:
        return {"passed": False, "reason": f"Execution blocked: BUY signal generated while RSI is overbought ({latest_rsi:.2f})."}
    if signal_type == "SELL" and latest_rsi < 32:
        return {"passed": False, "reason": f"Execution blocked: SELL signal generated while RSI is oversold ({latest_rsi:.2f})."}

    # Calculate dynamic SL/TP targets
    sl, tp = calculate_dynamic_sl_tp(current_price, latest_atr, signal_type)

    return {
        "passed": True,
        "reason": "Technical guardrails passed.",
        "metrics": {
            "rsi": round(latest_rsi, 2),
            "adx": round(latest_adx, 2),
            "atr": round(latest_atr, 2),
            "stop_loss": sl,
            "take_profit": tp
        }
    }
