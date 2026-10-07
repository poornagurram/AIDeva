import json
from datetime import datetime, timedelta, timezone

from tests.conftest import FakeLLM, FakeX, load_example_config, make_agent
from xagent.db import iso, parse_iso
from xagent.telegram import TgUpdate
from xagent.xapi import Mention, TweetMetrics, XDuplicate, XError, XForbidden, XPaymentRequired

T0 = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)  # midnight in New York (audience tz)


def first_slot(agent):
    return agent.db.one("SELECT * FROM slots ORDER BY scheduled_at LIMIT 1")


def run_until(agent, until, step_min=5, start=T0):
    t = start
    while t <= until:
        agent.tick(t)
        t += timedelta(minutes=step_min)
    return t


def test_autopilot_plans_drafts_and_posts_on_time(runtime, db):
    cfg = load_example_config(mode="autopilot", monetization__cta_every_n_posts=0, research__enabled=False)
    x = FakeX()
    agent = make_agent(cfg, runtime, db, x=x)
    agent.tick(T0)
    slot = first_slot(agent)
    sched = parse_iso(slot["scheduled_at"])

    agent.tick(sched - timedelta(minutes=30))
    assert x.posted == []  # not drafted yet (lead is 5 min in autopilot)
    agent.tick(sched - timedelta(minutes=4))
    assert db.one("SELECT status FROM slots WHERE id=?", (slot["id"],))["status"] == "queued"
    assert x.posted == []  # drafted, but not before its time

    agent.tick(sched + timedelta(seconds=30))
    s = db.one("SELECT * FROM slots WHERE id=?", (slot["id"],))
    assert s["status"] == "done" and s["tweet_id"]
    root = [p for p in x.posted if p["reply_to"] is None]
    assert len(root) == 1 and root[0]["made_with_ai"] is True
    post = db.post_by_tweet(s["tweet_id"])
    assert post["pillar"] == slot["pillar"] and post["hour_local"] == slot["hour_local"]


def test_full_day_autopilot_respects_caps(runtime, db):
    cfg = load_example_config(mode="autopilot", research__enabled=False)
    x = FakeX()
    agent = make_agent(cfg, runtime, db, x=x)
    run_until(agent, T0 + timedelta(hours=23, minutes=55))
    done = db.query("SELECT * FROM slots WHERE status='done'")
    assert len(done) == cfg.schedule.posts_per_day
    roots = [p for p in x.posted if p["reply_to"] is None]
    assert len(roots) == cfg.schedule.posts_per_day
    assert all("#" not in p["text"] for p in x.posted)


def test_cta_goes_in_self_reply_with_utm_and_affiliate_label(runtime, db):
    cfg = load_example_config(
        mode="autopilot", research__enabled=False, monetization__cta_every_n_posts=1,
        monetization__offers=[{"name": "Tool", "url": "https://tool.example/?ref=me", "pitch": "the tool I use",
                               "affiliate": True}],
    )
    x = FakeX()
    agent = make_agent(cfg, runtime, db, x=x)
    agent.tick(T0)
    sched = parse_iso(first_slot(agent)["scheduled_at"])
    agent.tick(sched - timedelta(minutes=4))
    agent.tick(sched + timedelta(seconds=10))
    root = next(p for p in x.posted if p["reply_to"] is None)
    cta = next(p for p in x.posted if p["reply_to"] == root["id"])
    assert "https://" not in root["text"]
    assert "utm_source=x" in cta["text"] and "ref=me" in cta["text"]
    assert cta["paid_partnership"] is True
    assert db.get("posts_since_cta") == 0
    assert db.one("SELECT kind FROM posts WHERE tweet_id=?", (cta["id"],))["kind"] == "cta_reply"


