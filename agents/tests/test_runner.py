"""Batch planning, concurrency, and the results CSV."""

from __future__ import annotations

import asyncio
import csv
import json
from collections import Counter

import pytest

from agents.harness.runner import (
    MatchSpec,
    Opponent,
    plan_matches,
    row_from_log,
    run_batch,
    write_csv,
)
from agents.tests.sample_log import sample_log


def test_every_comparison_is_planned_as_a_corner_swapped_pair():
    specs = plan_matches(["model-a"], [Opponent("jab")], repeats=3, deadline_ms=5000)
    assert len(specs) == 6  # 3 repeats x 2 corners

    by_pair = Counter(s.pair_id for s in specs)
    assert set(by_pair.values()) == {2}
    for pair_id in by_pair:
        pair = [s for s in specs if s.pair_id == pair_id]
        assert {s.corner for s in pair} == {"agent_a", "agent_b"}
        # The two halves are otherwise identical: same seed, same repeat.
        assert len({(s.seed, s.repeat, s.entrant, s.opponent) for s in pair}) == 1


def test_a_batch_is_planned_inside_one_deadline_bracket():
    specs = plan_matches(["a", "b"], [Opponent("jab"), Opponent("mixer")], deadline_ms=30000)
    assert {s.deadline_ms for s in specs} == {30000}
    assert {s.max_ticks for s in specs} == {60}
    assert {(s.entrant, s.opponent) for s in specs} == {
        ("a", "jab"), ("a", "mixer"), ("b", "jab"), ("b", "mixer")
    }


def test_model_opponents_are_marked_as_such():
    specs = plan_matches(["a"], [Opponent("openai/gpt-x", "model")])
    assert {s.opponent_kind for s in specs} == {"model"}


@pytest.mark.asyncio
async def test_run_batch_respects_the_concurrency_limit():
    specs = plan_matches(["a"], [Opponent("jab")], repeats=4)  # 8 matches
    in_flight = 0
    peak = 0

    async def run_one(spec: MatchSpec):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return {"hp_diff": 10, "won": True}

    rows = await run_batch(specs, run_one, concurrency=3)
    assert len(rows) == 8
    assert peak <= 3
    # Spec fields are merged into every row, so results are keyed on config.
    assert all(row["deadline_ms"] == 5000 and row["corner"] in ("agent_a", "agent_b")
               for row in rows)


@pytest.mark.asyncio
async def test_a_failing_match_becomes_a_row_not_a_dead_batch():
    specs = plan_matches(["a"], [Opponent("jab")], repeats=1)

    async def run_one(spec: MatchSpec):
        if spec.corner == "agent_a":
            raise RuntimeError("socket exploded")
        return {"hp_diff": 5}

    rows = await run_batch(specs, run_one, concurrency=2)
    errors = [r for r in rows if r.get("error")]
    assert len(rows) == 2 and len(errors) == 1
    assert "socket exploded" in errors[0]["error"]


def test_row_from_log_extracts_our_corner():
    spec = plan_matches(["anthropic/claude-opus-5"], [Opponent("jab")])[0]
    row = row_from_log(sample_log(), spec)
    assert row["corner"] == "agent_a"
    assert row["entrant"] == "anthropic/claude-opus-5"
    assert row["opponent"] == "jab"
    assert row["hp_diff"] == 5
    assert row["deadline_ms"] == 5000


def test_csv_is_keyed_on_profile_and_config(tmp_path):
    spec = plan_matches(["anthropic/claude-opus-5"], [Opponent("jab")])[0]
    row = {**spec.__dict__, **row_from_log(sample_log(), spec)}
    path = write_csv([row], tmp_path / "results.csv")

    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    out = rows[0]
    for field in ("deadline_ms", "max_ticks", "prompt_version", "agent_version",
                  "resolved_model", "temperature", "corner", "pair_id", "seed"):
        assert field in out
    assert out["resolved_model"] == "anthropic/claude-opus-5-20260101"
    assert out["deadline_ms"] == "5000"
    # Nested metrics survive as JSON rather than as a mangled dict repr.
    assert json.loads(out["no_move_by_cause"]) == {"malformed": 1}
