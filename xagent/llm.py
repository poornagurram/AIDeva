"""Thin, defensive wrapper around the Anthropic SDK (Claude)."""

from __future__ import annotations

import logging
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from .config import LLMConfig
from .db import DB

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL = "web_search_20260209"


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


class LLMAuthError(LLMError):
    """Invalid key, missing permission, or no credit left: needs a human, retrying won't help."""


def _fatal(e: anthropic.APIError) -> LLMError | None:
    if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return LLMAuthError(f"Anthropic auth/permission error: {e}")
    if isinstance(e, anthropic.BadRequestError) and "credit balance" in str(e).lower():
        return LLMAuthError(f"Anthropic credit balance too low: {e}")
    return None


class LLM:
    def __init__(self, cfg: LLMConfig, *, api_key: str | None = None, db: DB | None = None, client: Any = None):
        self.cfg = cfg
        self.db = db
        self.client = client or anthropic.Anthropic(api_key=api_key, max_retries=4, timeout=300.0)
        # Features the configured model rejected with a 400; dropped for the rest of the process.
        self._disabled: set[str] = set()
        if not cfg.use_refusal_fallback:
            self._disabled.add("fallbacks")

    # ------------------------------------------------------------------------------------------------
    def _request_kwargs(self, effort: str) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self.cfg.model, "betas": []}
        if "effort" not in self._disabled:
            kw["output_config"] = {"effort": effort}
        if "fallbacks" not in self._disabled:
            kw["betas"].append(FALLBACK_BETA)
            kw["fallbacks"] = "default"
        if not kw["betas"]:
            del kw["betas"]
        return kw

    def _degrade_on_400(self, err: anthropic.BadRequestError) -> bool:
        """If the model rejected an optional feature, disable it and signal a retry."""
        msg = str(getattr(err, "message", err)).lower()
        for feature in ("fallbacks", "effort"):
            if feature not in self._disabled and feature.rstrip("s") in msg:
                log.warning("Model %s rejected '%s'; disabling it. (%s)", self.cfg.model, feature, msg[:200])
                self._disabled.add(feature)
                return True
        return False

    def _record_usage(self, resp: Any) -> None:
        if not self.db or not getattr(resp, "usage", None):
            return
        u = resp.usage
        self.db.add_usage("llm:input_tokens", float(u.input_tokens or 0))
        self.db.add_usage("llm:output_tokens", float(u.output_tokens or 0))
        self.db.add_usage("llm:cache_read_tokens", float(getattr(u, "cache_read_input_tokens", 0) or 0))
        self.db.add_usage("llm:cache_write_tokens", float(getattr(u, "cache_creation_input_tokens", 0) or 0))
        stu = getattr(u, "server_tool_use", None)
        if stu is not None and getattr(stu, "web_search_requests", None):
            self.db.add_usage("llm:web_searches", float(stu.web_search_requests))

    @staticmethod
    def _check_stop(resp: Any) -> None:
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            cat = getattr(details, "category", None) if details else None
            raise LLMRefusal(f"model declined (category={cat})")
        if resp.stop_reason == "max_tokens":
            raise LLMError("response truncated at max_tokens")

    @staticmethod
    def _system(system: str) -> list[dict[str, Any]]:
        # Stable persona + playbook go first and are cached; volatile context goes in the user turn.
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    # ------------------------------------------------------------------------------------------------
    def structured(self, *, system: str, user: str, output: type[T], effort: str, max_tokens: int = 16000) -> T:
        for _ in range(3):
            try:
                resp = self.client.beta.messages.parse(
                    max_tokens=max_tokens,
                    system=self._system(system),
                    messages=[{"role": "user", "content": user}],
                    output_format=output,
                    **self._request_kwargs(effort),
                )
            except anthropic.BadRequestError as e:
                if not _fatal(e) and self._degrade_on_400(e):
                    continue
                raise (_fatal(e) or LLMError(f"bad request: {e}")) from e
            except anthropic.APIError as e:
                raise (_fatal(e) or LLMError(f"anthropic API error: {e}")) from e
            self._record_usage(resp)
            self._check_stop(resp)
            parsed = getattr(resp, "parsed_output", None)
            if parsed is None:
                raise LLMError("no structured output returned")
            return parsed
        raise LLMError("exhausted retries after disabling unsupported features")

    def web_research(self, *, system: str, user: str, effort: str, max_searches: int = 5) -> str:
        """Let Claude search the live web; returns its written briefing (plain text)."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        tools = [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": max_searches}]
        attempts = 0
        while True:
            try:
                resp = self.client.beta.messages.create(
                    max_tokens=16000,
                    system=self._system(system),
                    messages=messages,
                    tools=tools,
                    **self._request_kwargs(effort),
                )
            except anthropic.BadRequestError as e:
                attempts += 1
                if not _fatal(e) and attempts < 3 and self._degrade_on_400(e):
                    continue
                raise (_fatal(e) or LLMError(f"bad request: {e}")) from e
            except anthropic.APIError as e:
                raise (_fatal(e) or LLMError(f"anthropic API error: {e}")) from e
            self._record_usage(resp)
            if resp.stop_reason == "pause_turn" and attempts < 4:
                # Server-side tool loop paused; hand the partial turn back so it can continue.
                attempts += 1
                messages = [*messages, {"role": "assistant", "content": resp.content}]
                continue
            self._check_stop(resp)
            text = "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
            if not text:
                raise LLMError("web research returned no text")
            return text