def test_self_reply_blocked_switches_to_inline_cta(runtime, db):
    cfg = load_example_config(
        mode="autopilot", research__enabled=False, monetization__cta_every_n_posts=1,
        monetization__offers=[{"name": "NL", "url": "https://nl.example", "pitch": "weekly notes"}],
    )
    x = FakeX()
    x.fail_replies = XForbidden("403 forbidden: reply not allowed", status=403)
    agent = make_agent(cfg, runtime, db, x=x)
    agent.tick(T0)
    slots = db.query("SELECT * FROM slots ORDER BY scheduled_at")
    s1, s2 = parse_iso(slots[0]["scheduled_at"]), parse_iso(slots[1]["scheduled_at"])
    agent.tick(s1 - timedelta(minutes=4))
    agent.tick(s1 + timedelta(seconds=5))
    assert db.get("self_reply_blocked") is True
    agent.tick(s2 - timedelta(minutes=4))
    agent.tick(s2 + timedelta(seconds=5))
    second_root = [p for p in x.posted if p["reply_to"] is None][1]
    assert "https://nl.example" in second_root["text"]


def test_approval_flow_approve_edit_reject(runtime, db):
    cfg = load_example_config(research__enabled=False, monetization__cta_every_n_posts=0)
    x = FakeX()
    agent = make_agent(cfg, runtime, db, x=x)
    agent.tick(T0)
    slots = db.query("SELECT * FROM slots ORDER BY scheduled_at")
    s1 = parse_iso(slots[0]["scheduled_at"])

    agent.tick(s1 - timedelta(minutes=80))  # within 90 min lead: drafted and awaiting approval
    d = db.one("SELECT * FROM drafts WHERE slot_id=?", (slots[0]["id"],))
    assert d["status"] == "pending"
    assert db.one("SELECT status FROM slots WHERE id=?", (slots[0]["id"],))["status"] == "awaiting"

    msg = agent.decide(d["id"], "approve", edited_text="My own words, typed by a human, about shipping evals first.")
    assert "approved" in msg
    agent.tick(s1 - timedelta(minutes=10))
    assert x.posted == []  # approved early, still waits for its slot
    agent.tick(s1 + timedelta(seconds=5))
    assert x.posted[0]["text"].startswith("My own words")
    assert x.posted[0]["made_with_ai"] is False  # human-written

    s2 = parse_iso(slots[1]["scheduled_at"])
    agent.tick(s2 - timedelta(minutes=60))
    d2 = db.one("SELECT * FROM drafts WHERE slot_id=?", (slots[1]["id"],))
    agent.decide(d2["id"], "reject")
    agent.tick(s2 + timedelta(minutes=1))
    assert db.one("SELECT status FROM slots WHERE id=?", (slots[1]["id"],))["status"] == "skipped"
    assert len(x.posted) == 1


def test_unapproved_drafts_expire(runtime, db):
    cfg = load_example_config(research__enabled=False)
    agent = make_agent(cfg, runtime, db)
    agent.tick(T0)
    slot = first_slot(agent)
    s1 = parse_iso(slot["scheduled_at"])
    agent.tick(s1 - timedelta(minutes=60))
    agent.tick(s1 + timedelta(minutes=cfg.approval.timeout_minutes + 1))
    assert db.one("SELECT status FROM drafts WHERE slot_id=?", (slot["id"],))["status"] == "expired"
    assert db.one("SELECT status FROM slots WHERE id=?", (slot["id"],))["status"] == "missed"


def test_regenerate_writes_new_draft(runtime, db):
    cfg = load_example_config(research__enabled=False)
    agent = make_agent(cfg, runtime, db)
    agent.tick(T0)
    slot = first_slot(agent)
    s1 = parse_iso(slot["scheduled_at"])
    agent.tick(s1 - timedelta(minutes=60))
    d = db.one("SELECT * FROM drafts WHERE slot_id=?", (slot["id"],))
    agent.decide(d["id"], "regenerate")
    agent.tick(s1 - timedelta(minutes=59))
    drafts = db.query("SELECT status FROM drafts WHERE slot_id=? ORDER BY id", (slot["id"],))
    assert [r["status"] for r in drafts] == ["rejected", "pending"]


