from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from xagent.composer import Candidate, CandidateScore, CandidateSet, Judgement, ReplyDecision
from xagent.config import AgentConfig, Runtime, Secrets
from xagent.db import DB
from xagent.research import NewsBrief, NewsItem
from xagent.xapi import DryRunXClient, Mention, TweetMetrics

ROOT = Path(__file__).resolve().parents[1]

WORDS = (  # noqa: SIM905
    "latency evals pricing churn onboarding retries caching agents prompts tokens vectors routing "
    "benchmarks funnels cohorts invoices webhooks queues schemas migrations dashboards alerts "
    "founders customers interviews roadmap pivot runway hiring focus shipping deadlines refactor "
    "deploys rollbacks canaries metrics tracing logging budgets quotas margins discounts annual "
    "monthly trials signups activation referrals partnerships newsletters podcasts threads essays "
    "writing clarity leverage constraints tradeoffs defaults patterns heuristics failures lessons "
    "experiments hypotheses surveys feedback support tickets docs tutorials templates starters kits"
).split()


class FakeLLM:
    """Deterministic stand-in for the Claude wrapper. Produces varied, guardrail-clean text."""

    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.calls: list[tuple[str, str]] = []
        self.reply_action = "reply"
        self.publishable = True
        self.raise_on: set[str] = set()

    def _sentence(self, n: int = 14) -> str:
        words = self.rng.sample(WORDS, n)
        return (" ".join(words).capitalize() + ".").replace("  ", " ")

    def structured(self, *, system: str, user: str, output, effort: str, max_tokens: int = 16000):
        self.calls.append((output.__name__, user))
        if output.__name__ in self.raise_on:
            from xagent.llm import LLMError

            raise LLMError("forced failure")
        if output is CandidateSet:
            cands = []
            for _ in range(3):
                thread = [self._sentence() for _ in range(4)] if "Thread:" in user else []
                cands.append(Candidate(
                    text=thread[0] if thread else self._sentence(),
                    thread=thread,
                    cta_reply="If this was useful, I go deeper every week here:" if "CTA REQUESTED" in user else "",
                    hook_type="specific claim",
                    reply_bait="people will share their own numbers",
                ))
            return CandidateSet(candidates=cands)
        if output is Judgement:
            return Judgement(
                scores=[CandidateScore(index=0, hook=8, reply_potential=8, specificity=8, authenticity=9,
                                       negative_risk=1, notes="good")],
                best_index=0, final_text="", final_thread=[], final_cta_reply="", reason="best hook",
                publishable=self.publishable,
            )
        if output is ReplyDecision:
            return ReplyDecision(action=self.reply_action, category="question", reason="genuine question",
                                 reply="Good question. Start with ten real user inputs as evals, then iterate.")
        if output is NewsBrief:
            return NewsBrief(items=[NewsItem(title="New model released", summary="A lab released a model.",
                                             angle="Cheaper evals", url="https://example.com/news")])
        raise AssertionError(f"unexpected output type {output}")

    def web_research(self, *, system: str, user: str, effort: str, max_searches: int = 5) -> str:
        return "Briefing: a lab released a model."


class FakeX(DryRunXClient):
    """Dry-run client with scriptable mentions, metrics and failures."""

    def __init__(self):
        super().__init__("tester")
        self.dry_run = False  # behave like a live client for the agent's logic
        self.mention_queue: list[Mention] = []
        self.metric_rows: list[TweetMetrics] = []
        self.fail_next: list[Exception] = []
        self.fail_replies: Exception | None = None
        self.me_calls = 0

    def me(self) -> dict:
        self.me_calls += 1
        return {"id": "42", "username": "tester", "public_metrics": {"followers_count": 1000, "following_count": 10,
                                                                     "tweet_count": 5}}

    def create_post(self, text, *, reply_to=None, paid_partnership=False, made_with_ai=False, summoned=False):
        if self.fail_next:
            raise self.fail_next.pop(0)
        if reply_to and self.fail_replies and not summoned:
            raise self.fail_replies
        tid = str(1_000_000 + len(self.posted))
        self.posted.append({"id": tid, "text": text, "reply_to": reply_to, "paid_partnership": paid_partnership,
                            "made_with_ai": made_with_ai, "summoned": summoned})
        return tid

    def mentions(self, user_id, *, since_id, max_results=50):
        out, self.mention_queue = self.mention_queue, []
        return out

    def own_metrics(self, user_id, *, start_time: datetime):
        return list(self.metric_rows)


def load_example_config(**overrides) -> AgentConfig:
    raw = yaml.safe_load((ROOT / "config" / "agent.example.yaml").read_text())
    for dotted, value in overrides.items():
        node = raw
        keys = dotted.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return AgentConfig.model_validate(raw)


@pytest.fixture
def cfg() -> AgentConfig:
    return load_example_config()


@pytest.fixture
def runtime(tmp_path: Path) -> Runtime:
    return Runtime(config_path=ROOT / "config" / "agent.example.yaml", data_dir=tmp_path, dry_run=False,
                   log_level="INFO", notify_webhook_url=None, telegram_bot_token=None, telegram_chat_id=None,
                   health_port=None)


@pytest.fixture
def db(tmp_path: Path) -> DB:
    return DB(tmp_path / "test.db")


SECRETS = Secrets(anthropic_api_key="x", x_api_key="k", x_api_secret="s", x_access_token="t",
                  x_access_token_secret="ts", x_bearer_token=None)


def make_agent(cfg, runtime, db, *, x=None, llm=None, telegram=None, seed=1):
    from xagent.agent import Agent

    return Agent(cfg, runtime, SECRETS, db=db, x=x or FakeX(), llm=llm or FakeLLM(), telegram=telegram,
                 rng=random.Random(seed))
