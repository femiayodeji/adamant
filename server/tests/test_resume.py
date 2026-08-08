"""Pause, resume, and crash recovery (spec §6.4) — plan Principle 6.

The rule under test: *a missed move is penalised; a broken connection is not.* A
network blip must never be recorded as a combat loss, and a resume must not hand
either fighter an advantage.
"""

from __future__ import annotations

import asyncio

from .conftest import Agent, Spectator, seated_match, start_arena


async def _running_match(arena, **config):
    created, a, b = await seated_match(arena, **config)
    spectator = await Spectator.connect(arena, created["match_code"])
    await arena.start(created["match_code"], created["host_token"])
    await a.expect("match_start")
    await b.expect("match_start")
    return created, a, b, spectator


async def test_a_closed_socket_pauses_the_match_rather_than_resolving_the_tick(arena):
    created, a, b, spectator = await _running_match(arena, deadline_ms=500, max_ticks=5)
    await a.expect("state_update")
    await b.expect("state_update")

    await b.close()

    paused = await a.expect("match_paused", timeout=5)
    assert paused == {
        "type": "match_paused",
        "tick": 1,
        "missing": "agent_b",
        "resume_window_ms": 120000,
    }
    assert (await spectator.expect("match_paused", timeout=5))["missing"] == "agent_b"

    # The deadline was 500ms. Well past it, the tick still has not resolved — a
    # closed socket is infrastructure, not a no-move.
    await asyncio.sleep(1.0)
    status = (await arena.get(f"/api/matches/{created['match_code']}")).json()
    assert status["status"] == "paused" and status["tick"] == 0

    await a.close()
    await spectator.close()


async def test_a_spectator_arriving_mid_pause_is_told_the_match_is_paused(arena):
    """`match_paused` is broadcast once, when the corner drops, and never again.

    Anyone who opens the watch link during the pause — or whose own socket blipped
    through it — would otherwise be shown a frozen arena with no overlay and no
    explanation, because the catch-up replays the ticks and the result but said
    nothing about the pause. The window it reports is the part still to run, not the
    configured total, or the countdown on screen outlives the real one.
    """
    created, a, b, first = await _running_match(arena, deadline_ms=500, max_ticks=5)
    await a.expect("state_update")
    await b.expect("state_update")

    await b.close()
    await first.expect("match_paused", timeout=5)
    await asyncio.sleep(0.5)

    late = await Spectator.connect(arena, created["match_code"])
    paused = await late.expect("match_paused", timeout=5)

    assert paused["missing"] == "agent_b"
    assert paused["tick"] == 1, "the tick that is waiting to be re-issued"
    assert 0 < paused["resume_window_ms"] < 120000, paused["resume_window_ms"]

    await a.close()
    await first.close()
    await late.close()


async def test_resume_reissues_the_same_tick_with_a_fresh_deadline_and_no_penalty(arena):
    created, a, b, spectator = await _running_match(arena, deadline_ms=500, max_ticks=3)
    await a.expect("state_update")
    await b.expect("state_update")
    token = b.token

    await b.close()
    await a.expect("match_paused")
    await spectator.expect("match_paused")

    back = await Agent.connect(arena, created["match_code"])
    assert (await back.resume(token))["corner"] == "agent_b"

    # A resuming agent may be a fresh process that never saw the original
    # match_start, so it is re-sent before play continues.
    restart = await back.expect("match_start")
    assert restart["you"] == "agent_b" and restart["opponent_name"] == "Alpha"
    assert restart["config"]["deadline_ms"] == 500

    assert (await a.expect("match_resumed"))["tick"] == 1
    await back.expect("match_resumed")
    await spectator.expect("match_resumed")

    reissued_a = await a.expect("state_update")
    reissued_b = await back.expect("state_update")
    assert reissued_a["tick"] == 1 and reissued_b["tick"] == 1, "the tick does not advance"
    assert reissued_a["deadline_ms"] == 500

    await a.move(1, "jab")
    await back.move(1, "block_high")

    result = await spectator.expect("tick_result", timeout=5)
    assert result["tick"] == 1
    assert not [e for e in result["events"] if e["type"] == "no_move"]
    assert result["state"]["agent_b"]["penalties"] == 0, "a disconnect is never penalised"

    await a.close()
    await back.close()
    await spectator.close()


