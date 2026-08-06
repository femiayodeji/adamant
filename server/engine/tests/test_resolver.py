"""Resolution rules (spec §4)."""

from __future__ import annotations

import pytest

from server.engine import rules
from server.engine.resolver import (
    Fighter,
    MatchState,
    NoMove,
    check_end,
    initial_state,
    resolve,
    to_egocentric,
)


def state(**kwargs) -> MatchState:
    a = Fighter(**kwargs.pop("a", {}))
    b = Fighter(**kwargs.pop("b", {}))
    return MatchState(agent_a=a, agent_b=b, **kwargs)


def types(events) -> list[str]:
    return [e["type"] for e in events]


def only(events, kind) -> dict:
    matching = [e for e in events if e["type"] == kind]
    assert len(matching) == 1, f"expected exactly one {kind}, got {types(events)}"
    return matching[0]


# ─── §4.2 attack outcomes ───


def test_correct_block_negates_damage_and_does_not_riposte():
    new, events = resolve(initial_state(), "jab", "block_high")

    assert new.agent_b.hp == 100
    assert new.agent_a.hp == 100, "blocks do not riposte"
    assert new.agent_a.points == 0
    assert only(events, "blocked") == {
        "type": "blocked",
        "attacker": "agent_a",
        "defender": "agent_b",
        "action": "jab",
        "defense": "block_high",
    }


def test_correct_dodge_negates_and_ripostes_five():
    new, events = resolve(initial_state(), "jab", "dodge_right")

    assert new.agent_b.hp == 100, "dodge negates"
    assert new.agent_a.hp == 95, "attacker eats 5 chip damage"
    assert new.agent_b.points == 5, "riposte damage is added to the dodger's points"
    assert new.agent_a.points == 0
    assert only(events, "dodged") == {
        "type": "dodged",
        "attacker": "agent_a",
        "defender": "agent_b",
        "action": "jab",
        "defense": "dodge_right",
        "riposte": 5,
    }


def test_wrong_defense_takes_full_damage():
    # dodge_right counters jab only, so a cross lands clean.
    new, events = resolve(initial_state(), "cross", "dodge_right")

    assert new.agent_b.hp == 90
    assert new.agent_a.points == 10
    assert only(events, "hit")["damage"] == 10


def test_idle_takes_full_damage():
    new, _ = resolve(initial_state(), "hook", "idle")
    assert new.agent_b.hp == 85


def test_trade_lands_both_ways():
    new, events = resolve(initial_state(), "hook", "jab")

    assert new.agent_b.hp == 85
    assert new.agent_a.hp == 95
    assert new.agent_a.points == 15
    assert new.agent_b.points == 5
    assert types(events).count("hit") == 2


def test_hook_into_jab_is_the_trade_line():
    """The insight that solves the Jab-bot trap: net +10 per exchange (spec §11)."""
    new, _ = resolve(initial_state(), "hook", "jab")
    swing = (new.agent_a.hp - new.agent_b.hp) - (100 - 100)
    assert swing == 10


@pytest.mark.parametrize("defense_b", ["block_high", "dodge_left", "idle"])
def test_defense_versus_defense_does_nothing(defense_b):
    new, events = resolve(initial_state(), "block_low", defense_b)

    assert new.agent_a.hp == 100 and new.agent_b.hp == 100
    assert not [e for e in events if e["type"] in ("hit", "blocked", "dodged")], (
        "a riposte requires an incoming attack to evade"
    )


# ─── §3 stamina economy ───


def test_stamina_costs_are_deducted_then_passive_regen_applied():
    new, _ = resolve(state(a={"stamina": 50}, b={"stamina": 50}), "hook", "block_high")

    assert new.agent_a.stamina == 50 - 15 + 5
    assert new.agent_b.stamina == 50 - 3 + 5


def test_idle_regenerates_twenty_total():
    new, _ = resolve(state(a={"stamina": 40}, b={"stamina": 40}), "idle", "idle")
    assert new.agent_a.stamina == 60, "+5 passive and +15 idle bonus"


def test_stamina_is_capped_at_one_hundred():
    new, _ = resolve(state(a={"stamina": 99}, b={"stamina": 95}), "idle", "block_high")
    assert new.agent_a.stamina == 100
    assert new.agent_b.stamina == 97


def test_unaffordable_action_converts_to_idle_with_full_regen_and_no_penalty():
    new, events = resolve(state(a={"stamina": 4}, b={"stamina": 100}), "hook", "idle")

    assert only(events, "exhausted") == {
        "type": "exhausted",
        "who": "agent_a",
        "attempted": "hook",
        "stamina": 4,
    }
    assert new.agent_a.stamina == 24, "converted to idle, including the idle bonus"
    assert new.agent_a.penalties == 0, "a legal move submitted on time is never penalised"
    assert not [e for e in events if e["type"] == "hit"], "the hook never happened"


def test_exhausted_fighter_is_defenceless_like_any_idler():
    new, _ = resolve(state(a={"stamina": 100}, b={"stamina": 2}), "cross", "block_high")
    assert new.agent_b.hp == 90, "the block was unaffordable, so it became idle"


# ─── §4.4 no-move ───


