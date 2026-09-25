"""Month-to-date spend estimation and hard caps for X API and Claude usage."""

from __future__ import annotations

from datetime import datetime

from .config import AgentConfig
from .db import DB, utcnow

# USD per million tokens (input, output). Cache reads bill at 0.1x input, cache writes at 1.25x input.
LLM_PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}
WEB_SEARCH_PRICE = 10.0 / 1000


def month_start(now: datetime | None = None) -> str:
    now = now or utcnow()
    return now.strftime("%Y-%m-01")


def llm_spend(db: DB, cfg: AgentConfig, since_day: str | None = None) -> float:
    since_day = since_day or month_start()
    p_in, p_out = LLM_PRICES.get(cfg.llm.model, (5.0, 25.0))
    u = lambda b: db.usage_since(b, since_day)  # noqa: E731
    return (
        u("llm:input_tokens") * p_in / 1e6
        + u("llm:output_tokens") * p_out / 1e6
        + u("llm:cache_read_tokens") * p_in * 0.1 / 1e6
        + u("llm:cache_write_tokens") * p_in * 1.25 / 1e6
        + u("llm:web_searches") * WEB_SEARCH_PRICE
    )


def x_spend(db: DB, cfg: AgentConfig, since_day: str | None = None) -> float:
    since_day = since_day or month_start()
    b = cfg.budget
    prices = {
        "x:post": b.x_price_post,
        "x:link_post": b.x_price_link_post,
        "x:reply": b.x_price_reply,
        "x:owned_read": b.x_price_owned_read,
        "x:user_read": b.x_price_user_read,
        "x:manage": b.x_price_manage,
    }
    return sum(db.usage_since(bucket, since_day) * price for bucket, price in prices.items())


def x_allows(db: DB, cfg: AgentConfig, cost: float) -> bool:
    return x_spend(db, cfg) + cost <= cfg.budget.x_monthly_usd


def llm_allows(db: DB, cfg: AgentConfig) -> bool:
    return llm_spend(db, cfg) < cfg.budget.llm_monthly_usd
