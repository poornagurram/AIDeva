"""Daily niche research: fresh news via Claude web search and/or RSS, stored as raw material for posts."""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

import feedparser
from pydantic import BaseModel, Field

from .config import AgentConfig
from .db import DB, iso, utcnow
from .llm import LLM

log = logging.getLogger(__name__)


class NewsItem(BaseModel):
    title: str
    summary: str = Field(description="2-3 factual sentences. Only facts supported by the sources.")
    angle: str = Field(description="A sharp, non-obvious take this audience would care about.")
    url: str = Field(description="Source URL, or empty string if none.")


class NewsBrief(BaseModel):
    items: list[NewsItem]


RESEARCH_SYSTEM = """You are the research desk for an X account. You find what happened in the last 48 hours
that this account's audience will care about, verify it from primary or reputable sources, and suggest
angles the account owner can credibly comment on. Never invent facts, numbers, or quotes."""


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def fetch_rss(feeds: list[str], *, max_age_hours: int = 48, per_feed: int = 8) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    items: list[dict] = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url, agent="xagent/1.0")
        except Exception as e:  # feedparser rarely raises, but never let one feed kill research
            log.warning("RSS %s failed: %s", url, e)
            continue
        for e in parsed.entries[:per_feed]:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if ts and datetime.fromtimestamp(time.mktime(ts), tz=timezone.utc) < cutoff:
                continue
            items.append({
                "title": _strip_html(e.get("title", ""))[:200],
                "summary": _strip_html(e.get("summary", ""))[:500],
                "url": e.get("link", ""),
            })
    return items


def run_research(db: DB, llm: LLM, cfg: AgentConfig, today: date) -> int:
    rc = cfg.research
    persona = cfg.persona
    rss = fetch_rss(rc.rss_feeds) if rc.rss_feeds else []
    headlines = "\n".join(f"- {i['title']} ({i['url']}): {i['summary'][:200]}" for i in rss[:40])
    focus = "\n".join(f"- {q}" for q in rc.queries) or "\n".join(f"- {p.name}: {p.description}" for p in persona.pillars)
    ask = (
        f"Today is {today:%A %Y-%m-%d}. Audience: {persona.audience}.\n"
        f"Topics to cover:\n{focus}\n\n"
        + (f"Candidate headlines from RSS (verify before using):\n{headlines}\n\n" if headlines else "")
        + f"Find the {rc.max_items} most important, most discussable developments from the last 48 hours for "
        "this audience. For each: title, factual summary, the source URL, and a sharp angle."
    )

    if rc.web_search:
        briefing = llm.web_research(system=RESEARCH_SYSTEM, user=ask, effort=cfg.llm.effort_research,
                                    max_searches=rc.max_searches)
        source = "web"
    elif rss:
        briefing = headlines
        source = "rss"
    else:
        log.info("Research enabled but no web_search and no rss_feeds configured")
        return 0

    brief = llm.structured(
        system=RESEARCH_SYSTEM,
        user=f"{ask}\n\nMaterial:\n{briefing}\n\nReturn at most {rc.max_items} items, best first. "
        "Drop anything you cannot support from the material.",
        output=NewsBrief,
        effort="low",
    )
    n = 0
    for item in brief.items[: rc.max_items]:
        cur = db.execute(
            "INSERT OR IGNORE INTO research(date_local,title,summary,angle,url,source,created_at) VALUES(?,?,?,?,?,?,?)",
            (today.isoformat(), item.title[:300], item.summary[:1200], item.angle[:600], item.url[:500], source,
             iso(utcnow())),
        )
        n += cur.rowcount or 0
    log.info("Research stored %d items for %s", n, today)
    return n
