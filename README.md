# AIDeva · xagent

**An X (Twitter) growth agent that runs 24/7.** It writes and posts to your account on a schedule, drafts replies to people who mention you, learns which topics, formats and hours earn engagement, and moves attention toward the things that actually make money: your product, your newsletter, your offers.

Built on Claude (`claude-opus-5`) and the **official X API only**, with the ranking rules taken from X's open-sourced 2026 algorithm. Read [`docs/PLAYBOOK.md`](docs/PLAYBOOK.md) for the research behind every default.

---

## What it does

| | |
|---|---|
| 🗓 **Plans every day** | Picks 3–5 posting slots at least 2.5h apart, in your *audience's* timezone, with human-like jitter. A Thompson-sampling bandit learns which hours, topics and formats work, then shifts toward them. |
| ✍️ **Writes like an editor** | Claude drafts 5 candidates per slot. Deterministic checks throw out anything with hashtags, bait, links, AI clichés, placeholders, or near-duplicates of past posts. A second Claude pass then scores the survivors on hook, reply potential, specificity, authenticity and the risk of negative feedback, picks one and polishes it. |
| 📰 **Stays timely** | Each morning Claude searches the web and your RSS feeds for fresh news in your niche. That becomes material for `news_take` posts. |
| 🧠 **Learns** | It reads your own posts' metrics (cheap "Owned Reads"), scores them with X's production ranking weights, feeds the bandit, and shows Claude your best and worst posts so later drafts improve. |
| 💬 **Engages** | Drafts replies to people who @mention you, which is the only kind of reply X's API allows. Spam, hostile messages and bots are skipped. There are daily and per-person caps. |
| 💸 **Monetizes** | One post in N gets a soft call-to-action as a self-reply, with UTM tracking. Affiliate links get X's *Paid partnership* label automatically. |
| 📱 **One-tap control** | A Telegram bot lets you ✅ / ❌ / 🔁 each draft, reply with your own wording, send `/note` with real updates, and use `/pause`, `/resume` and `/status`. You also get a daily report. |
| 🛡 **Built for 24/7** | State lives in SQLite so restarts never double-post. It also has a circuit breaker, monthly spend caps for X and Claude, a daily write cap, a kill switch, a health endpoint, graceful shutdown, a single-instance lock, and auto-pause with an alert on auth, credit or policy errors. |

## Honest money math (read this first)

- **X's own payouts won't pay this bot.** In Sep 2026 X replaced Creator Revenue Sharing with *Original Content Rewards*. Its terms exclude content "created or posted using automated means". It also requires Premium, 500 verified followers and 500K verified impressions in 90 days. Treat any payout as a bonus earned by posts you write yourself.
- **The money is in using X as distribution for something you own:** a product, a newsletter, a paid template or kit, consulting, or affiliate offers. The agent is built for that. Most posts deliver value, and 1 in 5 carries a soft CTA to your offer.
- **Realistic ranges** (estimates, not guarantees): a consistent, genuinely useful niche account usually takes 3–6 months to reach 1–5K followers and 100–500K impressions a month. At about 0.2–1% link click-through and a decent offer, that's hundreds to low thousands of dollars a month. Content quality and a real offer decide the outcome; automation only multiplies it.
- **The single biggest lever the agent can't replace** is 20–30 minutes a day of *you* replying to bigger accounts in your niche. Since 2026, X bans automating that.

## How it works

```
 ┌──────────── every 20s tick (SQLite-backed, restart-safe) ────────────┐
 │                                                                       │
 │  plan day ──► slot due? ──► research + notes + best/worst posts       │
 │   (bandit)                    │                                       │
 │                               ▼                                       │
 │                   Claude: 5 candidates ──► guardrails ──► Claude judge│
 │                                                             │         │
 │              approval mode: Telegram ✅/❌/🔁/edit ◄──────────┤         │
 │              autopilot:     auto-approved ◄─────────────────┘         │
 │                               │                                       │
 │                               ▼ at slot time                          │
 │   budget + caps ──► POST /2/tweets ──► thread parts / CTA self-reply  │
 │                                                                       │
 │  mentions (15 min) ──► skip rules ──► Claude reply draft ──► approval │
 │  metrics (3 h) ──► Phoenix-weighted reward ──► bandit + prompt memory │
 │  daily: research (06:00) · report (22:00) · follower snapshot         │
 └───────────────────────────────────────────────────────────────────────┘
```

---

## Quick start (dry run, 5 minutes, no X keys needed)

```bash
git clone <this repo> && cd AIDeva
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
xagent init                      # creates config/agent.yaml and .env
# put ANTHROPIC_API_KEY in .env (XAGENT_DRY_RUN=1 is already set)
xagent check                     # validates config + keys
xagent draft --format insight -n 3      # see what it writes (never posts)
xagent draft --format news_take --cta   # with a CTA for your first offer
xagent run                       # full loop, logs what it WOULD post
```

Edit `config/agent.yaml` until the drafts sound like you. The fields that matter most:
- **`persona.voice` and `persona.example_posts`:** how you sound.
- **`persona.facts`:** the only personal claims it may make. It never invents revenue, customers or anecdotes.
- **`persona.pillars`:** what you post about.
- **`monetization.offers`:** where the money goes.

## Going live