def test_duplicate_fails_slot_and_breaker_pauses(runtime, db):
    cfg = load_example_config(mode="autopilot", research__enabled=False, safety__max_consecutive_failures=2)
    x = FakeX()
    agent = make_agent(cfg, runtime, db, x=x)
    agent.tick(T0)
    slots = db.query("SELECT * FROM slots ORDER BY scheduled_at")

    x.fail_next = [XDuplicate("dup", status=403)]
    s = parse_iso(slots[0]["scheduled_at"])
    agent.tick(s - timedelta(minutes=4))
    agent.tick(s + timedelta(seconds=5))
    assert db.one("SELECT status FROM slots WHERE id=?", (slots[0]["id"],))["status"] == "failed"
    assert db.get("paused_until") is None  # duplicates don't trip the breaker

    x.fail_next = [XError("500 boom", status=500), XError("500 boom", status=500)]
    for sl in slots[1:3]:
        t = parse_iso(sl["scheduled_at"])
        agent.tick(t - timedelta(minutes=4))
        agent.tick(t + timedelta(seconds=5))
    assert parse_iso(db.get("paused_until")) > parse_iso(slots[2]["scheduled_at"])
    assert agent.paused_reason(parse_iso(slots[2]["scheduled_at"]) + timedelta(minutes=1))


def test_payment_required_pauses_and_keeps_draft(runtime, db):
    cfg = load_example_config(mode="autopilot", research__enabled=False)
    x = FakeX()
    agent = make_agent(cfg, runtime, db, x=x)
    agent.tick(T0)
    slot = first_slot(agent)
    s = parse_iso(slot["scheduled_at"])
    x.fail_next = [XPaymentRequired("402", status=402)]
    agent.tick(s - timedelta(minutes=4))
    agent.tick(s + timedelta(seconds=5))
    assert db.one("SELECT status FROM drafts WHERE slot_id=?", (slot["id"],))["status"] == "approved"
    assert "credits" in (db.get("pause_reason") or "")


def test_recover_never_reposts_inflight(runtime, db):
    cfg = load_example_config(mode="autopilot", research__enabled=False)
    agent = make_agent(cfg, runtime, db)
    did = agent.create_draft("post", {"text": "x", "not_before": iso(T0), "pillar": "p", "format": "insight"},
                             status="posting")
    agent.recover()
    assert db.one("SELECT status FROM drafts WHERE id=?", (did,))["status"] == "failed"


def test_x_budget_blocks_posting(runtime, db):
    cfg = load_example_config(mode="autopilot", research__enabled=False, budget__x_monthly_usd=0.01)
    x = FakeX()
    agent = make_agent(cfg, runtime, db, x=x)
    agent.tick(T0)
    slot = first_slot(agent)
    s = parse_iso(slot["scheduled_at"])
    agent.tick(s - timedelta(minutes=4))
    agent.tick(s + timedelta(seconds=5))
    assert x.posted == []
    assert db.one("SELECT status FROM slots WHERE id=?", (slot["id"],))["status"] == "skipped"


def _mention(i, *, author="7", username="alice", age_min=5, text="@tester how do you run evals on agents?"):
    return Mention(id=str(5000 + i), text=text, author_id=author, author_username=username, author_followers=300,
                   created_at=iso(T0 - timedelta(minutes=age_min)), conversation_id=None, in_reply_to_user_id="42",
                   replied_to_id=None)


