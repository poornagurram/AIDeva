"""Collects own-post metrics and converts them into bandit rewards."""

from __future__ import annotations

import logging
import math
from datetime import timedelta

from .db import DB, iso, parse_iso, utcnow
from .xapi import TweetMetrics, XClient

log = logging.getLogger(__name__)

# Reward = what the ranker values + what the business values, computed from the metrics X returns.
# Ranker weights are X's production Phoenix weights (xai-org/x-algorithm, home-mixer/params/param.rs):
# like 0.5, reply 5, repost 1, quote 5, share 2 (bookmarks stand in: both mean "worth keeping").
# Profile clicks stand in for "follow author" (weight 4) at an assumed ~25% conversion.
# URL clicks are weighted for business value: they are the path to revenue.
WEIGHTS = {
    "likes": 0.5,
    "reposts": 1.0,
    "replies": 5.0,
    "quotes": 5.0,
    "bookmarks": 2.0,
    "profile_clicks": 1.0,
    "url_clicks": 3.0,
}

# Posts are tracked for 4 days. Reads of the same post are de-duplicated per UTC day by X's billing,
# so polling a few times a day costs the same as once.
REWARD_MIN_AGE_H = 24
MAX_AGE_H = 96


def engagement_score(m: TweetMetrics) -> float:
    return sum(getattr(m, k) * w for k, w in WEIGHTS.items())


def reward(m: TweetMetrics) -> float:
    """Log-compressed so one viral outlier doesn't dominate what the bandit learns."""
    return math.log1p(engagement_score(m))


def store(db: DB, ms: list[TweetMetrics], now=None) -> int:
    now = now or utcnow()
    n = 0
    for m in ms:
        post = db.post_by_tweet(m.tweet_id)
        if not post or post["kind"] != "post":
            continue
        age = (now - parse_iso(post["created_at"])).total_seconds() / 3600
        db.execute(
            "INSERT INTO metrics(tweet_id,fetched_at,age_hours,impressions,likes,replies,reposts,quotes,bookmarks,"
            "profile_clicks,url_clicks,score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (m.tweet_id, iso(now), age, m.impressions, m.likes, m.replies, m.reposts, m.quotes, m.bookmarks,
             m.profile_clicks, m.url_clicks, engagement_score(m)),
        )
        n += 1
        if age >= REWARD_MIN_AGE_H:
            db.execute(
                "INSERT INTO rewards(tweet_id,pillar,format,hour_local,reward,recorded_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(tweet_id) DO UPDATE SET reward=excluded.reward, recorded_at=excluded.recorded_at",
                (m.tweet_id, post["pillar"], post["format"], post["hour_local"], reward(m), iso(now)),
            )
    return n


def collect(db: DB, x: XClient, user_id: str, now=None) -> int:
    now = now or utcnow()
    since = now - timedelta(hours=MAX_AGE_H)
    if not db.one("SELECT 1 FROM posts WHERE kind='post' AND dry_run=0 AND created_at>=? LIMIT 1", (iso(since),)):
        return 0
    ms = x.own_metrics(user_id, start_time=since)
    n = store(db, ms, now)
    log.info("Stored metrics for %d posts", n)
    return n