async def test_a_move_submitted_before_the_pause_is_discarded(arena):
    """Both agents answer the re-issued tick. Discarding narrows — though it cannot
    fully close — the advantage the connected agent gets from having already seen
    the position (spec §6.4, known limitation)."""
    created, a, b, spectator = await _running_match(arena, deadline_ms=2000, max_ticks=2)
    await a.expect("state_update")
    await b.expect("state_update")

    await a.move(1, "hook")  # submitted, then the opponent drops
    token = b.token
    await b.close()
    await a.expect("match_paused")

    back = await Agent.connect(arena, created["match_code"])
    await back.resume(token)
    await back.expect("match_start")
    await a.expect("match_resumed")
    await back.expect("match_resumed")
    await a.expect("state_update")
    await back.expect("state_update")

    await a.move(1, "jab")  # a different answer to the same tick
    await back.move(1, "idle")

    result = await spectator.expect("tick_result", timeout=5)
    assert result["moves"]["agent_a"] == "jab", "the pre-pause hook must not survive"

    await a.close()
    await back.close()
    await spectator.close()


async def test_an_unknown_corner_token_is_refused(arena):
    """The match code alone is deliberately insufficient — anyone holding the share
    link could otherwise seize a corner mid-match."""
    created, a, b, spectator = await _running_match(arena, deadline_ms=500, max_ticks=2)
    await a.expect("state_update")
    await b.expect("state_update")
    await b.close()
    await a.expect("match_paused")

    impostor = await Agent.connect(arena, created["match_code"])
    assert await impostor.resume("ct_deadbeef") == {"type": "error", "reason": "invalid_token"}

    await a.close()
    await spectator.close()


async def test_the_resume_window_expiring_abandons_the_match(arena):
    created, a, b, spectator = await _running_match(arena, deadline_ms=500, max_ticks=5)
    arena.match(created["match_code"]).config.resume_window_ms = 400

    await a.expect("state_update")
    await b.expect("state_update")
    await b.close()
    await a.expect("match_paused")

    end = await a.expect("match_end", timeout=5)
    assert end["reason"] == "abandoned"
    assert end["winner"] == "agent_a", "the remaining fighter is named as winner"

    status = (await arena.get(f"/api/matches/{created['match_code']}")).json()
    assert status["status"] == "abandoned"

    logged = (await arena.get(f"/api/matches/{created['match_code']}/log")).json()
    assert logged["match_end"]["reason"] == "abandoned", (
        "abandoned must be distinguishable in the log so it can be excluded from aggregates"
    )

    await a.close()
    await spectator.close()


async def test_both_corners_abandoning_is_a_draw(arena):
    created, a, b, spectator = await _running_match(arena, deadline_ms=500, max_ticks=5)
    arena.match(created["match_code"]).config.resume_window_ms = 400

    await a.expect("state_update")
    await b.expect("state_update")
    await a.close()
    await b.close()

    end = await spectator.expect("match_end", timeout=5)
    assert end["reason"] == "abandoned"
    assert end["winner"] is None, "with both gone there is no remaining fighter to name"

    await spectator.close()