def test_mentions_drafted_for_approval_then_posted_as_summoned_reply(runtime, db):
    cfg = load_example_config(research__enabled=False)
    x = FakeX()
    agent = make_agent(cfg, runtime, db, x=x)
    x.mention_queue = [
        _mention(1),
        _mention(2, author="42", username="tester"),  # ourselves
        _mention(3, age_min=60 * 30),  # too old
        _mention(4, text="@tester @other"),  # no content
    ]
    agent.process_mentions(T0)
    statuses = {r["tweet_id"]: (r["status"], r["reason"]) for r in db.query("SELECT * FROM mentions")}
    assert statuses["5001"][0] == "drafted"
    assert statuses["5002"] == ("skipped", "own post")
    assert statuses["5003"] == ("skipped", "too old")
    assert statuses["5004"][0] == "skipped"
    assert db.get("mentions_since_id") == "5004"

    d = db.one("SELECT * FROM drafts WHERE kind='reply'")
    agent.decide(d["id"], "approve")
    agent.publish_due(T0)
    reply = x.posted[-1]
    assert reply["reply_to"] == "5001" and reply["summoned"] is True
    assert db.one("SELECT status FROM mentions WHERE tweet_id='5001'")["status"] == "replied"


def test_per_user_reply_cap(runtime, db):
    cfg = load_example_config(research__enabled=False, engagement__max_replies_per_user_per_day=2)
    x = FakeX()
    agent = make_agent(cfg, runtime, db, x=x)
    now = datetime.now(timezone.utc)
    x.mention_queue = [Mention(id=str(9000 + i), text="@tester question about evals?", author_id="7",
                               author_username="bob", author_followers=10, created_at=iso(now),
                               conversation_id=None, in_reply_to_user_id=None, replied_to_id=None) for i in range(4)]
    agent.process_mentions(now)
    reasons = [r["reason"] for r in db.query("SELECT reason FROM mentions ORDER BY tweet_id")]
    assert reasons.count("per-user reply cap reached") == 2


def test_llm_declines_reply(runtime, db):
    cfg = load_example_config(research__enabled=False)
    llm = FakeLLM()
    llm.reply_action = "skip"
    agent = make_agent(cfg, runtime, db, llm=llm)
    agent.x.mention_queue = [_mention(1)]
    agent.process_mentions(T0)
    assert db.one("SELECT status FROM mentions")["status"] == "skipped"
    assert db.one("SELECT COUNT(*) c FROM drafts")["c"] == 0


def test_metrics_turn_into_rewards(runtime, db):
    cfg = load_example_config(mode="autopilot", research__enabled=False)
    x = FakeX()
    agent = make_agent(cfg, runtime, db, x=x)
    now = datetime.now(timezone.utc)
    db.record_post(tweet_id="777", kind="post", text="hello", pillar="ai_engineering", fmt="list", hour_local=9)
    db.execute("UPDATE posts SET created_at=? WHERE tweet_id='777'", (iso(now - timedelta(hours=30)),))
    x.metric_rows = [TweetMetrics(tweet_id="777", created_at=None, impressions=5000, likes=40, replies=6, reposts=3,
                                  quotes=1, bookmarks=4, profile_clicks=10, url_clicks=2)]
    agent.collect_metrics(now)
    r = db.one("SELECT * FROM rewards WHERE tweet_id='777'")
    assert r["format"] == "list" and r["reward"] > 3


def test_research_runs_once_per_day(runtime, db):
    cfg = load_example_config(research__local_hour=6)
    agent = make_agent(cfg, runtime, db)
    morning = T0 + timedelta(hours=11)  # 07:00 New York
    agent.maybe_research(morning)
    agent.maybe_research(morning + timedelta(hours=2))
    assert db.one("SELECT COUNT(*) c FROM research")["c"] == 1


def test_news_take_uses_research_items(runtime, db):
    cfg = load_example_config(mode="autopilot")
    llm = FakeLLM()
    agent = make_agent(cfg, runtime, db, llm=llm)
    agent.maybe_research(T0 + timedelta(hours=11))
    draft = agent.composer.compose("industry_takes", "news_take", cta_offer=None,
                                   now_local=(T0 + timedelta(hours=11)).astimezone(cfg.tz))
    assert draft and draft.format == "news_take"
    assert "FRESH NEWS" in llm.calls[-2][1]


