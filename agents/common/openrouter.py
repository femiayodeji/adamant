"""OpenRouter over plain HTTP: read the key, make one chat-completions call.

The evaluation harness reaches OpenRouter through LiteLLM, which is the right
tool for a batch that has to speak to every provider and normalise their
answers. This module is the other end of that trade: ~100 lines of ``httpx``
against the one endpoint, with no dependency heavier than the platform already
has. It exists because two callers cannot afford LiteLLM —

* ``agents/openrouter_app`` is meant to be read and copied by someone writing
  their first agent, and a client they can read in a sitting is worth more than
  one that abstracts over vendors they are not using;
* ``server/llm_agents.py`` runs *inside the platform process*, and putting
  LiteLLM (with its provider SDKs and tokenizers) into the deployed image to
  make one JSON POST would be a poor bargain.

The contract is deliberately the same one ``LiteLLMClient`` implements —
``(system, user, deadline_ms) -> (text, tokens_in, tokens_out)`` — so either can
be handed to ``decision.make_decider`` and nothing downstream can tell them
apart.

The key is read from ``OPENROUTER_API_KEY`` (environment or ``.env``, which is
gitignored). It is held in memory, never logged, never written to a results row,
and never included in a profile.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .decision import budget_seconds

API_URL = "https://openrouter.ai/api/v1/chat/completions"
ENV_VAR = "OPENROUTER_API_KEY"
REPO_ROOT = Path(__file__).resolve().parents[2]

#: OpenRouter attributes traffic by these optional headers. Only the title is
#: sent: there is no canonical public URL for this arena to claim.
APP_TITLE = "Adamant"


#: HTTP statuses that will fail identically on every retry: the key is wrong, the
#: account cannot pay, or the model is not permitted to it. Everything else — 429
#: from a busy free-tier provider, a 5xx, a timeout — is worth trying again next
#: tick. The distinction is the difference between a corner that hedges through a
#: blip and a corner that hedges through all 60 ticks of a match nobody can win.
PERMANENT_STATUSES: frozenset[int] = frozenset({401, 402, 403})


class OpenRouterError(RuntimeError):
    """The call reached OpenRouter and came back unusable.

    Not fatal on its own: ``decision.decide`` catches everything and hedges, so a
    blip or an outage costs the corner a fallback action rather than the fight.
    ``permanent`` is what lets a caller tell a blip from a wall — retrying a 402
    fifty-nine more times produces fifty-nine more identical failures and a
    scoreless draw that looks like the engine never ran.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def permanent(self) -> bool:
        return self.status in PERMANENT_STATUSES


def load_api_key(env_path: Path | None = None) -> str:
    """Read ``OPENROUTER_API_KEY`` from the environment or from ``.env``."""
    key = os.environ.get(ENV_VAR)
    if not key:
        try:
            from dotenv import load_dotenv
        except ImportError:
            load_dotenv = None
        if load_dotenv is not None:
            load_dotenv(env_path or (REPO_ROOT / ".env"))
            key = os.environ.get(ENV_VAR)
    if not key:
        raise RuntimeError(f"{ENV_VAR} is not set (put it in .env)")
    return key


def have_api_key(env_path: Path | None = None) -> bool:
    """Is a key configured? Asked by ``GET /api/agents`` before offering the
    roster, so it must answer rather than raise."""
    try:
        load_api_key(env_path)
    except RuntimeError:
        return False
    return True


@dataclass
class OpenRouterClient:
    """Async ``(system, user, deadline_ms) -> (text, tokens_in, tokens_out)``.

    One instance per fighter per match: the underlying ``httpx`` client is
    created once and reused, because opening a fresh TLS connection every tick
    spends a chunk of a five-second deadline on a handshake.
    """

    model: str
    temperature: float | None = None
    max_tokens: int = 1024
    #: OpenRouter's unified reasoning control. A deadline bracket gives the model
    #: a couple of seconds, which is not enough for extended thinking, so the
    #: roster keeps it low and excluded from the reply — the reply we want is one
    #: JSON object. OpenRouter drops the parameter for models that have no
    #: reasoning mode, so it is safe to send to all of them.
    reasoning: dict[str, Any] | None = None
    api_key: str = field(repr=False, default="")
    #: Injectable for tests: an async ``(payload, headers, timeout) -> dict``.
    post: Any = None

    #: Set to the message of the first failure that will never succeed (see
    #: ``PERMANENT_STATUSES``). ``decision.decide`` catches every exception and
    #: hedges, by design — its contract is that it always returns an action — so a
    #: caller that wants to *act* on a dead key cannot see the exception and reads
    #: this instead.
    permanent_error: str | None = field(default=None, compare=False)

    _http: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.api_key and self.post is None:
            self.api_key = load_api_key()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "X-Title": APP_TITLE}

    async def preflight(self, timeout: float = 20.0) -> None:
        """Check this model can actually be called. Raises ``OpenRouterError`` if not.

        One request for a single token — the smallest thing OpenRouter will answer.
        A key that cannot pay for this model fails here for nothing, which is the
        entire point: the alternative is discovering it 120 model calls into a match
        that both corners spent hedging, and a scoreless draw that reads as a broken
        engine rather than a declined card.
        """
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        post = self.post or self._default_post
        try:
            await post(payload, self._headers(), timeout)
        except OpenRouterError as exc:
            if exc.permanent:
                self.permanent_error = str(exc)
            raise

    async def _default_post(self, payload: dict, headers: dict, timeout: float) -> dict:
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=timeout)
        response = await self._http.post(API_URL, json=payload, headers=headers, timeout=timeout)
        if response.status_code >= 400:
            raise OpenRouterError(
                f"HTTP {response.status_code}: {response.text[:200]}", response.status_code
            )
        return response.json()

    async def __call__(self, system: str, user: str, deadline_ms: int):
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.max_tokens,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.reasoning is not None:
            payload["reasoning"] = self.reasoning

        post = self.post or self._default_post
        try:
            data = await post(payload, self._headers(), budget_seconds(deadline_ms))
        except OpenRouterError as exc:
            if exc.permanent and self.permanent_error is None:
                self.permanent_error = str(exc)
            raise

        # An upstream provider failure arrives as HTTP 200 with an `error` object
        # and no choices, so a naive `data["choices"][0]` would raise KeyError and
        # report an outage as a bug in this file.
        choices = data.get("choices") or []
        if not choices:
            raise OpenRouterError(str(data.get("error") or "no choices in response"))

        text = (choices[0].get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        return text, usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
