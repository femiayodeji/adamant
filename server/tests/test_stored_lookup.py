"""Reading a finished match back after it has left memory (spec §8.1).

Every restart empties the in-memory registry of *finished* matches — recovery only
rebuilds unfinished ones. If the read endpoints could then only be keyed by
match_id, every spectator's share link and Replay button would break on deploy,
which is exactly the failure §8.1 exists to prevent.
"""

from __future__ import annotations

import server.main as main

from .conftest import seated_match, start_arena


async def _finished_match(arena):
    created, a, b = await seated_match(arena, deadline_ms=1000, max_ticks=2)
    await arena.start(created["match_code"], created["host_token"])
    await a.expect("match_start")
    await b.expect("match_start")
    for tick in (1, 2):
        await a.expect("state_update")
        await b.expect("state_update")
        await a.move(tick, "cross")
        await b.move(tick, "idle")
    await a.expect("match_end")
    await a.close()
    await b.close()
    return created


async def test_the_log_is_fetchable_by_code_once_the_match_has_left_memory(arena):
    created = await _finished_match(arena)

    # Simulate the restart: drop it from the registry, leaving only the log on disk.
    main.registry.remove(created["match_id"])

    by_code = await arena.get(f"/api/matches/{created['match_code']}/log")
    by_id = await arena.get(f"/api/matches/{created['match_id']}/log")

    assert by_code.status_code == 200, "a spectator holds a code, never an id"
    assert by_id.status_code == 200
    assert by_code.json() == by_id.json()
    assert by_code.json()["match_end"]["final_tick"] == 2


async def test_the_status_is_fetchable_by_code_once_the_match_has_left_memory(arena):
    created = await _finished_match(arena)
    main.registry.remove(created["match_id"])

    response = await arena.get(f"/api/matches/{created['match_code']}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "finished"
    assert body["match_code"] == created["match_code"]
    assert body["match_id"] == created["match_id"]
    assert body["result"]["final_tick"] == 2
    assert body["result"]["final_state"]["agent_b"]["hp"] == 80
    assert body["corners"]["agent_a"]["name"] == "Alpha"


async def test_lookup_by_code_is_case_insensitive(arena):
    created = await _finished_match(arena)
    main.registry.remove(created["match_id"])

    response = await arena.get(f"/api/matches/{created['match_code'].lower()}/log")
    assert response.status_code == 200


async def test_an_unknown_reference_is_still_404(arena):
    assert (await arena.get("/api/matches/NOPE-XXX/log")).status_code == 404
    assert (await arena.get("/api/matches/NOPE-XXX")).status_code == 404
    assert (await arena.get("/api/matches/deadbeef/log")).status_code == 404


async def test_the_log_survives_an_actual_restart_and_is_still_code_addressable(tmp_path):
    """The real thing, not a simulation: a second process, same log directory."""
    log_dir = tmp_path / "logs"
    first = await start_arena(log_dir)
    created = await _finished_match(first)
    await first.stop()

    second = await start_arena(log_dir)
    try:
        response = await second.get(f"/api/matches/{created['match_code']}/log")
        assert response.status_code == 200
        assert response.json()["match_end"]["reason"] in ("ko", "points", "draw")

        status = await second.get(f"/api/matches/{created['match_code']}")
        assert status.status_code == 200 and status.json()["status"] == "finished"
    finally:
        await second.stop()


async def test_a_stored_log_still_never_leaks_tokens(arena):
    created = await _finished_match(arena)
    main.registry.remove(created["match_id"])

    body = (await arena.get(f"/api/matches/{created['match_code']}/log")).text
    assert created["host_token"] not in body
