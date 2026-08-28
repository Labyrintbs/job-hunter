"""Notification channels. Each reports whether it's configured and sends a digest.

Credentials come from environment variables (documented in the README):
  Telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  Email:    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO
The File channel needs nothing and always works — the safe default.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

import httpx

from ..config import DATA_DIR


class Channel:
    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def send(self, subject: str, text: str, markdown: str) -> str:
        raise NotImplementedError


class FileChannel(Channel):
    name = "file"

    def available(self) -> bool:
        return True

    def send(self, subject: str, text: str, markdown: str) -> str:
        out = DATA_DIR / "notifications"
        out.mkdir(parents=True, exist_ok=True)
        # stable-ish filename without wall-clock: count existing files
        n = len(list(out.glob("digest-*.md")))
        path = out / f"digest-{n:04d}.md"
        path.write_text(markdown, encoding="utf-8")
        return str(path)


class TelegramChannel(Channel):
    name = "telegram"

    def available(self) -> bool:
        return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))

    def send(self, subject: str, text: str, markdown: str) -> str:
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = os.environ["TELEGRAM_CHAT_ID"]
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = f"*{subject}*\n\n{text}"[:4000]
        resp = httpx.post(url, json={"chat_id": chat_id, "text": body,
                                     "parse_mode": "Markdown",
                                     "disable_web_page_preview": True}, timeout=20)
        resp.raise_for_status()
        return "sent"


class EmailChannel(Channel):
    name = "email"

    def available(self) -> bool:
        return bool(os.environ.get("SMTP_HOST") and os.environ.get("EMAIL_TO"))

    def send(self, subject: str, text: str, markdown: str) -> str:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = os.environ.get("SMTP_USER", os.environ["EMAIL_TO"])
        msg["To"] = os.environ["EMAIL_TO"]
        msg.set_content(text)
        host = os.environ["SMTP_HOST"]
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
        return "sent"


ALL_CHANNELS = {c.name: c for c in (FileChannel(), TelegramChannel(), EmailChannel())}
