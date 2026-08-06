"""Offline log analysis: derived metrics without re-running a model."""

from __future__ import annotations

import json

import pytest

from agents.harness.analyze import (
    analyze_log,
    analyze_logs,
    exploitation_speed,
    load_log,
    match_metrics,
    no_move_breakdown,
    other_corner,
)
from agents.tests.sample_log import abandoned_log, ko_log, sample_log


@pytest.fixture()
def metrics():
    return match_metrics(sample_log(), "agent_a")


def test_provenance_comes_from_the_stored_profile_and_config(metrics):
    assert metrics["entrant"] == "anthropic/claude-opus-5-20260101"
    assert metrics["opponent"] == "jabbot@1.0"
    assert metrics["prompt_version"] == "ref-v1"
    assert metrics["agent_version"] == "harness@1.0.0"
    assert metrics["temperature"] is None
    assert (metrics["deadline_ms"], metrics["max_ticks"]) == (5000, 60)


def test_counter_accuracy_counts_only_defenses_against_incoming_attacks(metrics):
    # block_high vs jab (right), dodge_right vs jab (right), block_low vs jab (wrong).
    # The hook tick is an attack, not a defense, and the no-move tick does not count.
    assert metrics["counter_opportunities"] == 3
    assert metrics["counter_accuracy"] == pytest.approx(2 / 3)


def test_riposte_rate_is_per_incoming_attack(metrics):
    assert metrics["attacks_faced"] == 5
    assert metrics["dodges_landed"] == 1
    assert metrics["blocks_landed"] == 1
    assert metrics["riposte_rate"] == pytest.approx(1 / 5)
    assert metrics["riposte_damage"] == 5


def test_stamina_efficiency_is_damage_dealt_per_stamina_spent(metrics):
    # 3 (block_high) + 15 (hook) + 8 (dodge_right) + 3 (block_low); the no-move
    # tick costs nothing.
    assert metrics["stamina_spent"] == 29
    assert metrics["damage_dealt"] == 20  # 15 hook + 5 riposte
    assert metrics["damage_taken"] == 15
    assert metrics["stamina_efficiency"] == pytest.approx(20 / 29)


def test_no_move_rate_is_broken_down_by_cause(metrics):
    assert metrics["no_moves"] == 1
    assert metrics["no_move_by_cause"] == {"malformed": 1}
    assert metrics["no_move_rate"] == pytest.approx(1 / 5)


def test_instruction_following_counts_malformed_and_invalid_action(metrics):
    assert metrics["instruction_following_rate"] == pytest.approx(4 / 5)


def test_fallback_rate_comes_from_telemetry(metrics):
    assert metrics["fallback_rate"] == pytest.approx(1 / 5)
    assert metrics["mean_latency_ms"] == pytest.approx((800 * 4 + 4900) / 5)
    assert metrics["tokens_in"] == 600 * 4 + 610


def test_outcome_fields(metrics):
    assert metrics["reason"] == "points"
    assert metrics["winner"] == "agent_b"
    assert metrics["won"] is False
    assert metrics["abandoned"] is False
    assert metrics["hp_diff"] == 5  # 85 - 80
    assert metrics["decision_score"] == 5  # 20 points - 15 penalties
    assert metrics["ticks_to_ko"] is None  # no KO in this match


def test_the_opposite_corner_is_the_mirror_image():
    other = match_metrics(sample_log(), "agent_b")
    assert other["hp_diff"] == -5
    assert other["won"] is True
    assert other["damage_dealt"] == 15
    assert other["no_moves"] == 0


def test_ticks_to_ko_is_only_set_for_the_winner_of_a_ko():
    log = ko_log(final_tick=28, winner="agent_a")
    winner, loser = analyze_log(log)
    assert winner["ticks_to_ko"] == 28
    assert loser["ticks_to_ko"] is None


def test_exploitation_speed_groups_by_entrant_and_opponent():
    rows = analyze_log(ko_log(final_tick=28)) + analyze_log(ko_log(final_tick=20))
    speed = exploitation_speed(rows)
    key = ("anthropic/claude-opus-5-20260101", "jabbot@1.0")
    assert speed[key]["matches"] == 2
    assert speed[key]["kos"] == 2
    assert speed[key]["mean_ticks_to_ko"] == pytest.approx(24)


def test_abandoned_matches_are_excluded_from_exploitation_speed():
    rows = analyze_log(abandoned_log())
    assert all(row["abandoned"] for row in rows)
    assert exploitation_speed(rows) == {}


def test_no_move_breakdown_totals_across_matches():
    rows = analyze_log(sample_log()) + analyze_log(sample_log())
    assert dict(no_move_breakdown(rows)) == {"malformed": 2}


def test_other_corner():
    assert other_corner("agent_a") == "agent_b"
    assert other_corner("agent_b") == "agent_a"


def test_analyze_logs_reads_from_disk(tmp_path):
    path = tmp_path / "match.json"
    path.write_text(json.dumps(sample_log()), encoding="utf-8")
    assert load_log(path)["schema_version"] == 1
    rows = analyze_logs([path])
    assert [row["corner"] for row in rows] == ["agent_a", "agent_b"]
