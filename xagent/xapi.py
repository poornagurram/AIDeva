"""X API v2 client. Official API only: no scraping, no browser automation, no unofficial endpoints.

Pricing model (pay-per-use, Sep 2026): writes are billed per request, reads per resource returned, with
per-UTC-day de-duplication. We only use "Owned Read" endpoints ($0.001/post) for mentions and metrics.
"""

from __future__ import annotations

import base64
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import requests
from requests_oauthlib import OAuth1

from .config import Secrets
from .db import DB
from .textutil import find_urls

log = logging.getLogger(__name__)

API_BASE = "https://api.x.com/2"
TOKEN_URL = f"{API_BASE}/oauth2/token"
_ID_RE = re.compile(r"/\d+")

MENTION_TWEET_FIELDS = "author_id,conversation_id,created_at,in_reply_to_user_id,referenced_tweets,lang"
MENTION_USER_FIELDS = "username,name,public_metrics,verified,created_at,description"
METRIC_FIELDS_FULL = "public_metrics,non_public_metrics,organic_metrics,created_at"
METRIC_FIELDS_PUBLIC = "public_metrics,created_at"


# ---- errors -------------------------------------------------------------------------------------
class XError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class XAuthError(XError):
    """401: credentials invalid/revoked, or the app lacks write permission."""


class XPaymentRequired(XError):
    """402: pay-per-use credits depleted. Top up in the X Developer Console."""


class XForbidden(XError):
    """403: account restricted, spend cap hit, reply not 'summoned', or other policy block."""


class XDuplicate(XForbidden):
    """403: X refused the post because the same text was already posted."""


class XRateLimited(XError):
    def __init__(self, message: str, reset_at: float, **kw: Any):
        super().__init__(message, **kw)
        self.reset_at = reset_at


# ---- data ---------------------------------------------------------------------------------------
@dataclass
class Mention:
    id: str
    text: str
    author_id: str
    author_username: str
    author_followers: int
    created_at: str
    conversation_id: str | None
    in_reply_to_user_id: str | None
    replied_to_id: str | None
    is_retweet: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class TweetMetrics:
    tweet_id: str
    created_at: str | None
    impressions: int = 0
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    quotes: int = 0
    bookmarks: int = 0
    profile_clicks: int = 0
    url_clicks: int = 0


class XClient(Protocol):
    dry_run: bool

    def me(self) -> dict: ...
    def create_post(self, text: str, *, reply_to: str | None = None, paid_partnership: bool = False,
                    made_with_ai: bool = False, summoned: bool = False) -> str: ...
    def delete_post(self, tweet_id: str) -> None: ...
    def mentions(self, user_id: str, *, since_id: str | None, max_results: int = 50) -> list[Mention]: ...
    def own_metrics(self, user_id: str, *, start_time: datetime) -> list[TweetMetrics]: ...


def _parse_metrics(t: dict) -> TweetMetrics:
    pub = t.get("public_metrics") or {}
    org = t.get("organic_metrics") or {}
    npm = t.get("non_public_metrics") or {}
    return TweetMetrics(
        tweet_id=str(t["id"]),
        created_at=t.get("created_at"),
        impressions=int(org.get("impression_count") or npm.get("impression_count") or pub.get("impression_count") or 0),
        likes=int(pub.get("like_count") or 0),
        replies=int(pub.get("reply_count") or 0),
        reposts=int(pub.get("retweet_count") or 0),
        quotes=int(pub.get("quote_count") or 0),
        bookmarks=int(pub.get("bookmark_count") or 0),
        profile_clicks=int(npm.get("user_profile_clicks") or org.get("user_profile_clicks") or 0),
        url_clicks=int(npm.get("url_link_clicks") or org.get("url_link_clicks") or 0),
    )


