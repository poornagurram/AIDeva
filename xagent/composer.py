"""Generates post candidates with Claude, filters them with guardrails, and has a judge pick the winner."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from . import guardrails, playbook
from .config import AgentConfig
from .db import DB
from .llm import LLM, LLMError
from .textutil import clean_post

log = logging.getLogger(__name__)

LONG_POST_MAX = 2500  # Premium allows 25k; long enough for dwell, short enough to finish


# ---- structured output schemas ------------------------------------------------------------------
class Candidate(BaseModel):
    text: str = Field(description="The standalone post. For threads, this is the first post (the hook).")
    thread: list[str] = Field(
        default_factory=list,
        description="Thread format only: every post of the thread in order, including the first. Empty otherwise.",
    )
    cta_reply: str = Field(
        default="",
        description="Only when a CTA is requested: a one or two sentence self-reply bridging this post to the offer. "
        "Do NOT include the URL; it is appended automatically. Empty otherwise.",
    )
    hook_type: str = Field(description="Name of the hook pattern used, e.g. 'counterintuitive claim', 'specific number'.")
    reply_bait: str = Field(description="The honest reason a reader would want to reply to this post.")
    used_note_ids: list[int] = Field(default_factory=list, description="IDs of founder notes this post draws on.")
    used_news_ids: list[int] = Field(default_factory=list, description="IDs of news items this post draws on.")


class CandidateSet(BaseModel):
    candidates: list[Candidate]


class CandidateScore(BaseModel):
    index: int
    hook: int = Field(description="1-10: does the first line stop the scroll?")
    reply_potential: int = Field(description="1-10: will smart people want to reply or add their take?")
    specificity: int = Field(description="1-10: concrete details, numbers, examples vs generic advice.")
    authenticity: int = Field(description="1-10: sounds like the persona, not like AI; no fabricated claims.")
    negative_risk: int = Field(description="1-10: risk of mutes/blocks/'not interested'/reports. Higher is worse.")
    notes: str


class Judgement(BaseModel):
    scores: list[CandidateScore]
    best_index: int
    final_text: str = Field(description="The winning post, lightly polished. Keep the meaning; fix only weak words.")
    final_thread: list[str] = Field(default_factory=list, description="Thread format only: the polished thread parts.")
    final_cta_reply: str = Field(default="", description="Polished CTA self-reply without URL, if one was requested.")
    reason: str
    publishable: bool = Field(description="False if even the best candidate is not good enough to post.")


# ---- result -------------------------------------------------------------------------------------
@dataclass
class Draft:
    pillar: str
    format: str
    text: str
    thread: list[str] = field(default_factory=list)
    cta_reply: str = ""
    judge_reason: str = ""
    research_ids: list[int] = field(default_factory=list)
    note_ids: list[int] = field(default_factory=list)

    @property
    def parts(self) -> list[str]:
        return self.thread if self.thread else [self.text]


@dataclass
class ComposeContext:
    pillar: str
    format: str
    cta_offer: dict | None = None
    research: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    top_posts: list[dict] = field(default_factory=list)
    flops: list[dict] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)
    now_local: datetime | None = None


class Composer:
    def __init__(self, cfg: AgentConfig, llm: LLM, db: DB):
        self.cfg = cfg
        self.llm = llm
        self.db = db
        self._system = playbook.build_system_prompt(cfg)

    # ---- context ------------------------------------------------------------------------------
    def build_context(self, pillar: str, fmt: str, *, cta_offer: dict | None, now_local: datetime) -> ComposeContext:
        research = [
            dict(r)
            for r in self.db.query(
                "SELECT id,title,summary,angle,url FROM research WHERE used=0 AND date_local>=date(?, '-2 day') "
                "ORDER BY id DESC LIMIT 6",
                (now_local.date().isoformat(),),
            )
        ]
        if fmt == "news_take" and not research:
            fmt = "insight"
        notes = [dict(n) for n in self.db.query("SELECT id,text FROM notes WHERE used_count<2 ORDER BY id DESC LIMIT 5")]
        top = [
            dict(r)
            for r in self.db.query(
                "SELECT p.text, p.format, r.reward FROM rewards r JOIN posts p ON p.tweet_id=r.tweet_id "
                "WHERE p.dry_run=0 ORDER BY r.reward DESC LIMIT 5"
            )
        ]
        flops = [
            dict(r)
            for r in self.db.query(
                "SELECT p.text, p.format, r.reward FROM rewards r JOIN posts p ON p.tweet_id=r.tweet_id "
                "WHERE p.dry_run=0 ORDER BY r.reward ASC LIMIT 3"
            )
        ] if len(top) >= 5 else []
        recent = self.db.recent_texts(20)
        return ComposeContext(pillar, fmt, cta_offer, research, notes, top, flops, recent, now_local)

    # ---- prompts ------------------------------------------------------------------------------
    def _user_prompt(self, ctx: ComposeContext, n: int) -> str:
        pillar = next((p for p in self.cfg.persona.pillars if p.name == ctx.pillar), None)
        lines = [
            f"Local time for the audience: {ctx.now_local:%A %Y-%m-%d %H:%M}.",
            f"Write {n} distinct candidate posts.",
            f"Content pillar: {ctx.pillar} - {pillar.description if pillar else ''}",
            f"Format: {ctx.format} - {playbook.FORMAT_GUIDE.get(ctx.format, '')}",
            "Each candidate must use a DIFFERENT hook pattern and angle.",
        ]
        if ctx.format == "thread":
            lines.append("Thread: 4-7 posts. Post 1 must work alone and promise a payoff. Fill `thread` with ALL parts.")
        elif ctx.format == "long_post":
            lines.append("Long post: 600-1500 characters in `text`. Leave `thread` empty.")
        else:
            lines.append("Standard post: under 270 characters in `text`. Leave `thread` empty.")
        if ctx.notes:
            lines.append("\nFOUNDER NOTES (real, recent - prefer these as raw material; you may state these facts):")
            lines += [f"- [note {n['id']}] {n['text']}" for n in ctx.notes]
        if ctx.research:
            lines.append("\nFRESH NEWS (verified today; only state facts written here):")
            lines += [f"- [news {r['id']}] {r['title']}: {r['summary']} (angle: {r['angle'] or '-'})" for r in ctx.research]
            if ctx.format == "news_take":
                lines.append("For news_take: pick ONE news item and give a sharp, useful take on what it means for the audience.")
        if ctx.top_posts:
            lines.append("\nOUR BEST PERFORMERS (learn what this audience rewards; do not copy):")
            lines += [f"- ({p['format']}, score {p['reward']:.2f}) {p['text'][:280]}" for p in ctx.top_posts]
        if ctx.flops:
            lines.append("\nOUR WORST PERFORMERS (avoid what made these flat):")
            lines += [f"- ({p['format']}, score {p['reward']:.2f}) {p['text'][:200]}" for p in ctx.flops]
        if ctx.recent:
            lines.append("\nRECENTLY POSTED (do not repeat these ideas, openings, or phrasings):")
            lines += [f"- {t[:160]}" for t in ctx.recent]
        if ctx.cta_offer:
            o = ctx.cta_offer
            lines.append(
                f"\nCTA REQUESTED: the post itself must deliver standalone value on a topic adjacent to the offer "
                f"(no pitch, no link in the post). Then write `cta_reply`: a natural 1-2 sentence self-reply that "
                f"bridges to the offer '{o['name']}' ({o['pitch']}). No URL - it is appended automatically."
            )
        else:
            lines.append("\nNo CTA for this post: leave cta_reply empty. Pure value.")
        return "\n".join(lines)

    def _judge_prompt(self, ctx: ComposeContext, cands: list[Candidate]) -> str:
        lines = [
            f"Format: {ctx.format}. Pillar: {ctx.pillar}. CTA requested: {bool(ctx.cta_offer)}.",
            "Score every candidate with the rubric, pick the one most likely to earn replies and reposts from the "
            "target audience without negative feedback, then lightly polish it (same idea, stronger words, tighter).",
            "Hard-reject (publishable=false) if all candidates are generic, fabricate first-person facts not in the "
            "founder notes/facts, or would embarrass the author.",
            "",
        ]
        for i, c in enumerate(cands):
            body = "\n  ---\n  ".join(c.thread) if c.thread else c.text
            lines.append(f"[{i}] hook={c.hook_type!r}\n  {body}")
            if c.cta_reply:
                lines.append(f"  CTA reply: {c.cta_reply}")
            lines.append("")
        return "\n".join(lines)

    # ---- pipeline -----------------------------------------------------------------------------
    def _valid(self, c: Candidate, ctx: ComposeContext, history: list[str]) -> list[str]:
        problems: list[str] = []
        if ctx.format == "thread":
            parts = [clean_post(p) for p in c.thread] or [clean_post(c.text)]
            problems += guardrails.check_thread(parts, self.cfg, history=history).problems
        else:
            limit = LONG_POST_MAX if ctx.format == "long_post" else None
            problems += guardrails.check_text(clean_post(c.text), self.cfg, kind="post", history=history,
                                              max_length=limit).problems
        if ctx.cta_offer:
            if not c.cta_reply.strip():
                problems.append("missing cta_reply")
            else:
                problems += guardrails.check_text(
                    c.cta_reply, self.cfg, kind="cta_reply", max_length=250
                ).problems
        return problems

    def compose(self, pillar: str, fmt: str, *, cta_offer: dict | None, now_local: datetime) -> Draft | None:
        ctx = self.build_context(pillar, fmt, cta_offer=cta_offer, now_local=now_local)
        history = self.db.recent_texts(self.cfg.safety.history_window)
        n = self.cfg.llm.candidates_per_post

        valid: list[Candidate] = []
        rejected: list[str] = []
        for attempt in range(2):
            prompt = self._user_prompt(ctx, n)
            if rejected:
                prompt += "\n\nPrevious candidates were rejected by automated checks:\n" + "\n".join(rejected[-8:])
            cset = self.llm.structured(
                system=self._system, user=prompt, output=CandidateSet, effort=self.cfg.llm.effort_compose
            )
            for c in cset.candidates:
                problems = self._valid(c, ctx, history)
                if problems:
                    rejected.append(f"- {c.text[:80]!r}: {'; '.join(problems)}")
                else:
                    valid.append(c)
            if valid:
                break
            log.info("Attempt %d: all candidates failed guardrails", attempt + 1)
        if not valid:
            log.warning("No candidate passed guardrails for %s/%s: %s", pillar, ctx.format, rejected[-5:])
            return None

        judgement = self._judge(ctx, valid)
        if judgement is None:
            return None
        best = valid[judgement.best_index] if 0 <= judgement.best_index < len(valid) else valid[0]

        # Prefer the judge's polished version, but only if it still passes every check.
        polished = Candidate(
            text=clean_post(judgement.final_text or best.text),
            thread=[clean_post(p) for p in (judgement.final_thread or best.thread)],
            cta_reply=(judgement.final_cta_reply or best.cta_reply).strip(),
            hook_type=best.hook_type,
            reply_bait=best.reply_bait,
            used_note_ids=best.used_note_ids,
            used_news_ids=best.used_news_ids,
        )
        chosen = polished if not self._valid(polished, ctx, history) else best
        thread = [clean_post(p) for p in chosen.thread] if ctx.format == "thread" else []
        text = thread[0] if thread else clean_post(chosen.text)
        return Draft(
            pillar=pillar,
            format=ctx.format,
            text=text,
            thread=thread,
            cta_reply=chosen.cta_reply.strip() if ctx.cta_offer else "",
            judge_reason=judgement.reason,
            research_ids=[i for i in chosen.used_news_ids if i in {r["id"] for r in ctx.research}],
            note_ids=[i for i in chosen.used_note_ids if i in {n["id"] for n in ctx.notes}],
        )

    def _judge(self, ctx: ComposeContext, cands: list[Candidate]) -> Judgement | None:
        if len(cands) == 1 and self.cfg.llm.candidates_per_post == 1:
            c = cands[0]
            return Judgement(scores=[], best_index=0, final_text=c.text, final_thread=c.thread,
                             final_cta_reply=c.cta_reply, reason="single candidate", publishable=True)
        try:
            j = self.llm.structured(
                system=self._system + "\n\n" + playbook.JUDGE_RUBRIC,
                user=self._judge_prompt(ctx, cands),
                output=Judgement,
                effort=self.cfg.llm.effort_judge,
            )
        except LLMError as e:
            log.warning("Judge failed (%s); falling back to first valid candidate", e)
            c = cands[0]
            return Judgement(scores=[], best_index=0, final_text=c.text, final_thread=c.thread,
                             final_cta_reply=c.cta_reply, reason=f"judge unavailable: {e}", publishable=True)
        if not j.publishable:
            log.info("Judge rejected all candidates: %s", j.reason)
            return None
        return j


# ---- replies to mentions ------------------------------------------------------------------------
class ReplyDecision(BaseModel):
    action: Literal["reply", "skip"]
    category: Literal["question", "praise", "discussion", "disagreement", "spam", "hostile", "bot", "other"]
    reason: str
    reply: str = Field(default="", description="The reply text when action=reply. No @handles, no links, no hashtags.")


REPLY_TASK = """You are replying, as the account owner, to someone who mentioned or replied to us on X.
Decide whether to reply. Reply to genuine questions, thoughtful takes, respectful disagreement, and kind words.
SKIP: spam, crypto/promo shilling, bots, hostile or bad-faith messages, anything that needs private info, anything
where a reply could start a fight, and anything you'd have to invent facts to answer.
If replying: be specific to what they said, add something (an insight, an answer, a follow-up question that
invites them to keep talking). 1-3 short sentences. Warm, direct, human. No generic "Great point!" replies.
Never promise anything on the owner's behalf, never share links, never include @handles (X adds them)."""
