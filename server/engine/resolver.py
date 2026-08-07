"""Pure combat resolution (spec §4).

    resolve(state, move_a, move_b) -> (new_state, events)

Zero I/O, zero randomness, no mutation. Purity is load-bearing three times over
(plan.md Principle 3):

  * the rules are testable before a socket exists;
  * a stored match log replays to exact state, which is how crash recovery works
    (spec §6.4c) — there is no database, the log is the source of truth;
  * old matches can be re-scored offline against metrics that did not exist when
    they were fought.

Parties here are always absolute (`agent_a` / `agent_b`). Translation to the
egocentric `self` / `opponent` vocabulary happens at the protocol edge (spec §1.1),
never in the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Union

from . import rules
from .rules import (
    IDLE,
    MAX_CONSECUTIVE_REPEATS,
    NO_MOVE_PENALTY,
    PASSIVE_REGEN,
    IDLE_REGEN_BONUS,
    STAMINA_CAP,
    START_HP,
    START_STAMINA,
)

Corner = Literal["agent_a", "agent_b"]
CORNERS: tuple[Corner, Corner] = ("agent_a", "agent_b")
Event = dict[str, Any]


@dataclass(frozen=True)
class NoMove:
    """A tick the fighter failed to answer on an open connection (spec §4.4).

    A *closed* connection is not a NoMove — that pauses the match instead (§6.4).
    Constructing this is the protocol edge's job; the engine only applies it.
    """

    cause: str = "timeout"

    def __post_init__(self) -> None:
        if self.cause not in rules.NO_MOVE_CAUSES:
            raise ValueError(f"unknown no-move cause: {self.cause!r}")


Move = Union[str, NoMove]


@dataclass(frozen=True)
class Fighter:
    hp: int = START_HP
    stamina: int = START_STAMINA
    points: int = 0
    penalties: int = 0

    #: The last action this fighter *submitted*, and how many times in a row it has
    #: now submitted it (spec §4.6). Part of fighter state, and shipped in every
    #: `state_update`, precisely so an agent can tell that one more repeat would be
    #: voided without keeping any local state of its own — agents are stateless by
    #: design (spec §1). `None` at match start and after a no-move.
    last_action: Optional[str] = None
    repeat_streak: int = 0

    @property
    def decision_score(self) -> int:
        """`points − penalties` (spec §3). May be negative."""
        return self.points - self.penalties

    def as_dict(self) -> dict[str, Any]:
        return {
            "hp": self.hp,
            "stamina": self.stamina,
            "points": self.points,
            "penalties": self.penalties,
            "last_action": self.last_action,
            "repeat_streak": self.repeat_streak,
        }


@dataclass(frozen=True)
class MatchState:
    agent_a: Fighter = Fighter()
    agent_b: Fighter = Fighter()
    tick: int = 0  # last *completed* tick; ticks are 1-indexed (spec §5)

    def fighter(self, corner: Corner) -> Fighter:
        return self.agent_a if corner == "agent_a" else self.agent_b

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {"agent_a": self.agent_a.as_dict(), "agent_b": self.agent_b.as_dict()}


@dataclass(frozen=True)
class MatchResult:
    winner: Optional[Corner]  # None on a draw — the key is always present (spec §5.1)
    reason: str  # "ko" | "points" | "draw" | "abandoned"

    def as_dict(self) -> dict[str, Any]:
        return {"winner": self.winner, "reason": self.reason}


def initial_state() -> MatchState:
    return MatchState()


def opponent_of(corner: Corner) -> Corner:
    return "agent_b" if corner == "agent_a" else "agent_a"


@dataclass(frozen=True)
class _Normalised:
    """One fighter's move after steps 1–3 of spec §4.1.

    `effective` is None exactly when the fighter can neither deal damage nor
    defend — a no-move or a voided repeat. The two differ in what they cost:
    a no-move spends no stamina and carries a penalty, a void spends the stamina
    and carries none.
    """

    effective: Optional[str]  # the action that actually resolves, if any
    cost: int  # stamina to deduct
    no_move: bool
    last_action: Optional[str]  # streak bookkeeping for the next tick
    streak: int


def _normalise(
    move: Move, fighter: Fighter, corner: Corner, events: list[Event]
) -> _Normalised:
    """Steps 1–3 of spec §4.1: no-move, then the repeat cap, then affordability.

    A raw string that is not one of the eight actions is a caller bug: the protocol
    edge is responsible for turning an unknown action name into
    `NoMove("invalid_action")` before the engine sees it.
    """
    if isinstance(move, NoMove):
        events.append(
            {
                "type": "no_move",
                "who": corner,
                "cause": move.cause,
                "penalty": NO_MOVE_PENALTY,
            }
        )
        # A no-move breaks the streak: there was no submission to repeat.
        return _Normalised(None, 0, True, None, 0)

    if move not in rules.ACTIONS:
        raise ValueError(
            f"unknown action {move!r}; the protocol edge must convert these to "
            f"NoMove('invalid_action') before calling resolve()"
        )

    # The streak counts what was *submitted*, so it is unaffected by whatever the
    # action later converts to — and a voided repeat still extends it, which is
    # what stops spamming from being a way out of the cap (spec §4.6).
    streak = fighter.repeat_streak + 1 if move == fighter.last_action else 1
    cost = rules.stamina_cost(move)

    if streak > MAX_CONSECUTIVE_REPEATS:
        # Stripped to nothing: deals nothing, defends nothing, never ripostes. The
        # stamina is still spent, so spam is punished economically rather than by a
        # penalty — this was a legal move submitted on time, and keeping it out of
        # `penalties` keeps the no-move statistics meaning only what they say.
        #
        # Checked *before* affordability, so a void action never also reports
        # `exhausted`; the clamp is what keeps stamina non-negative in the rare case
        # where the repeat was unaffordable anyway.
        events.append({"type": "void", "who": corner, "action": move, "streak": streak})
        return _Normalised(None, min(cost, fighter.stamina), False, move, streak)

    if cost > fighter.stamina:
        # Legal move, submitted on time, simply unaffordable. Converted to idle
        # *including* the full idle regen, and carrying no penalty (spec §3).
        events.append(
            {
                "type": "exhausted",
                "who": corner,
                "attempted": move,
                "stamina": fighter.stamina,
            }
        )
        return _Normalised(IDLE, 0, False, move, streak)

    return _Normalised(move, cost, False, move, streak)


def _resolve_attack(
    attacker: Corner,
    attack: Optional[str],
    defender: Corner,
    defense: Optional[str],
    events: list[Event],
) -> tuple[int, int]:
    """Resolve one fighter's attack against the other's action (spec §4.2).

    Returns `(damage_to_defender, riposte_to_attacker)`. Both are scored against
    the state at the *start* of the tick by the caller, so resolution is genuinely
    simultaneous — a fighter reduced to 0 HP still lands the attack it threw.
    """
    if attack is None or not rules.is_attack(attack):
        return 0, 0

    damage = rules.damage_of(attack)

    if defense is not None and rules.is_defense(defense) and rules.counters(defense, attack):
        riposte = rules.riposte_of(defense)
        if riposte:
            events.append(
                {
                    "type": "dodged",
                    "attacker": attacker,
                    "defender": defender,
                    "action": attack,
                    "defense": defense,
                    "riposte": riposte,
                }
            )
            return 0, riposte
        events.append(
            {
                "type": "blocked",
                "attacker": attacker,
                "defender": defender,
                "action": attack,
                "defense": defense,
            }
        )
        return 0, 0

    # Wrong defense, idle, no-move, or a trade: full damage lands.
    events.append(
        {
            "type": "hit",
            "attacker": attacker,
            "defender": defender,
            "action": attack,
            "damage": damage,
        }
    )
    return damage, 0


def _regen(fighter_stamina: int, effective: Optional[str]) -> int:
    """Step 6 of spec §4.1. A no-move gets the passive regen only — never the idle
    bonus, even though it looks superficially like standing still. So does a voided
    repeat, which is why spamming `idle` past the cap stops paying for itself."""
    regen = PASSIVE_REGEN
    if effective == IDLE:
        regen += IDLE_REGEN_BONUS
    return min(fighter_stamina + regen, STAMINA_CAP)


def resolve(
    state: MatchState, move_a: Move, move_b: Move
) -> tuple[MatchState, list[Event]]:
    """Advance the match by exactly one tick. Pure.

    Events are emitted in order of operations (spec §4.1): the conversion events
    (`no_move`, `void`, `exhausted` — at most one per fighter) in corner order,
    then attack outcomes in corner order (agent_a's attack, then agent_b's), then
    any `ko`.
    """
    a, b = state.agent_a, state.agent_b
    events: list[Event] = []

    # 1–3. Normalise both moves: no-move, repeat cap, affordability.
    norm_a = _normalise(move_a, a, "agent_a", events)
    norm_b = _normalise(move_b, b, "agent_b", events)
    eff_a, eff_b = norm_a.effective, norm_b.effective

    # 4. Deduct stamina. A no-move spends nothing; a voided repeat spends its cost.
    stam_a = a.stamina - norm_a.cost
    stam_b = b.stamina - norm_b.cost

    # 5. Resolve both attacks against the opposing action, including riposte.
    dmg_to_b, riposte_to_a = _resolve_attack("agent_a", eff_a, "agent_b", eff_b, events)
    dmg_to_a, riposte_to_b = _resolve_attack("agent_b", eff_b, "agent_a", eff_a, events)

    # All landed damage, riposte included, is added to the *dealer's* points (§3).
    # `riposte_to_a` is chip damage agent_a suffered from agent_b's dodge, so it
    # scores for agent_b — the dodger, not the dodged.
    new_a = Fighter(
        hp=a.hp - dmg_to_a - riposte_to_a,
        stamina=_regen(stam_a, eff_a),
        points=a.points + dmg_to_b + riposte_to_b,
        penalties=a.penalties + (NO_MOVE_PENALTY if norm_a.no_move else 0),
        last_action=norm_a.last_action,
        repeat_streak=norm_a.streak,
    )
    new_b = Fighter(
        hp=b.hp - dmg_to_b - riposte_to_b,
        stamina=_regen(stam_b, eff_b),
        points=b.points + dmg_to_a + riposte_to_a,
        penalties=b.penalties + (NO_MOVE_PENALTY if norm_b.no_move else 0),
        last_action=norm_b.last_action,
        repeat_streak=norm_b.streak,
    )

    # 7. KO events. Emitted in corner order so a double-KO is deterministic.
    if new_a.hp <= 0:
        events.append({"type": "ko", "who": "agent_a"})
    if new_b.hp <= 0:
        events.append({"type": "ko", "who": "agent_b"})

    return MatchState(agent_a=new_a, agent_b=new_b, tick=state.tick + 1), events


def check_end(state: MatchState, max_ticks: int) -> Optional[MatchResult]:
    """Win conditions (spec §5). None if the match continues.

    There is no TKO rule — it was removed in v0.4 as redundant. An agent that stops
    deciding is already defenseless and accruing penalties, so it loses on damage or
    on the decision score without a special case.
    """
    a_down = state.agent_a.hp <= 0
    b_down = state.agent_b.hp <= 0

    if a_down and b_down:
        return MatchResult(winner=None, reason="draw")
    if b_down:
        return MatchResult(winner="agent_a", reason="ko")
    if a_down:
        return MatchResult(winner="agent_b", reason="ko")

    if state.tick >= max_ticks:
        score_a = state.agent_a.decision_score
        score_b = state.agent_b.decision_score
        if score_a > score_b:
            return MatchResult(winner="agent_a", reason="points")
        if score_b > score_a:
            return MatchResult(winner="agent_b", reason="points")
        return MatchResult(winner=None, reason="draw")

    return None


# ─── Egocentric projection (spec §1.1) ───

_PARTY_FIELDS = ("attacker", "defender", "who")


def to_egocentric(events: list[Event], me: Corner) -> list[Event]:
    """Rewrite absolute party references to `self` / `opponent` for agent-facing
    messages. Field sets are otherwise identical."""
    out: list[Event] = []
    for event in events:
        projected = dict(event)
        for field in _PARTY_FIELDS:
            if field in projected:
                projected[field] = "self" if projected[field] == me else "opponent"
        out.append(projected)
    return out