def test_judge_can_reject_everything(runtime, db, cfg):
    llm = FakeLLM()
    llm.publishable = False
    agent = make_agent(cfg, runtime, db, llm=llm)
    assert agent.composer.compose("ai_engineering", "insight", cta_offer=None, now_local=T0) is None


class FakeTelegram:
    chat_id = "123"

    def __init__(self, updates):
        self._updates = updates
        self.sent: list[str] = []
        self.answers: list[str] = []

    def updates(self, offset):
        out, self._updates = self._updates, []
        return out

    def send(self, text, buttons=None):
        self.sent.append(text)
        return len(self.sent)

    def edit(self, message_id, text):
        pass

    def answer(self, callback_id, text=""):
        self.answers.append(text)


def test_telegram_only_owner_can_control(runtime, db):
    cfg = load_example_config(research__enabled=False)
    tg = FakeTelegram([
        TgUpdate(update_id=1, chat_id="999", text="/pause"),  # stranger
        TgUpdate(update_id=2, chat_id="123", text="/note Hit 100 paying customers today"),
    ])
    agent = make_agent(cfg, runtime, db, telegram=tg)
    agent.poll_telegram()
    assert not db.get("manual_pause", False)
    assert db.one("SELECT text FROM notes")["text"] == "Hit 100 paying customers today"
    assert db.get("tg_offset") == 3


def test_telegram_button_approves_draft(runtime, db):
    cfg = load_example_config(research__enabled=False)
    tg = FakeTelegram([])
    agent = make_agent(cfg, runtime, db, telegram=tg)
    agent.tick(T0)
    slot = first_slot(agent)
    agent.tick(parse_iso(slot["scheduled_at"]) - timedelta(minutes=60))
    d = db.one("SELECT * FROM drafts")
    assert d["tg_message_id"] and "Post draft" in tg.sent[-1]
    tg._updates = [TgUpdate(update_id=5, chat_id="123", callback_id="c", callback_data=f"a:{d['id']}",
                            message_id=d["tg_message_id"])]
    agent.poll_telegram()
    assert db.one("SELECT status FROM drafts WHERE id=?", (d["id"],))["status"] == "approved"
    assert "approved" in tg.answers[-1]


def test_report_and_status_render(runtime, db):
    cfg = load_example_config(mode="autopilot", research__enabled=False)
    agent = make_agent(cfg, runtime, db)
    agent.tick(T0)
    agent.snapshot_account()
    assert "Daily report" in agent.report_text(T0)
    assert "mode=autopilot" in agent.status_text()
    payload = json.dumps({"x": 1})
    assert payload


def test_auto_replies_limited_to_one_per_user(runtime, db):
    cfg = load_example_config(research__enabled=False, engagement__mode="auto")
    x = FakeX()
    agent = make_agent(cfg, runtime, db, x=x)
    now = datetime.now(timezone.utc)
    x.mention_queue = [Mention(id=str(9100 + i), text="@tester follow-up question?", author_id="8",
                               author_username="carol", author_followers=10, created_at=iso(now),
                               conversation_id=None, in_reply_to_user_id=None, replied_to_id=None) for i in range(3)]
    agent.process_mentions(now)
    assert db.one("SELECT COUNT(*) c FROM drafts WHERE kind='reply'")["c"] == 1


def test_claude_auth_failure_pauses_agent(runtime, db):
    from xagent.llm import LLMAuthError

    cfg = load_example_config(mode="autopilot", research__enabled=False)

    class BrokenLLM(FakeLLM):
        def structured(self, **kw):
            raise LLMAuthError("invalid x-api-key")

    agent = make_agent(cfg, runtime, db, llm=BrokenLLM())
    agent.tick(T0)
    s = parse_iso(first_slot(agent)["scheduled_at"])
    agent.tick(s - timedelta(minutes=4))
    assert "Claude API unusable" in (db.get("pause_reason") or "")
    assert db.one("SELECT status FROM slots ORDER BY scheduled_at LIMIT 1")["status"] == "pending"
