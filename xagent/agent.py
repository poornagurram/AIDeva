"""The 24/7 runtime: plans the day, drafts, gets approval (or not), publishes, replies, learns, reports."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import random
import signal
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from . import budget, engage, metrics, planner, research
from .composer import Composer, Draft
from .config import AgentConfig, Runtime, Secrets
from .db import DB, iso, parse_iso, utcnow
from .llm import LLM, LLMAuthError, LLMError, LLMRefusal
from .notify import Notifier
from .telegram import Telegram
from .textutil import weighted_length, with_utm
from .xapi import (
    XAuthError,
    XClient,
    XDuplicate,
    XError,
    XForbidden,
    XPaymentRequired,
    XRateLimited,
    make_client,
)

log = logging.getLogger(__name__)

TICK_SECONDS = 20
METRICS_EVERY_S = 3 * 3600
# The heartbeat stays fresh while the loop made progress this recently (LLM calls can take minutes).
STALL_SECONDS = 20 * 60


class Agent:
    def __init__(
        self,
        cfg: AgentConfig,
        runtime: Runtime,
        secrets: Secrets,
        *,
        db: DB | None = None,
        x: XClient | None = None,
        llm: LLM | None = None,
        telegram: Telegram | None = None,
        rng: random.Random | None = None,
    ):
        self.cfg = cfg
        self.runtime = runtime
        self.db = db or DB(runtime.db_path)
        self.x = x or make_client(secrets, dry_run=runtime.dry_run, db=self.db, handle=cfg.account.handle)
        self.llm = llm or LLM(cfg.llm, api_key=secrets.anthropic_api_key, db=self.db)
        if telegram is None and runtime.telegram_bot_token and runtime.telegram_chat_id:
            telegram = Telegram(runtime.telegram_bot_token, runtime.telegram_chat_id)
        self.tg = telegram
        self.notifier = Notifier(telegram=telegram, webhook_url=runtime.notify_webhook_url)
        self.composer = Composer(cfg, self.llm, self.db)
        self.rng = rng or random.Random()
        self._stop = threading.Event()
        self._last: dict[str, float] = {}
        self._clock: datetime | None = None  # the current tick's time (lets tests simulate days)
        self._progress = time.monotonic()

    def now(self) -> datetime:
        return self._clock or utcnow()

    # =============================================================================================
    # identity / state helpers
    # =============================================================================================
    def identity(self, refresh: bool = False) -> tuple[str, str]:
        uid, handle = self.db.get("me_id"), self.db.get("me_handle")
        if refresh or not uid:
            me = self.x.me()
            uid, handle = str(me.get("id", "")), me.get("username", self.cfg.account.handle)
            self.db.set("me_id", uid)
            self.db.set("me_handle", handle)
            if handle and handle.lower() != self.cfg.account.handle.lower() and not self.x.dry_run:
                self.alert(f"⚠️ Credentials belong to @{handle}, but config says @{self.cfg.account.handle}.")
        return uid, handle

    def paused_reason(self, now: datetime) -> str | None:
        if self.runtime.pause_file.exists():
            return f"pause file {self.runtime.pause_file}"
        if self.db.get("manual_pause", False):
            return "paused via /pause"
        until = self.db.get("paused_until")
        if until and parse_iso(until) > now:
            return f"auto-paused until {until} ({self.db.get('pause_reason', '')})"
        return None

    def pause(self, minutes: int, reason: str) -> None:
        until = iso(self.now() + timedelta(minutes=minutes))
        self.db.set("paused_until", until)
        self.db.set("pause_reason", reason)
        self.db.event("pause", f"{reason} (until {until})", "WARNING")
        self.alert(f"⏸ Paused posting for {minutes} min: {reason}")

    def alert(self, text: str) -> None:
        log.warning(text)
        self.notifier.send(text)

    def _every(self, name: str, seconds: float) -> bool:
        now = time.monotonic()
        if now - self._last.get(name, -1e12) >= seconds:
            self._last[name] = now
            return True
        return False

    def _guard(self, name: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except XPaymentRequired as e:
            self.pause(360, f"X API credits depleted - top up in the Developer Console ({e})")
        except XAuthError as e:
            self.pause(240, f"X authentication failed - check keys/permissions ({e})")
        except LLMAuthError as e:
            self.pause(120, f"Claude API unusable - check ANTHROPIC_API_KEY / billing ({e})")
        except XRateLimited as e:
            log.info("%s: rate limited until %s", name, datetime.fromtimestamp(e.reset_at))
        except LLMRefusal as e:
            log.warning("%s: model declined: %s", name, e)
        except (LLMError, XError) as e:
            log.error("%s failed: %s", name, e)
            self.db.event(name, str(e), "ERROR")
        except Exception as e:  # the loop must survive anything
            log.exception("%s crashed", name)
            self.db.event(name, f"{type(e).__name__}: {e}", "ERROR")
        return None

    def _write_failure(self, err: Exception) -> None:
        n = int(self.db.get("consecutive_write_failures", 0)) + 1
        self.db.set("consecutive_write_failures", n)
        if n >= self.cfg.safety.max_consecutive_failures:
            self.db.set("consecutive_write_failures", 0)
            self.pause(self.cfg.safety.pause_minutes_on_breaker, f"{n} consecutive X write failures, last: {err}")

    def _write_success(self) -> None:
        self.db.set("consecutive_write_failures", 0)

    def writes_today(self) -> int:
        start = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.db.count_posts_since(start, ("post", "thread_part", "cta_reply", "reply"))

    # =============================================================================================
    # drafts
    # =============================================================================================
    def create_draft(self, kind: str, payload: dict, *, status: str, slot_id: int | None = None,
                     mention_id: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO drafts(kind,slot_id,mention_id,payload,status,created_at) VALUES(?,?,?,?,?,?)",
            (kind, slot_id, mention_id, json.dumps(payload), status, iso(utcnow())),
        )
        return int(cur.lastrowid)

    def set_draft(self, draft_id: int, status: str, note: str | None = None, payload: dict | None = None) -> None:
        if payload is not None:
            self.db.execute("UPDATE drafts SET status=?, note=COALESCE(?,note), payload=?, decided_at=? WHERE id=?",
                            (status, note, json.dumps(payload), iso(utcnow()), draft_id))
        else:
            self.db.execute("UPDATE drafts SET status=?, note=COALESCE(?,note), decided_at=? WHERE id=?",
                            (status, note, iso(utcnow()), draft_id))

    def set_slot(self, slot_id: int, status: str, *, error: str | None = None, tweet_id: str | None = None) -> None:
        self.db.execute(
            "UPDATE slots SET status=?, error=COALESCE(?,error), tweet_id=COALESCE(?,tweet_id), updated_at=? WHERE id=?",
            (status, error, tweet_id, iso(utcnow()), slot_id),
        )

    def draft_preview(self, d: dict, payload: dict) -> str:
        if d["kind"] == "reply":
            return (f"💬 Reply draft #{d['id']} to @{payload.get('author')} ({payload.get('category')})\n\n"
                    f"They said: {payload.get('mention_text', '')[:400]}\n\n"
                    f"Reply:\n{payload['text']}\n\n"
                    "Tap a button, or reply to this message with your own text to post that instead.")
        parts = payload.get("thread") or [payload["text"]]
        body = "\n\n— — —\n\n".join(parts)
        when = parse_iso(payload["not_before"]).astimezone(self.cfg.tz)
        cta = payload.get("cta_reply")
        offer = payload.get("offer") or {}
        extra = f"\n\n🔗 CTA self-reply ({offer.get('name')}):\n{cta}" if cta else ""
        return (f"📝 Post draft #{d['id']} · {payload['pillar']} / {payload['format']} · goes out {when:%a %H:%M}\n\n"
                f"{body}{extra}\n\nTap a button, or reply to this message with edited text.")

    def request_approval(self, draft_id: int) -> None:
        d = self.db.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if not d:
            return
        payload = json.loads(d["payload"])
        text = self.draft_preview(d, payload)
        if not self.tg:
            log.info("Draft #%d awaiting approval (run `xagent approve %d`):\n%s", draft_id, draft_id, text)
            self.notifier.send(text + f"\n\nApprove with: xagent approve {draft_id}")
            return
        buttons = [[("✅ Post", f"a:{draft_id}"), ("❌ Skip", f"r:{draft_id}")]]
        if d["kind"] == "post":
            buttons[0].append(("🔁 New draft", f"g:{draft_id}"))
        try:
            mid = self.tg.send(text, buttons=buttons)
            self.db.execute("UPDATE drafts SET tg_message_id=? WHERE id=?", (mid, draft_id))
        except Exception as e:
            log.error("Could not send draft #%d to Telegram: %s", draft_id, e)

    # =============================================================================================
    # slots -> drafts
    # =============================================================================================
    def pick_cta(self) -> dict | None:
        m = self.cfg.monetization
        if not m.cta_every_n_posts or not m.offers:
            return None
        since = int(self.db.get("posts_since_cta", 0))
        if since < m.cta_every_n_posts - 1:
            return None
        offer = self.rng.choices(m.offers, weights=[o.weight for o in m.offers], k=1)[0]
        return offer.model_dump()

    def process_slots(self, now: datetime) -> None:
        lead = self.cfg.approval.lead_minutes if self.cfg.mode == "approval" else 5
        horizon = iso(now + timedelta(minutes=lead))
        slot = self.db.one(
            "SELECT * FROM slots WHERE status='pending' AND scheduled_at<=? ORDER BY scheduled_at LIMIT 1", (horizon,)
        )
        if slot:
            self.run_slot(slot, now)

    def run_slot(self, slot: Any, now: datetime) -> None:
        sched = parse_iso(slot["scheduled_at"])
        if now > sched + timedelta(minutes=self.cfg.schedule.grace_minutes):
            self.set_slot(slot["id"], "missed", error="host was not running at slot time")
            return
        if not budget.llm_allows(self.db, self.cfg):
            self.set_slot(slot["id"], "skipped", error="monthly Claude budget reached")
            if self._every("llm_budget_alert", 86400):
                self.alert("💸 Monthly Claude budget reached; drafting paused. Raise budget.llm_monthly_usd to resume.")
            return

        self.db.execute("UPDATE slots SET status='drafting', attempts=attempts+1, updated_at=? WHERE id=?",
                        (iso(now), slot["id"]))
        cta = self.pick_cta()
        try:
            draft = self.composer.compose(slot["pillar"], slot["format"], cta_offer=cta,
                                          now_local=sched.astimezone(self.cfg.tz))
        except Exception:
            # Put it back so a transient failure can retry on a later tick (within the grace window).
            status = "pending" if slot["attempts"] < 2 else "failed"
            self.set_slot(slot["id"], status, error="compose error")
            raise
        if draft is None:
            self.set_slot(slot["id"], "failed", error="no candidate passed quality checks")
            return
        payload = self._draft_payload(draft, slot, cta)
        status = "approved" if self.cfg.mode == "autopilot" else "pending"
        draft_id = self.create_draft("post", payload, status=status, slot_id=slot["id"])
        self.set_slot(slot["id"], "queued" if status == "approved" else "awaiting")
        log.info("Slot %s drafted as #%d (%s)", slot["id"], draft_id, status)
        if status == "pending":
            self.request_approval(draft_id)

    def _draft_payload(self, draft: Draft, slot: Any, cta: dict | None) -> dict:
        return {
            "pillar": draft.pillar,
            "format": draft.format,
            "text": draft.text,
            "thread": draft.thread,
            "cta_reply": draft.cta_reply,
            "offer": cta,
            "not_before": slot["scheduled_at"],
            "hour_local": slot["hour_local"],
            "research_ids": draft.research_ids,
            "note_ids": draft.note_ids,
            "judge_reason": draft.judge_reason,
            "human_edited": False,
        }

    # =============================================================================================
    # publishing
    # =============================================================================================
    def publish_due(self, now: datetime) -> None:
        for d in self.db.query("SELECT * FROM drafts WHERE status='approved' ORDER BY id"):
            payload = json.loads(d["payload"])
            if d["kind"] == "post":
                nb = parse_iso(payload["not_before"])
                if now < nb:
                    continue
                if now > nb + timedelta(minutes=self.cfg.approval.timeout_minutes):
                    self.set_draft(d["id"], "expired", "approved too late")
                    if d["slot_id"]:
                        self.set_slot(d["slot_id"], "missed", error="approved after timeout")
                    continue
                self.publish_post(d, payload)
            elif d["kind"] == "reply":
                self.publish_reply(d, payload)
            if self.paused_reason(self.now()):
                return

    def _label_ai(self, payload: dict) -> bool:
        return self.cfg.safety.made_with_ai_label and not payload.get("human_edited")

    def publish_post(self, d: Any, payload: dict) -> str | None:
        parts: list[str] = payload.get("thread") or [payload["text"]]
        offer = payload.get("offer")
        cta_text = payload.get("cta_reply") if offer else None
        cta_url = None
        if offer and cta_text:
            m = self.cfg.monetization
            cta_url = with_utm(offer["url"], m.utm_source, m.utm_medium, m.utm_campaign, content=payload["format"])

        # When self-replies are blocked by the API, the CTA rides inline in the main post if it fits.
        inline_cta = bool(cta_url and self.db.get("self_reply_blocked", False))
        if inline_cta:
            candidate = f"{parts[0]}\n\n{cta_text} {cta_url}"
            if weighted_length(candidate) <= 280 or (self.cfg.account.premium and payload["format"] == "long_post"):
                parts = [candidate, *parts[1:]]
            cta_text = cta_url = None

        b = self.cfg.budget
        cost = b.x_price_post * len(parts) + (b.x_price_link_post if (cta_url or inline_cta) else 0.0)
        if not budget.x_allows(self.db, self.cfg, cost):
            self.set_draft(d["id"], "failed", "monthly X budget reached")
            if d["slot_id"]:
                self.set_slot(d["slot_id"], "skipped", error="monthly X budget reached")
            if self._every("x_budget_alert", 86400):
                self.alert("💸 Monthly X API budget reached; posting paused. Raise budget.x_monthly_usd to resume.")
            return None
        if self.writes_today() + len(parts) > self.cfg.safety.max_writes_per_day:
            self.set_draft(d["id"], "failed", "daily write cap reached")
            if d["slot_id"]:
                self.set_slot(d["slot_id"], "skipped", error="daily write cap reached")
            return None

        # Mark as in-flight first: after a crash we must never re-post blindly.
        self.set_draft(d["id"], "posting")
        label = self._label_ai(payload)
        dry = self.x.dry_run
        try:
            root = self.x.create_post(parts[0], made_with_ai=label,
                                      paid_partnership=bool(inline_cta and offer and offer.get("affiliate")))
        except XDuplicate as e:
            self.set_draft(d["id"], "failed", str(e))
            if d["slot_id"]:
                self.set_slot(d["slot_id"], "failed", error="duplicate content")
            return None
        except (XRateLimited, XAuthError, XPaymentRequired):
            self.set_draft(d["id"], "approved")  # retry after the condition clears
            raise
        except XError as e:
            self.set_draft(d["id"], "failed", str(e))
            if d["slot_id"]:
                self.set_slot(d["slot_id"], "failed", error=str(e)[:300])
            self._write_failure(e)
            return None
        self._write_success()

        hour_local = payload.get("hour_local")
        self.db.record_post(tweet_id=root, kind="post", text=parts[0], pillar=payload["pillar"], fmt=payload["format"],
                            hour_local=hour_local, slot_id=d["slot_id"], offer=offer["name"] if offer else None,
                            dry_run=dry)
        self.set_draft(d["id"], "posted", root)
        if d["slot_id"]:
            self.set_slot(d["slot_id"], "done", tweet_id=root)
        log.info("Posted %s (%s/%s)", root, payload["pillar"], payload["format"])

        prev = root
        for part in parts[1:]:
            time.sleep(0 if dry else self.rng.uniform(2, 5))
            tid = self._self_reply(part, prev, kind="thread_part", root=root, payload=payload, label=label)
            if not tid:
                break
            prev = tid
        if cta_url and cta_text:
            time.sleep(0 if dry else self.rng.uniform(3, 8))
            self._self_reply(f"{cta_text} {cta_url}", prev, kind="cta_reply", root=root, payload=payload,
                             label=label, paid=bool(offer and offer.get("affiliate")))

        self._after_post(payload, had_cta=bool(offer))
        return root

    def _self_reply(self, text: str, parent: str, *, kind: str, root: str, payload: dict, label: bool,
                    paid: bool = False) -> str | None:
        try:
            tid = self.x.create_post(text, reply_to=parent, made_with_ai=label, paid_partnership=paid)
        except XDuplicate as e:
            log.warning("%s rejected as duplicate: %s", kind, e)
            return None
        except XForbidden as e:
            # The API may refuse replies that weren't "summoned". Switch to inline CTAs and no threads.
            if not self.db.get("self_reply_blocked", False):
                self.db.set("self_reply_blocked", True)
                self.alert(f"⚠️ X refused a self-reply ({e}). Threads are disabled and CTAs will go inline.")
            return None
        except XError as e:
            log.error("%s failed: %s", kind, e)
            return None
        self.db.record_post(tweet_id=tid, kind=kind, text=text, root_tweet_id=root, parent_tweet_id=parent,
                            pillar=payload["pillar"], fmt=payload["format"], dry_run=self.x.dry_run)
        return tid

    def _after_post(self, payload: dict, *, had_cta: bool) -> None:
        self.db.set("posts_since_cta", 0 if had_cta else int(self.db.get("posts_since_cta", 0)) + 1)
        for rid in payload.get("research_ids") or []:
            self.db.execute("UPDATE research SET used=1 WHERE id=?", (rid,))
        for nid in payload.get("note_ids") or []:
            self.db.execute("UPDATE notes SET used_count=used_count+1 WHERE id=?", (nid,))

    def publish_reply(self, d: Any, payload: dict) -> str | None:
        if self.writes_today() + 1 > self.cfg.safety.max_writes_per_day:
            return None
        if not budget.x_allows(self.db, self.cfg, self.cfg.budget.x_price_reply):
            self.set_draft(d["id"], "failed", "monthly X budget reached")
            return None
        self.set_draft(d["id"], "posting")
        try:
            tid = self.x.create_post(payload["text"], reply_to=payload["mention_id"], summoned=True,
                                     made_with_ai=self._label_ai(payload))
        except (XRateLimited, XAuthError, XPaymentRequired):
            self.set_draft(d["id"], "approved")
            raise
        except XError as e:
            self.set_draft(d["id"], "failed", str(e))
            self.db.execute("UPDATE mentions SET status='failed', reason=? WHERE tweet_id=?",
                            (str(e)[:300], payload["mention_id"]))
            return None
        self.db.record_post(tweet_id=tid, kind="reply", text=payload["text"], parent_tweet_id=payload["mention_id"],
                            dry_run=self.x.dry_run)
        self.set_draft(d["id"], "posted", tid)
        self.db.execute("UPDATE mentions SET status='replied', reply_tweet_id=? WHERE tweet_id=?",
                        (tid, payload["mention_id"]))
        return tid

    def expire_drafts(self, now: datetime) -> None:
        timeout = timedelta(minutes=self.cfg.approval.timeout_minutes)
        for d in self.db.query("SELECT * FROM drafts WHERE status='pending'"):
            payload = json.loads(d["payload"])
            anchor = parse_iso(payload["not_before"]) if d["kind"] == "post" else parse_iso(d["created_at"])
            if now > anchor + timeout:
                self.set_draft(d["id"], "expired", "no approval in time")
                if d["slot_id"]:
                    self.set_slot(d["slot_id"], "missed", error="approval timed out")
                if d["mention_id"]:
                    self.db.execute("UPDATE mentions SET status='skipped', reason='approval expired' WHERE tweet_id=?",
                                    (d["mention_id"],))
                if self.tg and d["tg_message_id"]:
                    self.tg.edit(d["tg_message_id"], f"⌛ Draft #{d['id']} expired.")

    # =============================================================================================
    # approvals & commands (Telegram)
    # =============================================================================================
    def decide(self, draft_id: int, action: str, *, edited_text: str | None = None) -> str:
        d = self.db.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if not d:
            return f"Draft #{draft_id} not found."
        if d["status"] != "pending":
            return f"Draft #{draft_id} is already {d['status']}."
        payload = json.loads(d["payload"])
        if action == "approve":
            if edited_text:
                from . import guardrails

                kind = "reply" if d["kind"] == "reply" else "post"
                limit = 25000 if self.cfg.account.premium else 280
                v = guardrails.check_text(edited_text, self.cfg, kind=kind, max_length=limit)
                blocking = [p for p in v.problems if not p.startswith("banned phrase")]
                if blocking:
                    return f"Edit rejected: {'; '.join(blocking)}"
                if payload.get("thread"):
                    payload["thread"][0] = edited_text
                payload["text"] = edited_text
                payload["human_edited"] = True
            self.set_draft(draft_id, "approved", "approved", payload)
            if d["slot_id"]:
                self.set_slot(d["slot_id"], "queued")
            return f"✅ Draft #{draft_id} approved" + (" (your edit)" if edited_text else "") + "."
        if action == "reject":
            self.set_draft(draft_id, "rejected", "rejected")
            if d["slot_id"]:
                self.set_slot(d["slot_id"], "skipped", error="rejected")
            if d["mention_id"]:
                self.db.execute("UPDATE mentions SET status='skipped', reason='rejected' WHERE tweet_id=?",
                                (d["mention_id"],))
            return f"❌ Draft #{draft_id} skipped."
        if action == "regenerate" and d["kind"] == "post" and d["slot_id"]:
            self.set_draft(draft_id, "rejected", "regenerate requested")
            slot = self.db.one("SELECT * FROM slots WHERE id=?", (d["slot_id"],))
            new_time = max(parse_iso(slot["scheduled_at"]), self.now() + timedelta(minutes=5))
            self.db.execute("UPDATE slots SET status='pending', attempts=0, scheduled_at=?, updated_at=? WHERE id=?",
                            (iso(new_time), iso(utcnow()), d["slot_id"]))
            return f"🔁 Writing a new draft for slot {d['slot_id']}..."
        return "Unknown action."

    def poll_telegram(self) -> None:
        if not self.tg:
            return
        offset = self.db.get("tg_offset")
        for u in self.tg.updates(offset):
            self.db.set("tg_offset", u.update_id + 1)
            if u.chat_id != self.tg.chat_id:
                continue  # only the owner's chat may control the agent
            if u.callback_data:
                action = {"a": "approve", "r": "reject", "g": "regenerate"}.get(u.callback_data[:1])
                try:
                    draft_id = int(u.callback_data[2:])
                except ValueError:
                    continue
                result = self.decide(draft_id, action or "")
                if u.callback_id:
                    self.tg.answer(u.callback_id, result)
                if u.message_id:
                    d = self.db.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
                    if d:
                        preview = self.draft_preview(d, json.loads(d["payload"]))
                        self.tg.edit(u.message_id, f"{result}\n\n{preview}")
            elif u.text:
                self.handle_text(u.text, reply_to_message_id=u.message_id)

    def handle_text(self, text: str, *, reply_to_message_id: int | None = None) -> None:
        text = text.strip()
        if reply_to_message_id:
            d = self.db.one("SELECT id FROM drafts WHERE tg_message_id=? AND status='pending'", (reply_to_message_id,))
            if d:
                self.tg_reply(self.decide(int(d["id"]), "approve", edited_text=text))
                return
        cmd, _, rest = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        if cmd == "/note" and rest.strip():
            self.db.execute("INSERT INTO notes(text,created_at) VALUES(?,?)", (rest.strip(), iso(utcnow())))
            self.tg_reply("📝 Noted. I'll turn it into posts.")
        elif cmd == "/pause":
            self.db.set("manual_pause", True)
            self.tg_reply("⏸ Paused. /resume to continue.")
        elif cmd == "/resume":
            self.db.set("manual_pause", False)
            self.db.set("paused_until", None)
            self.db.set("consecutive_write_failures", 0)
            self.tg_reply("▶️ Resumed.")
        elif cmd == "/status":
            self.tg_reply(self.status_text())
        elif cmd in ("/start", "/help"):
            self.tg_reply("Commands:\n/note <real update> - raw material for posts\n/status\n/pause\n/resume\n"
                          "Reply to a draft with text to post your version instead.")

    def tg_reply(self, text: str) -> None:
        if self.tg:
            try:
                self.tg.send(text)
            except Exception as e:
                log.warning("telegram reply failed: %s", e)

    # =============================================================================================
    # mentions
    # =============================================================================================
    def process_mentions(self, now: datetime) -> None:
        mode = self.cfg.engagement.mode
        if mode == "off" or self.x.dry_run:
            return
        if not budget.x_allows(self.db, self.cfg, 0.1):
            return
        me_id, _ = self.identity()
        since = self.db.get("mentions_since_id")
        found = self.x.mentions(me_id, since_id=since)
        if found:
            self.db.set("mentions_since_id", str(max(int(m.id) for m in found)))
        system = self.composer._system
        for m in sorted(found, key=lambda m: int(m.id)):
            if self.db.one("SELECT 1 FROM mentions WHERE tweet_id=?", (m.id,)):
                continue
            reason = engage.skip_reason(m, self.cfg, self.db, me_id, now)
            if reason:
                engage.record_mention(self.db, m, "skipped", reason)
                continue
            if not budget.llm_allows(self.db, self.cfg):
                break
            plan = engage.draft_reply(m, self.cfg, self.db, self.llm, system)
            if not plan:
                continue
            status = "approved" if mode == "auto" else "pending"
            payload = {"mention_id": m.id, "text": plan.text, "author": m.author_username,
                       "mention_text": m.text, "category": plan.category, "human_edited": False}
            draft_id = self.create_draft("reply", payload, status=status, mention_id=m.id)
            engage.record_mention(self.db, m, "drafted", plan.category)
            if status == "pending":
                self.request_approval(draft_id)

    # =============================================================================================
    # research, metrics, reports
    # =============================================================================================
    def maybe_research(self, now: datetime) -> None:
        rc = self.cfg.research
        if not rc.enabled:
            return
        local = now.astimezone(self.cfg.tz)
        today = local.date().isoformat()
        if self.db.get("research_date") == today or local.hour < rc.local_hour:
            return
        if not self._every("research_attempt", 3600) or not budget.llm_allows(self.db, self.cfg):
            return
        research.run_research(self.db, self.llm, self.cfg, local.date())
        self.db.set("research_date", today)

    def collect_metrics(self, now: datetime) -> None:
        if self.x.dry_run or not budget.x_allows(self.db, self.cfg, 0.2):
            return
        me_id, _ = self.identity()
        metrics.collect(self.db, self.x, me_id, now)

    def snapshot_account(self) -> dict:
        me = self.x.me()
        pm = me.get("public_metrics") or {}
        self.db.execute(
            "INSERT OR REPLACE INTO account_snapshots(ts,followers,following,posts) VALUES(?,?,?,?)",
            (iso(utcnow()), pm.get("followers_count"), pm.get("following_count"), pm.get("tweet_count")),
        )
        return pm

    def status_text(self) -> str:
        now = utcnow()
        today = now.astimezone(self.cfg.tz).date().isoformat()
        slots = self.db.query("SELECT hour_local,format,status FROM slots WHERE date_local=? ORDER BY scheduled_at", (today,))
        pending = self.db.one("SELECT COUNT(*) c FROM drafts WHERE status='pending'")["c"]
        lines = [
            f"🤖 xagent @{self.cfg.account.handle} · mode={self.cfg.mode}{' · DRY RUN' if self.x.dry_run else ''}",
            f"State: {self.paused_reason(now) or 'running'}",
            "Today: " + (", ".join(f"{s['hour_local']:02d}h {s['format']}={s['status']}" for s in slots) or "no slots"),
            f"Pending approvals: {pending}",
            f"Spend this month: X ${budget.x_spend(self.db, self.cfg):.2f}/{self.cfg.budget.x_monthly_usd:.0f} · "
            f"Claude ${budget.llm_spend(self.db, self.cfg):.2f}/{self.cfg.budget.llm_monthly_usd:.0f}",
        ]
        return "\n".join(lines)

    def report_text(self, now: datetime) -> str:
        since = iso(now - timedelta(days=1))
        posts = self.db.query("SELECT * FROM posts WHERE kind='post' AND created_at>=?", (since,))
        replies = self.db.one("SELECT COUNT(*) c FROM posts WHERE kind='reply' AND created_at>=?", (since,))["c"]
        top = self.db.query(
            "SELECT p.text, m.impressions, m.replies, m.likes, m.reposts, m.url_clicks FROM metrics m "
            "JOIN posts p ON p.tweet_id=m.tweet_id WHERE p.created_at>=? "
            "AND m.id IN (SELECT MAX(id) FROM metrics GROUP BY tweet_id) ORDER BY m.score DESC LIMIT 3",
            (iso(now - timedelta(days=7)),),
        )
        snaps = self.db.query("SELECT followers FROM account_snapshots ORDER BY ts DESC LIMIT 2")
        followers = ""
        if snaps:
            delta = (snaps[0]["followers"] or 0) - (snaps[1]["followers"] or 0) if len(snaps) > 1 else 0
            followers = f"Followers: {snaps[0]['followers']:,} ({delta:+d} since last report)\n"
        lines = [
            f"📊 Daily report · @{self.cfg.account.handle} · {now.astimezone(self.cfg.tz):%a %Y-%m-%d}",
            followers + f"Posts (24h): {len(posts)} · replies: {replies}",
            f"Spend MTD: X ${budget.x_spend(self.db, self.cfg):.2f} · Claude ${budget.llm_spend(self.db, self.cfg):.2f}",
        ]
        if top:
            lines.append("\nTop posts (7d):")
            for t in top:
                lines.append(f"• {t['impressions'] or 0:,} impr · {t['replies']} replies · {t['url_clicks']} clicks — "
                             f"{t['text'][:90]}")
        return "\n".join(lines)

    def maybe_report(self, now: datetime) -> None:
        local = now.astimezone(self.cfg.tz)
        today = local.date().isoformat()
        if local.hour < self.cfg.schedule.report_local_hour or self.db.get("report_date") == today:
            return
        self.db.set("report_date", today)
        if not self.x.dry_run:
            self._guard("snapshot", self.snapshot_account)
        self.notifier.send(self.report_text(now))

    # =============================================================================================
    # loop
    # =============================================================================================
    def recover(self) -> None:
        """After a crash/restart: never re-post in-flight drafts; retry interrupted drafting."""
        for d in self.db.query("SELECT * FROM drafts WHERE status='posting'"):
            self.set_draft(d["id"], "failed", "interrupted while posting; not retried to avoid duplicates")
            self.alert(f"⚠️ Draft #{d['id']} was mid-publish during a restart. Check X; it was not retried.")
        self.db.execute("UPDATE slots SET status='pending' WHERE status='drafting'")

    def heartbeat(self, now: datetime) -> None:
        try:
            self.runtime.heartbeat_file.write_text(iso(now))
        except OSError as e:
            log.warning("heartbeat write failed: %s", e)

    def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        self._clock = now
        self._progress = time.monotonic()
        self.heartbeat(now)
        self._guard("telegram", self.poll_telegram)  # always, so /resume works while paused
        if self.paused_reason(now):
            return
        today = now.astimezone(self.cfg.tz).date()
        self._guard("plan", lambda: planner.plan_day(self.db, self.cfg, today, now=now, rng=self.rng))
        self._guard("research", lambda: self.maybe_research(now))
        self._guard("slots", lambda: self.process_slots(now))
        self._guard("publish", lambda: self.publish_due(now))
        self._guard("expire", lambda: self.expire_drafts(now))
        if self.cfg.engagement.mode != "off" and self._every("mentions", self.cfg.engagement.poll_minutes * 60):
            self._guard("mentions", lambda: self.process_mentions(now))
        if self._every("metrics", METRICS_EVERY_S):
            self._guard("metrics", lambda: self.collect_metrics(now))
        self._guard("report", lambda: self.maybe_report(now))

    def _watchdog(self) -> None:
        """Keep the heartbeat fresh during long LLM calls, but let it go stale if the loop truly hangs."""
        while not self._stop.wait(30):
            if time.monotonic() - self._progress < STALL_SECONDS:
                self.heartbeat(utcnow())
            else:
                log.error("Main loop stalled for %ds; letting heartbeat go stale", time.monotonic() - self._progress)

    def stop(self, *_: Any) -> None:
        log.info("Stopping...")
        self._stop.set()

    def run_forever(self) -> None:
        lock = open(self.runtime.lock_file, "w")  # noqa: SIM115 - held for the process lifetime
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise SystemExit(f"Another xagent is already running with data dir {self.runtime.data_dir}") from e
        lock.write(str(os.getpid()))
        lock.flush()
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        if self.runtime.health_port:
            start_health_server(self.runtime, self.runtime.health_port)

        self.recover()
        backoff = 60
        while not self._stop.is_set():
            try:
                uid, handle = self.identity(refresh=True)
                break
            except (XAuthError, XPaymentRequired, XError) as e:
                self.alert(f"❌ Can't reach X as the configured account: {e}. Retrying in {backoff // 60} min.")
                self.heartbeat(utcnow())
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 3600)
        else:
            return
        threading.Thread(target=self._watchdog, daemon=True, name="watchdog").start()
        mode = f"{self.cfg.mode}{' (DRY RUN)' if self.x.dry_run else ''}"
        self.notifier.send(f"🚀 xagent started for @{handle} · mode={mode} · model={self.cfg.llm.model}")
        if self.cfg.mode == "autopilot" and not self.x.dry_run:
            log.warning("Autopilot: make sure the account has X's 'Automated' label and a bio disclosure.")
        while not self._stop.is_set():
            started = time.monotonic()
            self.tick()
            self._stop.wait(max(1.0, TICK_SECONDS - (time.monotonic() - started)))
        log.info("Stopped.")


def start_health_server(runtime: Runtime, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            ok, age = heartbeat_ok(runtime)
            body = json.dumps({"ok": ok, "heartbeat_age_s": age}).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health").start()
    log.info("Health endpoint on :%d", port)


def heartbeat_ok(runtime: Runtime, max_age_s: int = 300) -> tuple[bool, float | None]:
    try:
        ts = parse_iso(runtime.heartbeat_file.read_text().strip())
    except (OSError, ValueError):
        return False, None
    age = (utcnow() - ts).total_seconds()
    return age <= max_age_s, round(age, 1)
