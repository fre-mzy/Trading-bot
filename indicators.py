import numpy as np
import pandas as pd

from config import MAX_SPREAD_FRACTION_OF_SL


def calculate_ema(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    return df[column].ewm(span=period, adjust=False).mean()


def calculate_rsi(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    delta = df[column].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    # loss == 0 with gains -> RSI 100; no movement at all -> neutral 50
    rsi = rsi.where(loss != 0, np.where(gain > 0, 100.0, 50.0))
    return rsi


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = df["high"] - df["high"].shift(1)
    down_move = df["low"].shift(1) - df["low"]
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    atr = calculate_atr(df, period)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def calculate_dynamic_sl_tp(current_price: float, atr_value: float, signal_type: str):
    """SL = 1.5 x ATR, TP = 2.5 x ATR."""
    sl_distance = round(atr_value * 1.5, 2)
    tp_distance = round(atr_value * 2.5, 2)
    if signal_type == "BUY":
        return round(float(current_price - sl_distance), 2), round(float(current_price + tp_distance), 2)
    if signal_type == "SELL":
        return round(float(current_price + sl_distance), 2), round(float(current_price - tp_distance), 2)
    return None, None


def evaluate_technical_guardrails(df_5m: pd.DataFrame, spread_points: float, signal_type: str,
                                  entry_price: float = None) -> dict:
    """ADX regime, RSI exhaustion and spread-vs-stop cost checks; returns metrics + SL/TP.

    Market-hours / rollover live ONLY in filters.py (they used to be duplicated here, and this
    copy had an `or` bug that blocked every trade, forever)."""
    latest_rsi = calculate_rsi(df_5m).iloc[-1]
    latest_adx = calculate_adx(df_5m).iloc[-1]
    latest_atr = calculate_atr(df_5m).iloc[-1]
    price = float(entry_price if entry_price is not None else df_5m["close"].iloc[-1])

    if pd.isna(latest_adx) or pd.isna(latest_atr) or latest_atr <= 0:
        return {"passed": False, "reason": "Execution blocked: not enough data for ADX/ATR."}
    if latest_adx < 20:
        return {"passed": False, "reason": f"Execution blocked: ADX {latest_adx:.2f} < 20 (ranging market)."}
    if signal_type == "BUY" and latest_rsi > 68:
        return {"passed": False, "reason": f"Execution blocked: BUY while RSI overbought ({latest_rsi:.2f})."}
    if signal_type == "SELL" and latest_rsi < 32:
        return {"passed": False, "reason": f"Execution blocked: SELL while RSI oversold ({latest_rsi:.2f})."}

    sl, tp = calculate_dynamic_sl_tp(price, latest_atr, signal_type)
    sl_distance = abs(price - sl)
    if spread_points > MAX_SPREAD_FRACTION_OF_SL * sl_distance:
        return {"passed": False,
                "reason": f"Execution blocked: spread {spread_points:.2f} is too large vs stop distance {sl_distance:.2f}."}

    return {"passed": True, "reason": "Technical guardrails passed.",
            "metrics": {"rsi": round(float(latest_rsi), 2), "adx": round(float(latest_adx), 2),
                        "atr": round(float(latest_atr), 2), "stop_loss": sl, "take_profit": tp,
                        "entry": round(price, 2)}}
