"""Deterministic checks every piece of text passes before it can be published."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import AgentConfig
from .textutil import find_urls, hashtags, max_similarity, mentions, weighted_length

# Phrases that read as machine-written or as engagement bait. Both cost reach and trust.
DEFAULT_BANNED_PHRASES = (
    "as an ai",
    "as a language model",
    "i cannot assist",
    "delve",
    "in today's fast-paced",
    "in the ever-evolving",
    "game-changer",
    "game changer",
    "buckle up",
    "let that sink in",
    "unlock the power",
    "harness the power",
    "a testament to",
    "like and retweet",
    "like & retweet",
    "rt if",
    "follow for more",
    "follow me for more",
    "smash that",
    "comment below",
    "tag a friend",
    "link in bio",
    "breaking:",
    "🚨",
    "reply with",
    "who else",
)

_PLACEHOLDER_RE = re.compile(r"\[(?:link|url|insert|your|name|product)[^\]]*\]|\{[a-z_]+\}|<insert|TODO|lorem ipsum", re.I)


@dataclass
class Verdict:
    ok: bool
    problems: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def check_text(
    text: str,
    cfg: AgentConfig,
    *,
    kind: str,
    history: list[str] | None = None,
    max_length: int | None = None,
) -> Verdict:
    """kind: post | thread_part | cta_reply | reply."""
    s = cfg.safety
    problems: list[str] = []
    t = text.strip()
    if not t:
        return Verdict(False, ["empty"])

    limit = max_length or s.max_weighted_length
    wl = weighted_length(t)
    if wl > limit:
        problems.append(f"too long ({wl}>{limit})")

    tags = hashtags(t)
    if len(tags) > s.max_hashtags:
        problems.append(f"too many hashtags ({len(tags)})")

    urls = find_urls(t)
    if urls and kind in ("post", "thread_part", "reply") and not s.allow_links_in_main_post:
        problems.append(f"links not allowed in {kind}: {urls[:2]}")
    if kind == "cta_reply" and len(urls) > 1:
        problems.append("cta reply must contain at most one link")

    if kind in ("post", "thread_part") and not s.allow_mentions_in_posts and mentions(t):
        problems.append(f"@mentions not allowed in posts: {mentions(t)[:3]}")

    low = t.lower()
    for phrase in (*DEFAULT_BANNED_PHRASES, *(p.lower() for p in cfg.persona.banned_phrases)):
        if re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", low):
            problems.append(f"banned phrase: {phrase!r}")

    if _PLACEHOLDER_RE.search(t):
        problems.append("contains a placeholder")

    if history:
        sim = max_similarity(t, history)
        if sim > s.max_similarity:
            problems.append(f"too similar to a previous post ({sim:.2f})")

    return Verdict(not problems, problems)


def check_thread(parts: list[str], cfg: AgentConfig, *, history: list[str] | None = None) -> Verdict:
    if not 2 <= len(parts) <= 10:
        return Verdict(False, [f"thread must have 2-10 parts, got {len(parts)}"])
    problems: list[str] = []
    part_limit = min(280, cfg.safety.max_weighted_length)
    for i, p in enumerate(parts):
        v = check_text(p, cfg, kind="thread_part", history=history if i == 0 else None, max_length=part_limit)
        problems += [f"part {i + 1}: {x}" for x in v.problems]
    total_tags = sum(len(hashtags(p)) for p in parts)
    if total_tags > cfg.safety.max_hashtags:
        problems.append(f"too many hashtags across thread ({total_tags})")
    return Verdict(not problems, problems)
