# The X Growth & Monetization Playbook (Sep 2026)

This is the research behind xagent's defaults. It covers:
- how X ranks posts, read from the open-sourced code;
- what X's rules allow for automated accounts;
- where the money realistically comes from.

**Evidence labels:**
- **[code]**: verified in source code.
- **[official]**: from X's docs or X leadership.
- **[data]**: third-party studies.
- **[estimate]**: my own judgement.

---

## 1. How X ranks posts in 2026 (reverse-engineered from the source)

The 2023 `twitter/the-algorithm` weights are **obsolete**. That includes the widely quoted "author reply = 75x", "reply = 13.5" and the "Blue ×4 boost". Production *For You* now runs on **xai-org/x-algorithm**:
- The ranker is "Phoenix", a Grok-architecture transformer served by a Rust home-mixer.
- It was open-sourced on 2026-01-20. The production weights were published on 2026-08-13 and are synced to the repo continuously.
- This research used the repo at commit `bf7db1b` (2026-09-25).

### The scoring formula **[code]**
`score = Σ weight_i × P(action_i)`, where P is the model's *predicted probability* that this viewer takes the action. Source: `home-mixer/params/param.rs`.

| Action | Weight | | Action | Weight |
|---|---:|---|---|---:|
| Like | 0.5 | | Share via copy-link | **20** |
| Reply | **5** (+15 if mutual follow) | | Follow author | **4** |
| Quote | **5** | | Click | 0.4 |
| Repost | 1 | | Open link | 0.2 |
| Share | 2 | | Dwell | 0.05 (+0.004/s) |
| Share via DM | **5** | | Profile click | 0 |
| Not interested | **−43.2** | | Mute author | **−58.8** |
| Block author | **−31.2** | | Report | **−234** |

After scoring, several adjustments apply **[code]**:
- **Author diversity:** your 2nd post in someone's feed is multiplied by 0.625, the 3rd by 0.44, with a floor of 0.25. Posting more often ≠ more reach.
- **Out-of-network:** scores are multiplied by 0.75.
- **Cold start:** one slot per feed request goes to an original post from an author with **≤1,000 followers**, fewer than 1,000 impressions, and less than 48h old. Small accounts get a real shot.
- **Diversity re-ranking:** a DPP step spreads similar posts apart, so sameness is penalized.
- **What reaches non-followers:** only **original posts**. Replies, reposts and community posts are skipped. Posts age out after **48h**.
- **Threads:** only the highest-scoring branch of a conversation is shown.

### Grok reads every post **[code]**
Every post, along with the author's bio, verification tier and follower counts, is embedded with the prompt *"Represent this X post for recommendation: capture its topic, intent, key entities, and likely engagement."*
- A Grok safety classifier labels a range of spam types:
  - engagement baiting and engagement farming;
  - hashtag abuse and mention abuse;
  - bot-produced spam and scam links.
- Engagement bait and farming get `SpamHighRecall`, which means **no out-of-network reach, even for high-reputation accounts**.
- Near-duplicate text gets **`COPYPASTA_SPAM`**.
- Posts with **2 or more @mentions** get an extra real-time spam scan.
- A behavioural bot detector flags mechanical timing ("burstiness, mechanical cadence").

### Links, hashtags, Premium
- **Links:** there is no link penalty in the ranking code, and opening a link is weighted +0.2 **[code]**. Musk and Bier (July 2026) say links haven't been penalized "for over a year" **[official]**. Buffer's data still shows link posts underperforming, sharply so for non-Premium accounts **[data]**. Via the API a link post also costs **$0.20 vs $0.015**. → The agent keeps links out of the main post and puts them in a self-reply.
- **Hashtags:** Musk in Dec 2024: "Please stop using hashtags… they look ugly" **[official]**. Hashtag abuse is also a Grok spam label **[code]**. → The default is 0 hashtags.
- **Premium:** there is no explicit multiplier in the 2026 code. It helps through reputation, since the UserCred PageRank seeds half its teleport weight to Premium/verified accounts **[code]**. It also gives reply prioritization **[official]**. Buffer measured median impressions per post: Free <100, Premium >600, Premium+ >1,500 **[data]**. → Get Premium ($8/month).

### What xagent encodes from this
| Finding | Implementation |
|---|---|
| Replies, quotes and shares ≫ likes | Prompts and the judge optimize for "would reply / quote / send to a friend". The reward uses Phoenix weights. |
| Negative feedback is brutal | The judge scores `negative_risk`. No dunks, no rage bait, stay on niche. |
| Engagement bait → spam label | Banned-phrase guardrail (like/RT if, reply with, follow for more, 🚨 BREAKING…) |
| Copypasta and diversity penalties | Similarity check against the last 400 posts; recent posts shown to Claude to avoid repeats |
| Author diversity decay | 4 posts/day, ≥150 min apart, jittered times |
| Only originals travel | Full value in the main post; links and CTAs in a self-reply |
| Dwell counts | "First line must earn the stop" rule; Premium `long_post` format |
| Posts die at 48h | Daily web research feeds `news_take` posts |
| Niche coherence (bio embedded) | Fixed pillars, persona and voice in a cached system prompt |

## 2. Timing & frequency **[data]**

| Source | Recommendation |
|---|---|
| Buffer (8.7M posts) | Best slots Tue 9am and Wed 9–10am |
| Sprout (~2B engagements) | Tue–Thu 12–6pm local |
| Hootsuite | 9–11am Wed–Fri |

- Buffer also recommends 3–5 posts a day.
- Consistency matters: accounts posting in 20+ of 26 weeks got ~450% more engagement per post.
- Text posts have the highest median engagement rate on X (3.56%).

→ The candidate hours are 8am–9pm in the audience's timezone. The bandit learns your actual best hours from your own metrics.

