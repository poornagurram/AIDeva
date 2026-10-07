"""Minimal Telegram Bot API client: alerts, reports, and one-tap approve/reject for drafts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)


@dataclass
class TgUpdate:
    update_id: int
    callback_id: str | None = None
    callback_data: str | None = None
    message_id: int | None = None  # message the button was attached to / replied to
    chat_id: str | None = None
    text: str | None = None  # free-text reply (used for edits)


class Telegram:
    def __init__(self, token: str, chat_id: str, *, session: requests.Session | None = None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.http = session or requests.Session()

    def _call(self, method: str, payload: dict[str, Any], timeout: int = 20) -> Any:
        r = self.http.post(f"{self.base}/{method}", json=payload, timeout=timeout)
        data = r.json() if r.content else {}
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {data.get('description') or r.status_code}")
        return data.get("result")

    def send(self, text: str, *, buttons: list[list[tuple[str, str]]] | None = None) -> int:
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]
            }
        return int(self._call("sendMessage", payload)["message_id"])

    def edit(self, message_id: int, text: str) -> None:
        try:
            self._call("editMessageText", {"chat_id": self.chat_id, "message_id": message_id, "text": text[:4000],
                                           "disable_web_page_preview": True})
        except RuntimeError as e:
            log.debug("telegram edit failed: %s", e)

    def answer(self, callback_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})
        except RuntimeError as e:
            log.debug("telegram answer failed: %s", e)

    def updates(self, offset: int | None) -> list[TgUpdate]:
        payload: dict[str, Any] = {"timeout": 0, "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            payload["offset"] = offset
        out: list[TgUpdate] = []
        for u in self._call("getUpdates", payload, timeout=30) or []:
            cq = u.get("callback_query")
            msg = u.get("message")
            if cq:
                m = cq.get("message") or {}
                out.append(TgUpdate(
                    update_id=u["update_id"], callback_id=cq.get("id"), callback_data=cq.get("data"),
                    message_id=m.get("message_id"), chat_id=str((m.get("chat") or {}).get("id", "")),
                ))
            elif msg:
                reply_to = msg.get("reply_to_message") or {}
                out.append(TgUpdate(
                    update_id=u["update_id"], message_id=reply_to.get("message_id"),
                    chat_id=str((msg.get("chat") or {}).get("id", "")), text=msg.get("text"),
                ))
            else:
                out.append(TgUpdate(update_id=u["update_id"]))
        return out
