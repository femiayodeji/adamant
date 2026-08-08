"""The socket shell for agents the platform runs on the host's behalf.

Two of them exist: the ladder bots (`bots.py`, a pure strategy) and the roster
fighters (`llm_agents.py`, an OpenRouter model). They differ only in how a
`state_update` becomes an action — everything around that is this file: connect,
join, answer every tick, stop at `match_end`.

There is deliberately **no privileged internal path**. Both dial `/ws/join/{code}`
over the ordinary public protocol and are indistinguishable, to the match and to
the log, from an agent someone is running on their own laptop. That is what makes
a baseline and a contender comparable at all (spec §11).

`handshake()` is separated from `run()` because the REST handler that spawns one
must be able to report the corner it took *in its response* — the lobby card has
to flip in the host's browser, and a task that joins later cannot answer a request
that has already returned.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from websockets.asyncio.client import connect

logger = logging.getLogger("arena.corner")

CONNECT_TIMEOUT_SECONDS = 10

_tasks: set[asyncio.Task] = set()


class CornerClient:
    """Connect, join, and answer ticks until the match ends.

    Subclasses implement `decide`. They may also override `aclose` to release
    whatever their brain holds open.
    """

    def __init__(
        self,
        url: str,
        code: str,
        name: str,
        profile: dict[str, Any],
        corner: Optional[str] = None,
    ) -> None:
        self.uri = f"{url.rstrip('/')}/ws/join/{code}"
        self.name = name
        self.profile = profile
        self.corner_request = corner
        self.corner: Optional[str] = None
        # From `match_start`. A pure strategy ignores both; a model needs them,
        # because `max_ticks` and the opponent's name are in its prompt and the
        # deadline is its time budget.
        self.config: dict[str, Any] = {}
        self.opponent_name: Optional[str] = None
        self._ws: Any = None

    async def decide(self, message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """`state_update` → `(action, telemetry)`. Telemetry is logged verbatim."""
        raise NotImplementedError

    def unrecoverable(self) -> Optional[str]:
        """A reason this client can never decide again, or None to keep fighting.

        Checked after every tick. A pure strategy never has one; a model client does
        when the account behind it cannot be charged, and answering the remaining
        ticks with a hedge would only produce a scoreless draw under the model's name.
        """
        return None

    async def aclose(self) -> None:
        """Release brain-side resources. Called once, after the socket closes."""

    async def handshake(self) -> str:
        """Connect and join, returning the corner taken."""
        self._ws = await asyncio.wait_for(connect(self.uri), timeout=CONNECT_TIMEOUT_SECONDS)
        await self._ws.send(
            json.dumps(
                {
                    "type": "join",
                    "name": self.name,
                    "corner": self.corner_request,
                    "profile": self.profile,
                }
            )
        )
        reply = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=CONNECT_TIMEOUT_SECONDS))
        if reply.get("type") != "joined":
            await self._ws.close()
            await self.aclose()
            raise RuntimeError(f"join rejected: {reply.get('reason', reply)}")

        # No `corner_token` is kept: a platform-run client does not resume. If its
        # socket drops the match pauses and waits, exactly as it would for a guest
        # agent, and the host refills or the pause expires (spec §6.4).
        self.corner = reply["corner"]
        # Two entrants asking for the same name means the second is renamed (spec
        # §6.1). Take the platform's answer, or this client's own log lines call it
        # something nobody else in the match does.
        self.name = reply.get("name", self.name)
        return self.corner

    async def run(self) -> None:
        """Decide and reply until the match ends.

        Stateless between ticks, so a pause is invisible: a re-issued
        `state_update` is simply answered again.
        """
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                kind = message.get("type")
                if kind == "match_start":
                    self.config = message.get("config", {}) or {}
                    self.opponent_name = message.get("opponent_name")
                elif kind == "state_update":
                    action, telemetry = await self.decide(message)
                    await self._ws.send(
                        json.dumps(
                            {
                                "type": "move",
                                "tick": message["tick"],
                                "action": action,
                                "telemetry": telemetry,
                            }
                        )
                    )
                    # The already-decided move goes out first, costing nothing;
                    # whether that tick resolves depends on which the server notices
                    # first, and either way it stops here rather than hedging out the
                    # rest of the match. The match then pauses on a missing corner,
                    # which is what this failure actually is (spec §6.4).
                    reason = self.unrecoverable()
                    if reason:
                        logger.error(
                            "%s cannot continue after tick %s: %s",
                            self.name,
                            message["tick"],
                            reason,
                        )
                        break
                elif kind == "match_end":
                    break
        except Exception:
            logger.debug("%s socket closed", self.name, exc_info=True)
        finally:
            try:
                await self._ws.close()
            except Exception:
                pass
            await self.aclose()


def launch(client: CornerClient, name: str) -> None:
    """Run a joined client's match in the background, holding a reference.

    Without the set, the only reference to the task is local and the garbage
    collector is free to cancel a fighter mid-match.
    """
    task = asyncio.create_task(client.run(), name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def shutdown() -> None:
    """Cancel every in-flight platform-run client. Called on app shutdown."""
    for task in list(_tasks):
        task.cancel()
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
    _tasks.clear()
