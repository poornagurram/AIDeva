"""Alerts and reports to Telegram and/or a generic webhook (Slack/Discord compatible)."""

from __future__ import annotations

import logging

import requests

from .telegram import Telegram

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, *, telegram: Telegram | None = None, webhook_url: str | None = None):
        self.telegram = telegram
        self.webhook_url = webhook_url

    def send(self, text: str) -> None:
        if self.telegram:
            try:
                self.telegram.send(text)
            except Exception as e:  # notifications must never crash the agent
                log.warning("telegram notify failed: %s", e)
        if self.webhook_url:
            try:
                # "text" is read by Slack, "content" by Discord.
                requests.post(self.webhook_url, json={"text": text, "content": text[:1900]}, timeout=15)
            except Exception as e:
                log.warning("webhook notify failed: %s", e)
        if not self.telegram and not self.webhook_url:
            log.info("NOTIFY: %s", text)
