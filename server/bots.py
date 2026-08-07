"""Built-in bots — the baseline ladder (spec §11), Phase 3.

Bots dial in over the **same public WebSocket protocol** as any external agent.
There is deliberately no privileged internal path: baselines and contenders must be
measured through identical machinery, or the ladder is not a comparable instrument.

The brains live in `engine.strategies` and are pure; the socket shell is
`corner_client.CornerClient`, shared with the OpenRouter fighters in
`llm_agents.py`. This module is only the join between the two.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .corner_client import CornerClient, launch, shutdown
from .engine.strategies import LADDER, Strategy, build

__all__ = ["BotClient", "spawn", "shutdown"]


def _profile(bot: str, seed: Optional[int]) -> dict[str, Any]:
    """Even a non-LLM agent declares a profile — a result that cannot say what
    produced it is not a measurement (spec §6.1)."""
    return {
        "model": "none",
        "provider": "builtin",
        "prompt_version": "none",
        "temperature": 0,
        "agent_version": f"{bot}bot@1.0",
        "seed": seed,
    }


class BotClient(CornerClient):
    def __init__(
        self,
        url: str,
        code: str,
        bot: str,
        seed: Optional[int] = None,
        corner: Optional[str] = None,
        **options: Any,
    ) -> None:
        self.bot = bot
        self.seed = seed
        self.strategy: Strategy = build(bot, seed=seed, **options)
        # `cycle` carries its period in its name, so two periods are two entrants
        # rather than one merged row in any aggregate keyed on bot name.
        super().__init__(
            url=url,
            code=code,
            name=f"{self.strategy.name.capitalize()}-bot"[:24],
            profile=_profile(bot, seed),
            corner=corner,
        )

    async def decide(self, message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        started = time.monotonic()
        action = self.strategy.decide(message)
        return action, {
            "latency_ms": int((time.monotonic() - started) * 1000),
            "fallback": False,
        }


async def spawn(
    url: str,
    code: str,
    bot: str,
    seed: Optional[int] = None,
    corner: Optional[str] = None,
    **options: Any,
) -> str:
    """Fill a corner with a ladder bot. Returns the corner it took.

    `options` are strategy-specific keyword arguments — currently only `period`,
    which `cycle` needs. Without a route for these, spec §11's period-5 pattern is
    unreachable from the REST API and half of that ladder rung does not exist.
    """
    if bot not in LADDER:
        raise ValueError(f"unknown bot {bot!r}")

    client = BotClient(url=url, code=code, bot=bot, seed=seed, corner=corner, **options)
    taken = await client.handshake()
    launch(client, name=f"bot-{bot}-{code}")
    return taken
