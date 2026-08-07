"""The repetition cap (spec §4.6).

A fighter may submit the same action at most `MAX_CONSECUTIVE_REPEATS` times in a
row; the next consecutive repeat is stripped to nothing. These tests pin the three
things that are easy to get subtly wrong: what the streak counts, what a void
costs, and how a void differs from the two conversions that already existed.
"""

from __future__ import annotations

import pytest

from server.engine import rules
from server.engine.resolver import (
    Fighter,
    MatchState,
    NoMove,
    initial_state,
    resolve,
    to_egocentric,
)

CAP = rules.MAX_CONSECUTIVE_REPEATS


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


def only_for(events, kind, who) -> dict:
    matching = [e for e in events if e["type"] == kind and e.get("who") == who]
    assert len(matching) == 1, f"expected one {kind} for {who}, got {events}"
    return matching[0]


def bag(attack, n):
    """A punching bag for `attack`: never counters it, and never repeats itself past
    the cap — so any `void` in the log belongs unambiguously to the fighter under
    test."""
    if attack in rules.ATTACKS:
        safe = next(d for d in rules.DEFENSES if not rules.counters(d, attack))
    else:
        safe = "block_low"
    return [rules.IDLE if i % 2 == 0 else safe for i in range(n)]


def run(actions_a, actions_b, start=None):
    """Drive a fixed pair of move sequences, returning (final_state, per-tick events)."""
    current = start or initial_state()
    log = []
    for move_a, move_b in zip(actions_a, actions_b):
        current, events = resolve(current, move_a, move_b)
        log.append(events)
    return current, log


# ─── Where the cap bites ───


def test_three_in_a_row_is_legal_and_the_fourth_is_void():
    final, log = run(["jab"] * 5, bag("jab", 5))

    assert types(log[0]) == ["hit"]
    assert types(log[1]) == ["hit"]
    assert types(log[2]) == ["hit"], "three identical actions in a row are legal"
    assert "void" in types(log[3]), "the fourth consecutive repeat is void"
    assert "void" in types(log[4])
    assert "hit" not in types(log[3]) and "hit" not in types(log[4])

    # Exactly three jabs landed, not five.
    assert final.agent_b.hp == rules.START_HP - 3 * rules.damage_of("jab")


def test_the_void_event_carries_the_action_and_the_streak():
    _, log = run(["jab"] * 4, bag("jab", 4))
    event = only(log[3], "void")

    assert event == {"type": "void", "who": "agent_a", "action": "jab", "streak": 4}


def test_a_different_action_resets_the_streak():
    final, log = run(["jab", "jab", "jab", "cross", "jab"], bag("jab", 5))

    assert "void" not in types(log[3]), "a different action is never a repeat"
    assert "void" not in types(log[4]), "and it resets the count for the next one"
    assert final.agent_a.repeat_streak == 1
    assert final.agent_a.last_action == "jab"


def test_a_void_move_still_extends_the_streak():
    """Spamming is not a way out of the cap: the count keeps climbing."""
    final, log = run(["jab"] * 6, bag("jab", 6))

    assert only(log[5], "void")["streak"] == 6
    assert final.agent_a.repeat_streak == 6


@pytest.mark.parametrize("action", rules.ACTIONS)
def test_the_cap_applies_to_every_action_including_idle(action):
    _, log = run([action] * (CAP + 1), bag(action, CAP + 1))

    assert "void" not in types(log[CAP - 1]), f"{action} should survive {CAP} repeats"
    assert only_for(log[CAP], "void", "agent_a")["action"] == action


# ─── What a void costs ───


def test_a_void_action_still_pays_its_stamina():
    """Spam is punished economically, not with a penalty."""
    final, _ = run(["cross"] * 4, bag("cross", 4))

    # Four crosses at 10, four ticks of +5 passive regen, no idle bonus.
    assert final.agent_a.stamina == rules.START_STAMINA - 4 * 10 + 4 * rules.PASSIVE_REGEN


def test_a_void_carries_no_penalty():
    final, _ = run(["jab"] * 6, bag("jab", 6))
    assert final.agent_a.penalties == 0, "a legal move submitted on time is never penalised"


def test_a_void_gets_the_passive_regen_only():
    """A voided `idle` does not collect the idle bonus — the action was stripped."""
    # Started low enough that the +20s never reach the cap and hide the difference.
    start = state(a={"stamina": 20}, b={"stamina": 20})
    final, _ = run(["idle"] * 4, bag("idle", 4), start=start)

    # Three real idles at +20, then one void at +5.
    assert final.agent_a.stamina == 20 + 3 * (rules.PASSIVE_REGEN + rules.IDLE_REGEN_BONUS) + 5


