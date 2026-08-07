"""Rematch (spec §8.4) and the public rules table (spec §8.5).

Both are read/write shells around things tested elsewhere — the point here is the
contract the frontend and external agent builders code against.
"""

from __future__ import annotations

from server.engine import rules
from server.engine.strategies import LADDER

from .conftest import Agent, Spectator

FAST = {"deadline_ms": 500, "max_ticks": 2}


async def _finished_bot_match(arena, **config):
    """A completed jab-vs-random match, filled through the public REST path so the
    fills are recorded exactly as a host's would be."""
    created = await arena.create_match(**(FAST | config))
    code = created["match_code"]
    token = created["host_token"]

    assert (await arena.fill_bot(code, token, corner="agent_a", bot="jab")).status_code == 200
    assert (
        await arena.fill_bot(code, token, corner="agent_b", bot="random", seed=7)
    ).status_code == 200

    watcher = await Spectator.connect(arena, code)
    try:
        assert (await arena.start(code, token)).status_code == 200
        await watcher.drain_until("match_end")
    finally:
        await watcher.close()
    return created


# ─── §8.5 rules as data ───


async def test_rules_endpoint_serves_the_engine_table_not_a_copy(arena):
    """Asserted against `engine.rules` rather than against literals: the value of
    this endpoint is that it cannot drift from the resolver, and a test with the
    numbers typed into it would drift in exactly the same way."""
    response = await arena.get("/api/rules")
    assert response.status_code == 200
    body = response.json()

    assert body["actions"] == list(rules.ACTIONS)
    assert body["attacks"] == rules.ATTACKS
    assert body["repeat_cap"] == rules.MAX_CONSECUTIVE_REPEATS
    assert body["no_move"]["penalty"] == rules.NO_MOVE_PENALTY
    assert body["no_move"]["causes"] == list(rules.NO_MOVE_CAUSES)
    assert body["economy"]["passive_regen"] == rules.PASSIVE_REGEN
    assert body["economy"]["idle_regen_bonus"] == rules.IDLE_REGEN_BONUS
    assert body["defaults"]["deadline_ms"] == rules.DEFAULT_DEADLINE_MS
    assert body["defaults"]["max_ticks_range"] == list(rules.MAX_TICKS_RANGE)
    assert body["bots"] == sorted(LADDER)

    for name, defense in body["defenses"].items():
        assert defense["counters"] == list(rules.DEFENSES[name]["counters"])
        assert defense["riposte"] == rules.DEFENSES[name]["riposte"]


async def test_rules_endpoint_needs_no_token(arena):
    """Agent builders read it before they have a match, let alone a host token."""
    assert (await arena.get("/api/rules")).status_code == 200


# ─── §8.4 rematch ───


async def _rematch(arena, ref: str, token: str | None):
    headers = {"X-Host-Token": token} if token else {}
    async with arena.client() as http:
        return await http.post(f"/api/matches/{ref}/rematch", headers=headers)


async def test_rematch_of_an_unfinished_match_is_409(arena):
    created = await arena.create_match()
    response = await _rematch(arena, created["match_code"], created["host_token"])
    assert response.status_code == 409
    assert "lobby" in response.json()["detail"]


async def test_rematch_without_the_host_token_is_401(arena):
    created = await arena.create_match()
    assert (await _rematch(arena, created["match_code"], None)).status_code == 401
    assert (await _rematch(arena, created["match_code"], "ht_nope")).status_code == 401


async def test_rematch_of_an_unknown_match_is_404(arena):
    assert (await _rematch(arena, "NOPE-XXX", "ht_whatever")).status_code == 404


async def test_rematch_is_a_new_match_under_the_same_config(arena):
    """A new code, not a reset: one log is one fight, so the previous match's
    result and log have to survive the rematch untouched."""
    first = await _finished_bot_match(arena, max_ticks=2, deadline_ms=500)

    response = await _rematch(arena, first["match_code"], first["host_token"])
    assert response.status_code == 200
    second = response.json()

    assert second["match_code"] != first["match_code"]
    assert second["match_id"] != first["match_id"]
    assert second["host_token"] != first["host_token"]
    assert second["rematch_of"] == first["match_code"]
    assert second["config"] == first["config"]

    # The fought match is still there, still finished, still holding its log.
    previous = await arena.get(f"/api/matches/{first['match_code']}")
    assert previous.status_code == 200
    assert previous.json()["status"] == "finished"
    assert (await arena.get(f"/api/matches/{first['match_code']}/log")).json()["ticks"]


async def test_rematch_puts_the_same_bots_back_in_the_same_corners(arena):
    """The whole point for a ladder run: fighting the same baseline again is one
    click, and it is the *same* baseline — same bot, same seed, same corner."""
    first = await _finished_bot_match(arena)

    second = (await _rematch(arena, first["match_code"], first["host_token"])).json()

    assert {b["corner"]: b["bot"] for b in second["bots"]} == {
        "agent_a": "jab",
        "agent_b": "random",
    }
    assert [b["seed"] for b in second["bots"] if b["corner"] == "agent_b"] == [7]

    # Filled for real, over the public protocol, and ready to be started.
    status = (await arena.get(f"/api/matches/{second['match_code']}")).json()
    assert status["status"] == "lobby"
    assert status["corners"]["agent_a"]["name"] == "Jab-bot"
    assert status["corners"]["agent_b"]["name"] == "Random-bot"
    assert (await arena.start(second["match_code"], second["host_token"])).status_code == 200


async def test_rematch_leaves_a_human_corner_open_for_the_agent_to_dial(arena):
    """An externally-run agent cannot be re-spawned — it is somebody else's process.
    Its corner comes back empty, and the new code is what the host sends them."""
    created = await arena.create_match(**FAST)
    code, token = created["match_code"], created["host_token"]

    human = await Agent.connect(arena, code)
    await human.join("Human")
    assert (await arena.fill_bot(code, token, corner="agent_b", bot="jab")).status_code == 200
    assert (await arena.start(code, token)).status_code == 200

    watcher = await Spectator.connect(arena, code)
    try:
        await watcher.drain_until("match_end")
    finally:
        await watcher.close()
        await human.close()

    second = (await _rematch(arena, code, token)).json()
    assert [b["corner"] for b in second["bots"]] == ["agent_b"]

    status = (await arena.get(f"/api/matches/{second['match_code']}")).json()
    assert status["corners"]["agent_a"] is None
    assert status["corners"]["agent_b"]["name"] == "Jab-bot"
