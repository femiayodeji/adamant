"""Headless match driver and balance round-robin (plan Phase 1 gate).

No sockets, no server, no I/O beyond printing. Two `Strategy` objects are driven
through `resolve()` exactly the way the tick loop will drive two WebSocket agents,
so balance can be checked — and re-checked after any rules-table change — long
before Phase 2 exists.

Run:  python -m server.engine.simulate
"""

from __future__ import annotations

import argparse
import itertools
from dataclasses import dataclass, field
from typing import Any, Optional

from . import rules
from .resolver import (
    Corner,
    Event,
    MatchResult,
    MatchState,
    Move,
    check_end,
    initial_state,
    resolve,
    to_egocentric,
)
from .strategies import (
    CappedConstantBot,
    ConstantBot,
    CycleBot,
    FrequencyBot,
    HookEveryNthBot,
    JabBot,
    MixerBot,
    RandomBot,
    Strategy,
)

HISTORY_WINDOW = 10


@dataclass
class TickRecord:
    tick: int
    moves: dict[str, Optional[str]]
    events: list[Event]
    state: dict[str, dict[str, int]]


@dataclass
class SimResult:
    result: MatchResult
    final_state: MatchState
    ticks: list[TickRecord] = field(default_factory=list)

    @property
    def final_tick(self) -> int:
        return self.final_state.tick

    @property
    def hp_differential(self) -> int:
        """agent_a HP minus agent_b HP. Margin carries far more signal per match
        than a win/loss bit (plan, Evaluation Design)."""
        return self.final_state.agent_a.hp - self.final_state.agent_b.hp

    def summary(self) -> str:
        w = self.result.winner or "draw"
        a, b = self.final_state.agent_a, self.final_state.agent_b
        return (
            f"{w:<8} by {self.result.reason:<7} @t{self.final_tick:<3} "
            f"hp {a.hp:>4}/{b.hp:<4} pts {a.points:>3}/{b.points:<3} "
            f"pen {a.penalties:>2}/{b.penalties:<2}"
        )


def _context(
    corner: Corner,
    state: MatchState,
    tick: int,
    deadline_ms: int,
    history: list[TickRecord],
) -> dict[str, Any]:
    """Build the payload of a `state_update` (spec §6.2) for one corner.

    Egocentric: the agent never has to track which corner it is in to read its own
    history. `history` is the last 10 ticks, most recent first.
    """
    me = state.fighter(corner)
    them = state.fighter("agent_b" if corner == "agent_a" else "agent_a")
    other: Corner = "agent_b" if corner == "agent_a" else "agent_a"

    window = []
    for record in reversed(history[-HISTORY_WINDOW:]):
        window.append(
            {
                "tick": record.tick,
                "moves": {
                    "self": record.moves[corner],
                    "opponent": record.moves[other],
                },
                "events": to_egocentric(record.events, corner),
            }
        )

    return {
        "type": "state_update",
        "tick": tick,
        "deadline_ms": deadline_ms,
        "self": me.as_dict(),
        "opponent": them.as_dict(),
        "history": window,
    }


def run_match(
    strategy_a: Strategy,
    strategy_b: Strategy,
    max_ticks: int = rules.DEFAULT_MAX_TICKS,
    deadline_ms: int = rules.DEFAULT_DEADLINE_MS,
) -> SimResult:
    """Drive a full match between two pure strategies."""
    strategy_a.reset()
    strategy_b.reset()

    state = initial_state()
    ticks: list[TickRecord] = []

    for tick in range(1, max_ticks + 1):
        move_a: Move = strategy_a.decide(_context("agent_a", state, tick, deadline_ms, ticks))
        move_b: Move = strategy_b.decide(_context("agent_b", state, tick, deadline_ms, ticks))

        state, events = resolve(state, move_a, move_b)
        ticks.append(
            TickRecord(
                tick=tick,
                moves={"agent_a": move_a, "agent_b": move_b},
                events=events,
                state=state.as_dict(),
            )
        )

        result = check_end(state, max_ticks)
        if result is not None:
            return SimResult(result=result, final_state=state, ticks=ticks)

    # check_end fires on the final tick, so this is unreachable for max_ticks >= 1.
    raise AssertionError("match did not terminate within max_ticks")


# ─── Balance round-robin ───


