"""
Telegram Command Handler Bot
Runs continuously to answer /status, /open, and /stats queries.
"""

import os
import sqlite3
import requests
from telebot import TeleBot

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DB_PATH = "signals_test.db"

bot = TeleBot(TELEGRAM_BOT_TOKEN)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    help_text = (
        "🤖 **Gold Trading Bot Interactive Commands:**\n\n"
        "/status - Show account balance & current performance\n"
        "/open - View currently active positions\n"
        "/stats - View all-time win/loss statistics"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def handle_status(message):
    if not os.path.exists(DB_PATH):
        bot.reply_to(message, "⚠️ Database file not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT balance FROM account WHERE id = 1")
    acc = c.fetchone()
    balance = acc[0] if acc else 10000.0

    c.execute("SELECT COUNT(*) FROM open_calls")
    open_count = c.fetchone()[0]

    conn.close()

    status_msg = (
        f"💳 **Account Overview (Paper Trading)**\n"
        f"Balance: **${balance:,.2f}**\n"
        f"Active Open Trades: **{open_count}**"
    )
    bot.reply_to(message, status_msg, parse_mode="Markdown")

@bot.message_handler(commands=['open'])
def handle_open(message):
    if not os.path.exists(DB_PATH):
        bot.reply_to(message, "⚠️ Database file not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT direction, entry, take_profit, stop_loss, timestamp FROM open_calls")
    rows = c.fetchall()
    conn.close()

    if not rows:
        bot.reply_to(message, "🟢 No active positions currently open.")
        return

    response = "📊 **Active Open Positions:**\n\n"
    for r in rows:
        response += (
            f"• **{r[0]} XAU/USD**\n"
            f"  Entry: `{r[1]}` | TP: `{r[2]}` | SL: `{r[3]}`\n"
            f"  Opened: {r[4][:16]}\n\n"
        )
    bot.reply_to(message, response, parse_mode="Markdown")

@bot.message_handler(commands=['stats'])
def handle_stats(message):
    if not os.path.exists(DB_PATH):
        bot.reply_to(message, "⚠️ Database file not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT result, SUM(pnl_usd), COUNT(*) FROM history GROUP BY result")
    stats = c.fetchall()
    
    c.execute("SELECT balance FROM account WHERE id = 1")
    balance = c.fetchone()[0]
    conn.close()

    wins, losses = 0, 0
    total_pnl = 0.0

    for row in stats:
        res, pnl, count = row
        if res == "WIN":
            wins = count
            total_pnl += (pnl or 0.0)
        elif res == "LOSS":
            losses = count
            total_pnl += (pnl or 0.0)

    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

    stats_msg = (
        f"📈 **All-Time Performance Stats**\n"
        f"Total Trades: {total_trades}\n"
        f"Wins: {wins} ✅ | Losses: {losses} ❌\n"
        f"Win Rate: **{win_rate:.1f}%**\n"
        f"Net PnL: **${total_pnl:+.2f}**\n"
        f"Current Balance: **${balance:,.2f}**"
    )
    bot.reply_to(message, stats_msg, parse_mode="Markdown")

if __name__ == "__main__":
    print("Telegram Command Bot started...")
    bot.infinity_polling()
