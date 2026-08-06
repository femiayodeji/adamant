"""The decision loop: time-box a model call, parse it strictly, or hedge.

Shared by the reference agent and the harness. The only thing either of them
supplies is ``model_call`` -- an async callable
``(system, user, deadline_ms) -> (text, tokens_in, tokens_out)``.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from .prompt import SYSTEM_PROMPT, build_user_prompt
from .rules import ACTIONS, FALLBACK_ACTION

#: Fraction of ``deadline_ms`` the model call is allowed to take (spec 10).
#: The remainder covers prompt building, parsing, and the trip back to the
#: platform, so a decision that overruns still arrives as a real move.
TIME_BUDGET_FRACTION = 0.70

#: Never give the model less than this, and never wait longer than the deadline.
MIN_BUDGET_S = 0.05

REASON_MAX_CHARS = 200


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    fallback: bool
    latency_ms: int
    tokens_in: int
    tokens_out: int

    def telemetry(self) -> dict:
        """The optional ``telemetry`` object of a ``move`` message (spec 6.1)."""
        return {
            "latency_ms": self.latency_ms,
            "fallback": self.fallback,
            "reason": self.reason[:REASON_MAX_CHARS],
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
        }


class ReplyError(ValueError):
    """The model's reply was not a usable single JSON object."""


def parse_reply(text: str) -> tuple[str, str]:
    """Strictly parse a model reply into ``(action, reason)``.

    Deliberately strict: exactly one JSON object, an ``action`` drawn from the
    eight names in spec 2. Anything else is a parse failure and becomes a
    hedge, because instruction-following is part of the agent being evaluated.
    """
    try:
        data = json.loads((text or "").strip())
    except (json.JSONDecodeError, TypeError) as exc:
        raise ReplyError(f"not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReplyError(f"not a JSON object: {type(data).__name__}")
    action = data.get("action")
    if action not in ACTIONS:
        raise ReplyError(f"not one of the eight actions: {action!r}")
    reason = data.get("reason", "")
    if not isinstance(reason, str):
        reason = str(reason)
    return action, reason[:REASON_MAX_CHARS]


def budget_seconds(deadline_ms: int | None) -> float:
    deadline_ms = deadline_ms or 5000
    return max(MIN_BUDGET_S, (deadline_ms / 1000.0) * TIME_BUDGET_FRACTION)


async def decide(
    model_call,
    state: dict,
    opponent_name: str | None = None,
    deadline_ms: int | None = None,
    max_ticks: int | None = None,
) -> Decision:
    """Always return a valid action, within roughly 70% of the deadline.

    On timeout or on a reply we cannot parse, hedge with ``block_high`` and set
    ``telemetry.fallback = true`` so the log records that the action came from a
    local default rather than a finished decision.
    """
    deadline_ms = deadline_ms or state.get("deadline_ms") or 5000
    user = build_user_prompt(state, opponent_name, max_ticks)
    started = time.perf_counter()
    tokens_in = tokens_out = 0
    try:
        text, tokens_in, tokens_out = await asyncio.wait_for(
            model_call(SYSTEM_PROMPT, user, deadline_ms), budget_seconds(deadline_ms)
        )
        action, reason = parse_reply(text)
        fallback = False
    except asyncio.TimeoutError:
        action, reason, fallback = FALLBACK_ACTION, "fallback: model call timed out", True
    except ReplyError as exc:
        action, reason, fallback = FALLBACK_ACTION, f"fallback: bad reply ({exc})", True
    except Exception as exc:  # transport error, rate limit, anything else
        action, reason, fallback = (
            FALLBACK_ACTION,
            f"fallback: {type(exc).__name__}: {exc}",
            True,
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    return Decision(
        action=action,
        reason=reason[:REASON_MAX_CHARS],
        fallback=fallback,
        latency_ms=latency_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def make_decider(model_call):
    """Adapt a model client into the ``decide`` callable ``run_match`` expects."""

    async def agent_decide(state, opponent_name=None, deadline_ms=None, max_ticks=None):
        return await decide(model_call, state, opponent_name, deadline_ms, max_ticks)

    return agent_decide
