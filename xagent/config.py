"""Configuration: secrets come from the environment, strategy comes from a YAML file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = "config/agent.yaml"


class Pillar(BaseModel):
    name: str
    description: str
    weight: float = 1.0


class Offer(BaseModel):
    name: str
    url: str
    pitch: str
    weight: float = 1.0
    # Affiliate/referral/sponsored links must carry X's "Paid partnership" label (sent as paid_partnership=true).
    affiliate: bool = False


class AccountConfig(BaseModel):
    handle: str
    # Premium accounts may post up to 25k chars; we still default to punchy <=280 posts.
    premium: bool = False
    # Timezone of the AUDIENCE you want to reach. Posting slots are planned in this zone.
    timezone: str = "America/New_York"

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (KeyError, ValueError) as e:  # ZoneInfoNotFoundError is a KeyError
            raise ValueError(f"unknown timezone {v!r}") from e
        return v

    @field_validator("handle")
    @classmethod
    def _strip_at(cls, v: str) -> str:
        return v.lstrip("@").strip()


class PersonaConfig(BaseModel):
    name: str
    bio: str
    audience: str
    voice: str
    pillars: list[Pillar] = Field(min_length=1)
    # Ground truth the agent is allowed to state about you. It must NEVER invent
    # personal metrics, customers, revenue, or events that aren't written here or in notes.
    facts: str = ""
    banned_topics: list[str] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)
    example_posts: list[str] = Field(default_factory=list)


FORMATS = (
    "insight",
    "contrarian",
    "list",
    "story",
    "how_to",
    "question",
    "one_liner",
    "news_take",
    "thread",
    "long_post",  # Premium only: long-form single post (dwell time is a ranking signal)
)


class ScheduleConfig(BaseModel):
    # 3-5 originals/day is the sweet spot: each extra post from the same author in one feed is down-weighted
    # (x0.625, x0.44, ...) by the ranker's author-diversity decay, so more posts != more reach.
    posts_per_day: int = Field(default=4, ge=1, le=20)
    # Local (audience-timezone) hours the planner may pick from. The bandit learns which work.
    candidate_hours: list[int] = Field(default_factory=lambda: [8, 9, 10, 11, 12, 13, 15, 17, 19, 21])
    jitter_minutes: int = Field(default=12, ge=0, le=45)
    min_gap_minutes: int = Field(default=150, ge=15)
    formats: list[str] = Field(default_factory=lambda: list(FORMATS))
    max_threads_per_day: int = Field(default=1, ge=0)
    # A slot that couldn't run within this window after its time (e.g. the host was down) is skipped.
    grace_minutes: int = Field(default=45, ge=5)
    # Local hour for the daily performance report.
    report_local_hour: int = Field(default=22, ge=0, le=23)

    @field_validator("candidate_hours")
    @classmethod
    def _hours(cls, v: list[int]) -> list[int]:
        if not v or any(h < 0 or h > 23 for h in v):
            raise ValueError("candidate_hours must be 0-23")
        return sorted(set(v))

    @field_validator("formats")
    @classmethod
    def _formats(cls, v: list[str]) -> list[str]:
        bad = [f for f in v if f not in FORMATS]
        if bad:
            raise ValueError(f"unknown formats {bad}; allowed: {FORMATS}")
        return v


class EngagementConfig(BaseModel):
    # off: ignore mentions. approval: draft replies and wait for your tap in Telegram.
    # auto: reply autonomously. X's automation rules require X's prior written approval for AI reply
    # bots, so only use "auto" if you have it.
    mode: str = "approval"
    poll_minutes: int = Field(default=15, ge=5)
    max_replies_per_day: int = Field(default=40, ge=0)
    max_replies_per_user_per_day: int = Field(default=3, ge=1)
    # Only answer mentions younger than this. Old threads get no bot necro-replies.
    reply_window_hours: int = Field(default=24, ge=1)
    # Skip accounts younger / smaller than this (cheap spam filter).
    min_author_followers: int = Field(default=0, ge=0)
    ignore_users: list[str] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in {"off", "approval", "auto"}:
            raise ValueError("engagement.mode must be off|approval|auto")
        return v

    @field_validator("ignore_users")
    @classmethod
    def _users(cls, v: list[str]) -> list[str]:
        return [u.lstrip("@").lower() for u in v]


class MonetizationConfig(BaseModel):
    # One in N original posts gets a soft CTA posted as a self-reply (links in the main post are down-ranked).
    cta_every_n_posts: int = Field(default=5, ge=0)
    offers: list[Offer] = Field(default_factory=list)
    utm_source: str = "x"
    utm_medium: str = "social"
    utm_campaign: str = "xagent"


class ResearchConfig(BaseModel):
    enabled: bool = True
    web_search: bool = True
    max_searches: int = Field(default=5, ge=1, le=20)
    local_hour: int = Field(default=6, ge=0, le=23)
    queries: list[str] = Field(default_factory=list)
    rss_feeds: list[str] = Field(default_factory=list)
    max_items: int = Field(default=8, ge=1, le=30)


class LLMConfig(BaseModel):
    model: str = "claude-opus-5"
    effort_compose: str = "high"
    effort_judge: str = "high"
    effort_reply: str = "medium"
    effort_research: str = "medium"
    candidates_per_post: int = Field(default=5, ge=1, le=10)
    use_refusal_fallback: bool = True

    @field_validator("effort_compose", "effort_judge", "effort_reply", "effort_research")
    @classmethod
    def _effort(cls, v: str) -> str:
        if v not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("effort must be low|medium|high|xhigh|max")
        return v


class SafetyConfig(BaseModel):
    # X leadership: "Please stop using hashtags." Grok also labels hashtag abuse as spam.
    max_hashtags: int = Field(default=0, ge=0)
    allow_links_in_main_post: bool = False
    allow_mentions_in_posts: bool = False
    max_similarity: float = Field(default=0.62, gt=0, le=1)
    history_window: int = Field(default=400, ge=10)
    # Keep a margin under X's 280 weighted-char limit.
    max_weighted_length: int = Field(default=275, ge=50, le=25000)
    # Circuit breaker: consecutive X write failures before posting pauses.
    max_consecutive_failures: int = Field(default=4, ge=1)
    pause_minutes_on_breaker: int = Field(default=180, ge=10)
    # Absolute ceiling on X writes (posts + replies) per UTC day, far below X's own limits.
    max_writes_per_day: int = Field(default=60, ge=1, le=500)
    # Sets X's "Made with AI" flag on AI-written posts (not on text you typed yourself in Telegram).
    made_with_ai_label: bool = True


class ApprovalConfig(BaseModel):
    # Drafts are composed this long before their slot so you have time to approve them.
    lead_minutes: int = Field(default=90, ge=5, le=720)
    # Drafts not approved within this window after their slot time expire (stale takes shouldn't go out late).
    timeout_minutes: int = Field(default=240, ge=10)


class BudgetConfig(BaseModel):
    """Hard monthly spend caps. X API is pay-per-use; Claude is billed per token."""

    x_monthly_usd: float = Field(default=30.0, ge=0)
    llm_monthly_usd: float = Field(default=60.0, ge=0)
    # X pay-per-use unit prices (USD, docs.x.com pricing as of Sep 2026). Check the Developer Console.
    x_price_post: float = 0.015
    x_price_link_post: float = 0.20
    x_price_reply: float = 0.010  # "summoned" reply to someone who @mentioned you
    x_price_owned_read: float = 0.001  # per post, own timeline/mentions
    x_price_user_read: float = 0.010
    x_price_manage: float = 0.005


class AgentConfig(BaseModel):
    # approval: every original post waits for a one-tap approval in Telegram (safest for your main account).
    # autopilot: posts publish without review. Use on a clearly labeled automated account.
    mode: str = "approval"
    account: AccountConfig
    persona: PersonaConfig
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    engagement: EngagementConfig = Field(default_factory=EngagementConfig)
    monetization: MonetizationConfig = Field(default_factory=MonetizationConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in {"approval", "autopilot"}:
            raise ValueError("mode must be approval|autopilot")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> AgentConfig:
        if self.schedule.posts_per_day > len(self.schedule.candidate_hours):
            raise ValueError("posts_per_day cannot exceed the number of candidate_hours")
        if self.monetization.cta_every_n_posts and not self.monetization.offers:
            # No offers configured: CTAs are silently disabled rather than failing startup.
            self.monetization.cta_every_n_posts = 0
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.account.timezone)


@dataclass(frozen=True)
class Secrets:
    anthropic_api_key: str | None
    x_api_key: str | None
    x_api_secret: str | None
    x_access_token: str | None
    x_access_token_secret: str | None
    x_bearer_token: str | None
    x_auth_mode: str = "oauth1"
    x_oauth2_client_id: str | None = None
    x_oauth2_client_secret: str | None = None
    x_oauth2_refresh_token: str | None = None

    def x_write_ready(self) -> bool:
        return all([self.x_api_key, self.x_api_secret, self.x_access_token, self.x_access_token_secret])


@dataclass(frozen=True)
class Runtime:
    config_path: Path
    data_dir: Path
    dry_run: bool
    log_level: str
    notify_webhook_url: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    health_port: int | None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "xagent.db"

    @property
    def pause_file(self) -> Path:
        return self.data_dir / "PAUSE"

    @property
    def heartbeat_file(self) -> Path:
        return self.data_dir / "heartbeat"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "xagent.lock"


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def load_secrets() -> Secrets:
    load_dotenv()
    g = os.environ.get
    return Secrets(
        anthropic_api_key=g("ANTHROPIC_API_KEY"),
        x_api_key=g("X_API_KEY"),
        x_api_secret=g("X_API_SECRET"),
        x_access_token=g("X_ACCESS_TOKEN"),
        x_access_token_secret=g("X_ACCESS_TOKEN_SECRET"),
        x_bearer_token=g("X_BEARER_TOKEN"),
        x_auth_mode=(g("X_AUTH_MODE") or "oauth1").lower(),
        x_oauth2_client_id=g("X_OAUTH2_CLIENT_ID") or None,
        x_oauth2_client_secret=g("X_OAUTH2_CLIENT_SECRET") or None,
        x_oauth2_refresh_token=g("X_OAUTH2_REFRESH_TOKEN") or None,
    )


def load_runtime() -> Runtime:
    load_dotenv()
    g = os.environ.get
    port = g("PORT") or g("XAGENT_HEALTH_PORT")
    data_dir = Path(g("XAGENT_DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Runtime(
        config_path=Path(g("XAGENT_CONFIG", DEFAULT_CONFIG_PATH)),
        data_dir=data_dir,
        dry_run=_truthy(g("XAGENT_DRY_RUN")),
        log_level=g("XAGENT_LOG_LEVEL", "INFO").upper(),
        notify_webhook_url=g("NOTIFY_WEBHOOK_URL") or None,
        telegram_bot_token=g("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=g("TELEGRAM_CHAT_ID") or None,
        health_port=int(port) if port else None,
    )


def load_config(path: str | Path) -> AgentConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Config not found at {p}. Copy config/agent.example.yaml to {p} and edit it."
        )
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AgentConfig.model_validate(raw)
