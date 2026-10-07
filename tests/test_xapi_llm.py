import json
from datetime import datetime, timezone
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from tests.conftest import SECRETS
from xagent.config import LLMConfig, Secrets
from xagent.llm import LLM, LLMError, LLMRefusal
from xagent.xapi import (
    LiveXClient,
    OAuth1Provider,
    OAuth2Provider,
    XAuthError,
    XDuplicate,
    XError,
    XForbidden,
    XPaymentRequired,
    XRateLimited,
)


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode()

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, json=None, auth=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "auth": auth})
        return self.responses.pop(0)

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "data": data, "headers": headers})
        return self.responses.pop(0)


def client(responses, db=None):
    s = FakeSession(responses)
    return LiveXClient(OAuth1Provider(SECRETS), db=db, session=s), s


def test_create_post_body_and_cost_buckets(db):
    x, s = client([FakeResponse(201, {"data": {"id": "1"}}), FakeResponse(201, {"data": {"id": "2"}}),
                   FakeResponse(201, {"data": {"id": "3"}})], db=db)
    assert x.create_post("hello", made_with_ai=True) == "1"
    x.create_post("see https://a.com", reply_to="1", paid_partnership=True)
    x.create_post("thanks!", reply_to="99", summoned=True)
    body = s.calls[1]["json"]
    assert body == {"text": "see https://a.com", "reply": {"in_reply_to_tweet_id": "1"}, "paid_partnership": True}
    assert s.calls[0]["json"]["made_with_ai"] is True
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert db.usage_since("x:post", day) == 1
    assert db.usage_since("x:link_post", day) == 1
    assert db.usage_since("x:reply", day) == 1


def test_error_mapping():
    x, _ = client([FakeResponse(403, {"detail": "You are not allowed to create a Tweet with duplicate content."})])
    with pytest.raises(XDuplicate):
        x.create_post("dup")
    x, _ = client([FakeResponse(403, {"title": "Forbidden", "detail": "reply not permitted"})])
    with pytest.raises(XForbidden):
        x.create_post("hi", reply_to="5")
    x, _ = client([FakeResponse(402, {"title": "CreditsDepleted"})])
    with pytest.raises(XPaymentRequired):
        x.create_post("hi")
    x, _ = client([FakeResponse(401, {"title": "Unauthorized"})])
    with pytest.raises(XAuthError):
        x.me()


