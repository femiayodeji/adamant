"""Phase 3 gate: the built-in bot ladder (spec §11).

Bots connect over the same public WebSocket protocol as any external agent, so these
tests double as end-to-end coverage of that protocol from the client side.
"""

from __future__ import annotations

import pytest

from server.engine.strategies import LADDER

from .conftest import Agent, Spectator, seated_match

LADDER_NAMES = sorted(LADDER)


async def _bot_match(arena, bot_a: str, bot_b: str, seed: int = 1, **config):
    created = await arena.create_match(**config)
    code, host = created["match_code"], created["host_token"]

    first = await arena.fill_bot(code, host, corner="agent_a", bot=bot_a, seed=seed)
    second = await arena.fill_bot(code, host, corner="agent_b", bot=bot_b, seed=seed)
    assert first.status_code == 200 and second.status_code == 200

    spectator = await Spectator.connect(arena, code)
    assert (await arena.start(code, host)).status_code == 200
    return created, spectator


async def test_a_bot_versus_bot_match_completes_and_persists(arena):
    """The gate: create → fill both corners → start → complete, with an intact log."""
    created, spectator = await _bot_match(
        arena, "jab", "random", deadline_ms=2000, max_ticks=20
    )

    stream = await spectator.drain_until("match_end", timeout=60)
    end = stream[-1]
    ticks = [m for m in stream if m["type"] == "tick_result"]

    assert end["reason"] in ("ko", "points", "draw")
    assert (end["winner"] is None) == (end["reason"] == "draw")
    assert ticks, "the tick stream must not be empty"
    assert [t["tick"] for t in ticks] == list(range(1, len(ticks) + 1))

    logged = (await arena.get(f"/api/matches/{created['match_code']}/log")).json()
    assert logged["schema_version"] == 1
    assert len(logged["ticks"]) == len(ticks)
    assert logged["match_end"] == end

    # Both profiles present — a result that cannot say what produced it is not a
    # measurement, and this holds for baselines too.
    agents = logged["match_start"]["agents"]
    assert agents["agent_a"]["profile"]["agent_version"] == "jabbot@1.0"
    assert agents["agent_b"]["profile"]["agent_version"] == "randombot@1.0"
    assert agents["agent_a"]["profile"]["seed"] == 1
    assert agents["agent_a"]["name"] == "Jab-bot"

    await spectator.close()


@pytest.mark.parametrize("bot", LADDER_NAMES)
async def test_every_ladder_bot_can_fight_a_full_match(arena, bot):
    _, spectator = await _bot_match(arena, bot, "jab", deadline_ms=2000, max_ticks=12)

    stream = await spectator.drain_until("match_end", timeout=60)
    ticks = [m for m in stream if m["type"] == "tick_result"]

    assert ticks
    assert stream[-1]["reason"] in ("ko", "points", "draw")
    # Bots answer every tick, so no baseline should ever be recorded as no-moving.
    for record in ticks:
        assert not [e for e in record["events"] if e["type"] == "no_move"], record

    await spectator.close()


async def test_bots_never_submit_an_action_outside_the_published_space(arena):
    from server.engine import rules

    _, spectator = await _bot_match(arena, "mixer", "frequency", deadline_ms=2000, max_ticks=15)
    stream = await spectator.drain_until("match_end", timeout=60)

    for record in [m for m in stream if m["type"] == "tick_result"]:
        for action in record["moves"].values():
            assert action in rules.ACTIONS

    await spectator.close()


async def test_the_same_seed_reproduces_the_same_fight(arena):
    """Determinism given a seed is what makes the ladder a measuring instrument
    rather than a source of noise."""
    runs = []
    for _ in range(2):
        _, spectator = await _bot_match(
            arena, "random", "mixer", seed=42, deadline_ms=2000, max_ticks=15
        )
        stream = await spectator.drain_until("match_end", timeout=60)
        runs.append([m["moves"] for m in stream if m["type"] == "tick_result"])
        await spectator.close()

    assert runs[0] == runs[1]


