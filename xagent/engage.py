"""Mention handling: decide which mentions deserve a reply and draft it.

X's API only allows programmatic replies to posts that @mention (or quote) you, and X's automation rules
require prior approval for AI reply bots. So replies are drafted for approval by default.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import guardrails
from .composer import REPLY_TASK, ReplyDecision
from .config import AgentConfig
from .db import DB, iso, parse_iso, utcnow
from .llm import LLM
from .textutil import clean_post
from .xapi import Mention

log = logging.getLogger(__name__)

_HANDLES_RE = re.compile(r"(?:^|\s)@\w{1,15}")


@dataclass
class ReplyPlan:
    mention: Mention
    text: str
    category: str


def replies_today(db: DB, now: datetime, author_id: str | None = None) -> int:
    start = iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
    if author_id:
        row = db.one("SELECT COUNT(*) c FROM mentions WHERE status IN ('replied','drafted') AND author_id=? AND handled_at>=?",
                     (author_id, start))
    else:
        row = db.one("SELECT COUNT(*) c FROM mentions WHERE status IN ('replied','drafted') AND handled_at>=?", (start,))
    return int(row["c"]) if row else 0


def skip_reason(m: Mention, cfg: AgentConfig, db: DB, me_id: str, now: datetime) -> str | None:
    ec = cfg.engagement
    if m.author_id == me_id:
        return "own post"
    if m.is_retweet:
        return "repost"
    if m.author_username.lower() in ec.ignore_users:
        return "ignored user"
    if m.created_at:
        age = now - parse_iso(m.created_at)
        if age > timedelta(hours=ec.reply_window_hours):
            return "too old"
    if m.author_followers < ec.min_author_followers:
        return "author below follower threshold"
    if not _HANDLES_RE.sub("", m.text).strip():
        return "no content besides handles"
    if replies_today(db, now) >= ec.max_replies_per_day:
        return "daily reply cap reached"
    # X's developer guidelines allow unattended auto-replies to people who engaged first, limited to one.
    per_user_cap = 1 if ec.mode == "auto" else ec.max_replies_per_user_per_day
    if replies_today(db, now, m.author_id) >= per_user_cap:
        return "per-user reply cap reached"
    return None


def record_mention(db: DB, m: Mention, status: str, reason: str = "", reply_tweet_id: str | None = None) -> None:
    db.execute(
        "INSERT INTO mentions(tweet_id,author_id,author_username,text,conversation_id,created_at,status,reason,"
        "reply_tweet_id,handled_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tweet_id) DO UPDATE SET "
        "status=excluded.status, reason=excluded.reason, reply_tweet_id=COALESCE(excluded.reply_tweet_id, reply_tweet_id), "
        "handled_at=excluded.handled_at",
        (m.id, m.author_id, m.author_username, m.text[:1000], m.conversation_id, m.created_at, status, reason[:500],
         reply_tweet_id, iso(utcnow())),
    )


def draft_reply(m: Mention, cfg: AgentConfig, db: DB, llm: LLM, system_prompt: str) -> ReplyPlan | None:
    parent = db.post_by_tweet(m.replied_to_id) if m.replied_to_id else None
    lines = [REPLY_TASK, ""]
    if parent:
        lines.append(f"They are replying to OUR post:\n\"\"\"{parent['text']}\"\"\"\n")
    lines += [
        f"From @{m.author_username} ({m.author_followers} followers). Their bio: {m.extra.get('author_description', '')[:200]}",
        f"Their post:\n\"\"\"{m.text}\"\"\"",
    ]
    decision = llm.structured(system=system_prompt, user="\n".join(lines), output=ReplyDecision,
                              effort=cfg.llm.effort_reply, max_tokens=8000)
    if decision.action != "reply" or not decision.reply.strip():
        record_mention(db, m, "skipped", f"{decision.category}: {decision.reason}")
        return None
    text = clean_post(_HANDLES_RE.sub(" ", decision.reply)).strip()
    verdict = guardrails.check_text(text, cfg, kind="reply", max_length=min(280, cfg.safety.max_weighted_length))
    if not verdict:
        record_mention(db, m, "skipped", "guardrails: " + "; ".join(verdict.problems))
        return None
    return ReplyPlan(m, text, decision.category)
