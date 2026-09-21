"""Offline tests: no network, no API keys. Run with `pytest -q`."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import bot_commands
import db
import main
from filters import market_closed_reason
from indicators import calculate_atr, evaluate_technical_guardrails

UTC = timezone.utc
NOW = datetime(2026, 9, 16, 14, 7, tzinfo=UTC)          # Wednesday 10:07 New York: market open


# ----------------------------------------------------------------------------- helpers
def make_df(n, minutes, last_start, seed, drift, wave_amp, noise, wave_len, base=2000.0):
    rng = np.random.default_rng(seed)
    i = np.arange(n)
    close = base + drift * i + wave_amp * np.sin(i / wave_len * 2 * np.pi) + np.cumsum(rng.normal(0, noise, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + np.abs(rng.normal(0, noise * 0.6, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, noise * 0.6, n))
    times = [last_start - timedelta(minutes=minutes * (n - 1 - k)) for k in range(n)]
    return pd.DataFrame({"datetime": pd.to_datetime(times, utc=True), "open": open_,
                         "high": high, "low": low, "close": close})


def trending_data():
    """Seed 1 gives a clean uptrend on both timeframes that passes every rule."""
    d5 = make_df(500, 5, datetime(2026, 9, 16, 14, 5, tzinfo=UTC), 1, 0.04, 2.0, 0.5, 40)
    d1 = make_df(60, 60, datetime(2026, 9, 16, 14, 0, tzinfo=UTC), 1, 0.7, 4, 1.5, 12)
    return d5, d1


def candle_rows(rows):
    return pd.DataFrame(
        [{"datetime": pd.Timestamp(t, tz="UTC"), "open": o, "high": h, "low": l, "close": c}
         for t, o, h, l, c in rows])


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    return db


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(main, "send_telegram", lambda text, chat_id=None: messages.append(text) or True)
    return messages


def open_buy(entry=2000.0, sl=1997.0, tp=2004.0, candle_time="2026-09-16T14:10:00+00:00"):
    risk = 100.0
    units = risk / (abs(entry - sl) + 0.30)
    return db.add_open_call("BUY", entry, tp, sl, units, risk, 80, "test", candle_time, 0.30,
                            timestamp=NOW.isoformat())


# ----------------------------------------------------------------------------- market hours
@pytest.mark.parametrize("when,closed", [
    (datetime(2026, 9, 16, 14, 0, tzinfo=UTC), False),    # Wed midday
    (datetime(2026, 9, 16, 21, 30, tzinfo=UTC), True),    # Wed 17:30 ET (summer) -> rollover
    (datetime(2026, 9, 16, 22, 30, tzinfo=UTC), False),   # Wed 18:30 ET -> back open
    (datetime(2026, 12, 16, 22, 30, tzinfo=UTC), True),   # winter Wed 17:30 ET -> rollover (DST-proof)
    (datetime(2026, 9, 18, 21, 30, tzinfo=UTC), True),    # Friday after close
    (datetime(2026, 9, 19, 12, 0, tzinfo=UTC), True),     # Saturday
    (datetime(2026, 9, 20, 21, 0, tzinfo=UTC), True),     # Sunday 17:00 ET, not open yet
    (datetime(2026, 9, 20, 22, 30, tzinfo=UTC), False),   # Sunday 18:30 ET, open
])
def test_market_hours(when, closed):
    assert (market_closed_reason(when) is not None) == closed


# ----------------------------------------------------------------------------- the original bug
def test_guardrails_no_longer_block_everything():
    """The old rollover check used `or`, which is true at every hour, so nothing could ever pass."""
    d5, _ = trending_data()
    closed = main.drop_forming_candle(d5, 5, NOW)
    res = evaluate_technical_guardrails(closed, spread_points=0.30, signal_type="BUY")
    assert res["passed"], res["reason"]
    assert res["metrics"]["stop_loss"] < res["metrics"]["entry"] < res["metrics"]["take_profit"]


def test_guardrails_block_ranging_market():
    rng = np.random.default_rng(0)
    df = make_df(200, 5, datetime(2026, 9, 16, 14, 0, tzinfo=UTC), 0, 0.0, 0.0, 0.05, 40)
    df["close"] = 2000 + np.sin(np.arange(200) / 3) * 0.5
    df["open"], df["high"], df["low"] = df["close"], df["close"] + 0.2, df["close"] - 0.2
    res = evaluate_technical_guardrails(df, 0.30, "BUY")
    assert not res["passed"]


# ----------------------------------------------------------------------------- candles
def test_forming_candle_is_dropped():
    d5, _ = trending_data()
    closed = main.drop_forming_candle(d5, 5, NOW)
    assert closed["datetime"].iloc[-1] == pd.Timestamp("2026-09-16T14:00:00Z")
    assert len(closed) == len(d5) - 1


# ----------------------------------------------------------------------------- settling trades
def test_take_profit_wins_and_updates_balance(tmp_db, sent):
    open_buy()
    df = candle_rows([("2026-09-16T14:10:00Z", 2000, 2004.5, 1999, 2003)])
    main.resolve_open_calls(df, NOW)
    s = db.get_stats()
    assert (s["wins"], s["losses"]) == (1, 0)
    assert db.get_balance() > 10000 and db.get_open_calls() == []
    assert "WIN" in sent[0]


def test_stop_loss_loses_exactly_the_risk(tmp_db, sent):
    open_buy()
    df = candle_rows([("2026-09-16T14:10:00Z", 2000, 2001, 1996.5, 1998)])
    main.resolve_open_calls(df, NOW)
    assert db.get_stats()["losses"] == 1
    assert db.get_balance() == pytest.approx(9900.0)


def test_candle_touching_both_levels_counts_as_loss(tmp_db, sent):
    open_buy()
    df = candle_rows([("2026-09-16T14:10:00Z", 2000, 2005, 1996, 2001)])
    main.resolve_open_calls(df, NOW)
    assert db.get_stats()["losses"] == 1


def test_candles_before_signal_are_ignored(tmp_db, sent):
    open_buy()
    df = candle_rows([("2026-09-16T14:05:00Z", 2000, 2010, 1990, 2000)])   # fired inside this candle
    main.resolve_open_calls(df, NOW)
    assert len(db.get_open_calls()) == 1 and sent == []


def test_time_stop(tmp_db, sent):
    open_buy()
    start = datetime(2026, 9, 16, 14, 10, tzinfo=UTC)
    rows = [((start + timedelta(minutes=5 * k)).isoformat(), 2000, 2001, 1999, 2000.5)
            for k in range(main.MAX_HOLD_CANDLES)]
    main.resolve_open_calls(candle_rows(rows), NOW)
    assert db.get_open_calls() == [] and db.get_stats()["total"] == 1


# ----------------------------------------------------------------------------- telegram commands
def test_commands_work_on_a_fresh_database(tmp_db):
    """Regression: the old bot read tables nothing ever created."""
    assert "10,000.00" in bot_commands.render_status()
    assert "No open positions" in bot_commands.render_open()
    assert "Trades: 0" in bot_commands.render_stats()
    assert bot_commands.reply_for("/status@MyBot") is not None
    assert bot_commands.reply_for("hello") is None


# ----------------------------------------------------------------------------- whole pipeline
@pytest.fixture
def pipeline(tmp_db, sent, monkeypatch):
    d5, d1 = trending_data()
    state = {"d5": d5, "d1": d1}
    monkeypatch.setattr(main, "fetch_market_data",
                        lambda symbol=None, interval="5min", outputsize=100:
                        state["d5"].copy() if interval == "5min" else state["d1"].copy())
    monkeypatch.setattr(main, "evaluate_trade_with_gemini", lambda *a, **k: {
        "decision": "EXECUTE", "confidence_score": 82.0, "reasoning": "Clean trend.", "risk_notes": "News."})
    monkeypatch.setattr(main, "process_pending_commands", lambda: 0)
    return state


def test_pipeline_opens_one_signal_then_waits(pipeline, sent):
    assert main.run_trading_pipeline(NOW) == "signal_opened"
    calls = db.get_open_calls()
    assert len(calls) == 1 and calls[0]["direction"] == "BUY"
    assert "PAPER SIGNAL BUY" in sent[0]
    assert main.run_trading_pipeline(NOW + timedelta(minutes=5)) == "position_open"   # no duplicate signal


def test_pipeline_settles_then_respects_cooldown(pipeline, sent):
    main.run_trading_pipeline(NOW)
    call = db.get_open_calls()[0]
    later = NOW + timedelta(minutes=5)
    d5 = pipeline["d5"]
    spike = pd.DataFrame([{"datetime": pd.Timestamp("2026-09-16T14:10:00Z"), "open": call["entry"],
                           "high": call["take_profit"] + 1, "low": call["entry"] - 0.1,
                           "close": call["take_profit"]}])
    pipeline["d5"] = pd.concat([d5, spike], ignore_index=True)
    assert main.run_trading_pipeline(later) == "cooldown"        # settled the win, then cooled down
    assert db.get_stats()["wins"] == 1 and any("WIN" in m for m in sent)


def test_pipeline_does_nothing_when_market_closed(tmp_db, monkeypatch):
    monkeypatch.setattr(main, "process_pending_commands", lambda: 0)

    def boom(*a, **k):
        raise AssertionError("must not call the data API while the market is closed")
    monkeypatch.setattr(main, "fetch_market_data", boom)
    assert main.run_trading_pipeline(datetime(2026, 9, 19, 12, 0, tzinfo=UTC)) == "market_closed"


def test_ai_veto_blocks_trade(pipeline, monkeypatch):
    monkeypatch.setattr(main, "evaluate_trade_with_gemini", lambda *a, **k: {
        "decision": "REJECT", "confidence_score": 90.0, "reasoning": "Into resistance.", "risk_notes": ""})
    assert main.run_trading_pipeline(NOW) == "ai_rejected" and db.get_open_calls() == []


def test_low_confidence_blocks_trade(pipeline, monkeypatch):
    monkeypatch.setattr(main, "evaluate_trade_with_gemini", lambda *a, **k: {
        "decision": "EXECUTE", "confidence_score": 60.0, "reasoning": "Meh.", "risk_notes": ""})
    assert main.run_trading_pipeline(NOW) == "ai_rejected"
