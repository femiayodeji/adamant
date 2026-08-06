"""Aggregation must enforce brackets, exclusions, and margin-first reporting."""

from __future__ import annotations

import pytest

from agents.harness.aggregate import (
    BracketError,
    aggregate,
    exclusion_reason,
    partition,
    require_single_bracket,
    summarise_exclusions,
)


def row(**overrides):
    base = {
        "deadline_ms": 5000,
        "max_ticks": 60,
        "entrant": "model-a",
        "opponent": "jab",
        "prompt_version": "ref-v1",
        "agent_version": "harness@1.0.0",
        "resolved_model": "model-a-20260101",
        "temperature": 0.0,
        "corner": "agent_a",
        "hp_diff": 30,
        "ticks_to_ko": 28,
        "won": True,
        "abandoned": False,
        "fallback_rate": 0.02,
        "decision_score": 40,
        "counter_accuracy": 0.5,
        "riposte_rate": 0.2,
        "stamina_efficiency": 1.0,
        "instruction_following_rate": 1.0,
        "no_move_rate": 0.0,
        "reason": "ko",
    }
    base.update(overrides)
    return base


def test_comparing_across_brackets_is_refused():
    rows = [row(deadline_ms=5000), row(deadline_ms=30000)]
    with pytest.raises(BracketError):
        require_single_bracket(rows)
    require_single_bracket([row(), row()])  # single bracket is fine


def test_abandoned_and_hedged_matches_are_excluded():
    assert exclusion_reason(row(abandoned=True)) == "abandoned"
    assert exclusion_reason(row(reason="abandoned", abandoned=False)) == "abandoned"
    assert exclusion_reason(row(fallback_rate=0.4)) == "fallback_rate"
    assert exclusion_reason(row(error="boom")) == "error"
    assert exclusion_reason(row()) is None
    # The threshold is a strict "over", so a run exactly at the limit survives.
    assert exclusion_reason(row(fallback_rate=0.10)) is None


def test_partition_reports_what_it_dropped():
    rows = [row(), row(abandoned=True), row(fallback_rate=0.9), row(fallback_rate=0.9)]
    kept, dropped = partition(rows)
    assert len(kept) == 1
    assert {k: len(v) for k, v in dropped.items()} == {"abandoned": 1, "fallback_rate": 2}
    assert summarise_exclusions(rows) == {"abandoned": 1, "fallback_rate": 2}


def test_aggregate_reports_margins_and_corner_balance():
    rows = [
        row(corner="agent_a", hp_diff=40, ticks_to_ko=24, won=True),
        row(corner="agent_b", hp_diff=20, ticks_to_ko=32, won=True),
        row(corner="agent_a", hp_diff=-10, ticks_to_ko=None, won=False, reason="points"),
        row(corner="agent_b", hp_diff=10, ticks_to_ko=None, won=False, reason="points"),
    ]
    summaries = aggregate(rows)
    assert len(summaries) == 1
    summary = summaries[0]

    assert summary["matches"] == 4
    assert summary["mean_hp_diff"] == pytest.approx(15.0)
    assert summary["mean_ticks_to_ko"] == pytest.approx(28.0)
    assert summary["ko_rate"] == pytest.approx(0.5)
    assert summary["corner_balanced"] is True
    assert summary["matches_agent_a"] == summary["matches_agent_b"] == 2
    assert summary["win_rate"] == pytest.approx(0.5)
    # The cell is keyed on both config and profile.
    for field in ("deadline_ms", "max_ticks", "entrant", "opponent",
                  "prompt_version", "agent_version", "resolved_model", "temperature"):
        assert field in summary


def test_aggregate_separates_brackets_and_entrants_into_their_own_cells():
    rows = [row(), row(deadline_ms=30000), row(entrant="model-b")]
    summaries = aggregate(rows)
    assert len(summaries) == 3
    assert {s["matches"] for s in summaries} == {1}


def test_aggregate_drops_contaminated_rows_and_says_how_many():
    rows = [row(), row(abandoned=True), row(fallback_rate=0.5)]
    summaries = aggregate(rows)
    assert len(summaries) == 1
    assert summaries[0]["matches"] == 1
    assert summaries[0]["excluded_total"] == 2


def test_unbalanced_corners_are_flagged_not_silently_averaged():
    rows = [row(corner="agent_a"), row(corner="agent_a"), row(corner="agent_b")]
    summary = aggregate(rows)[0]
    assert summary["corner_balanced"] is False