1. **X API** ([console.x.com](https://console.x.com)). Pay-per-use: roughly $10–15 a month at 4 posts a day.
   1. Create the developer app **while logged in as the account that will post**. That makes mention and metric reads "Owned Reads" at $0.001 each.
   2. Set *User authentication → App permissions* to **Read and write** *before* generating tokens.
   3. Generate the **Access Token and Secret**. Put them in `.env` along with the API Key and Secret.
   4. Buy credits ($10 is plenty to start), and set a spending limit and auto-recharge.
2. **Telegram** (strongly recommended):
   1. Create a bot with @BotFather.
   2. Message it once.
   3. Get your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.
   4. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Only that chat can control the agent.
3. **Compliance setup** (do it, it protects your account):
   - **Approval mode on your main account** (the default): the agent works like Typefully or Hypefury. It drafts, you approve.
   - **Autopilot** only on an account with X's **Automated** label, set at *Settings → Your account → Account information → Automation*. Its bio must also say who runs it, e.g. "Automated by @you".
   - Keep `engagement.mode: approval`. X requires **prior written approval** for AI-generated replies, and unattended auto-replies are limited to one per person.
4. Set `XAGENT_DRY_RUN=0`, run `xagent check`, then deploy (below).

## Deploy (24/7)

**Docker (any VPS, about $5/month):**
```bash
cp config/agent.example.yaml config/agent.yaml   # edit
cp .env.example .env                             # fill in
docker compose up -d --build
docker compose logs -f
```
State persists in the `xagent-data` volume. The container healthcheck runs `xagent health`.

**systemd (no Docker):** see [`deploy/xagent.service`](deploy/xagent.service).

**Railway / Render / Fly.io:** deploy the Dockerfile as a *worker* with a persistent volume mounted at `/data`. Put the `.env` values in the platform's secrets. Setting `PORT` (or `XAGENT_HEALTH_PORT`) exposes `GET /healthz`. Run exactly one instance: a lock file enforces it per volume.

## Your daily routine (15–30 min, where growth actually comes from)

1. **Approve drafts** as they arrive (✅ / ❌ / 🔁, or reply with your own wording).
2. **Feed it reality:** `/note Shipped usage-based billing, 3 customers upgraded in the first hour`. Real numbers and stories are the best-performing and the only honest build-in-public material.
3. **Reply by hand** to 5–10 posts from bigger accounts in your niche. This is the #1 growth lever for small accounts, and X bans automating it.
4. Read the 22:00 report. Every few weeks, cut what flops and double down on what the bandit favours.

## Commands

| Command | What it does |
|---|---|
| `xagent run` | the 24/7 daemon |
| `xagent check` | validate config, Claude key, X credentials, Telegram |
| `xagent draft [--format F] [--pillar P] [--cta] [-n N]` | write drafts and print them (never posts) |
| `xagent post-now [--format F]` | draft a post now (sent for approval in approval mode) |
| `xagent note "..."` / `xagent note` | add or list real founder updates |
| `xagent pending` · `approve ID [text]` · `reject ID` | approve without Telegram |
| `xagent plan` · `status` · `report` | today's slots, state and spend, performance |
| `xagent research` · `mentions` · `metrics` | run one job now |
| `xagent health` | exit 0 if the daemon's heartbeat is fresh |

Kill switch: `touch data/PAUSE` (or send `/pause` in Telegram).

## Costs (defaults)

| | Monthly |
|---|---|
| X API: ~120 posts at $0.015, ~25 CTA link replies at $0.20, replies at $0.01, Owned Reads at $0.001 | ≈ $10–15 |
| Claude (Opus 5): ~2 calls per post + replies + daily research, with prompt caching | ≈ $20–50 |
| X Premium (recommended: reach, long posts, reply priority) | $8 |

Hard caps are set in `budget.x_monthly_usd` and `budget.llm_monthly_usd`. When a cap is hit, the agent stops that activity and alerts you.

## Built-in safety

- Official X API only. No scraping, browser automation, auto-likes, auto-follows, DMs, or keyword-triggered replies.
- Never states personal facts that aren't in `persona.facts` or your notes.
- Guardrails: no hashtags or @mentions, no engagement bait, no fake "🚨 BREAKING", no placeholders, no near-duplicates (X labels copypasta as spam), and weighted-length checks.
- A post in flight when the process crashes is **never re-posted**; you get an alert instead.
- Errors map to actions:

  | Error | Action |
  |---|---|
  | 401 | pause + alert |
  | 402 (out of credits) | pause + alert |
  | 403 | circuit breaker |
  | 429 | wait for the reset |
  | Duplicate post | skip |
  | Self-reply refused | inline CTAs, threads disabled |

- *Made with AI* flag on AI-written posts (on by default; posts you typed yourself aren't labeled).
- Paid-partnership flag on affiliate offers.

## Development

```bash
pip install -e ".[dev]"
ruff check . && pytest        # 59 tests: X client, Claude wrapper, planner, bandit, guardrails, full agent loop
```

Layout: `xagent/agent.py` (runtime loop) · `composer.py` (drafting + judge) · `playbook.py` (algorithm knowledge) · `xapi.py` (X API v2, OAuth 1.0a/2.0) · `llm.py` (Claude) · `planner.py` + `bandit.py` (learning schedule) · `engage.py` (mentions) · `metrics.py` (rewards) · `research.py` · `guardrails.py` · `budget.py` · `telegram.py` · `db.py`.

## License

MIT
