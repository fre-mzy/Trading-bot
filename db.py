"""SQLite storage. One place owns the schema so main.py and bot_commands.py can never disagree."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config import DB_PATH, START_BALANCE

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL,
    equity REAL
);
CREATE TABLE IF NOT EXISTS open_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    direction TEXT,
    entry REAL,
    take_profit REAL,
    stop_loss REAL,
    units REAL,
    risk_usd REAL,
    confidence TEXT,
    reasoning TEXT
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    direction TEXT,
    entry REAL,
    exit_price REAL,
    pnl_usd REAL,
    result TEXT,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after the first version of the DB shipped: (table, column, type)
MIGRATIONS = [
    ("open_calls", "candle_time", "TEXT"),   # first candle allowed to resolve the trade
    ("open_calls", "spread", "REAL"),
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create tables, add missing columns, seed the account. Safe to call on every run."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, column, coltype in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        if conn.execute("SELECT 1 FROM account WHERE id = 1").fetchone() is None:
            conn.execute("INSERT INTO account (id, balance, equity) VALUES (1, ?, ?)",
                         (START_BALANCE, START_BALANCE))


def get_balance() -> float:
    with connect() as conn:
        row = conn.execute("SELECT balance FROM account WHERE id = 1").fetchone()
    return float(row["balance"]) if row else START_BALANCE


def get_open_calls() -> list:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM open_calls ORDER BY id")]


def add_open_call(direction, entry, take_profit, stop_loss, units, risk_usd,
                  confidence, reasoning, candle_time, spread, timestamp=None) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO open_calls (timestamp, direction, entry, take_profit, stop_loss,
               units, risk_usd, confidence, reasoning, candle_time, spread)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp or utc_now_iso(), direction, entry, take_profit, stop_loss, units, risk_usd,
             str(confidence), reasoning, candle_time, spread))
        return cur.lastrowid


def close_call(call: dict, result: str, exit_price: float, pnl_usd: float, resolved_at=None) -> float:
    """Move a call from open_calls to history and update the balance. Returns new balance."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO history (timestamp, direction, entry, exit_price, pnl_usd, result, resolved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (call["timestamp"], call["direction"], call["entry"], exit_price,
             round(pnl_usd, 2), result, resolved_at or utc_now_iso()))
        conn.execute("DELETE FROM open_calls WHERE id = ?", (call["id"],))
        bal = conn.execute("SELECT balance FROM account WHERE id = 1").fetchone()["balance"]
        new_bal = round(bal + pnl_usd, 2)
        conn.execute("UPDATE account SET balance = ?, equity = ? WHERE id = 1", (new_bal, new_bal))
    return new_bal


def get_stats() -> dict:
    with connect() as conn:
        rows = conn.execute(
            "SELECT result, COUNT(*) AS n, COALESCE(SUM(pnl_usd), 0) AS pnl FROM history GROUP BY result"
        ).fetchall()
    wins = losses = 0
    net = 0.0
    for r in rows:
        if r["result"] == "WIN":
            wins = r["n"]
        elif r["result"] == "LOSS":
            losses = r["n"]
        net += r["pnl"]
    total = wins + losses
    return {"wins": wins, "losses": losses, "total": total,
            "win_rate": (wins / total * 100) if total else 0.0, "net_pnl": net,
            "balance": get_balance()}


def last_signal_time():
    """Timestamp of the most recent signal (open or closed), or None."""
    with connect() as conn:
        row = conn.execute(
            """SELECT MAX(ts) AS ts FROM (
                 SELECT timestamp AS ts FROM open_calls
                 UNION ALL SELECT timestamp AS ts FROM history)""").fetchone()
    return datetime.fromisoformat(row["ts"]) if row and row["ts"] else None


def meta_get(key: str, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
