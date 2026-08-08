"""The boxing rules an agent needs to know, as data plus a prompt-ready summary.

Source of truth: docs/adamant_spec.md sections 2-5 (v0.6).  The platform owns the
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

#: The repetition cap (spec 4.6). The same action may be submitted at most this
#: many times in a row; the 4th and every further consecutive repeat is VOID.
MAX_CONSECUTIVE_REPEATS = 3

#: What ``decide()`` falls back to when the model times out or answers badly.
#: A hedge is strictly better than silence (spec 4.4/10) but it is still a real
#: move with real consequences -- a high fallback rate means the model ran out of
#: clock, not that it chose to defend.
FALLBACK_ACTION = "block_high"

#: The hedge's escape hatch. Under the repetition cap a fallback that fires on
#: four consecutive ticks would void on the fourth and leave us defenseless --
#: the exact opposite of what a hedge is for -- so at the cap it steps sideways
#: to the other block. Both cost 3 stamina and both are real defenses, and
#: alternating can never void, because switching action resets the streak to 1.
FALLBACK_ALTERNATE = "block_low"

#: Tried in order; the first that would not be voided is played.
FALLBACK_CHAIN = (FALLBACK_ACTION, FALLBACK_ALTERNATE)

#: Causes the platform logs for a no-move (spec 4.4).
NO_MOVE_CAUSES = ("timeout", "malformed", "invalid_action", "wrong_tick", "duplicate")


def would_void(fighter: dict | None, action: str) -> bool:
    """True if submitting ``action`` now would be the 4th+ consecutive repeat.

    Read straight off a fighter state block, which carries ``last_action`` and
    ``repeat_streak`` for exactly this purpose (spec 4.6) -- so an agent can see
    the cap coming without keeping any state of its own (spec 1).
    """
    fighter = fighter if isinstance(fighter, dict) else {}
    if fighter.get("last_action") != action:
        return False
    try:
        streak = int(fighter.get("repeat_streak") or 0)
    except (TypeError, ValueError):
        return False  # unreadable state is not a reason to refuse to hedge
    return streak >= MAX_CONSECUTIVE_REPEATS


def fallback_action(fighter: dict | None = None) -> str:
    """The hedge to play, given our own fighter state. Never a voided move.

    ``fighter`` is ``state_update.self``. Passing nothing yields the documented
    default, which is right when we have no state to consult.
    """
    for action in FALLBACK_CHAIN:
        if not would_void(fighter, action):
            return action
    # Unreachable: `last_action` is a single value, so it cannot void both.
    return FALLBACK_ACTION

RULES_SUMMARY = """\
Adamant - boxing rules (spec v0.6)

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

REPETITION CAP
  You may submit the same action at most 3 times IN A ROW. The 4th consecutive
  repeat, and every one after it, is VOID: stripped to nothing. A void deals no
  damage, blocks nothing, dodges nothing and never ripostes, and you stand
  defenseless for that tick and take full damage. You still pay the stamina, and
  you still get only the +5 passive regen - a voided idle earns no idle bonus.
  There is no penalty: it was a legal move sent on time.
  A void STILL EXTENDS the streak, so repeating harder does not unstick you; only
  submitting a different action resets the count to 1. A no-move also clears it.
  The cap covers all eight actions, idle included. One action is therefore never
  a whole strategy - not even a defensive one.
  Your own last_action and repeat_streak are in the state every tick, so you can
  always see the cap coming. At a streak of 3, change your action.

WINNING
  HP <= 0 is a KO and ends the match immediately; a double KO is a draw.
  Otherwise the higher decision score (points - penalties) wins at max_ticks.
  Points are all the damage you dealt, riposte damage included.
"""
