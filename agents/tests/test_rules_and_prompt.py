"""The rules the prompt conveys must match the rules the analysis assumes."""

from __future__ import annotations

import pytest

from agents.common import prompt as prompt_mod
from agents.common.rules import (
    ACTIONS,
    ATTACKS,
    BLOCKS,
    COUNTERS,
    DAMAGE,
    DEFENSES,
    DODGES,
    FALLBACK_ACTION,
    FALLBACK_ALTERNATE,
    FALLBACK_CHAIN,
    MAX_CONSECUTIVE_REPEATS,
    RULES_SUMMARY,
    STAMINA_COST,
    fallback_action,
    would_void,
)


def test_every_action_is_named_in_the_summary():
    for action in ACTIONS:
        assert action in RULES_SUMMARY, action


def test_every_attack_has_exactly_one_block_and_one_dodge_counter():
    assert set(COUNTERS) == set(ATTACKS)
    for attack, counters in COUNTERS.items():
        assert len([c for c in counters if c in BLOCKS]) == 1, attack
        assert len([c for c in counters if c in DODGES]) == 1, attack


def test_no_defense_counters_every_attack():
    for defense in DEFENSES:
        covered = [a for a in ATTACKS if defense in COUNTERS[a]]
        assert len(covered) < len(ATTACKS), defense


def test_summary_states_the_published_damage_and_stamina_numbers():
    for attack in ATTACKS:
        line = next(l for l in RULES_SUMMARY.splitlines() if l.strip().startswith(attack))
        assert str(DAMAGE[attack]) in line
        assert str(STAMINA_COST[attack]) in line


def test_summary_conveys_the_trade_line_and_the_no_move_penalty():
    # Beating Jab-bot requires knowing that attack-vs-attack is a trade, and
    # silence must never look viable.
    assert "TRADE" in RULES_SUMMARY
    assert "15-point penalty" in RULES_SUMMARY
    assert "riposte" in RULES_SUMMARY.lower()


def test_system_prompt_asks_for_a_single_json_object_over_the_eight_actions():
    system = prompt_mod.SYSTEM_PROMPT
    assert "SINGLE JSON object" in system
    assert '"action"' in system and '"reason"' in system
    for action in ACTIONS:
        assert action in system


def test_user_prompt_carries_state_and_the_ten_tick_history():
    state = {
        "type": "state_update",
        "tick": 12,
        "deadline_ms": 5000,
        "self": {"hp": 85, "stamina": 60, "points": 30, "penalties": 0},
        "opponent": {"hp": 70, "stamina": 40, "points": 15, "penalties": 2},
        "history": [
            {
                "tick": 11,
                "moves": {"self": "jab", "opponent": "block_high"},
                "events": [{"type": "blocked", "attacker": "self", "defender": "opponent",
                            "action": "jab", "defense": "block_high"}],
            },
            {
                "tick": 10,
                "moves": {"self": "idle", "opponent": "hook"},
                "events": [{"type": "hit", "attacker": "opponent", "defender": "self",
                            "action": "hook", "damage": 15}],
            },
        ],
    }
    text = prompt_mod.build_user_prompt(state, opponent_name="RivalBot", max_ticks=60)
    assert "Tick 12 of 60" in text
    assert "5000 ms" in text
    assert "hp 85" in text and "hp 70" in text
    assert "RivalBot" in text
    assert "tick 11" in text and "tick 10" in text
    # Egocentric rendering: no corner IDs leak into the prompt.
    assert "agent_a" not in text and "agent_b" not in text
    assert "the opponent hit you with hook for 15" in text


def test_user_prompt_handles_the_opening_tick():
    state = {"tick": 1, "deadline_ms": 5000, "self": {"hp": 100}, "opponent": {"hp": 100},
             "history": []}
    assert "opening tick" in prompt_mod.build_user_prompt(state)


# --- the repetition cap (spec 4.6) -----------------------------------------


def test_the_summary_states_the_cap_and_what_a_void_costs():
    assert "REPETITION CAP" in RULES_SUMMARY
    assert str(MAX_CONSECUTIVE_REPEATS) in RULES_SUMMARY
    assert "VOID" in RULES_SUMMARY
    lowered = RULES_SUMMARY.lower()
    # The four things that make the cap actionable rather than trivia.
    assert "in a row" in lowered
    assert "defenseless" in lowered, "a void is not merely a wasted attack"
    assert "still pay the stamina" in lowered, "a void is not free"
    assert "no penalty" in lowered, "a void must not be confused with a no-move"
    assert "repeat_streak" in RULES_SUMMARY, "the agent must know where to read it"