async def test_one_disconnect_announces_the_pause_exactly_once(arena):
    """One `match_paused` per absent corner, not per poll of the resume loop. A
    spectator that counts frames to drive a countdown overlay would otherwise see
    the timer restart."""
    created, a, b = await seated_match(arena, deadline_ms=5000, max_ticks=6)
    spectator = await Spectator.connect(arena, created["match_code"])
    await spectator.expect("lobby_update")
    await arena.start(created["match_code"], created["host_token"])
    await a.expect("match_start")
    await b.expect("match_start")
    await spectator.expect("match_start")
    await a.expect("state_update")
    await b.expect("state_update")

    await b.close()
    await asyncio.sleep(1.0)  # several passes of the resume loop

    frames = []
    while True:
        try:
            frames.append(await spectator.expect("match_paused", timeout=0.4))
        except (AssertionError, asyncio.TimeoutError, Exception):
            break

    assert len(frames) == 1, f"expected one pause announcement, got {len(frames)}"
    assert frames[0]["missing"] == "agent_b"

    await a.close()
    await spectator.close()


async def test_a_server_restart_rebuilds_the_match_from_its_log(tmp_path):
    """Resumability covers the server too. Because `resolve()` is pure and the log is
    an event stream, replaying the log reconstructs exact state — which is why the
    MVP needs no database (spec §6.4c)."""
    log_dir = tmp_path / "logs"
    first = await start_arena(log_dir)

    created, a, b = await seated_match(first, deadline_ms=1000, max_ticks=5)
    await first.start(created["match_code"], created["host_token"])
    await a.expect("match_start")
    await b.expect("match_start")

    for tick in (1, 2):
        await a.expect("state_update")
        await b.expect("state_update")
        await a.move(tick, "cross")
        await b.move(tick, "idle")
        await asyncio.sleep(0.05)

    token_a, token_b = a.token, b.token
    await first.stop()  # the process dies mid-match

    second = await start_arena(log_dir)
    try:
        status = (await second.get(f"/api/matches/{created['match_code']}")).json()
        assert status["status"] == "paused", "unfinished matches reopen for resume"
        assert status["tick"] == 2
        assert status["corners"]["agent_a"]["name"] == "Alpha"
        # 2 ticks of cross into idle: 10 damage each, from a rebuilt state.
        assert (await second.get(f"/api/matches/{created['match_code']}/log")).json()["ticks"][1][
            "state"
        ]["agent_b"]["hp"] == 80

        back_a = await Agent.connect(second, created["match_code"])
        back_b = await Agent.connect(second, created["match_code"])
        assert (await back_a.resume(token_a))["corner"] == "agent_a"
        assert (await back_b.resume(token_b))["corner"] == "agent_b"

        # Both are new processes on a new server; each needs the config re-sent.
        assert (await back_a.expect("match_start"))["config"]["max_ticks"] == 5
        assert (await back_b.expect("match_start"))["opponent_name"] == "Alpha"

        await back_a.expect("match_resumed", timeout=5)
        await back_b.expect("match_resumed", timeout=5)

        update = await back_a.expect("state_update")
        await back_b.expect("state_update")
        assert update["tick"] == 3, "play continues from where the crash interrupted it"
        assert update["opponent"]["hp"] == 80, "state survived the restart"
        assert [h["tick"] for h in update["history"]] == [2, 1], "so did the history window"

        for tick in (3, 4, 5):
            if tick > 3:
                await back_a.expect("state_update")
                await back_b.expect("state_update")
            # A fourth consecutive `cross` would void (spec §4.6), so tick 4 breaks
            # the streak with a jab. The streak is part of the state rebuilt from the
            # log, which is the point: it has to survive the restart like everything
            # else, or tick 4 would be judged against a streak of zero.
            await back_a.move(tick, "jab" if tick == 4 else "cross")
            await back_b.move(tick, "idle")

        end = await back_a.expect("match_end", timeout=5)
        assert end["final_tick"] == 5
        assert end["final_state"]["agent_b"]["hp"] == 55, "4 crosses and a jab across two processes"

        logged = (await second.get(f"/api/matches/{created['match_code']}/log")).json()
        assert [t["tick"] for t in logged["ticks"]] == [1, 2, 3, 4, 5], "one continuous log"

        await back_a.close()
        await back_b.close()
    finally:
        await second.stop()
