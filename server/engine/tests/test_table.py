"""Table invariants (plan Phase 1 gate).

These test the *rules table*, not the resolver. They are what stops a tuning pass
from quietly deleting an action from the game — which is exactly what happened to
`dodge_right` in spec v0.3, and what a coverage-only check would not have caught.

Every payoff below is measured by running the real `resolve()`, so these cannot
drift from the engine by reimplementing its logic.
"""

from __future__ import annotations

import itertools

import pytest

from server.engine import rules
from server.engine.resolver import Fighter, MatchState, NoMove, resolve

REAL_ACTIONS = rules.ACTIONS
FRESH = MatchState(agent_a=Fighter(), agent_b=Fighter())


def swing(mine, theirs) -> int:
    """HP differential produced by one tick from full health: my HP change minus
    theirs. Positive is good for me. Riposte damage counts, because chip damage to
    the opponent moves the differential exactly as avoided damage does."""
    new, _ = resolve(FRESH, mine, theirs)
    return (new.agent_a.hp - 100) - (new.agent_b.hp - 100)


def penalty(mine, theirs) -> int:
    new, _ = resolve(FRESH, mine, theirs)
    return new.agent_a.penalties


# ─── Coverage ───


@pytest.mark.parametrize("attack", sorted(rules.ATTACKS))
def test_every_attack_has_at_least_one_block_and_one_dodge_counter(attack):
    blocks = [d for d in rules.DEFENSES if rules.counters(d, attack) and not rules.riposte_of(d)]
    dodges = [d for d in rules.DEFENSES if rules.counters(d, attack) and rules.riposte_of(d)]

    assert blocks, f"{attack} has no block counter — it would be unanswerable cheaply"
    assert dodges, f"{attack} has no dodge counter — no way to profit from reading it"


@pytest.mark.parametrize("defense", sorted(rules.DEFENSES))
def test_no_defense_counters_every_attack(defense):
    covered = [a for a in rules.ATTACKS if rules.counters(defense, a)]
    assert len(covered) < len(rules.ATTACKS), (
        f"{defense} answers everything, which collapses the guessing game"
    )


def test_every_attack_has_exactly_two_counters():
    """Spec §2: one block, one dodge. The matrix is arbitrary but published —
    reading it and predicting the opponent *is* the game."""
    for attack in rules.ATTACKS:
        assert sum(rules.counters(d, attack) for d in rules.DEFENSES) == 2, attack


def test_action_space_is_exactly_the_eight_of_the_spec():
    assert set(REAL_ACTIONS) == {
        "jab", "cross", "hook",
        "block_high", "block_low", "dodge_left", "dodge_right",
        "idle",
    }


# ─── Costs ───


@pytest.mark.parametrize("action", [a for a in REAL_ACTIONS if a != rules.IDLE])
def test_no_action_is_free_except_idle(action):
    assert rules.stamina_cost(action) > 0


def test_idle_is_the_only_free_action():
    assert rules.stamina_cost(rules.IDLE) == 0


def test_dodges_cost_strictly_more_than_blocks():
    """The economic difference between them: blocks are cheap and safe, dodges cost
    more and pay off only on a correct read."""
    blocks = [rules.stamina_cost(d) for d in rules.DEFENSES if not rules.riposte_of(d)]
    dodges = [rules.stamina_cost(d) for d in rules.DEFENSES if rules.riposte_of(d)]
    assert min(dodges) > max(blocks)


def test_bigger_attacks_cost_and_hit_harder():
    ordered = sorted(rules.ATTACKS, key=lambda a: rules.damage_of(a))
    costs = [rules.ATTACKS[a]["stamina"] for a in ordered]
    assert costs == sorted(costs), "damage and stamina must be monotone together"


# ─── No strictly dominated action ───


def _pareto_dominates(better: str, worse: str) -> bool:
    """`better` dominates `worse` if it is at least as good on the HP differential
    against *every* opponent action and costs no more stamina, with at least one
    strict advantage. An agent would then never rationally pick `worse`, silently
    shrinking the action space."""
    payoffs_better = [swing(better, o) for o in REAL_ACTIONS]
    payoffs_worse = [swing(worse, o) for o in REAL_ACTIONS]
    cost_better = rules.stamina_cost(better)
    cost_worse = rules.stamina_cost(worse)

    at_least_as_good = all(x >= y for x, y in zip(payoffs_better, payoffs_worse))
    no_dearer = cost_better <= cost_worse
    strictly_better = any(x > y for x, y in zip(payoffs_better, payoffs_worse)) or (
        cost_better < cost_worse
    )
    return at_least_as_good and no_dearer and strictly_better


@pytest.mark.parametrize(
    "better,worse", list(itertools.permutations(sorted(rules.DEFENSES), 2))
)
def test_no_defense_is_strictly_dominated(better, worse):
    """The invariant that catches the `dodge_right` bug.

    Without riposte, `block_high` counters everything `dodge_right` counters *plus*
    `cross`, at under half the stamina — so `dodge_right` is a dead action and the
    eight-action space is really seven. A coverage-only check passes anyway; this
    one does not.
    """
    assert not _pareto_dominates(better, worse), (
        f"{worse} is strictly dominated by {better} — it is a dead action"
    )


@pytest.mark.parametrize(
    "better,worse", list(itertools.permutations(sorted(REAL_ACTIONS), 2))
)
def test_no_action_at_all_is_strictly_dominated(better, worse):
    """Same check widened across the whole action space, attacks and idle included."""
    assert not _pareto_dominates(better, worse), f"{worse} is dominated by {better}"


def test_riposte_is_what_saves_dodge_right():
    """Guards the reasoning, not just the outcome: if riposte is ever tuned to 0,
    this test explains why the suite went red."""
    assert rules.riposte_of("dodge_right") > 0
    assert swing("dodge_right", "jab") > swing("block_high", "jab"), (
        "dodge_right must beat block_high somewhere, or nothing justifies its cost"
    )


# ─── No-move is never a hedge ───


@pytest.mark.parametrize("mine", REAL_ACTIONS)
@pytest.mark.parametrize("theirs", REAL_ACTIONS)
def test_no_move_is_worse_than_every_real_action(mine, theirs):
    """Silence must never be viable. A no-move is defenceless, gets the passive
    regen only, and costs 15 — so every real move, including a badly chosen one,
    beats it in every branch.

    Note the comparison is on the *combined* payoff, not on HP swing alone. HP swing
    alone is not the invariant and asserting it would be wrong: throwing a `jab` into
    a correct `dodge_right` costs 5 chip damage, whereas no-moving against a
    defensive opponent costs 0 HP, since a defence has no attack to land. It is the
    15-point penalty that makes silence strictly worse there — which is exactly the
    job the penalty was sized for (spec §13).
    """
    real = swing(mine, theirs) - penalty(mine, theirs)
    dead = swing(NoMove("timeout"), theirs) - penalty(NoMove("timeout"), theirs)

    assert penalty(mine, theirs) < penalty(NoMove("timeout"), theirs)
    assert real > dead, f"{mine} vs {theirs}: no-move is not strictly worse"


def test_no_move_penalty_equals_the_biggest_hit_in_the_game():
    """Chosen so that no-move is worse than any real move in every branch —
    including throwing a hook that gets countered (spec §13)."""
    assert rules.NO_MOVE_PENALTY == max(rules.damage_of(a) for a in rules.ATTACKS)
