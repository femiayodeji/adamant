"""The rules the prompt conveys must match the rules the analysis assumes."""

from __future__ import annotations

from agents.common import prompt as prompt_mod
from agents.common.rules import (
    ACTIONS,
    ATTACKS,
    BLOCKS,
    COUNTERS,
    DAMAGE,
    DEFENSES,
    DODGES,
    RULES_SUMMARY,
    STAMINA_COST,
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
