"""Pre-flight checks. Cheap, deterministic, run before any indicator scoring or AI call."""
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from config import MAX_DATA_AGE_MINUTES

NY = ZoneInfo("America/New_York")


def market_closed_reason(now: Optional[datetime] = None) -> Optional[str]:
    """Why gold is untradeable right now, or None if the market is open.

    Spot gold follows the FX week in New York time, so this is DST-proof:
      - daily break:   16:45 - 18:00 ET (rollover, spreads blow out from 16:45)
      - weekly close:  Friday 16:45 ET -> Sunday 18:00 ET
    """
    now = now or datetime.now(timezone.utc)
    et = now.astimezone(NY)
    wd, t = et.weekday(), et.time()          # Monday = 0 ... Sunday = 6
    if wd == 5:
        return "Weekend: market closed."
    if wd == 6 and t < time(18, 0):
        return "Weekend: market closed until Sunday 18:00 ET."
    if wd == 4 and t >= time(16, 45):
        return "Weekend: market closed since Friday 16:45 ET."
    if time(16, 45) <= t < time(18, 0):
        return "Daily rollover window (16:45-18:00 ET)."
    return None


def check_data_fresh(df_5m: pd.DataFrame, now: Optional[datetime] = None) -> tuple:
    """Stale candles mean a holiday / feed problem. Never trade off old data."""
    now = now or datetime.now(timezone.utc)
    last = df_5m["datetime"].iloc[-1].to_pydatetime()
    age = now - last
    if age > timedelta(minutes=MAX_DATA_AGE_MINUTES):
        return False, f"Latest closed 5m candle is {int(age.total_seconds() // 60)} min old (feed stale or market closed)."
    return True, "Data fresh."


def check_ema_confluence(df_5m: pd.DataFrame, df_1h: pd.DataFrame) -> tuple:
    """BUY if EMA9 > EMA21 on both 5m and 1h; SELL if EMA9 < EMA21 on both."""
    f5, s5 = df_5m["ema_fast"].iloc[-1], df_5m["ema_slow"].iloc[-1]
    f1, s1 = df_1h["ema_fast"].iloc[-1], df_1h["ema_slow"].iloc[-1]
    if f5 > s5 and f1 > s1:
        return True, "BUY"
    if f5 < s5 and f1 < s1:
        return True, "SELL"
    return False, "HOLD (timeframes out of alignment)"


def check_news_spike(df_5m: pd.DataFrame, atr_value: float, threshold_multiplier: float = 3.0) -> tuple:
    """Block if the latest closed 5m candle is more than 3x ATR (news / slippage risk)."""
    last = df_5m.iloc[-1]
    candle_range = last["high"] - last["low"]
    max_safe = atr_value * threshold_multiplier
    if candle_range > max_safe:
        return False, f"Volatile candle spike ({candle_range:.2f} pts vs max safe {max_safe:.2f})."
    return True, "Volatility normal."


def run_pre_execution_filters(df_5m: pd.DataFrame, df_1h: pd.DataFrame, atr_value: float,
                              now: Optional[datetime] = None) -> dict:
    reason = market_closed_reason(now)
    if reason:
        return {"passed": False, "signal": "HOLD", "reason": reason}

    ok, msg = check_data_fresh(df_5m, now)
    if not ok:
        return {"passed": False, "signal": "HOLD", "reason": msg}

    ok, signal_or_msg = check_ema_confluence(df_5m, df_1h)
    if not ok:
        return {"passed": False, "signal": "HOLD", "reason": signal_or_msg}

    ok, msg = check_news_spike(df_5m, atr_value)
    if not ok:
        return {"passed": False, "signal": "HOLD", "reason": msg}

    return {"passed": True, "signal": signal_or_msg, "reason": "All pre-flight filters passed."}
