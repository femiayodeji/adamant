"""decide() must always return a valid action inside the deadline."""

from __future__ import annotations

import asyncio

import pytest

from agents.common.decision import (
    TIME_BUDGET_FRACTION,
    Decision,
    ReplyError,
    budget_seconds,
    decide,
    make_decider,
    parse_reply,
)
from agents.common.rules import (
    FALLBACK_ACTION,
    FALLBACK_ALTERNATE,
    MAX_CONSECUTIVE_REPEATS,
)

STATE = {
    "tick": 7,
    "deadline_ms": 400,
    "self": {"hp": 100, "stamina": 100, "points": 0, "penalties": 0},
    "opponent": {"hp": 100, "stamina": 100, "points": 0, "penalties": 0},
    "history": [],
}


def state_with_streak(action, streak):
    """A state_update whose `self` block reports an in-progress repeat streak."""
    return {**STATE, "self": {**STATE["self"], "last_action": action, "repeat_streak": streak}}


def _reply(action="hook", reason="trading into the jab"):
    return '{"action": "%s", "reason": "%s"}' % (action, reason)


def stub_model(text, tokens=(600, 20), delay=0.0, raises=None):
    async def model_call(system, user, deadline_ms):
        if delay:
            await asyncio.sleep(delay)
        if raises:
            raise raises
        return text, tokens[0], tokens[1]

    return model_call


# --- parsing ---------------------------------------------------------------


def test_parse_reply_accepts_a_bare_json_object():
    assert parse_reply(_reply()) == ("hook", "trading into the jab")


def test_parse_reply_tolerates_surrounding_whitespace_only():
    assert parse_reply("\n  " + _reply() + "  \n")[0] == "hook"


@pytest.mark.parametrize(
    "text",
    [
        "I choose hook.",
        "```json\n{\"action\": \"hook\"}\n```",  # fenced -> strict parse fails
        '{"action": "uppercut"}',
        '["hook"]',
        "",
        None,
    ],
)
def test_parse_reply_rejects_anything_else(text):
    with pytest.raises(ReplyError):
        parse_reply(text)


def test_parse_reply_truncates_reason_to_200_chars():
    _, reason = parse_reply('{"action": "jab", "reason": "%s"}' % ("x" * 500))
    assert len(reason) == 200


# --- time-boxing -----------------------------------------------------------


def test_budget_is_seventy_percent_of_the_deadline():
    assert budget_seconds(5000) == pytest.approx(5.0 * TIME_BUDGET_FRACTION)
    assert budget_seconds(30000) == pytest.approx(30.0 * TIME_BUDGET_FRACTION)
    assert budget_seconds(None) == pytest.approx(5.0 * TIME_BUDGET_FRACTION)


@pytest.mark.asyncio
async def test_happy_path_reports_full_telemetry():
    decision = await decide(stub_model(_reply()), STATE, "Jab-bot", 400)
    assert isinstance(decision, Decision)
    assert (decision.action, decision.fallback) == ("hook", False)
    telemetry = decision.telemetry()
    assert set(telemetry) == {"latency_ms", "fallback", "reason", "tokens_in", "tokens_out"}
    assert telemetry["tokens_in"] == 600 and telemetry["tokens_out"] == 20
    assert telemetry["latency_ms"] >= 0
    assert len(telemetry["reason"]) <= 200


@pytest.mark.asyncio
async def test_timeout_hedges_with_block_high_inside_the_deadline():
    # Model takes far longer than the 70% budget of a 400 ms deadline.
    decision = await decide(stub_model(_reply(), delay=5.0), STATE, None, 400)
    assert decision.action == FALLBACK_ACTION
    assert decision.fallback is True
    assert "timed out" in decision.reason
    assert decision.latency_ms < 400  # answered before the platform's deadline


@pytest.mark.asyncio
async def test_unparseable_reply_hedges():
    decision = await decide(stub_model("sure, I'll jab"), STATE, None, 400)
    assert (decision.action, decision.fallback) == (FALLBACK_ACTION, True)
    assert "bad reply" in decision.reason


@pytest.mark.asyncio
async def test_invalid_action_hedges():
    decision = await decide(stub_model('{"action": "spin_kick"}'), STATE, None, 400)
    assert (decision.action, decision.fallback) == (FALLBACK_ACTION, True)


