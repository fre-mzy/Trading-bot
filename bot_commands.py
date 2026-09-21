"""Telegram commands (/status /open /stats) without needing a server.

Each pipeline run (every ~5 min in GitHub Actions) calls process_pending_commands(), which reads
any new messages via getUpdates and replies. Expect answers within about 5 minutes. Only the chat
in TELEGRAM_CHAT_ID is answered. You can still run `python bot_commands.py` on an always-on machine
to poll continuously if you want instant replies."""
import logging
import os
import time

import requests

import db
from notify import send_telegram

HELP = ("Gold paper-trading bot\n/status - balance and open trades\n"
        "/open - open positions\n/stats - all-time win/loss stats")


def render_status() -> str:
    return (f"Account (paper trading)\nBalance: ${db.get_balance():,.2f}\n"
            f"Open trades: {len(db.get_open_calls())}")


def render_open() -> str:
    calls = db.get_open_calls()
    if not calls:
        return "No open positions."
    lines = ["Open positions:"]
    for c in calls:
        lines.append(f"- {c['direction']} XAU/USD  entry {c['entry']}  TP {c['take_profit']}  "
                     f"SL {c['stop_loss']}  opened {str(c['timestamp'])[:16]} UTC")
    return "\n".join(lines)


def render_stats() -> str:
    s = db.get_stats()
    return (f"All-time paper stats\nTrades: {s['total']}\nWins: {s['wins']} | Losses: {s['losses']}\n"
            f"Win rate: {s['win_rate']:.1f}%\nNet PnL: ${s['net_pnl']:+,.2f}\nBalance: ${s['balance']:,.2f}")


COMMANDS = {"/start": HELP, "/help": HELP}
RENDERERS = {"/status": render_status, "/open": render_open, "/stats": render_stats}


def reply_for(text: str):
    cmd = (text or "").strip().split()[0].split("@")[0].lower() if (text or "").strip() else ""
    if cmd in COMMANDS:
        return COMMANDS[cmd]
    if cmd in RENDERERS:
        return RENDERERS[cmd]()
    return None


def process_pending_commands() -> int:
    """Answer new commands. Returns how many were handled."""
    token, allowed_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not allowed_chat:
        return 0
    offset = int(db.meta_get("tg_offset", "0"))
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": offset, "timeout": 0}, timeout=15)
        updates = r.json().get("result", []) if r.status_code == 200 else []
    except Exception as e:
        logging.warning("Telegram getUpdates failed: %s", type(e).__name__)
        return 0

    handled, new_offset = 0, offset
    for upd in updates:
        new_offset = max(new_offset, upd["update_id"] + 1)
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(allowed_chat):
            continue                                   # ignore strangers
        answer = reply_for(msg.get("text", ""))
        if answer:
            send_telegram(answer, chat_id=allowed_chat)
            handled += 1
    if new_offset != offset:                           # write only on change -> no noisy DB commits
        db.meta_set("tg_offset", str(new_offset))
    return handled


if __name__ == "__main__":                             # optional always-on mode
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    while True:
        process_pending_commands()
        time.sleep(3)
