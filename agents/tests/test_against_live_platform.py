"""The published agent and the harness, driven against a **real** running platform.

Everything else in `agents/tests/` runs against a mock WS server, because the
platform did not exist when those were written. These tests close that gap: a real
uvicorn process, real sockets, a real deadline clock, and a real built-in bot in the
other corner. Only the model is stubbed — no API call is made and no budget spent.

What this actually proves, which a mock cannot: that the agent's 70% time-box beats
a live deadline, that its hedge arrives as a real move rather than a no-move, and
that the corner-swapping convention the harness relies on holds against the server's
own corner assignment.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agents.common.decision import make_decider
from agents.common.wsloop import run_match
from agents.harness.runner import ArenaEndpoints, ArenaRunner, MatchSpec
from server.tests.conftest import start_arena

pytestmark = pytest.mark.asyncio

PROFILE = {
    "model": "stub",
    "provider": "test",
    "prompt_version": "ref-v1",
    "temperature": 0,
    "agent_version": "stub@1.0",
}


@pytest.fixture
async def arena(tmp_path):
    instance = await start_arena(tmp_path / "logs")
    try:
        yield instance
    finally:
        await instance.stop()


class TradeLineModel:
    """Stands in for a model that has found the Jab-bot answer (spec §11): hook
    into an incoming jab is a trade worth net +10, block otherwise."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, system: str, user: str, deadline_ms: int):
        self.calls += 1
        assert "jab" in system.lower() or "jab" in user.lower(), "rules must reach the prompt"
        action = "hook" if self.calls % 4 == 0 else "block_high"
        return json.dumps({"action": action, "reason": "hook into the jab is a trade"}), 640, 25


async def _versus_jab_bot(arena, decider, *, deadline_ms=2000, max_ticks=60):
    created = await arena.create_match(deadline_ms=deadline_ms, max_ticks=max_ticks)
    code, host = created["match_code"], created["host_token"]
    await arena.fill_bot(code, host, corner="agent_b", bot="jab")

    agent = asyncio.create_task(run_match(arena.ws, code, "RefBot", PROFILE, decider))
    await asyncio.sleep(0.2)
    assert (await arena.start(code, host)).status_code == 200

    end = await asyncio.wait_for(agent, timeout=120)
    log = (await arena.get(f"/api/matches/{code}/log")).json()
    return end, log


async def test_the_published_agent_loop_kos_jab_bot_on_the_real_platform(arena):
    model = TradeLineModel()
    end, log = await _versus_jab_bot(arena, make_decider(model))

    assert end is not None, "the agent did not reach match_end"
    assert end["reason"] == "ko" and end["winner"] == "agent_a"
    # The same figure the headless simulation and the server tests produce.
    assert end["final_tick"] == 28

    assert log["match_start"]["agents"]["agent_a"]["profile"]["prompt_version"] == "ref-v1"
    assert log["match_start"]["agents"]["agent_b"]["name"] == "Jab-bot"

    telemetry = [t["telemetry"]["agent_a"] for t in log["ticks"]]
    assert all(t["fallback"] is False for t in telemetry)
    assert all(t["tokens_in"] == 640 for t in telemetry), "telemetry reaches the log verbatim"
    assert all(t["reason"] for t in telemetry), "a log without reasons is not a transcript"


async def test_a_slow_model_hedges_in_time_instead_of_no_moving(arena):
    """The point of the time-box. Under v0.6 rules a hedge is strictly better than
    silence — a no-move is defenceless and costs 15 — so the agent must always get
    *something* on the wire before the deadline.

    And the hedge must survive the repetition cap. Five ticks of hedging is one
    tick past the cap: a fallback pinned to `block_high` would be voided on the
    fourth and stand the agent up defenceless, which is the opposite of hedging.
    So the hedge steps sideways on the fourth and the streak resets."""

    async def never_answers(system, user, deadline_ms):
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    end, log = await _versus_jab_bot(
        arena, make_decider(never_answers), deadline_ms=500, max_ticks=5
    )

    assert end is not None and end["final_tick"] == 5
    played = [record["moves"]["agent_a"] for record in log["ticks"]]
    assert played == ["block_high", "block_high", "block_high", "block_low", "block_high"]

    for record in log["ticks"]:
        assert record["telemetry"]["agent_a"]["fallback"] is True
        assert not [e for e in record["events"] if e["type"] == "no_move"], (
            "a hedge that arrives late is worse than useless — it becomes a penalty"
        )
        assert not [
            e for e in record["events"] if e["type"] == "void" and e["who"] == "agent_a"
        ], "a hedge that voids itself is not a hedge"
    assert log["match_end"]["final_state"]["agent_a"]["penalties"] == 0


