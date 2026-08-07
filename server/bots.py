"""Built-in bots — the baseline ladder (spec §11), Phase 3.

Bots dial in over the **same public WebSocket protocol** as any external agent.
There is deliberately no privileged internal path: baselines and contenders must be
measured through identical machinery, or the ladder is not a comparable instrument.

The brains live in `engine.strategies` and are pure; this module is only the socket
shell around them, and is the same shape as `agents/reference_agent.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from websockets.asyncio.client import connect

from .engine.strategies import LADDER, Strategy, build

logger = logging.getLogger("arena.bots")

CONNECT_TIMEOUT_SECONDS = 10

_tasks: set[asyncio.Task] = set()


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


class BotClient:
    def __init__(
        self,
        url: str,
        code: str,
        bot: str,
        seed: Optional[int] = None,
        corner: Optional[str] = None,
        **options: Any,
    ) -> None:
        self.uri = f"{url.rstrip('/')}/ws/join/{code}"
        self.bot = bot
        self.seed = seed
        self.corner_request = corner
        self.name = f"{bot.capitalize()}-bot"[:24]
        self.strategy: Strategy = build(bot, seed=seed, **options)
        # `cycle` carries its period in its name, so two periods are two entrants
        # rather than one merged row in any aggregate keyed on bot name.
        self.name = f"{self.strategy.name.capitalize()}-bot"[:24]
        self.corner: Optional[str] = None
        self.corner_token: Optional[str] = None
        self._ws: Any = None

    async def handshake(self) -> str:
        """Connect and join, returning the corner taken. Awaited by the REST handler
        so `fill_bot` can report the corner synchronously."""
        self._ws = await asyncio.wait_for(connect(self.uri), timeout=CONNECT_TIMEOUT_SECONDS)
        await self._ws.send(
            json.dumps(
                {
                    "type": "join",
                    "name": self.name,
                    "corner": self.corner_request,
                    "profile": _profile(self.bot, self.seed),
                }
            )
        )
        reply = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=CONNECT_TIMEOUT_SECONDS))
        if reply.get("type") != "joined":
            await self._ws.close()
            raise RuntimeError(f"bot join rejected: {reply.get('reason', reply)}")

        self.corner = reply["corner"]
        self.corner_token = reply.get("corner_token")
        return self.corner

    async def run(self) -> None:
        """Decide and reply until the match ends. Bots are stateless between ticks,
        so a pause is invisible to them — a re-issued `state_update` is simply
        answered again."""
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                kind = message.get("type")
                if kind == "state_update":
                    started = time.monotonic()
                    action = self.strategy.decide(message)
                    await self._ws.send(
                        json.dumps(
                            {
                                "type": "move",
                                "tick": message["tick"],
                                "action": action,
                                "telemetry": {
                                    "latency_ms": int((time.monotonic() - started) * 1000),
                                    "fallback": False,
                                },
                            }
                        )
                    )
                elif kind == "match_end":
                    break
        except Exception:
            logger.debug("bot %s socket closed", self.name, exc_info=True)
        finally:
            try:
                await self._ws.close()
            except Exception:
                pass


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

    task = asyncio.create_task(client.run(), name=f"bot-{bot}-{code}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return taken


async def shutdown() -> None:
    for task in list(_tasks):
        task.cancel()
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
    _tasks.clear()
