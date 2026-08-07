"""One client for every vendor: LiteLLM pointed at OpenRouter.

The key lives in ``.env`` as ``OPENROUTER_API_KEY`` (gitignored). It is read into
memory and passed to LiteLLM; it is never logged, never written to a results
row, and never included in a profile.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..common.decision import budget_seconds
from ..common.personas import get_persona, prompt_version
from ..common.prompt import build_system_prompt

HARNESS_VERSION = "harness@1.0.0"
OPENROUTER_PREFIX = "openrouter/"
REPO_ROOT = Path(__file__).resolve().parents[2]


def load_api_key(env_path: Path | None = None) -> str:
    """Read ``OPENROUTER_API_KEY`` from the environment or from ``.env``."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        try:
            from dotenv import load_dotenv
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise RuntimeError("python-dotenv is required to read .env") from exc
        load_dotenv(env_path or (REPO_ROOT / ".env"))
        key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set (put it in .env)")
    return key


@dataclass(frozen=True)
class ModelSpec:
    """A pinned model configuration -- one entrant in a comparison.

    ``temperature=None`` means "do not send the parameter at all", which is
    required for models that reject it (the Claude Opus 5 family). Anything else
    is pinned and reported in the profile, because a comparison that cannot say
    what temperature produced it is not a measurement.
    """

    model: str  # OpenRouter model id, e.g. "anthropic/claude-opus-5"
    temperature: float | None = 0.0
    max_tokens: int = 512
    label: str | None = None
    #: Persona key, or None for the neutral prompt -- which is the default,
    #: because a model comparison that varies the strategy prompt at the same
    #: time is confounded. Set it deliberately, and it travels into the profile.
    persona: str | None = None

    @property
    def name(self) -> str:
        return self.label or self.model


@dataclass
class LiteLLMClient:
    """Async ``(system, user, deadline_ms) -> (text, tokens_in, tokens_out)``."""

    spec: ModelSpec
    api_key: str = field(repr=False, default="")
    # Injectable for tests; defaults to litellm.acompletion.
    completion: Callable[..., Awaitable] | None = None
    resolved_model: str | None = None

    def __post_init__(self) -> None:
        if not self.api_key:
            self.api_key = load_api_key()
        if self.completion is None:
            from litellm import acompletion

            self.completion = acompletion

    @property
    def system_prompt(self) -> str:
        """The system prompt for this entrant. ``ArenaRunner`` pins it per match."""
        return build_system_prompt(get_persona(self.spec.persona))

    def profile(self) -> dict:
        """Agent profile sent at join and stored verbatim in the match log."""
        return {
            "model": self.spec.model,
            # The alias is what we asked for; the resolved id is what answered.
            # Filled in after the first call; both are recorded.
            "resolved_model": self.resolved_model,
            "provider": "openrouter",
            "persona": self.spec.persona,
            "prompt_version": prompt_version(get_persona(self.spec.persona)),
            "temperature": self.spec.temperature,
            "agent_version": HARNESS_VERSION,
        }

    async def __call__(self, system: str, user: str, deadline_ms: int):
        request = {
            "model": OPENROUTER_PREFIX + self.spec.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.spec.max_tokens,
            "api_key": self.api_key,
            "timeout": budget_seconds(deadline_ms),
            "num_retries": 0,  # a retry would eat the whole deadline
        }
        if self.spec.temperature is not None:
            request["temperature"] = self.spec.temperature
        reply = await self.completion(**request)
        self.resolved_model = getattr(reply, "model", None) or self.spec.model
        text = reply.choices[0].message.content or ""
        usage = getattr(reply, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0
        return text, tokens_in, tokens_out
