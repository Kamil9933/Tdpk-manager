"""
Phone notification channels for TDPK Manager -- Telegram and email, picked
over WhatsApp for this round because WhatsApp's Business Platform needs
Meta business verification plus a pre-approved message template (and
per-message cost) for every message that isn't a reply inside a 24h
customer-initiated window, which every one of these alerts would be. Both
channels here are free and have no approval step.

Both channels degrade independently and silently: if TELEGRAM_BOT_TOKEN/
TELEGRAM_CHAT_ID aren't set, Telegram sends are skipped (reported, not
raised); same for the SMTP_* vars and email. notify_all() always also writes
to the in-app notifications bell (the `notifications` table, already wired
to the dashboard's bell icon but previously unused) so nothing is silently
lost even if neither phone channel is configured yet.

Telegram setup (free, ~5 minutes):
  1. In Telegram, message @BotFather -> /newbot -> follow the prompts ->
     you get a bot token. Set TELEGRAM_BOT_TOKEN to that.
  2. Message your new bot anything (e.g. "hi") so it can see your chat.
  3. Visit https://api.telegram.org/bot<token>/getUpdates in a browser --
     your numeric chat id is in the JSON response ("chat":{"id": ...}).
     Set TELEGRAM_CHAT_ID to that number.

Email setup (Gmail, free): turn on 2-Step Verification on the Gmail account,
then create an "App Password" (myaccount.google.com/apppasswords) -- use
that as SMTP_PASS, never the real account password. SMTP_USER is the full
gmail address. DIGEST_EMAIL_TO is where alerts land (can be the same
address, or any inbox).
"""

import os
import asyncio
import smtplib
from email.message import EmailMessage
import httpx

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
DIGEST_EMAIL_TO = os.getenv("DIGEST_EMAIL_TO", "")


async def send_telegram(text: str) -> dict:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"ok": False, "configured": False, "error": "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]},
            )
            data = r.json()
            if data.get("ok"):
                return {"ok": True, "configured": True, "error": ""}
            return {"ok": False, "configured": True, "error": data.get("description", "unknown Telegram error")}
    except Exception as e:
        return {"ok": False, "configured": True, "error": str(e)}


def _send_email_sync(subject: str, body: str) -> dict:
    if not (SMTP_USER and SMTP_PASS and DIGEST_EMAIL_TO):
        return {"ok": False, "configured": False, "error": "SMTP_USER/SMTP_PASS/DIGEST_EMAIL_TO not set"}
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = DIGEST_EMAIL_TO
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return {"ok": True, "configured": True, "error": ""}
    except Exception as e:
        return {"ok": False, "configured": True, "error": str(e)}


async def send_email(subject: str, body: str) -> dict:
    return await asyncio.to_thread(_send_email_sync, subject, body)


async def notify_all(subject: str, text: str, db=None) -> dict:
    """Fires every configured phone channel and always records an in-app
    notification too, so an alert is never silently lost just because a
    channel isn't set up yet or a send fails."""
    telegram_result = await send_telegram(f"{subject}\n\n{text}" if subject else text)
    email_result = await send_email(subject or "TDPK Manager", text)
    if db is not None:
        try:
            await db.add_notification("alert", subject or "TDPK Manager", text)
        except Exception:
            pass
    return {"telegram": telegram_result, "email": email_result}
