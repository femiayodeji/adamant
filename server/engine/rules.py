"""The BOXING rules table.

Rules are data (plan.md Principle 2). The resolver is a generic engine that reads
this table; a future combat type is a new table plus new clips, with zero engine
changes. There is deliberately no combat-type selector or plugin system — callers
import BOXING directly.

Every number here is mirrored in arena_spec.md §2, §3, §5 and §13. If one changes,
the spec changes in the same commit.
"""

from __future__ import annotations

from typing import Any

# ─── Action space (spec §2) ───

ATTACKS: dict[str, dict[str, int]] = {
    "jab": {"damage": 5, "stamina": 5},
    "cross": {"damage": 10, "stamina": 10},
    "hook": {"damage": 15, "stamina": 15},
}

# `counters` lists the attacks this defense negates. `riposte` is chip damage
# dealt back to the attacker on a successful evade — blocks do not riposte.
#
# Riposte is what stops `dodge_right` being strictly dominated by `block_high`,
# which counters everything dodge_right counters *plus* cross, at under half the
# stamina. See spec §2 and the no-domination invariant in tests/test_table.py.
DEFENSES: dict[str, dict[str, Any]] = {
    "block_high": {"stamina": 3, "counters": ("jab", "cross"), "riposte": 0},
    "block_low": {"stamina": 3, "counters": ("hook",), "riposte": 0},
    "dodge_left": {"stamina": 8, "counters": ("cross", "hook"), "riposte": 5},
    "dodge_right": {"stamina": 8, "counters": ("jab",), "riposte": 5},
}

IDLE = "idle"

# Ordered so that `random`-bot seeds and any enumeration are stable across runs.
ACTIONS: tuple[str, ...] = tuple(ATTACKS) + tuple(DEFENSES) + (IDLE,)

# ─── Fighter economy (spec §3) ───

START_HP = 100
START_STAMINA = 100
STAMINA_CAP = 100
PASSIVE_REGEN = 5  # every tick, unconditionally — including on a no-move
IDLE_REGEN_BONUS = 15  # on top of PASSIVE_REGEN, so idle is +20 total

# ─── No-move (spec §4.4) ───

# Equal to `hook` damage, the largest single hit in the game, so that a no-move is
# worse than any real move in every branch — including throwing a hook that gets
# countered. A materially smaller penalty would make silence a viable hedge.
NO_MOVE_PENALTY = 15

NO_MOVE_CAUSES: tuple[str, ...] = (
    "timeout",  # connection open, nothing arrived before the deadline
    "malformed",  # not valid JSON, or not a `move` message
    "invalid_action",  # action name not in the eight of §2
    "wrong_tick",  # `tick` field does not match the current tick
    "duplicate",  # a move for this tick was already accepted (the first is kept)
)

# ─── Match configuration (spec §5, §8.3) ───

DEFAULT_MAX_TICKS = 60
DEFAULT_DEADLINE_MS = 5000
DEFAULT_RESUME_WINDOW_MS = 120_000

MAX_TICKS_RANGE = (1, 200)
DEADLINE_MS_RANGE = (500, 120_000)

BOXING: dict[str, Any] = {
    "attacks": ATTACKS,
    "defenses": DEFENSES,
    "idle": IDLE,
    "actions": ACTIONS,
    "start_hp": START_HP,
    "start_stamina": START_STAMINA,
    "stamina_cap": STAMINA_CAP,
    "passive_regen": PASSIVE_REGEN,
    "idle_regen_bonus": IDLE_REGEN_BONUS,
    "no_move_penalty": NO_MOVE_PENALTY,
}


def is_attack(action: str) -> bool:
    return action in ATTACKS


def is_defense(action: str) -> bool:
    return action in DEFENSES


def stamina_cost(action: str) -> int:
    if action in ATTACKS:
        return ATTACKS[action]["stamina"]
    if action in DEFENSES:
        return DEFENSES[action]["stamina"]
    if action == IDLE:
        return 0
    raise ValueError(f"unknown action: {action!r}")


def damage_of(attack: str) -> int:
    return ATTACKS[attack]["damage"]


def counters(defense: str, attack: str) -> bool:
    """True if `defense` negates `attack`."""
    return attack in DEFENSES[defense]["counters"]


def riposte_of(defense: str) -> int:
    return DEFENSES[defense]["riposte"]