## 3. The rules for automated accounts **[official]**

From X's developer guidelines, automation rules (updated Apr 2026) and the 2026 API changes.

**Allowed**
- Scheduled original posts.
- Replying to your own posts *(not officially confirmed; xagent detects a refusal and adapts)*.
- Replying to people who @mentioned you or replied to you. Unattended auto-replies are limited to 1.

**Needs X's prior approval**
- **AI-generated replies.** That's why `engagement.mode` defaults to `approval`.

**Required for bots**
- The **"Automated" label** on the profile.
- A bio disclosure of who runs it.
- An easy opt-out.

**Prohibited** (suspension)

| Area | Prohibited |
|---|---|
| Likes and follows | Auto-likes; auto-follow, follow-back or unfollow churn |
| Replies | Keyword-triggered replies; auto-replies to strangers; affiliate links in replies to random posts |
| Other contact | Auto-DMs, including welcome DMs; bulk list-adds |
| Trends | Posting to trending topics |
| Duplicates and accounts | Duplicate content across accounts; account farms |
| Access method | Scraping or browser automation (permanent suspension) |
| Data | Using X data to train models |

**API realities (2026)**
- Pricing is pay-per-use (credits). The free tier is gone.
- API replies only work if the author mentioned or quoted you.
- Quote posts are Enterprise-only.
- Out of credits returns **402**.
- OAuth 1.0a is "being retired", with no date announced. xagent supports OAuth 2.0 via token exchange (`X_AUTH_MODE=oauth2`).

**Enforcement climate**
- Oct 2025: 1.7M reply-spam bots removed.
- Feb 2026, Bier: *"If a human is not tapping on the screen, the account and all associated accounts will likely be suspended."*
- May 2026: suspensions for AI replies began.
- Jul 2026: 42,000 chatbot-reply accounts removed.

→ **Run approval mode on any account you care about.** Autopilot belongs on a clearly labeled automated brand account.

## 4. Where the money comes from

| Path | Requirements | Realistic for a new account | Notes |
|---|---|---|---|
| **Your product / SaaS** | An offer + a CTA funnel | Highest ceiling, slowest start | X is the launch channel; build in public with real numbers |
| **Newsletter → sponsors + launches** | Lead magnet + email tool | Sponsors viable at ~2–5K engaged subs ($50–150 CPM) **[data]** | The best asset: you own the audience |
| **Digital product** ($29–79 kit, template or course) | Gumroad / Lemon Squeezy / Stripe | First $ in weeks if the audience fits **[estimate]** | Fastest path to first revenue |
| **Affiliate** (AI tools, 20–30% recurring) | Paid-partnership label (auto) + FTC disclosure | Modest add-on | No finance, crypto or gambling partnerships allowed |
| **X Original Content Rewards** | Premium, 500 verified followers, 500K verified impressions in 90d | **$0 for automated posts** (excluded) | Only posts you write and publish yourself qualify |
| **X Creator Subscriptions** | 2,000 verified followers, 5M impressions in 3 months | Later stage | Subscriber-only threads |

**Reference points [data]:**
- @levelsio (one of the largest indie-hacker accounts): ~$10–12K per 28 days from X payouts in 2025–26.
- @marclou: ~$4K/month from X, out of ~$84K/month total income. The rest came from his products.
- The lesson: X payouts are the tip; owned products are the iceberg.

### A 90-day plan **[estimate]**
1. **Week 0–1:**
   - Get Premium and optimize the bio (who you help plus a proof point). Pin your best post with a link to a free lead magnet.
   - Configure persona, facts and offers. Dry-run drafts until they sound like you.
2. **Week 1–4:**
   - Approval mode, 4 posts a day.
   - `/note` real updates daily.
   - 20–30 min a day of manual replies to 10–20 bigger accounts in your niche.
   - Ship one small paid product ($29–79).
3. **Month 2–3:**
   - Let the bandit steer.
   - Retire pillars and formats that flop.
   - Turn your top posts into newsletter issues.
   - Launch or promote with 1-in-5 CTAs.
4. **Expect:** 1–5K followers and 100–500K impressions a month by month 3–6 if the content is genuinely useful. Revenue is then hundreds to low thousands a month from owned offers. Most accounts do worse; consistency and a real offer are what separate the ones that don't.

## Sources
- xai-org/x-algorithm @ `bf7db1b`:
  - `home-mixer/params/param.rs`
  - `vm-ranker/params.rs`
  - `xai-value-model/scoring.rs`
  - `home-mixer/scorers/author_cold_start.rs`
  - `grox/flows/ptos/*`
  - `botmaker-rules/scarecrow/bot/*`
  - `user-cred-v2/UserCredV2App.scala`
  - `bdsm/README.md`
- twitter/the-algorithm-ml `projects/home/recap/README.md` (2023 legacy weights)
- docs.x.com (source: github.com/xdevplatform/docs, Sep 2026):
  - pricing
  - rate limits
  - manage-tweets/integrate (reply restriction)
  - developer-guidelines
  - media upload
  - OAuth 1.0a → 2.0 token exchange
- help.x.com:
  - x-automation
  - original-content-rewards
  - paid-partnerships-policy
  - automated-account-labels
  - x-limits
- TechCrunch:
  - 2026-08-08 (Original Content Rewards)
  - 2026-09-02 (X Money payouts)
  - 2026-04-12 (clickbait payout cuts)
- Buffer studies:
  - Premium reach
  - links on X
  - best time to post
  - 2026 engagement report
  - posting frequency
- Posts by @nikitabier and @elonmusk:
  - links, 2025-04-25 and 2026-07-28
  - hashtags, 2024-12-17
  - automation, 2026-02-14 and 2026-07-24
