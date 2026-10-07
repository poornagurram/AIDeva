import random
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from tests.conftest import load_example_config
from xagent import bandit, planner
from xagent.db import iso, parse_iso


def test_example_config_is_valid(cfg):
    assert cfg.account.handle == "your_handle"
    assert cfg.mode == "approval"
    assert cfg.safety.max_hashtags == 0


def test_bad_values_rejected():
    with pytest.raises(ValidationError):
        load_example_config(account__timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        load_example_config(schedule__posts_per_day=11)
    with pytest.raises(ValidationError):
        load_example_config(mode="yolo")


def test_cta_disabled_without_offers():
    cfg = load_example_config(monetization__offers=[])
    assert cfg.monetization.cta_every_n_posts == 0


def test_bandit_learns_best_arm():
    rng = random.Random(0)
    stats = bandit.arm_stats([("a", 1.0)] * 20 + [("b", 3.0)] * 20 + [("c", 0.5)] * 20)
    picks = [bandit.choose(["a", "b", "c"], stats, epsilon=0.0, rng=rng) for _ in range(200)]
    assert picks.count("b") > 180


def test_bandit_explores_unseen_arms():
    rng = random.Random(1)
    stats = bandit.arm_stats([("a", 1.0)] * 3)
    picks = {bandit.choose(["a", "new"], stats, epsilon=0.0, rng=rng) for _ in range(100)}
    assert "new" in picks


def test_choose_hours_respects_gap():
    hours = bandit.choose_hours(list(range(8, 22)), {}, 4, min_gap_minutes=150, rng=random.Random(3))
    assert len(hours) == 4
    assert all(b - a >= 3 for a, b in zip(hours, hours[1:], strict=False))


def test_plan_day_is_idempotent_and_future_only(cfg, db):
    day = date(2026, 9, 29)
    start_of_day = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)  # midnight New York
    slots = planner.plan_day(db, cfg, day, now=start_of_day, rng=random.Random(2))
    assert len(slots) == cfg.schedule.posts_per_day
    assert planner.plan_day(db, cfg, day, now=start_of_day) == slots
    times = [parse_iso(s["scheduled_at"]) for s in slots]
    assert times == sorted(times)
    assert sum(s["format"] == "thread" for s in slots) <= cfg.schedule.max_threads_per_day

    late = datetime(2026, 10, 2, 1, 30, tzinfo=timezone.utc)  # Oct 1, 21:30 New York
    later = planner.plan_day(db, cfg, date(2026, 10, 1), now=late)
    assert later == []  # nothing left today


def test_long_post_only_for_premium(db):
    cfg = load_example_config(account__premium=False, schedule__formats=["long_post", "insight"])
    now = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)
    slots = planner.plan_day(db, cfg, date(2026, 9, 29), now=now, rng=random.Random(5))
    assert {s["format"] for s in slots} == {"insight"}


def test_rewards_steer_formats(cfg, db):
    now = datetime.now(timezone.utc)
    for i in range(30):
        db.execute("INSERT INTO rewards(tweet_id,pillar,format,hour_local,reward,recorded_at) VALUES(?,?,?,?,?,?)",
                   (f"t{i}", "ai_engineering", "list" if i % 2 else "question", 9, 6.0 if i % 2 else 0.1,
                    iso(now - timedelta(days=1))))
    stats = planner.reward_stats(db, "format")
    assert stats["list"].mean > stats["question"].mean
