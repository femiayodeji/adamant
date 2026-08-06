"""Pure decision strategies — the baseline ladder's brains (spec §11).

These are deliberately socket-free so the balance gate can run round-robins
headlessly (plan Phase 1 gate). `server/bots.py` wraps them in ordinary WebSocket
clients that dial in through the same public protocol as any external agent — there
is no privileged internal path, so baselines and contenders are measured through
identical machinery.

`decide()` takes the payload of a `state_update` (spec §6.2) verbatim, egocentric
party references and all, so the wrapper does no translation.
"""

from __future__ import annotations

import random
from typing import Any, Optional

from . import rules
from .rules import DEFENSES, IDLE

Context = dict[str, Any]


class Strategy:
    """Deterministic given a seed. `name` is what appears in the ladder."""

    name = "strategy"

    def __init__(self, seed: Optional[int] = None) -> None:
        self.seed = seed
        self.rng = random.Random(seed)

    def reset(self) -> None:
        self.rng = random.Random(self.seed)

    def decide(self, ctx: Context) -> str:
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _stamina(ctx: Context) -> int:
        return ctx.get("self", {}).get("stamina", rules.START_STAMINA)

    def _affordable(self, ctx: Context, action: str) -> bool:
        return rules.stamina_cost(action) <= self._stamina(ctx)

    def _or_idle(self, ctx: Context, action: str) -> str:
        """Self-manage stamina rather than eat an `exhausted` conversion.

        Converting is legal and carries no penalty (spec §3), but it wastes the
        tick's regen bonus decision and makes bot behaviour harder to read in logs.
        """
        return action if self._affordable(ctx, action) else IDLE


class RandomBot(Strategy):
    """Uniform choice over the eight actions. Probes the floor."""

    name = "random"

    def decide(self, ctx: Context) -> str:
        return self.rng.choice(rules.ACTIONS)


class JabBot(Strategy):
    """Always `jab`. A designed trap, not a punching bag (spec §11).

    The obvious answer — always `block_high` — takes zero damage and deals zero:
    both fighters finish at 100 HP, a draw. Winning requires noticing that `hook`
    into an incoming `jab` is a trade worth net +10 per exchange.

    Kept pure: jab costs 5 and passive regen is 5, so stamina never moves and the
    bot never needs an affordability guard.
    """

    name = "jab"

    def decide(self, ctx: Context) -> str:
        return "jab"


class CycleBot(Strategy):
    """A fixed short pattern. Probes pattern detection inside the 10-tick history
    window — both periods fit, by design."""

    name = "cycle"

    PATTERNS: dict[int, tuple[str, ...]] = {
        3: ("jab", "block_high", "cross"),
        5: ("jab", "jab", "hook", "block_high", "dodge_left"),
    }

    def __init__(self, seed: Optional[int] = None, period: int = 3) -> None:
        super().__init__(seed)
        if period not in self.PATTERNS:
            raise ValueError(f"cycle period must be one of {sorted(self.PATTERNS)}")
        self.period = period
        # The period must be in the name: two cycle bots with different periods are
        # different entrants, and a shared name silently merges them in any ladder
        # table or log aggregation keyed on bot name.
        self.name = f"cycle-{period}"

    def decide(self, ctx: Context) -> str:
        # 1-indexed ticks (spec §5), so tick 1 is the first element.
        idx = (int(ctx.get("tick", 1)) - 1) % self.period
        return self._or_idle(ctx, self.PATTERNS[self.period][idx])


class FrequencyBot(Strategy):
    """Counters the opponent's most-used action so far.

    Probes whether an agent notices it is being read and starts mixing. Its own
    exploitability is the point.
    """

    name = "frequency"

    def decide(self, ctx: Context) -> str:
        counts: dict[str, int] = {}
        for entry in ctx.get("history", []):
            action = entry.get("moves", {}).get("opponent")
            if action in rules.ACTIONS:
                counts[action] = counts.get(action, 0) + 1

        if not counts:
            return self._or_idle(ctx, "jab")

        # Ties broken by ACTIONS order for determinism, not by dict insertion.
        top = max(rules.ACTIONS, key=lambda a: (counts.get(a, 0), -rules.ACTIONS.index(a)))

        if rules.is_attack(top):
            # Prefer a dodge — it ripostes — but only when affordable.
            dodges = [d for d in DEFENSES if rules.counters(d, top) and rules.riposte_of(d)]
            blocks = [d for d in DEFENSES if rules.counters(d, top) and not rules.riposte_of(d)]
            for candidate in dodges + blocks:
                if self._affordable(ctx, candidate):
                    return candidate
            return IDLE

        if rules.is_defense(top):
            # Throw the hardest attack that this defense does not counter.
            for attack in ("hook", "cross", "jab"):
                if not rules.counters(top, attack) and self._affordable(ctx, attack):
                    return attack
            return IDLE

        # Opponent idles a lot: hit it as hard as stamina allows.
        for attack in ("hook", "cross", "jab"):
            if self._affordable(ctx, attack):
                return attack
        return IDLE


class MixerBot(Strategy):
    """A near-equilibrium randomised mix. The ceiling — nobody should reliably beat
    it, because two agents both playing near-equilibrium score alike regardless of
    strength. Discrimination comes from exploiting *non*-equilibrium opponents,
    which is why the ladder rather than model-vs-model play is the instrument."""

    name = "mixer"

    WEIGHTS: dict[str, float] = {
        "jab": 0.20,
        "cross": 0.15,
        "hook": 0.10,
        "block_high": 0.20,
        "block_low": 0.15,
        "dodge_left": 0.10,
        "dodge_right": 0.05,
        IDLE: 0.05,
    }

    def decide(self, ctx: Context) -> str:
        actions = list(self.WEIGHTS)
        weights = [self.WEIGHTS[a] for a in actions]
        choice = self.rng.choices(actions, weights=weights, k=1)[0]
        return self._or_idle(ctx, choice)


# ─── Fixed strategies used by the Phase 1 balance simulation ───


class ConstantBot(Strategy):
    """Always the same action. The pure-strategy corner of the round-robin."""

    def __init__(self, action: str, seed: Optional[int] = None) -> None:
        super().__init__(seed)
        if action not in rules.ACTIONS:
            raise ValueError(f"unknown action: {action!r}")
        self.action = action
        self.name = f"always-{action}"

    def decide(self, ctx: Context) -> str:
        return self._or_idle(ctx, self.action)


class HookEveryNthBot(Strategy):
    """Blocks high, but throws `hook` every Nth tick.

    This is the line that solves the Jab-bot trap: hook into an incoming jab is a
    trade (spec §4.2) dealing 15 and taking 5, net +10 per exchange.
    """

    def __init__(self, n: int = 4, seed: Optional[int] = None) -> None:
        super().__init__(seed)
        self.n = n
        self.name = f"hook-every-{n}"

    def decide(self, ctx: Context) -> str:
        tick = int(ctx.get("tick", 1))
        action = "hook" if tick % self.n == 0 else "block_high"
        return self._or_idle(ctx, action)


LADDER: dict[str, type[Strategy]] = {
    "random": RandomBot,
    "jab": JabBot,
    "cycle": CycleBot,
    "frequency": FrequencyBot,
    "mixer": MixerBot,
}


def build(name: str, seed: Optional[int] = None, **kwargs: Any) -> Strategy:
    if name not in LADDER:
        raise ValueError(f"unknown bot {name!r}; expected one of {sorted(LADDER)}")
    return LADDER[name](seed=seed, **kwargs)
