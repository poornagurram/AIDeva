"""Command-line interface: `xagent run` for the daemon, plus tools for drafting, approving and inspecting."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import shutil
import sys
from pathlib import Path

from .config import Runtime, load_config, load_runtime, load_secrets
from .db import iso, utcnow


def setup_logging(runtime: Runtime) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(runtime.log_level)
    root.handlers.clear()
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.handlers.RotatingFileHandler(runtime.data_dir / "xagent.log", maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    for noisy in ("httpx", "httpx2", "urllib3", "anthropic", "requests_oauthlib", "oauthlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_agent(args: argparse.Namespace):
    from .agent import Agent

    runtime = load_runtime()
    if getattr(args, "dry_run", False):
        runtime = Runtime(**{**runtime.__dict__, "dry_run": True})
    setup_logging(runtime)
    cfg = load_config(runtime.config_path)
    return Agent(cfg, runtime, load_secrets())


# ---- commands -------------------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    pairs = [("config/agent.example.yaml", "config/agent.yaml"), (".env.example", ".env")]
    for src, dst in pairs:
        if Path(dst).exists():
            print(f"exists: {dst}")
        elif Path(src).exists():
            shutil.copy(src, dst)
            print(f"created: {dst}  <- edit this")
        else:
            print(f"missing template: {src}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    agent.run_forever()
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    cfg = agent.cfg
    pillar = args.pillar or cfg.persona.pillars[0].name
    offer = cfg.monetization.offers[0].model_dump() if args.cta and cfg.monetization.offers else None
    for i in range(args.n):
        draft = agent.composer.compose(pillar, args.format, cta_offer=offer, now_local=utcnow().astimezone(cfg.tz))
        print(f"\n===== draft {i + 1} ({pillar}/{args.format}) =====")
        if draft is None:
            print("(no candidate passed the quality bar)")
            continue
        print("\n\n--- next post in thread ---\n\n".join(draft.parts))
        if draft.cta_reply:
            print(f"\n[CTA self-reply] {draft.cta_reply} <link>")
        print(f"\n[editor] {draft.judge_reason}")
    return 0


def cmd_post_now(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    cfg = agent.cfg
    now = utcnow()
    pillar = args.pillar or cfg.persona.pillars[0].name
    local = now.astimezone(cfg.tz)
    cur = agent.db.execute(
        "INSERT INTO slots(date_local,scheduled_at,hour_local,pillar,format,status,updated_at) VALUES(?,?,?,?,?,?,?)",
        (local.date().isoformat(), iso(now), local.hour, pillar, args.format, "pending", iso(now)),
    )
    slot_id = cur.lastrowid
    agent.identity()
    agent.run_slot(agent.db.one("SELECT * FROM slots WHERE id=?", (slot_id,)), now)
    agent.publish_due(utcnow())
    row = agent.db.one("SELECT status, tweet_id, error FROM slots WHERE id=?", (slot_id,))
    print(dict(row) if row else "slot missing")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    text = " ".join(args.text).strip()
    if not text:
        for n in agent.db.query("SELECT id,text,used_count,created_at FROM notes ORDER BY id DESC LIMIT 20"):
            print(f"#{n['id']} (used {n['used_count']}x) {n['text']}")
        return 0
    agent.db.execute("INSERT INTO notes(text,created_at) VALUES(?,?)", (text, iso(utcnow())))
    print("noted")
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    rows = agent.db.query("SELECT * FROM drafts WHERE status='pending' ORDER BY id")
    if not rows:
        print("no pending drafts")
    for d in rows:
        print(agent.draft_preview(d, json.loads(d["payload"])))
        print("-" * 60)
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    edited = " ".join(args.text).strip() if getattr(args, "text", None) else None
    print(agent.decide(args.id, args.action, edited_text=edited or None))
    return 0


def cmd_research(args: argparse.Namespace) -> int:
    from . import research

    agent = build_agent(args)
    local = utcnow().astimezone(agent.cfg.tz)
    research.run_research(agent.db, agent.llm, agent.cfg, local.date())
    for r in agent.db.query("SELECT * FROM research WHERE date_local=? ORDER BY id", (local.date().isoformat(),)):
        print(f"- {r['title']}\n  {r['summary']}\n  angle: {r['angle']}\n  {r['url']}\n")
    return 0


def cmd_mentions(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    agent.process_mentions(utcnow())
    agent.publish_due(utcnow())
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    agent.collect_metrics(utcnow())
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    print(agent.status_text())
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    print(agent.report_text(utcnow()))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    from . import planner

    agent = build_agent(args)
    now = utcnow()
    slots = planner.plan_day(agent.db, agent.cfg, now.astimezone(agent.cfg.tz).date(), now=now)
    for s in slots:
        from .db import parse_iso

        t = parse_iso(s["scheduled_at"]).astimezone(agent.cfg.tz)
        print(f"{t:%H:%M} {s['pillar']:<22} {s['format']:<12} {s['status']}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    runtime = load_runtime()
    setup_logging(runtime)
    ok = True
    try:
        cfg = load_config(runtime.config_path)
        print(f"✔ config {runtime.config_path}: @{cfg.account.handle}, mode={cfg.mode}, "
              f"{cfg.schedule.posts_per_day} posts/day, model={cfg.llm.model}")
    except Exception as e:
        print(f"✘ config: {e}")
        return 1
    secrets = load_secrets()

    import anthropic

    try:
        m = anthropic.Anthropic(api_key=secrets.anthropic_api_key).models.retrieve(cfg.llm.model)
        print(f"✔ Anthropic key works; model {m.id} available")
    except Exception as e:
        ok = False
        print(f"✘ Anthropic: {e}")

    if runtime.dry_run:
        print("• X: DRY RUN mode - nothing will be posted (unset XAGENT_DRY_RUN to go live)")
    else:
        from .db import DB
        from .xapi import make_client

        try:
            db = DB(runtime.db_path)
            me = make_client(secrets, dry_run=False, db=db, handle=cfg.account.handle).me()
            print(f"✔ X credentials work: @{me.get('username')} ({(me.get('public_metrics') or {}).get('followers_count')} followers)")
            if me.get("username", "").lower() != cfg.account.handle.lower():
                ok = False
                print(f"✘ credentials are for @{me.get('username')}, config says @{cfg.account.handle}")
        except Exception as e:
            ok = False
            print(f"✘ X: {e}")

    if runtime.telegram_bot_token and runtime.telegram_chat_id:
        from .telegram import Telegram

        try:
            Telegram(runtime.telegram_bot_token, runtime.telegram_chat_id).send("✅ xagent check: Telegram connected.")
            print("✔ Telegram: test message sent")
        except Exception as e:
            ok = False
            print(f"✘ Telegram: {e}")
    elif cfg.mode == "approval" or cfg.engagement.mode == "approval":
        print("• Telegram not configured: approve drafts with `xagent pending` / `xagent approve ID`")
    return 0 if ok else 1


def cmd_health(args: argparse.Namespace) -> int:
    from .agent import heartbeat_ok

    ok, age = heartbeat_ok(load_runtime())
    print(json.dumps({"ok": ok, "heartbeat_age_s": age}))
    return 0 if ok else 1


# ---- parser ---------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    from .config import FORMATS

    p = argparse.ArgumentParser(prog="xagent", description="Autonomous X growth agent powered by Claude")
    p.add_argument("--dry-run", action="store_true", help="never post; log what would be posted")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create config/agent.yaml and .env from templates").set_defaults(fn=cmd_init)
    sub.add_parser("run", help="run the 24/7 agent").set_defaults(fn=cmd_run)
    sub.add_parser("check", help="validate config and credentials").set_defaults(fn=cmd_check)
    sub.add_parser("status", help="show today's plan, approvals and spend").set_defaults(fn=cmd_status)
    sub.add_parser("plan", help="show (or create) today's posting plan").set_defaults(fn=cmd_plan)
    sub.add_parser("report", help="print the performance report").set_defaults(fn=cmd_report)
    sub.add_parser("research", help="run today's research now").set_defaults(fn=cmd_research)
    sub.add_parser("mentions", help="process mentions once").set_defaults(fn=cmd_mentions)
    sub.add_parser("metrics", help="collect post metrics once").set_defaults(fn=cmd_metrics)
    sub.add_parser("health", help="exit 0 if the daemon heartbeat is fresh").set_defaults(fn=cmd_health)
    sub.add_parser("pending", help="list drafts awaiting approval").set_defaults(fn=cmd_pending)

    d = sub.add_parser("draft", help="write drafts and print them (never posts)")
    d.add_argument("--pillar")
    d.add_argument("--format", default="insight", choices=FORMATS)
    d.add_argument("--cta", action="store_true", help="include a CTA for the first offer")
    d.add_argument("-n", type=int, default=1)
    d.set_defaults(fn=cmd_draft)

    pn = sub.add_parser("post-now", help="draft one post now (approval mode: sends for approval)")
    pn.add_argument("--pillar")
    pn.add_argument("--format", default="insight", choices=FORMATS)
    pn.set_defaults(fn=cmd_post_now)

    n = sub.add_parser("note", help="add a real founder update the agent can post about (no text: list notes)")
    n.add_argument("text", nargs="*")
    n.set_defaults(fn=cmd_note)

    a = sub.add_parser("approve", help="approve a pending draft, optionally with your own text")
    a.add_argument("id", type=int)
    a.add_argument("text", nargs="*")
    a.set_defaults(fn=cmd_decide, action="approve")
    r = sub.add_parser("reject", help="reject a pending draft")
    r.add_argument("id", type=int)
    r.set_defaults(fn=cmd_decide, action="reject")

    args = p.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
