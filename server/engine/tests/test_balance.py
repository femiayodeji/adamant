"""The Phase 1 balance gate.

A scripted simulation (no sockets) reproducing the published figures, plus the
property-style termination check. These are the numbers the plan names explicitly;
if a tuning pass moves them, the spec moves in the same commit.
"""

from __future__ import annotations

import itertools

import pytest

from server.engine import rules
from server.engine.resolver import check_end
from server.engine.simulate import published_figures, round_robin, run_match
from server.engine.strategies import (
    CappedConstantBot,
    ConstantBot,
    CycleBot,
    FrequencyBot,
    HookEveryNthBot,
    JabBot,
    MixerBot,
    RandomBot,
)


@pytest.fixture(scope="module")
def figures():
    return published_figures()


@pytest.fixture(scope="module")
def table():
    return round_robin(seed=0)


# ─── The three published figures ───


def test_the_drawing_line_against_jab_bot_is_still_nil_nil(figures):
    """Jab-bot's trap (spec §11). The obvious answer — answer every jab with
    `block_high` — takes zero damage and deals zero: both fighters finish untouched,
    which is a draw, not a win.

    Under the repetition cap (spec §4.6) this line now has to be played by a bot
    that resets its streak, since a literal always-`block_high` voids out and loses
    outright (the test below). The lesson is unchanged and the figure is unchanged:
    blocking correctly is not the same as winning.
    """
    sim = figures["mostly_block_high_vs_jab"]

    assert sim.result.reason == "draw"
    assert sim.result.winner is None
    assert sim.final_state.agent_a.hp == 100 and sim.final_state.agent_b.hp == 100
    assert sim.final_state.agent_a.points == 0 and sim.final_state.agent_b.points == 0
    assert sim.final_tick == rules.DEFAULT_MAX_TICKS


def test_a_literal_always_block_high_is_knocked_out_by_jab_bot(figures):
    """What the repetition cap added to the trap.

    Before the cap, one-note blocking was merely unwinnable. Now the fourth
    consecutive `block_high` is void, the blocker is defenceless from tick 4 on, and
    Jab-bot walks through it. The naive answer went from drawing to losing.
    """
    sim = figures["block_high_vs_jab"]

    assert sim.result.winner == "agent_b"
    assert sim.result.reason == "ko"
    assert sim.final_tick == 30
    assert sim.final_state.agent_a.hp <= 0


def test_hook_every_fourth_kos_jab_bot_around_tick_28(figures):
    """The trade line: hook into an incoming jab deals 15 and takes 5, net +10.

    This figure survived the repetition cap untouched — the line never repeats an
    action four times running, so the cap never binds on it. What did change is the
    margin: Jab-bot now spends every fourth tick blocking to reset its own streak,
    so the hooker finishes unmarked instead of on 65 HP.
    """
    sim = figures["hook_every_4th_vs_jab"]

    assert sim.result.winner == "agent_a"
    assert sim.result.reason == "ko"
    assert sim.final_tick == 28, f"expected the published KO at tick 28, got {sim.final_tick}"
    assert sim.final_state.agent_a.hp == 100
    assert sim.final_state.agent_a.points == 105


def test_dodge_right_is_not_a_dead_action(figures):
    """At riposte 0 this bot loses to Jab-bot on points. At 5 it wins.

    Measured with the cap-respecting bot on purpose: a literal always-`dodge_right`
    voids out, which would report the cap's effect where this figure is meant to
    report the riposte's. Riposte 5 stays justified.
    """
    sim = figures["dodge_right_vs_jab"]

    assert sim.result.winner == "agent_a", "dodge_right must be able to beat Jab-bot"
    assert sim.result.reason == "ko"


def test_a_correct_read_beats_aggression(figures):
    """dodge_left counters both cross and hook, so it punishes hook-spam."""
    sim = figures["dodge_left_vs_hook"]
    assert sim.result.winner == "agent_a"
    assert sim.result.reason == "ko"


def test_a_wrong_read_loses_to_aggression():
    """The flip side — dodges only pay off when the read is right."""
    sim = run_match(CappedConstantBot("dodge_right"), CappedConstantBot("hook"))
    assert sim.result.winner == "agent_b"


# ─── §4.6 the repetition cap, at match scale ───


def test_a_one_note_attacker_lands_three_punches_and_then_nothing(figures):
    """The cap's whole purpose, measured over a full match rather than a tick.

    60 ticks of nothing but `jab` against a fighter that never defends produces
    three landed jabs — 15 damage — and 57 wasted ticks.
    """
    sim = figures["pure_jab_voids_out"]

    assert sim.final_state.agent_a.points == 3 * rules.damage_of("jab")
    assert sim.final_state.agent_b.hp == rules.START_HP - 3 * rules.damage_of("jab")
    assert sim.final_tick == rules.DEFAULT_MAX_TICKS, "and it cannot even finish the job"


@pytest.mark.parametrize(
    "action", ["jab", "cross", "hook", "block_high", "block_low", "dodge_left", "dodge_right"]
)
def test_respecting_the_cap_beats_ignoring_it(action):
    """For every action, the cap-respecting version of a one-note strategy is at
    least as good as the literal one. If this ever inverts, the cap is rewarding the
    behaviour it exists to discourage."""
    sim = run_match(CappedConstantBot(action), ConstantBot(action))

    assert sim.result.winner in ("agent_a", None), (
        f"always-{action} beat mostly-{action}: the cap is backwards"
    )
    assert sim.hp_differential >= 0