def test_no_move_is_defenceless_penalised_and_gets_passive_regen_only():
    new, events = resolve(state(a={"stamina": 50}, b={"stamina": 50}), "hook", NoMove("timeout"))

    assert new.agent_b.hp == 85, "defenceless: takes full damage"
    assert new.agent_b.stamina == 55, "+5 passive only, never the idle bonus"
    assert new.agent_b.penalties == 15
    assert new.agent_b.points == 0, "and deals none"
    assert only(events, "no_move") == {
        "type": "no_move",
        "who": "agent_b",
        "cause": "timeout",
        "penalty": 15,
    }


@pytest.mark.parametrize("cause", rules.NO_MOVE_CAUSES)
def test_every_no_move_cause_is_recorded_distinctly(cause):
    _, events = resolve(initial_state(), "jab", NoMove(cause))
    assert only(events, "no_move")["cause"] == cause


def test_unknown_no_move_cause_is_rejected():
    with pytest.raises(ValueError, match="unknown no-move cause"):
        NoMove("vibes")


def test_both_fighters_can_no_move():
    new, events = resolve(initial_state(), NoMove("timeout"), NoMove("malformed"))

    assert types(events) == ["no_move", "no_move"]
    assert new.agent_a.penalties == 15 and new.agent_b.penalties == 15
    assert new.agent_a.hp == 100 and new.agent_b.hp == 100


def test_unknown_action_string_is_a_caller_bug():
    """The protocol edge must convert these to NoMove('invalid_action') first."""
    with pytest.raises(ValueError, match="unknown action"):
        resolve(initial_state(), "roundhouse", "jab")


# ─── §4.1 simultaneity and purity ───


def test_damage_is_computed_from_start_of_tick_state():
    """A fighter reduced to 0 HP still lands the attack it threw that tick."""
    new, events = resolve(state(a={"hp": 15}, b={"hp": 15}), "hook", "hook")

    assert new.agent_a.hp == 0 and new.agent_b.hp == 0
    assert types(events).count("ko") == 2

    result = check_end(new, max_ticks=60)
    assert result.reason == "draw" and result.winner is None, "double KO is a draw"


def test_resolve_does_not_mutate_its_input():
    before = state(a={"hp": 60, "stamina": 30}, b={"hp": 70, "stamina": 40})
    snapshot = (before.agent_a, before.agent_b, before.tick)

    resolve(before, "hook", "cross")

    assert (before.agent_a, before.agent_b, before.tick) == snapshot


def test_resolve_is_deterministic():
    s = state(a={"hp": 80, "stamina": 44}, b={"hp": 55, "stamina": 61}, tick=7)
    first = resolve(s, "cross", "dodge_left")
    second = resolve(s, "cross", "dodge_left")
    assert first == second


def test_tick_advances_by_exactly_one():
    new, _ = resolve(state(tick=11), "jab", "jab")
    assert new.tick == 12


# ─── §4.5 event schema ───


def test_every_event_names_its_parties_explicitly():
    _, events = resolve(
        state(a={"stamina": 2}, b={"stamina": 100}), "hook", NoMove("wrong_tick")
    )
    for event in events:
        assert ("who" in event) or ("attacker" in event and "defender" in event), event


def test_egocentric_projection_rewrites_only_party_fields():
    _, events = resolve(initial_state(), "jab", "dodge_right")
    projected = to_egocentric(events, "agent_b")

    dodged = only(projected, "dodged")
    assert dodged["attacker"] == "opponent" and dodged["defender"] == "self"
    assert dodged["riposte"] == 5 and dodged["action"] == "jab"

    mirrored = only(to_egocentric(events, "agent_a"), "dodged")
    assert mirrored["attacker"] == "self" and mirrored["defender"] == "opponent"


# ─── §5 win conditions ───


def test_ko_when_hp_reaches_zero():
    new, events = resolve(state(a={"hp": 100}, b={"hp": 10}), "hook", "idle")
    assert new.agent_b.hp == -5
    assert only(events, "ko")["who"] == "agent_b"
    assert check_end(new, max_ticks=60) is not None
    assert check_end(new, max_ticks=60).winner == "agent_a"
    assert check_end(new, max_ticks=60).reason == "ko"


def test_match_continues_before_max_ticks():
    assert check_end(state(tick=59), max_ticks=60) is None


def test_decision_on_points_at_max_ticks():
    result = check_end(state(a={"points": 40}, b={"points": 25}, tick=60), max_ticks=60)
    assert result.winner == "agent_a" and result.reason == "points"


def test_decision_score_subtracts_penalties():
    result = check_end(
        state(a={"points": 40, "penalties": 30}, b={"points": 25}, tick=60), max_ticks=60
    )
    assert result.winner == "agent_b", "40 − 30 < 25 − 0"


def test_decision_score_may_be_negative():
    assert Fighter(points=5, penalties=45).decision_score == -40


def test_equal_decision_score_is_a_draw_with_null_winner():
    result = check_end(state(a={"points": 30}, b={"points": 30}, tick=60), max_ticks=60)
    assert result.reason == "draw"
    assert result.winner is None
    assert "winner" in result.as_dict(), "clients may rely on the key always being present"


def test_ko_beats_the_tick_limit():
    result = check_end(state(a={"hp": 5, "points": 0}, b={"hp": 0, "points": 99}, tick=60), 60)
    assert result.reason == "ko" and result.winner == "agent_a"