def test_the_summary_says_a_void_still_extends_the_streak():
    """The one counter-intuitive half of the rule: repeating harder does not
    unstick you, only changing action does."""
    lowered = RULES_SUMMARY.lower()
    assert "still extends" in lowered
    assert "resets" in lowered


def test_the_user_prompt_reports_the_streak_from_state_not_from_memory():
    state = {
        "tick": 9, "deadline_ms": 5000,
        "self": {"hp": 90, "stamina": 70, "points": 10, "penalties": 0,
                 "last_action": "jab", "repeat_streak": 2},
        "opponent": {"hp": 80, "stamina": 60, "points": 5, "penalties": 0,
                     "last_action": "block_high", "repeat_streak": 3},
        "history": [],
    }
    text = prompt_mod.build_user_prompt(state)
    assert "last_action jab" in text and "repeat_streak 2" in text
    assert "last_action block_high" in text and "repeat_streak 3" in text
    # Below the cap: told how much room is left, not warned.
    assert "1 more before" in text
    assert "REPETITION CAP REACHED" not in text


@pytest.mark.parametrize("streak", [MAX_CONSECUTIVE_REPEATS, MAX_CONSECUTIVE_REPEATS + 2])
def test_the_prompt_warns_in_words_when_one_more_repeat_would_void(streak):
    state = {
        "tick": 9, "deadline_ms": 5000,
        "self": {"hp": 90, "last_action": "jab", "repeat_streak": streak},
        "opponent": {"hp": 80}, "history": [],
    }
    text = prompt_mod.build_user_prompt(state)
    assert "REPETITION CAP REACHED" in text
    assert "Another jab this tick is VOID" in text
    assert "Choose a different action" in text


def test_the_prompt_says_nothing_about_repeats_at_the_start_of_a_match():
    state = {"tick": 1, "deadline_ms": 5000,
             "self": {"hp": 100, "last_action": None, "repeat_streak": 0},
             "opponent": {"hp": 100}, "history": []}
    text = prompt_mod.build_user_prompt(state)
    assert "REPETITION CAP REACHED" not in text and "Repetition:" not in text
    assert "last_action" not in text


def test_a_void_in_the_history_is_rendered_egocentrically():
    state = {
        "tick": 6, "deadline_ms": 5000, "self": {"hp": 100}, "opponent": {"hp": 100},
        "history": [{
            "tick": 5,
            "moves": {"self": "jab", "opponent": "hook"},
            "events": [
                {"type": "void", "who": "self", "action": "jab", "streak": 4},
                {"type": "hit", "attacker": "opponent", "defender": "self",
                 "action": "hook", "damage": 15},
            ],
        }],
    }
    text = prompt_mod.build_user_prompt(state)
    assert "you repeated jab 4 times in a row: VOID" in text
    assert "defenseless" in text
    assert "agent_a" not in text and "agent_b" not in text


# --- the hedge must not void itself ----------------------------------------


def test_would_void_reads_the_fighter_state_block():
    at_cap = {"last_action": "jab", "repeat_streak": MAX_CONSECUTIVE_REPEATS}
    assert would_void(at_cap, "jab") is True
    assert would_void(at_cap, "cross") is False
    assert would_void({"last_action": "jab", "repeat_streak": 2}, "jab") is False
    assert would_void({"last_action": None, "repeat_streak": 0}, "jab") is False
    assert would_void(None, "jab") is False


def test_the_two_hedges_are_both_real_defenses_and_are_not_the_same_action():
    assert FALLBACK_CHAIN == (FALLBACK_ACTION, FALLBACK_ALTERNATE)
    assert FALLBACK_ACTION != FALLBACK_ALTERNATE
    assert set(FALLBACK_CHAIN) <= set(DEFENSES), "a hedge must actually defend"
    # Cheap enough that a run of them can never also exhaust the fighter.
    for action in FALLBACK_CHAIN:
        assert STAMINA_COST[action] <= 5


def test_fallback_action_avoids_the_cap_and_defaults_to_block_high():
    assert fallback_action() == FALLBACK_ACTION
    assert fallback_action({}) == FALLBACK_ACTION
    assert fallback_action({"last_action": "jab", "repeat_streak": 9}) == FALLBACK_ACTION
    capped = {"last_action": FALLBACK_ACTION, "repeat_streak": MAX_CONSECUTIVE_REPEATS}
    assert fallback_action(capped) == FALLBACK_ALTERNATE
    other = {"last_action": FALLBACK_ALTERNATE, "repeat_streak": MAX_CONSECUTIVE_REPEATS}
    assert fallback_action(other) == FALLBACK_ACTION