def test_no_bot_in_the_ladder_ever_voids_a_move(table):
    """Ladder bots are repeat-aware by design (spec §11): a bot that voided out
    would stop probing the thing it exists to probe. `ConstantBot` is exempt — its
    voiding is the measurement."""
    ladder = {"random", "jab", "cycle-3", "cycle-5", "frequency", "mixer", "hook-every-4"}

    for name_a, name_b, sim in table["matches"]:
        for record in sim.ticks:
            for event in record.events:
                if event["type"] != "void":
                    continue
                who = "agent_a" if event["who"] == "agent_a" else "agent_b"
                offender = name_a if who == "agent_a" else name_b
                assert offender not in ladder, (
                    f"{offender} voided {event['action']} on tick {record.tick}"
                )


# ─── Property-style termination ───

STRATEGIES = [
    ConstantBot("jab"),
    ConstantBot("hook"),
    ConstantBot("block_high"),
    ConstantBot("dodge_left"),
    ConstantBot("idle"),
    CappedConstantBot("hook"),
    CappedConstantBot("block_high"),
    CappedConstantBot("dodge_right"),
    JabBot(),
    RandomBot(seed=7),
    CycleBot(period=3),
    CycleBot(period=5),
    FrequencyBot(),
    MixerBot(seed=7),
    HookEveryNthBot(4),
]


@pytest.mark.parametrize(
    "left,right", list(itertools.combinations(range(len(STRATEGIES)), 2))
)
def test_any_match_terminates_with_a_valid_result(left, right):
    sim = run_match(STRATEGIES[left], STRATEGIES[right])

    assert sim.result.reason in ("ko", "points", "draw")
    assert sim.result.winner in (None, "agent_a", "agent_b")
    assert (sim.result.winner is None) == (sim.result.reason == "draw")
    assert 1 <= sim.final_tick <= rules.DEFAULT_MAX_TICKS
    assert len(sim.ticks) == sim.final_tick
    assert [t.tick for t in sim.ticks] == list(range(1, sim.final_tick + 1)), "1-indexed"


@pytest.mark.parametrize(
    "left,right", list(itertools.combinations(range(len(STRATEGIES)), 2))
)
def test_state_stays_within_legal_bounds_throughout(left, right):
    sim = run_match(STRATEGIES[left], STRATEGIES[right])

    for record in sim.ticks:
        for corner, fighter in record.state.items():
            assert 0 <= fighter["stamina"] <= rules.STAMINA_CAP, (corner, record.tick)
            assert fighter["points"] >= 0 and fighter["penalties"] >= 0
            assert fighter["hp"] <= rules.START_HP


def test_no_bot_ever_submits_an_illegal_action():
    for left, right in itertools.combinations(range(len(STRATEGIES)), 2):
        sim = run_match(STRATEGIES[left], STRATEGIES[right])
        for record in sim.ticks:
            for action in record.moves.values():
                assert action in rules.ACTIONS, f"{action!r} is not in the action space"


def test_points_reconcile_against_damage_taken():
    """Every point is damage someone actually took, riposte included."""
    sim = run_match(HookEveryNthBot(4), JabBot())
    a, b = sim.final_state.agent_a, sim.final_state.agent_b

    assert a.points == rules.START_HP - b.hp
    assert b.points == rules.START_HP - a.hp


def test_resolution_is_symmetric_across_corners():
    """Corner swapping should be a pure mirror — no positional effect exists to
    correct for, and this forecloses the question (plan, Evaluation Design)."""
    forward = run_match(HookEveryNthBot(4), JabBot())
    swapped = run_match(JabBot(), HookEveryNthBot(4))

    assert forward.final_tick == swapped.final_tick
    assert forward.final_state.agent_a.as_dict() == swapped.final_state.agent_b.as_dict()
    assert forward.final_state.agent_b.as_dict() == swapped.final_state.agent_a.as_dict()


# ─── Round-robin sanity: no degenerate strategy ───


def test_no_pure_strategy_sweeps_the_field(table):
    """If one strategy beats everything, the game is not measuring anything."""
    entrants = len(table["names"]) - 1
    for name, score in table["scores"].items():
        assert score["w"] < 2 * entrants, f"{name} won every match in both corners"


def test_always_jab_is_not_dominant(table):
    """The explicit trigger condition in plan Phase 3: if always-jab dominates, tune
    stamina/damage in rules.py and mirror the spec in the same commit."""
    scores = table["scores"]
    better = [n for n, s in scores.items() if s["w"] > scores["always-jab"]["w"]]
    assert better, "always-jab tops the ladder — the table needs tuning"


def test_the_ladder_bots_all_participate(table):
    for name in ("random", "jab", "cycle", "frequency", "mixer"):
        matching = [n for n in table["names"] if n.startswith(name)]
        assert matching, f"{name} missing from the round-robin"


def test_every_round_robin_match_terminates(table):
    for _, _, sim in table["matches"]:
        assert check_end(sim.final_state, rules.DEFAULT_MAX_TICKS) is not None
