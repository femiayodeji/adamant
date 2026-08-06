"""A mock ADAM Arena platform: just enough of spec 6 to exercise an agent.

Scripted, not simulated -- it plays a fixed sequence of messages at the agent and
records everything the agent sends back. It deliberately includes the awkward
cases: a re-issued ``state_update`` for a tick that was already answered
(pause/resume, spec 6.3/6.4), and a rejection path for a bad name.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from websockets.asyncio.server import serve

FIGHTER = {"hp": 100, "stamina": 100, "points": 0, "penalties": 0}


def state_update(tick: int, deadline_ms: int = 200, history=None) -> dict:
    return {
        "type": "state_update",
        "tick": tick,
        "deadline_ms": deadline_ms,
        "self": dict(FIGHTER),
        "opponent": dict(FIGHTER),
        "history": history or [],
    }


MATCH_END = {
    "type": "match_end",
    "winner": "agent_a",
    "reason": "ko",
    "final_tick": 3,
    "final_state": {
        "agent_a": {"hp": 65, "stamina": 66, "points": 105, "penalties": 0},
        "agent_b": {"hp": 0, "stamina": 40, "points": 35, "penalties": 15},
    },
}


class MockPlatform:
    """Serves ``/ws/join/{code}``; records every message the agent sent."""

    def __init__(self, script=None, reject_reason: str | None = None, deadline_ms: int = 200):
        self.received: list[dict] = []
        self.reject_reason = reject_reason
        self.deadline_ms = deadline_ms
        self.script = script if script is not None else self.default_script()
        self.paths: list[str] = []

    def default_script(self) -> list[dict]:
        return [
            {"type": "match_start", "you": "agent_a", "opponent_name": "Jab-bot",
             "config": {"max_ticks": 60, "deadline_ms": self.deadline_ms,
                        "resume_window_ms": 120000}},
            state_update(1, self.deadline_ms),
            state_update(
                2,
                self.deadline_ms,
                history=[{
                    "tick": 1,
                    "moves": {"self": "hook", "opponent": "jab"},
                    "events": [
                        {"type": "hit", "attacker": "self", "defender": "opponent",
                         "action": "hook", "damage": 15},
                        {"type": "hit", "attacker": "opponent", "defender": "self",
                         "action": "jab", "damage": 5},
                    ],
                }],
            ),
            {"type": "match_paused", "tick": 2, "missing": "agent_b",
             "resume_window_ms": 120000},
            {"type": "match_resumed"},
            # The same tick, re-issued with a fresh deadline. A stateless agent
            # just answers it again.
            state_update(2, self.deadline_ms),
            MATCH_END,
        ]

    async def handler(self, websocket):
        self.paths.append(getattr(websocket, "request", None) and websocket.request.path or "")
        first = json.loads(await websocket.recv())
        self.received.append(first)
        if self.reject_reason:
            await websocket.send(json.dumps({"type": "error", "reason": self.reject_reason}))
            await websocket.close()
            return
        await websocket.send(
            json.dumps({
                "type": "joined",
                "corner": "agent_a",
                "match_code": "RUMBLE-4F2",
                "corner_token": "ct_test",
            })
        )
        for message in self.script:
            await websocket.send(json.dumps(message))
            if message.get("type") == "state_update":
                self.received.append(json.loads(await websocket.recv()))
        await websocket.close()

    @property
    def join(self) -> dict:
        return self.received[0]

    @property
    def moves(self) -> list[dict]:
        return [m for m in self.received if m.get("type") == "move"]


@asynccontextmanager
async def running(platform: MockPlatform):
    """Start the mock on an ephemeral localhost port; yield its ws:// base URL."""
    async with serve(platform.handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}"