async def test_a_bot_can_fight_an_external_agent(arena):
    """No privileged internal path: a bot and a scripted agent share one protocol."""
    created = await arena.create_match(deadline_ms=2000, max_ticks=6)
    code, host = created["match_code"], created["host_token"]

    human = await Agent.connect(arena, code)
    await human.join("Challenger")
    assert (await arena.fill_bot(code, host, corner="agent_b", bot="jab")).status_code == 200

    await arena.start(code, host)
    start = await human.expect("match_start")
    assert start["opponent_name"] == "Jab-bot"

    for tick in range(1, 7):
        await human.expect("state_update")
        await human.move(tick, "block_high")

    end = await human.expect("match_end", timeout=10)
    assert end["reason"] == "draw", "always-block_high against Jab-bot is the drawing line"
    assert end["final_state"]["agent_a"]["hp"] == 100
    assert end["final_state"]["agent_b"]["hp"] == 100

    await human.close()


# ─── fill_bot contract (spec §8) ───


async def test_fill_bot_requires_the_host_token(arena):
    created = await arena.create_match()
    async with arena.client() as http:
        response = await http.post(
            f"/api/matches/{created['match_code']}/fill_bot",
            json={"corner": "agent_a", "bot": "jab"},
        )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "body", [{"corner": "agent_c", "bot": "jab"}, {"corner": "agent_a", "bot": "boxer"}]
)
async def test_fill_bot_validates_its_arguments(arena, body):
    created = await arena.create_match()
    response = await arena.fill_bot(created["match_code"], created["host_token"], **body)
    assert response.status_code == 422


async def test_filling_an_occupied_corner_is_409(arena):
    created = await arena.create_match()
    code, host = created["match_code"], created["host_token"]

    assert (await arena.fill_bot(code, host, corner="agent_a", bot="jab")).status_code == 200
    second = await arena.fill_bot(code, host, corner="agent_a", bot="random")
    assert second.status_code == 409


async def test_filling_a_corner_after_the_bell_is_409(arena):
    created, a, b = await seated_match(arena, deadline_ms=500, max_ticks=2)
    await arena.start(created["match_code"], created["host_token"])

    response = await arena.fill_bot(
        created["match_code"], created["host_token"], corner="agent_a", bot="jab"
    )
    assert response.status_code == 409

    await a.close()
    await b.close()


async def test_fill_bot_honours_the_requested_corner(arena):
    """The lobby's "add a bot" button sits on a specific corner card, so asking for
    agent_b while agent_a is still empty has to work."""
    created = await arena.create_match()
    response = await arena.fill_bot(
        created["match_code"], created["host_token"], corner="agent_b", bot="jab"
    )

    assert response.status_code == 200 and response.json()["corner"] == "agent_b"
    status = (await arena.get(f"/api/matches/{created['match_code']}")).json()
    assert status["corners"]["agent_a"] is None
    assert status["corners"]["agent_b"]["name"] == "Jab-bot"


async def test_a_corner_preference_is_ignored_when_that_corner_is_taken(arena):
    created = await arena.create_match()
    code, host = created["match_code"], created["host_token"]

    assert (await arena.fill_bot(code, host, corner="agent_b", bot="jab")).json()["corner"] == "agent_b"
    second = await arena.fill_bot(code, host, corner="agent_a", bot="random")
    assert second.json()["corner"] == "agent_a"


async def test_a_bot_joining_shows_up_in_the_lobby(arena):
    created = await arena.create_match()
    spectator = await Spectator.connect(arena, created["match_code"])
    await spectator.expect("lobby_update")

    await arena.fill_bot(
        created["match_code"], created["host_token"], corner="agent_a", bot="frequency"
    )

    update = await spectator.expect("lobby_update", timeout=5)
    assert update["corners"]["agent_a"]["name"] == "Frequency-bot"
    assert update["corners"]["agent_b"] is None

    await spectator.close()
