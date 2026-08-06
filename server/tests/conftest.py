"""Integration-test harness: a real uvicorn server, real WebSocket clients.

Deliberately not `TestClient`. The behaviour under test is concurrent — two agents,
one spectator, a deadline clock, and sockets that drop mid-tick — and a synchronous
test client cannot express any of it. Bots also connect over a real socket by
design (spec §11), so there must be a real port to connect to.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect

import server.main as main
import server.matchlog as matchlog
from server.registry import Registry

STARTUP_TIMEOUT = 10.0
RECV_TIMEOUT = 5.0


class Arena:
    """A running server plus the helpers to talk to it."""

    def __init__(self, port: int, log_dir: Path, server: uvicorn.Server, task: asyncio.Task):
        self.port = port
        self.log_dir = log_dir
        self._server = server
        self._task = task

    @property
    def http(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.http, timeout=10.0)

    async def create_match(self, **body: Any) -> dict[str, Any]:
        async with self.client() as http:
            response = await http.post("/api/matches", json=body)
        response.raise_for_status()
        return response.json()

    async def start(self, ref: str, host_token: str) -> httpx.Response:
        async with self.client() as http:
            return await http.post(
                f"/api/matches/{ref}/start", headers={"X-Host-Token": host_token}
            )

    async def fill_bot(self, ref: str, host_token: str, **body: Any) -> httpx.Response:
        async with self.client() as http:
            return await http.post(
                f"/api/matches/{ref}/fill_bot",
                json=body,
                headers={"X-Host-Token": host_token},
            )

    async def get(self, path: str) -> httpx.Response:
        async with self.client() as http:
            return await http.get(path)

    def match(self, ref: str):
        """Reach into the in-process registry. Used only to set knobs the REST API
        deliberately does not expose, such as `resume_window_ms`."""
        return main.registry.get(ref)

    async def stop(self) -> None:
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=STARTUP_TIMEOUT)
        except asyncio.TimeoutError:
            self._task.cancel()


async def start_arena(log_dir: Path) -> Arena:
    """Boot a server against `log_dir`. Calling this twice on the same directory is
    how the crash-recovery test simulates a restart."""
    log_dir.mkdir(parents=True, exist_ok=True)
    matchlog.LOG_DIR = log_dir
    main.registry = Registry(log_dir=log_dir)

    config = uvicorn.Config(
        main.app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    waited = 0.0
    while not server.started:
        if task.done():
            await task  # surface the startup exception
        await asyncio.sleep(0.02)
        waited += 0.02
        if waited > STARTUP_TIMEOUT:
            raise TimeoutError("server did not start")

    port = server.servers[0].sockets[0].getsockname()[1]
    return Arena(port=port, log_dir=log_dir, server=server, task=task)


@pytest.fixture
async def arena(tmp_path):
    instance = await start_arena(tmp_path / "logs")
    try:
        yield instance
    finally:
        await instance.stop()


DEFAULT_PROFILE = {
    "model": "none",
    "provider": "test",
    "prompt_version": "t1",
    "temperature": 0,
    "agent_version": "mock@1.0",
}


class Agent:
    """A mock agent: an ordinary WebSocket client, same as any real one."""

    def __init__(self, ws: Any, arena: Arena, code: str):
        self.ws = ws
        self.arena = arena
        self.code = code
        self.corner: Optional[str] = None
        self.token: Optional[str] = None

    @classmethod
    async def connect(cls, arena: Arena, code: str) -> "Agent":
        ws = await connect(f"{arena.ws}/ws/join/{code}")
        return cls(ws, arena, code)

    async def send(self, payload: Any) -> None:
        await self.ws.send(payload if isinstance(payload, str) else json.dumps(payload))

    async def recv(self, timeout: float = RECV_TIMEOUT) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        return json.loads(raw)

    async def expect(self, kind: str, timeout: float = RECV_TIMEOUT) -> dict[str, Any]:
        message = await self.recv(timeout=timeout)
        assert message["type"] == kind, f"expected {kind}, got {message}"
        return message

    async def join(self, name: str = "Mock", profile: Any = None) -> dict[str, Any]:
        await self.send(
            {
                "type": "join",
                "name": name,
                "profile": DEFAULT_PROFILE if profile is None else profile,
            }
        )
        reply = await self.recv()
        if reply["type"] == "joined":
            self.corner = reply["corner"]
            self.token = reply["corner_token"]
        return reply

    async def resume(self, token: str) -> dict[str, Any]:
        await self.send({"type": "resume", "corner_token": token})
        reply = await self.recv()
        if reply["type"] == "joined":
            self.corner = reply["corner"]
            self.token = reply["corner_token"]
        return reply

    async def move(self, tick: int, action: str, **telemetry: Any) -> None:
        payload: dict[str, Any] = {"type": "move", "tick": tick, "action": action}
        if telemetry:
            payload["telemetry"] = telemetry
        await self.send(payload)

    async def close(self) -> None:
        await self.ws.close()


class Spectator:
    def __init__(self, ws: Any):
        self.ws = ws

    @classmethod
    async def connect(cls, arena: Arena, code: str) -> "Spectator":
        return cls(await connect(f"{arena.ws}/ws/watch/{code}"))

    async def recv(self, timeout: float = RECV_TIMEOUT) -> dict[str, Any]:
        return json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))

    async def expect(self, kind: str, timeout: float = RECV_TIMEOUT) -> dict[str, Any]:
        """Skip past frames of other kinds — a spectator stream interleaves lobby
        updates with the fight and tests should not be brittle about ordering
        between the two."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            assert remaining > 0, f"timed out waiting for {kind}"
            message = await self.recv(timeout=remaining)
            if message["type"] == kind:
                return message

    async def drain_until(self, kind: str, timeout: float = 30.0) -> list[dict[str, Any]]:
        collected = []
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            assert remaining > 0, f"timed out collecting up to {kind}"
            message = await self.recv(timeout=remaining)
            collected.append(message)
            if message["type"] == kind:
                return collected

    async def close(self) -> None:
        await self.ws.close()


async def seated_match(arena: Arena, **config: Any) -> tuple[dict[str, Any], Agent, Agent]:
    """Create a match and fill both corners with mock agents."""
    created = await arena.create_match(**config)
    code = created["match_code"]

    a = await Agent.connect(arena, code)
    await a.join("Alpha")
    b = await Agent.connect(arena, code)
    await b.join("Bravo")
    return created, a, b