def test_rate_limit_blocks_until_reset():
    reset = datetime.now(timezone.utc).timestamp() + 600
    x, s = client([FakeResponse(429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": str(reset)})])
    with pytest.raises(XRateLimited):
        x.mentions("42", since_id=None)
    with pytest.raises(XRateLimited):  # short-circuits without another HTTP call
        x.mentions("42", since_id="1")
    assert len(s.calls) == 1


def test_server_errors_retry_reads_but_not_writes(monkeypatch):
    monkeypatch.setattr("xagent.xapi.time.sleep", lambda s: None)
    x, s = client([FakeResponse(503, {}), FakeResponse(200, {"data": {"id": "42", "username": "me"}})])
    assert x.me()["id"] == "42"
    x, s = client([FakeResponse(503, {})])
    with pytest.raises(XError):
        x.create_post("hello")
    assert len(s.calls) == 1


def test_mentions_parsing_and_owned_read_billing(db):
    body = {
        "data": [{"id": "10", "text": "@me hi", "author_id": "7", "created_at": "2026-09-25T10:00:00.000Z",
                  "referenced_tweets": [{"type": "replied_to", "id": "3"}]}],
        "includes": {"users": [{"id": "7", "username": "alice", "public_metrics": {"followers_count": 12},
                                "description": "builder"}]},
    }
    x, s = client([FakeResponse(200, body)], db=db)
    ms = x.mentions("42", since_id="5")
    assert ms[0].author_username == "alice" and ms[0].replied_to_id == "3" and ms[0].author_followers == 12
    assert s.calls[0]["params"]["since_id"] == "5"
    assert db.usage_since("x:owned_read", "2000-01-01") == 1


def test_own_metrics_falls_back_to_public_fields():
    body = {"data": [{"id": "1", "public_metrics": {"like_count": 3, "reply_count": 2, "impression_count": 100}}]}
    x, s = client([FakeResponse(403, {"detail": "not authorized for non_public_metrics"}), FakeResponse(200, body)])
    ms = x.own_metrics("42", start_time=datetime(2026, 9, 20, tzinfo=timezone.utc))
    assert ms[0].likes == 3 and ms[0].impressions == 100
    assert "non_public_metrics" not in s.calls[1]["params"]["tweet.fields"]
    assert s.calls[0]["params"]["exclude"] == "replies,retweets"


def test_fields_param_rename_fallback():
    x, s = client([FakeResponse(400, {"detail": "The query parameter [tweet.fields] is not one of [post.fields]"}),
                   FakeResponse(200, {"data": []})])
    x.mentions("42", since_id=None)
    assert "post.fields" in s.calls[1]["params"] and "tweet.fields" not in s.calls[1]["params"]


def test_oauth2_exchange_refresh_and_retry_on_401(db):
    secrets = Secrets(**{**SECRETS.__dict__, "x_auth_mode": "oauth2", "x_oauth2_client_id": "cid",
                         "x_oauth2_client_secret": "csec"})
    tok = FakeSession([
        FakeResponse(200, {"access_token": "A1", "refresh_token": "R1", "expires_in": 7200}),
        FakeResponse(200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 7200}),
    ])
    provider = OAuth2Provider(secrets, db, session=tok)
    api = FakeSession([FakeResponse(401, {"title": "Unauthorized"}), FakeResponse(200, {"data": {"id": "1"}})])
    x = LiveXClient(provider, db=db, session=api)
    assert x.me()["id"] == "1"
    assert tok.calls[0]["data"]["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert tok.calls[1]["data"] == {"grant_type": "refresh_token", "refresh_token": "R1"}
    assert tok.calls[0]["headers"]["Authorization"].startswith("Basic ")
    assert db.get(OAuth2Provider.KV_KEY)["refresh_token"] == "R2"


# ---- LLM wrapper ---------------------------------------------------------------------------------
class Out(BaseModel):
    answer: str


def _bad_request(msg: str) -> anthropic.BadRequestError:
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(msg, response=httpx2.Response(400, request=req), body=None)


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.kwargs = []

    def _next(self, **kw):
        self.kwargs.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    parse = _next
    create = _next


def fake_client(script):
    msgs = FakeMessages(script)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def resp(stop="end_turn", parsed=None, content=None):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0, server_tool_use=None)
    return SimpleNamespace(stop_reason=stop, parsed_output=parsed, content=content or [], usage=usage,
                           stop_details=SimpleNamespace(category="cyber") if stop == "refusal" else None)


def test_structured_sends_fallbacks_effort_and_cached_system(db):
    c, msgs = fake_client([resp(parsed=Out(answer="ok"))])
    out = LLM(LLMConfig(), client=c, db=db).structured(system="S", user="U", output=Out, effort="high")
    assert out.answer == "ok"
    kw = msgs.kwargs[0]
    assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]
    assert kw["output_config"] == {"effort": "high"} and kw["model"] == "claude-opus-5"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert db.usage_since("llm:input_tokens", "2000-01-01") == 10


def test_structured_degrades_unsupported_features():
    c, msgs = fake_client([_bad_request("effort: not supported on this model"), resp(parsed=Out(answer="ok"))])
    llm = LLM(LLMConfig(model="claude-haiku-4-5"), client=c)
    assert llm.structured(system="S", user="U", output=Out, effort="low").answer == "ok"
    assert "output_config" not in msgs.kwargs[1]


def test_refusal_and_truncation_raise():
    c, _ = fake_client([resp(stop="refusal")])
    with pytest.raises(LLMRefusal):
        LLM(LLMConfig(), client=c).structured(system="S", user="U", output=Out, effort="high")
    c, _ = fake_client([resp(stop="max_tokens")])
    with pytest.raises(LLMError):
        LLM(LLMConfig(), client=c).structured(system="S", user="U", output=Out, effort="high")


def test_web_research_continues_after_pause_turn():
    text = SimpleNamespace(type="text", text="Briefing")
    c, msgs = fake_client([resp(stop="pause_turn", content=[SimpleNamespace(type="server_tool_use")]),
                           resp(content=[text])])
    out = LLM(LLMConfig(), client=c).web_research(system="S", user="U", effort="medium")
    assert out == "Briefing"
    assert msgs.kwargs[1]["messages"][-1]["role"] == "assistant"
    assert msgs.kwargs[0]["tools"][0]["type"] == "web_search_20260209"