@pytest.mark.asyncio
async def test_transport_failure_hedges_rather_than_raising():
    decision = await decide(
        stub_model(_reply(), raises=RuntimeError("429 rate limited")), STATE, None, 400
    )
    assert (decision.action, decision.fallback) == (FALLBACK_ACTION, True)
    assert "RuntimeError" in decision.reason


@pytest.mark.asyncio
async def test_deadline_is_taken_from_the_state_update_when_not_passed():
    decision = await decide(stub_model(_reply(), delay=1.0), {**STATE, "deadline_ms": 100}, None)
    assert decision.fallback is True
    assert decision.latency_ms < 100


@pytest.mark.asyncio
async def test_make_decider_matches_the_run_match_signature():
    decider = make_decider(stub_model(_reply("dodge_left")))
    decision = await decider(STATE, "Jab-bot", 400, 60)
    assert decision.action == "dodge_left"


@pytest.mark.asyncio
async def test_make_decider_pins_a_system_prompt_for_the_whole_match():
    seen = []

    async def model_call(system, user, deadline_ms):
        seen.append(system)
        return _reply(), 1, 1

    await make_decider(model_call, system="PERSONA SYSTEM")(STATE, None, 400, 60)
    assert seen == ["PERSONA SYSTEM"]


# --- the hedge under the repetition cap ------------------------------------
#
# A fallback pinned to one action fires on consecutive ticks by construction --
# a slow model is slow repeatedly. At the cap that hedge would be voided and
# leave the agent defenceless, which is the opposite of hedging.


@pytest.mark.asyncio
@pytest.mark.parametrize("streak", range(MAX_CONSECUTIVE_REPEATS))
async def test_below_the_cap_the_hedge_is_the_documented_block_high(streak):
    state = state_with_streak(FALLBACK_ACTION, streak)
    decision = await decide(stub_model("garbage"), state, None, 400)
    assert (decision.action, decision.fallback) == (FALLBACK_ACTION, True)


@pytest.mark.asyncio
async def test_at_the_cap_the_hedge_steps_sideways_instead_of_voiding():
    state = state_with_streak(FALLBACK_ACTION, MAX_CONSECUTIVE_REPEATS)
    decision = await decide(stub_model(_reply(), delay=5.0), state, None, 400)
    assert (decision.action, decision.fallback) == (FALLBACK_ALTERNATE, True)
    assert decision.latency_ms < 400, "the sidestep must not cost the deadline"


@pytest.mark.asyncio
async def test_the_sidestep_itself_can_never_void():
    """The other block is only reached when block_high is capped, and a fighter
    has one last_action, so the two can never both be capped."""
    state = state_with_streak(FALLBACK_ALTERNATE, MAX_CONSECUTIVE_REPEATS + 4)
    decision = await decide(stub_model("garbage"), state, None, 400)
    assert decision.action == FALLBACK_ACTION


@pytest.mark.asyncio
async def test_a_run_of_hedges_alternates_rather_than_repeating_forever():
    """Simulate the streak the platform would report back across six dead ticks."""
    last, streak, played = None, 0, []
    for _ in range(6):
        decision = await decide(
            stub_model("garbage"), state_with_streak(last, streak), None, 400
        )
        played.append(decision.action)
        streak = streak + 1 if decision.action == last else 1
        last = decision.action
        assert streak <= MAX_CONSECUTIVE_REPEATS, f"voided after {played}"
    assert played == [FALLBACK_ACTION] * 3 + [FALLBACK_ALTERNATE] + [FALLBACK_ACTION] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("me", [{}, {"last_action": None, "repeat_streak": None},
                                {"last_action": "block_high", "repeat_streak": "lots"}])
async def test_a_state_without_usable_streak_fields_still_hedges(me):
    """Old or odd state must never turn a hedge into an exception, which would
    become a no-move -- the one outcome worse than any action."""
    decision = await decide(stub_model("garbage"), {**STATE, "self": me}, None, 400)
    assert decision.action in (FALLBACK_ACTION, FALLBACK_ALTERNATE)
    assert decision.fallback is True


@pytest.mark.asyncio
async def test_an_action_the_model_chose_is_never_silently_repaired():
    """Only the hedge is overridden. A model that walks into the cap must be
    recorded doing it -- repairing it would launder the thing being measured."""
    state = state_with_streak("jab", MAX_CONSECUTIVE_REPEATS)
    decision = await decide(stub_model(_reply("jab")), state, None, 400)
    assert (decision.action, decision.fallback) == ("jab", False)