def _entrants(seed: int = 0) -> list[Strategy]:
    # Both the pure one-action strategies and their cap-respecting successors. The
    # pure ones are kept deliberately: under spec §4.6 they void out after three
    # ticks, and the round-robin should say so out loud rather than quietly drop
    # them. The `mostly-` variants are what a one-action strategy is now worth.
    return [
        ConstantBot("jab"),
        ConstantBot("cross"),
        ConstantBot("hook"),
        ConstantBot("block_high"),
        ConstantBot("block_low"),
        ConstantBot("dodge_left"),
        ConstantBot("dodge_right"),
        ConstantBot("idle"),
        CappedConstantBot("jab"),
        CappedConstantBot("cross"),
        CappedConstantBot("hook"),
        CappedConstantBot("block_high"),
        CappedConstantBot("block_low"),
        CappedConstantBot("dodge_left"),
        CappedConstantBot("dodge_right"),
        RandomBot(seed=seed),
        JabBot(),
        CycleBot(period=3),
        CycleBot(period=5),
        FrequencyBot(),
        MixerBot(seed=seed),
        HookEveryNthBot(4),
    ]


def round_robin(seed: int = 0, max_ticks: int = rules.DEFAULT_MAX_TICKS) -> dict[str, Any]:
    """Every entrant against every other, both corners (corner swapping — the cost
    is near zero and it forecloses the question of positional effect)."""
    entrants = _entrants(seed)
    names = [s.name for s in entrants]
    scores = {n: {"w": 0, "l": 0, "d": 0, "hp_diff": 0} for n in names}
    matches: list[tuple[str, str, SimResult]] = []

    for left, right in itertools.permutations(range(len(entrants)), 2):
        a, b = entrants[left], entrants[right]
        sim = run_match(a, b, max_ticks=max_ticks)
        matches.append((a.name, b.name, sim))

        scores[a.name]["hp_diff"] += sim.hp_differential
        scores[b.name]["hp_diff"] -= sim.hp_differential
        if sim.result.winner == "agent_a":
            scores[a.name]["w"] += 1
            scores[b.name]["l"] += 1
        elif sim.result.winner == "agent_b":
            scores[b.name]["w"] += 1
            scores[a.name]["l"] += 1
        else:
            scores[a.name]["d"] += 1
            scores[b.name]["d"] += 1

    return {"scores": scores, "matches": matches, "names": names}


def published_figures(max_ticks: int = rules.DEFAULT_MAX_TICKS) -> dict[str, SimResult]:
    """The figures the Phase 1 gate names explicitly.

    Note which bot each uses. `block_high_vs_jab` is deliberately the *pure*
    ConstantBot — it is the naive answer to the Jab-bot trap, and the point of the
    figure is what the repetition cap now does to it. The dodge figures use
    `CappedConstantBot`, because a pure dodge bot voids out and would report the
    cap's effect where the figure is meant to report the riposte's.
    """
    return {
        "block_high_vs_jab": run_match(ConstantBot("block_high"), JabBot(), max_ticks=max_ticks),
        "mostly_block_high_vs_jab": run_match(
            CappedConstantBot("block_high"), JabBot(), max_ticks=max_ticks
        ),
        "hook_every_4th_vs_jab": run_match(HookEveryNthBot(4), JabBot(), max_ticks=max_ticks),
        "dodge_right_vs_jab": run_match(
            CappedConstantBot("dodge_right"), JabBot(), max_ticks=max_ticks
        ),
        "dodge_left_vs_hook": run_match(
            CappedConstantBot("dodge_left"), CappedConstantBot("hook"), max_ticks=max_ticks
        ),
        "pure_jab_voids_out": run_match(ConstantBot("jab"), ConstantBot("idle"), max_ticks=max_ticks),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="ADAM Arena balance simulation")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-ticks", type=int, default=rules.DEFAULT_MAX_TICKS)
    args = parser.parse_args()

    print("── Published balance figures (plan Phase 1 gate) ──\n")
    for label, sim in published_figures(args.max_ticks).items():
        print(f"  {label:<24} {sim.summary()}")

    print("\n── Round-robin, both corners ──\n")
    table = round_robin(seed=args.seed, max_ticks=args.max_ticks)
    ranked = sorted(
        table["scores"].items(), key=lambda kv: (-kv[1]["w"], kv[1]["l"], -kv[1]["hp_diff"])
    )
    print(f"  {'strategy':<18} {'W':>3} {'L':>3} {'D':>3} {'HPdiff':>8}")
    for name, s in ranked:
        print(f"  {name:<18} {s['w']:>3} {s['l']:>3} {s['d']:>3} {s['hp_diff']:>8}")


if __name__ == "__main__":
    main()