async def test_an_unparseable_model_never_produces_an_invalid_action(arena):
    """Phase 5's validity criterion, measurable without a live model: whatever the
    model says, the agent must never put a non-action on the wire."""

    async def babbles(system, user, deadline_ms):
        return "Sure! I'd throw a **super punch** here.", 100, 20

    end, log = await _versus_jab_bot(arena, make_decider(babbles), deadline_ms=800, max_ticks=4)

    assert end is not None
    causes = [
        e["cause"]
        for record in log["ticks"]
        for e in record["events"]
        if e["type"] == "no_move" and e["who"] == "agent_a"
    ]
    assert causes == [], "0 malformed or invalid_action no-moves"
    assert all(t["telemetry"]["agent_a"]["fallback"] is True for t in log["ticks"])


async def test_a_mid_match_disconnect_is_survived_by_the_agent_loop(arena):
    """`run_match` reclaims its corner with the corner_token and plays on. From the
    agent's side a pause is invisible: it just answers a re-issued state_update."""
    created = await arena.create_match(deadline_ms=1500, max_ticks=6)
    code, host = created["match_code"], created["host_token"]
    await arena.fill_bot(code, host, corner="agent_b", bot="jab")

    dropped = asyncio.Event()

    async def drop_once(system, user, deadline_ms):
        if not dropped.is_set():
            dropped.set()
            # Kill the socket from under the loop, mid-decision.
            for slot in arena.match(code).corners.values():
                if slot and slot.name == "RefBot" and slot.ws is not None:
                    await slot.ws.close()
        return json.dumps({"action": "block_high", "reason": "guard up"}), 10, 5

    agent = asyncio.create_task(
        run_match(arena.ws, code, "RefBot", PROFILE, make_decider(drop_once),
                  reconnect_delay_s=0.2)
    )
    await asyncio.sleep(0.2)
    await arena.start(code, host)

    end = await asyncio.wait_for(agent, timeout=60)
    assert end is not None, "the agent failed to reclaim its corner"
    assert end["reason"] != "abandoned"
    assert end["final_tick"] == 6

    log = (await arena.get(f"/api/matches/{code}/log")).json()
    assert log["match_end"]["final_state"]["agent_a"]["penalties"] == 0, (
        "a broken connection must never be scored as a combat failure"
    )


# ─── harness ───


class StubClient:
    """A LiteLLMClient-shaped stand-in: same `profile()` and same call signature."""

    def __init__(self, spec: MatchSpec) -> None:
        self.spec = spec

    def profile(self) -> dict:
        return {
            "model": "stub/model",
            "resolved_model": "stub/model-2026-01",
            "provider": "openrouter",
            "prompt_version": "ref-v1",
            "temperature": 0,
            "agent_version": "harness@1.0",
        }

    async def __call__(self, system: str, user: str, deadline_ms: int):
        return json.dumps({"action": "block_high", "reason": "hold the guard"}), 500, 20


@pytest.mark.parametrize("corner", ["agent_a", "agent_b"])
async def test_the_harness_runs_a_match_end_to_end_in_either_corner(arena, corner):
    """Corner swapping is a plan-mandated convention, and it is only reproducible
    because `join` takes a corner preference — otherwise corners fall out of join
    order and the harness cannot control them."""
    runner = ArenaRunner(
        endpoints=ArenaEndpoints(http_url=arena.http, ws_url=arena.ws),
        make_client=StubClient,
        name="HarnessBot",
    )
    spec = MatchSpec(
        pair_id="p1",
        corner=corner,
        entrant="stub/model",
        opponent="jab",
        opponent_kind="bot",
        deadline_ms=1500,
        max_ticks=5,
        seed=1,
        repeat=0,
    )

    row = await runner.run_one(spec)

    assert row["match_id"]
    assert row["corner"] == corner
    assert row["deadline_ms"] == 1500, "the bracket travels with every result row"
    assert row["reason"] in ("ko", "points", "draw")

    status = (await arena.get(f"/api/matches/{row['match_id']}")).json()
    other = "agent_b" if corner == "agent_a" else "agent_a"
    assert status["corners"][corner]["name"] == "HarnessBot"
    assert status["corners"][other]["name"] == "Jab-bot", "the bot took the opposite corner"

    await runner.http.aclose()
