"""The BOXING rules an agent needs to know, as data plus a prompt-ready summary.

Source of truth: docs/arena_spec.md sections 2-5 (v0.4).  The platform owns the
real table; this is the agent-side mirror used for prompting and for offline log
analysis.  ``test_rules.py`` checks the prose summary against the data.
"""

from __future__ import annotations

ACTIONS: tuple[str, ...] = (
    "jab",
    "cross",
    "hook",
    "block_high",
    "block_low",
    "dodge_left",
    "dodge_right",
    "idle",
)

ATTACKS: tuple[str, ...] = ("jab", "cross", "hook")
BLOCKS: tuple[str, ...] = ("block_high", "block_low")
DODGES: tuple[str, ...] = ("dodge_left", "dodge_right")
DEFENSES: tuple[str, ...] = BLOCKS + DODGES

DAMAGE = {"jab": 5, "cross": 10, "hook": 15}

STAMINA_COST = {
    "jab": 5,
    "cross": 10,
    "hook": 15,
    "block_high": 3,
    "block_low": 3,
    "dodge_left": 8,
    "dodge_right": 8,
    "idle": 0,
}

# Which defenses counter which attack (spec 2).
COUNTERS = {
    "jab": ("block_high", "dodge_right"),
    "cross": ("block_high", "dodge_left"),
    "hook": ("block_low", "dodge_left"),
}

RIPOSTE_DAMAGE = 5
NO_MOVE_PENALTY = 15
PASSIVE_REGEN = 5
IDLE_REGEN_BONUS = 15

#: What ``decide()`` falls back to when the model times out or answers badly.
#: A hedge is strictly better than silence (spec 4.4/10) but it is still a real
#: move with real consequences -- a high fallback rate means the model ran out of
#: clock, not that it chose to defend.
FALLBACK_ACTION = "block_high"

#: Causes the platform logs for a no-move (spec 4.4).
NO_MOVE_CAUSES = ("timeout", "malformed", "invalid_action", "wrong_tick", "duplicate")

RULES_SUMMARY = """\
ADAM Arena - boxing rules (spec v0.4)

Two fighters. Each tick both secretly submit ONE of eight actions; the moves
resolve simultaneously against the state at the start of the tick, so a fighter
knocked to 0 HP still lands the attack it threw that tick.

ATTACKS            damage  stamina  countered by
  jab                 5       5     block_high, dodge_right
  cross              10      10     block_high, dodge_left
  hook               15      15     block_low,  dodge_left

DEFENSES           stamina  effect
  block_high          3     negates jab and cross. No riposte.
  block_low           3     negates hook. No riposte.
  dodge_left          8     negates cross and hook, ripostes 5 to the attacker.
  dodge_right         8     negates jab, ripostes 5 to the attacker.

OTHER
  idle                0     +15 stamina on top of the +5 passive regen
                            (+20 total). No defense: takes full damage.

RESOLUTION
  attack vs its correct block  -> 0 damage; the attacker still pays stamina.
  attack vs its correct dodge  -> 0 damage; the defender ripostes 5 damage.
  attack vs a wrong defense, idle, or a no-move -> full damage lands.
  attack vs attack             -> a TRADE: both attacks land at full damage.
  defense vs defense or idle   -> nothing happens; a riposte needs an incoming
                                  attack to evade.

ECONOMY
  Every fighter regenerates +5 stamina per tick, capped at 100.
  An action you cannot afford is converted to idle, with the full idle regen and
  no penalty.
  Submitting nothing before the deadline is a NO-MOVE: you are defenseless, get
  only the +5 regen, and take a 15-point penalty. It is strictly worse than every
  real action, so never stall.

WINNING
  HP <= 0 is a KO and ends the match immediately; a double KO is a draw.
  Otherwise the higher decision score (points - penalties) wins at max_ticks.
  Points are all the damage you dealt, riposte damage included.
"""