def test_a_voided_attack_deals_nothing_and_a_voided_defense_defends_nothing():
    # agent_a voids a jab; agent_b voids a block_high that would have countered it.
    start = state(
        a={"last_action": "jab", "repeat_streak": CAP},
        b={"last_action": "block_high", "repeat_streak": CAP},
    )
    new, events = resolve(start, "jab", "block_high")

    assert types(events) == ["void", "void"], "no attack outcome — neither action exists"
    assert new.agent_b.hp == rules.START_HP, "the voided jab dealt nothing"
    assert new.agent_a.points == 0


def test_a_voided_defense_leaves_the_fighter_defenceless():
    start = state(b={"last_action": "block_high", "repeat_streak": CAP})
    new, events = resolve(start, "cross", "block_high")

    assert types(events) == ["void", "hit"], "the block was stripped, so the cross lands"
    assert new.agent_b.hp == rules.START_HP - rules.damage_of("cross")


def test_a_voided_dodge_does_not_riposte():
    start = state(b={"last_action": "dodge_left", "repeat_streak": CAP})
    new, events = resolve(start, "hook", "dodge_left")

    assert "dodged" not in types(events)
    assert new.agent_a.hp == rules.START_HP, "no riposte from an action that did not happen"


# ─── How a void differs from the neighbouring conversions ───


def test_a_void_is_checked_before_affordability_and_never_reports_exhausted():
    """At most one of no_move / void / exhausted fires per fighter per tick."""
    start = state(a={"stamina": 2, "last_action": "hook", "repeat_streak": CAP})
    _, events = resolve(start, "hook", "idle")

    assert types(events) == ["void"], "a void action is stripped before it is priced"


def test_an_unaffordable_void_cannot_drive_stamina_negative():
    start = state(a={"stamina": 2, "last_action": "hook", "repeat_streak": CAP})
    new, _ = resolve(start, "hook", "idle")

    assert new.agent_a.stamina == 0 + rules.PASSIVE_REGEN, "clamped, then regenerated"


def test_an_exhausted_conversion_does_not_change_what_the_streak_counts():
    """The streak follows what was *submitted*, not what it converted to."""
    start = state(a={"stamina": 5})
    new, events = resolve(start, "hook", "idle")

    assert only(events, "exhausted")["attempted"] == "hook"
    assert new.agent_a.last_action == "hook", "not `idle`, which is what it resolved as"
    assert new.agent_a.repeat_streak == 1


def test_a_no_move_breaks_the_streak():
    """There was no submission, so there is nothing to have repeated."""
    start = state(a={"last_action": "jab", "repeat_streak": CAP})
    new, _ = resolve(start, NoMove("timeout"), "idle")

    assert new.agent_a.last_action is None
    assert new.agent_a.repeat_streak == 0


def test_a_no_move_lets_the_next_repeat_through():
    _, log = run(["jab", "jab", "jab", NoMove("timeout"), "jab"], bag("jab", 5))

    assert "void" not in types(log[4]), "the streak restarted at the no-move"


# ─── Bookkeeping visible to agents ───


def test_fighter_state_starts_with_no_history():
    fighter = initial_state().agent_a

    assert fighter.last_action is None
    assert fighter.repeat_streak == 0


def test_state_dict_exposes_the_streak_so_agents_can_stay_stateless():
    """Spec §1: everything needed to decide arrives in the state. An agent must be
    able to see the cap coming without tracking anything itself."""
    final, _ = run(["jab"] * 2, bag("jab", 2))
    payload = final.as_dict()["agent_a"]

    assert payload["last_action"] == "jab"
    assert payload["repeat_streak"] == 2
    assert set(payload) == {
        "hp",
        "stamina",
        "points",
        "penalties",
        "last_action",
        "repeat_streak",
    }


def test_void_events_project_egocentrically_like_every_other_event():
    _, log = run(["jab"] * 4, bag("jab", 4))

    assert to_egocentric(log[3], "agent_a") == [
        {"type": "void", "who": "self", "action": "jab", "streak": 4}
    ]
    assert to_egocentric(log[3], "agent_b")[0]["who"] == "opponent"


def test_the_streak_is_tracked_per_fighter():
    final, _ = run(["jab"] * 4, ["jab", "cross", "jab", "jab"])

    assert final.agent_a.repeat_streak == 4
    assert final.agent_b.repeat_streak == 2


# ─── The invariant the cap exists to create ───


def test_no_single_action_can_carry_a_whole_match():
    """The point of the rule: one move is not a strategy. Against a fighter that
    idles — the most permissive possible opponent — a one-note attacker lands only
    `CAP` hits before the cap shuts it down, and never lands another."""
    final, log = run(["hook"] * 20, bag("hook", 20))

    landed = sum(1 for events in log if "hit" in types(events))
    assert landed == CAP
    assert final.agent_b.hp == rules.START_HP - CAP * rules.damage_of("hook")