# ---- auth ---------------------------------------------------------------------------------------
class OAuth1Provider:
    """OAuth 1.0a user context. Console-generated tokens never expire: simplest for a single-account daemon."""

    def __init__(self, s: Secrets):
        if not s.x_write_ready():
            raise XAuthError("X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN and X_ACCESS_TOKEN_SECRET are required")
        self._auth = OAuth1(s.x_api_key, client_secret=s.x_api_secret,
                            resource_owner_key=s.x_access_token, resource_owner_secret=s.x_access_token_secret)

    def auth(self) -> Any:
        return self._auth

    def on_unauthorized(self) -> bool:
        return False


class OAuth2Provider:
    """OAuth 2.0 user tokens with automatic refresh (2h access tokens, single-use ~6-month refresh tokens).

    Bootstraps either from X_OAUTH2_REFRESH_TOKEN or by exchanging the OAuth 1.0a token pair
    (POST /2/oauth2/token, token-exchange grant). The newest pair is persisted before it is used.
    """

    KV_KEY = "x_oauth2_tokens"

    def __init__(self, s: Secrets, db: DB, *, session: requests.Session | None = None):
        if not s.x_oauth2_client_id:
            raise XAuthError("X_OAUTH2_CLIENT_ID is required for X_AUTH_MODE=oauth2")
        self.s = s
        self.db = db
        self.http = session or requests.Session()

    def _token_request(self, data: dict[str, str]) -> dict:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.s.x_oauth2_client_secret:
            raw = f"{self.s.x_oauth2_client_id}:{self.s.x_oauth2_client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        else:
            data = {**data, "client_id": self.s.x_oauth2_client_id or ""}
        r = self.http.post(TOKEN_URL, data=data, headers=headers, timeout=30)
        body = _safe_json(r) or {}
        if r.status_code != 200 or "access_token" not in body:
            raise XAuthError(f"OAuth2 token request failed ({r.status_code}): {_error_text(body) or r.text[:200]}",
                             status=r.status_code)
        tokens = {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token") or data.get("refresh_token"),
            "expires_at": time.time() + int(body.get("expires_in") or 7200),
        }
        self.db.set(self.KV_KEY, tokens)  # persist first: refresh tokens are single-use
        return tokens

    def _bootstrap(self) -> dict:
        if self.s.x_oauth2_refresh_token:
            return self._token_request({"grant_type": "refresh_token", "refresh_token": self.s.x_oauth2_refresh_token})
        if self.s.x_access_token and self.s.x_access_token_secret:
            return self._token_request({
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": self.s.x_access_token,
                "subject_token_type": "urn:x:params:oauth:token-type:oauth1_token",
                "oauth_token_secret": self.s.x_access_token_secret,
            })
        raise XAuthError("OAuth2 needs X_OAUTH2_REFRESH_TOKEN or OAuth 1.0a tokens to exchange")

    def _tokens(self, force_refresh: bool = False) -> dict:
        tokens = self.db.get(self.KV_KEY)
        if not tokens:
            return self._bootstrap()
        if force_refresh or tokens.get("expires_at", 0) - 300 < time.time():
            return self._token_request({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
        return tokens

    def auth(self) -> Any:
        token = self._tokens()["access_token"]

        def _bearer(r: requests.PreparedRequest) -> requests.PreparedRequest:
            r.headers["Authorization"] = f"Bearer {token}"
            return r

        return _bearer

    def on_unauthorized(self) -> bool:
        try:
            self._tokens(force_refresh=True)
            return True
        except XAuthError as e:
            log.error("OAuth2 refresh failed: %s", e)
            return False


# ---- live client --------------------------------------------------------------------------------
class LiveXClient:
    dry_run = False

    def __init__(self, provider: OAuth1Provider | OAuth2Provider, *, db: DB | None = None,
                 session: requests.Session | None = None):
        self.provider = provider
        self.db = db
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = "xagent/1.0"
        self._blocked_until: dict[str, float] = {}
        self._full_metrics = True
        self._fields_param = "tweet.fields"  # the OpenAPI spec has started calling this "post.fields"

    def _usage(self, bucket: str, amount: float = 1.0) -> None:
        if self.db and amount:
            self.db.add_usage(bucket, amount)

    def _fields(self, params: dict[str, Any]) -> dict[str, Any]:
        if "tweet.fields" in params and self._fields_param != "tweet.fields":
            params = dict(params)
            params[self._fields_param] = params.pop("tweet.fields")
        return params

    def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None,
                 retries: int = 3) -> dict:
        key = method + " " + _ID_RE.sub("/:id", path)
        until = self._blocked_until.get(key, 0)
        if until > time.time():
            raise XRateLimited(f"rate limited on {key} until {until:.0f}", reset_at=until, status=429)
        url = f"{API_BASE}{path}"
        reauthed = False
        attempt = 0
        while True:
            try:
                r = self.http.request(method, url, params=self._fields(params) if params else None, json=json,
                                      auth=self.provider.auth(), timeout=30)
            except requests.RequestException as e:
                if attempt < retries:
                    attempt += 1
                    time.sleep(min(30, 2 ** attempt + random.random()))
                    continue
                raise XError(f"network error calling {key}: {e}") from e
            if r.status_code < 300:
                return r.json() if r.content else {}
            body = _safe_json(r)
            detail = _error_text(body) or r.text[:300]
            if r.status_code == 429:
                reset = float(r.headers.get("x-rate-limit-reset") or (time.time() + 900))
                self._blocked_until[key] = reset
                raise XRateLimited(f"429 on {key}: {detail}", reset_at=reset, status=429, payload=body)
            if r.status_code == 401:
                if not reauthed and self.provider.on_unauthorized():
                    reauthed = True
                    continue
                raise XAuthError(f"401 unauthorized on {key}: {detail}", status=401, payload=body)
            if r.status_code == 402:
                raise XPaymentRequired(f"402 credits depleted on {key}: {detail}", status=402, payload=body)
            if r.status_code == 403:
                if "duplicate" in detail.lower():
                    raise XDuplicate(f"duplicate content: {detail}", status=403, payload=body)
                raise XForbidden(f"403 forbidden on {key}: {detail}", status=403, payload=body)
            if (r.status_code == 400 and params and "tweet.fields" in params
                    and "field" in detail.lower() and self._fields_param == "tweet.fields"):
                log.warning("API rejected tweet.fields (%s); switching to post.fields", detail[:120])
                self._fields_param = "post.fields"
                continue
            if r.status_code >= 500 and attempt < retries:
                attempt += 1
                time.sleep(min(60, 2 ** attempt + random.random()))
                continue
            raise XError(f"{r.status_code} on {key}: {detail}", status=r.status_code, payload=body)

    # ---- endpoints ------------------------------------------------------------------------------
    def me(self) -> dict:
        data = self._request("GET", "/users/me", params={"user.fields": "username,name,public_metrics,verified,created_at"})
        self._usage("x:user_read")
        return data.get("data", {})

    def create_post(self, text: str, *, reply_to: str | None = None, paid_partnership: bool = False,
                    made_with_ai: bool = False, summoned: bool = False) -> str:
        body: dict[str, Any] = {"text": text}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        if paid_partnership:
            body["paid_partnership"] = True
        if made_with_ai:
            body["made_with_ai"] = True
        # Never retry writes blindly: a timeout may still have created the post.
        data = self._request("POST", "/tweets", json=body, retries=0)
        if find_urls(text):
            self._usage("x:link_post")
        elif summoned:
            self._usage("x:reply")
        else:
            self._usage("x:post")
        tid = (data.get("data") or {}).get("id")
        if not tid:
            raise XError(f"create_post returned no id: {data}")
        return str(tid)

    def delete_post(self, tweet_id: str) -> None:
        self._request("DELETE", f"/tweets/{tweet_id}", retries=1)
        self._usage("x:manage")

    def mentions(self, user_id: str, *, since_id: str | None, max_results: int = 50) -> list[Mention]:
        params: dict[str, Any] = {
            "max_results": max(5, min(100, max_results)),
            "tweet.fields": MENTION_TWEET_FIELDS,
            "expansions": "author_id",
            "user.fields": MENTION_USER_FIELDS,
        }
        if since_id:
            params["since_id"] = since_id
        data = self._request("GET", f"/users/{user_id}/mentions", params=params)
        posts = data.get("data") or []
        self._usage("x:owned_read", len(posts))
        users = {u["id"]: u for u in (data.get("includes") or {}).get("users", [])}
        out: list[Mention] = []
        for t in posts:
            refs = t.get("referenced_tweets") or []
            u = users.get(t.get("author_id", ""), {})
            out.append(Mention(
                id=str(t["id"]),
                text=t.get("text", ""),
                author_id=str(t.get("author_id", "")),
                author_username=u.get("username", ""),
                author_followers=int((u.get("public_metrics") or {}).get("followers_count") or 0),
                created_at=t.get("created_at", ""),
                conversation_id=t.get("conversation_id"),
                in_reply_to_user_id=t.get("in_reply_to_user_id"),
                replied_to_id=next((str(r["id"]) for r in refs if r.get("type") == "replied_to"), None),
                is_retweet=any(r.get("type") == "retweeted" for r in refs),
                extra={"author_description": u.get("description", "")},
            ))
        return out

    def own_metrics(self, user_id: str, *, start_time: datetime) -> list[TweetMetrics]:
        """Metrics for our own recent root posts via the Owned Read timeline endpoint ($0.001/post)."""
        base = {
            "max_results": 100,
            "exclude": "replies,retweets",
            "start_time": start_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        fields = METRIC_FIELDS_FULL if self._full_metrics else METRIC_FIELDS_PUBLIC
        try:
            data = self._request("GET", f"/users/{user_id}/tweets", params={**base, "tweet.fields": fields})
        except XError as e:
            if not self._full_metrics or e.status not in (400, 403) or isinstance(e, XDuplicate):
                raise
            # Private metrics need user-context auth and posts <30 days old; fall back to public ones.
            log.info("Private metrics unavailable (%s); using public metrics", e)
            self._full_metrics = False
            data = self._request("GET", f"/users/{user_id}/tweets", params={**base, "tweet.fields": METRIC_FIELDS_PUBLIC})
        posts = data.get("data") or []
        self._usage("x:owned_read", len(posts))
        return [_parse_metrics(t) for t in posts]


# ---- dry run ------------------------------------------------------------------------------------
class DryRunXClient:
    """Logs instead of posting. Lets you run the full agent loop with no X credentials and no spend."""

    dry_run = True

    def __init__(self, handle: str = "dryrun"):
        self.handle = handle
        self.posted: list[dict] = []

    def me(self) -> dict:
        return {"id": "0", "username": self.handle, "name": self.handle, "public_metrics": {}}

    def create_post(self, text: str, *, reply_to: str | None = None, paid_partnership: bool = False,
                    made_with_ai: bool = False, summoned: bool = False) -> str:
        tid = f"dry-{uuid.uuid4().hex[:12]}"
        self.posted.append({"id": tid, "text": text, "reply_to": reply_to, "paid_partnership": paid_partnership,
                            "made_with_ai": made_with_ai})
        log.info("[DRY RUN] would post%s:\n%s", f" (reply to {reply_to})" if reply_to else "", text)
        return tid

    def delete_post(self, tweet_id: str) -> None:
        log.info("[DRY RUN] would delete %s", tweet_id)

    def mentions(self, user_id: str, *, since_id: str | None, max_results: int = 50) -> list[Mention]:
        return []

    def own_metrics(self, user_id: str, *, start_time: datetime) -> list[TweetMetrics]:
        return []


def _safe_json(r: requests.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return None


def _error_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    parts = [str(body.get(k)) for k in ("title", "detail", "error", "error_description") if body.get(k)]
    for e in body.get("errors") or []:
        if isinstance(e, dict):
            parts.append(str(e.get("message") or e.get("detail") or e))
    return " | ".join(parts)


def make_client(secrets: Secrets, *, dry_run: bool, db: DB, handle: str) -> XClient:
    if dry_run:
        return DryRunXClient(handle)
    if (secrets.x_auth_mode or "oauth1").lower() == "oauth2":
        return LiveXClient(OAuth2Provider(secrets, db), db=db)
    return LiveXClient(OAuth1Provider(secrets), db=db)
