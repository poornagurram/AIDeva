"""Plans each day's posting slots, steered by the bandit's learned rewards."""

from __future__ import annotations

import logging
import random
from datetime import date, datetime, time, timedelta, timezone

from . import bandit
from .config import AgentConfig
from .db import DB, iso, utcnow

log = logging.getLogger(__name__)

# Relative prior weights before the bandit has data. Threads are costlier, questions are situational.
FORMAT_PRIOR = {
    "insight": 1.3,
    "contrarian": 1.1,
    "list": 1.0,
    "story": 1.0,
    "how_to": 1.0,
    "question": 0.7,
    "one_liner": 0.9,
    "news_take": 1.1,
    "thread": 0.5,  # only post 1 is recommended out-of-network; later parts reach people who click in
    "long_post": 1.0,
}

REWARD_LOOKBACK_DAYS = 60


def reward_stats(db: DB, dimension: str) -> dict[str, bandit.ArmStats]:
    assert dimension in ("pillar", "format", "hour_local")
    since = iso(utcnow() - timedelta(days=REWARD_LOOKBACK_DAYS))
    rows = db.query(f"SELECT {dimension} AS arm, reward FROM rewards WHERE recorded_at>=? AND {dimension} IS NOT NULL", (since,))
    return bandit.arm_stats((str(r["arm"]), float(r["reward"])) for r in rows)


def slots_for(db: DB, day: date) -> list:
    return db.query("SELECT * FROM slots WHERE date_local=? ORDER BY scheduled_at", (day.isoformat(),))


def plan_day(db: DB, cfg: AgentConfig, day: date, *, now: datetime | None = None, rng: random.Random | None = None) -> list:
    """Create the day's slots (idempotent). Only hours still in the future are planned."""
    existing = slots_for(db, day)
    if existing:
        return existing
    rng = rng or random.Random()
    now = now or utcnow()
    tz = cfg.tz
    sc = cfg.schedule

    def local_dt(hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=tz)

    future_hours = [h for h in sc.candidate_hours if local_dt(h) + timedelta(minutes=2 * sc.jitter_minutes) > now + timedelta(minutes=5)]
    if not future_hours:
        db.set(f"plan:{day.isoformat()}", "empty")
        return []
    k = min(sc.posts_per_day, len(future_hours))
    hours = bandit.choose_hours(future_hours, reward_stats(db, "hour_local"), k, min_gap_minutes=sc.min_gap_minutes, rng=rng)

    pillar_names = [p.name for p in cfg.persona.pillars]
    pillar_weights = {p.name: p.weight for p in cfg.persona.pillars}
    formats = [
        f for f in sc.formats
        if not (f == "news_take" and not cfg.research.enabled)
        and not (f == "long_post" and not cfg.account.premium)
        and not (f == "thread" and db.get("self_reply_blocked", False))
    ]
    format_weights = {f: FORMAT_PRIOR.get(f, 1.0) for f in formats}
    p_stats, f_stats = reward_stats(db, "pillar"), reward_stats(db, "format")

    threads = 0
    last_pillar: str | None = None
    used_formats: list[str] = []
    rows = []
    for hour in hours:
        minute = rng.randint(0, min(59, 2 * sc.jitter_minutes))
        when = local_dt(hour, minute)
        if when <= now + timedelta(minutes=2):
            when = now + timedelta(minutes=rng.randint(3, 10))
        pillar = bandit.choose(pillar_names, p_stats, weights=pillar_weights, exclude=[last_pillar] if last_pillar else [], rng=rng)
        exclude = list(used_formats[-2:])  # avoid the same format back to back
        if threads >= sc.max_threads_per_day:
            exclude.append("thread")
        fmt = bandit.choose(formats, f_stats, weights=format_weights, exclude=exclude, rng=rng)
        threads += fmt == "thread"
        used_formats.append(fmt)
        last_pillar = pillar
        rows.append((day.isoformat(), iso(when.astimezone(timezone.utc)), hour, pillar, fmt))

    with db.tx() as c:
        for r in rows:
            c.execute(
                "INSERT INTO slots(date_local,scheduled_at,hour_local,pillar,format,updated_at) VALUES(?,?,?,?,?,?)",
                (*r, iso(now)),
            )
    planned = slots_for(db, day)
    log.info("Planned %d slots for %s: %s", len(planned), day, [(s["hour_local"], s["pillar"], s["format"]) for s in planned])
    return planned
