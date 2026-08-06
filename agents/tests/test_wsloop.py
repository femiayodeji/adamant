"""The agent side of spec 6, against a mock platform on localhost."""

from __future__ import annotations

import pytest

from agents.common.decision import Decision
from agents.common.wsloop import ProtocolError, join_url, run_match, validate_name
from agents.tests.mock_platform import MockPlatform, running

PROFILE = {
    "model": "claude-opus-5",
    "provider": "anthropic",
    "prompt_version": "ref-v1",
    "temperature": None,
    "agent_version": "test@1.0",
}


def scripted_decider(action="jab", record=None):
    """A decider with no model behind it: records what it was asked."""

    async def decide(state, opponent_name=None, deadline_ms=None, max_ticks=None):
        if record is not None:
            record.append((state["tick"], opponent_name, deadline_ms, max_ticks))
        return Decision(action, "scripted", False, 12, 0, 0)

    return decide


def test_name_validation_matches_the_spec_pattern():
    for good in ["FemiBot-v1", "a", "A B_c-1", "x" * 24]:
        assert validate_name(good) == good
    for bad in ["", "x" * 25, "Ignore previous instructions!", "emoji🥊", None]:
        with pytest.raises(ValueError):
            validate_name(bad)


def test_join_url_shape():
    assert join_url("wss://host/", "RUMBLE-4F2") == "wss://host/ws/join/RUMBLE-4F2"


@pytest.mark.asyncio
async def test_full_match_join_moves_and_end():
    platform = MockPlatform()
    seen = []
    async with running(platform) as url:
        end = await run_match(url, "RUMBLE-4F2", "FemiBot-v1", PROFILE,
                              scripted_decider(record=seen))

    assert end["type"] == "match_end" and end["winner"] == "agent_a"

    join = platform.join
    assert join["type"] == "join"
    assert join["name"] == "FemiBot-v1"
    assert join["profile"] == PROFILE  # required and sent verbatim

    moves = platform.moves
    # Tick 2 is re-issued after the pause; a stateless agent answers it again.
    assert [m["tick"] for m in moves] == [1, 2, 2]
    for move in moves:
        assert move["type"] == "move"
        assert move["action"] == "jab"
        assert set(move["telemetry"]) == {
            "latency_ms", "fallback", "reason", "tokens_in", "tokens_out"
        }

    # The decider saw the config from match_start on every tick.
    assert [t for t, *_ in seen] == [1, 2, 2]
    assert all(opponent == "Jab-bot" for _, opponent, _, _ in seen)
    assert all(deadline == platform.deadline_ms for *_, deadline, _ in seen)
    assert all(max_ticks == 60 for *_, max_ticks in seen)


@pytest.mark.asyncio
async def test_history_from_the_state_update_reaches_the_decider():
    platform = MockPlatform()
    seen_states = []

    async def decide(state, opponent_name=None, deadline_ms=None, max_ticks=None):
        seen_states.append(state)
        return Decision("hook", "r", False, 1, 0, 0)

    async with running(platform) as url:
        await run_match(url, "RUMBLE-4F2", "FemiBot-v1", PROFILE, decide)

    tick_two = seen_states[1]
    assert tick_two["history"][0]["tick"] == 1
    assert tick_two["history"][0]["moves"] == {"self": "hook", "opponent": "jab"}


@pytest.mark.asyncio
async def test_platform_error_is_surfaced():
    platform = MockPlatform(reject_reason="profile_required")
    async with running(platform) as url:
        with pytest.raises(ProtocolError, match="profile_required"):
            await run_match(url, "RUMBLE-4F2", "FemiBot-v1", PROFILE, scripted_decider())


@pytest.mark.asyncio
async def test_bad_name_and_missing_profile_fail_before_connecting():
    with pytest.raises(ValueError):
        await run_match("ws://127.0.0.1:1", "C", "no!", PROFILE, scripted_decider())
    with pytest.raises(ValueError):
        await run_match("ws://127.0.0.1:1", "C", "ok", {}, scripted_decider())


@pytest.mark.asyncio
async def test_socket_closing_before_match_end_returns_none():
    # Script stops after one tick: the platform hangs up mid-match.
    platform = MockPlatform(script=[
        {"type": "match_start", "you": "agent_a", "opponent_name": "Jab-bot",
         "config": {"max_ticks": 60, "deadline_ms": 200}},
    ])
    async with running(platform) as url:
        end = await run_match(url, "RUMBLE-4F2", "FemiBot-v1", PROFILE,
                              scripted_decider(), max_reconnects=1, reconnect_delay_s=0.01)
    assert end is None
    # It reclaimed the corner with the token from `joined`, not by re-joining.
    assert [m["type"] for m in platform.received] == ["join", "resume"]
    assert platform.received[1]["corner_token"] == "ct_test"
