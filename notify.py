import logging
import os

import requests


def send_telegram(text: str, chat_id=None) -> bool:
    """Send a plain-text Telegram message. Plain text on purpose: AI-written text with stray
    * or _ characters makes Telegram reject Markdown messages and the alert silently vanishes."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logging.warning("Telegram credentials missing. Skipping notification.")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text}, timeout=10)
        if r.status_code != 200:
            try:
                why = r.json().get("description", r.text[:120])
            except Exception:
                why = r.text[:120]
            logging.error("Telegram rejected message (HTTP %s): %s", r.status_code, why)
            return False
        return True
    except Exception as e:  # never let a notification failure kill the pipeline
        logging.error("Failed to send Telegram alert: %s", type(e).__name__)
        return False
