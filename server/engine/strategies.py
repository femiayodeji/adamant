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

    def _would_void(self, ctx: Context, action: str) -> bool:
        """True if submitting `action` now would be the 4th+ consecutive repeat and
        therefore be stripped to nothing (spec §4.6).

        Read straight off `state_update.self`, which carries `last_action` and
        `repeat_streak` for exactly this purpose — no bot keeps local state.
        """
        me = ctx.get("self", {})
        return (
            me.get("last_action") == action
            and int(me.get("repeat_streak", 0)) >= rules.MAX_CONSECUTIVE_REPEATS
        )

    def _respecting_cap(self, ctx: Context, action: str) -> str:
        """`action`, or a filler that resets the streak when it would void.

        Every ladder bot routes its choice through here. A bot that voided out
        would stop probing the thing it exists to probe — Jab-bot in particular is
        a designed trap, not a punching bag (spec §11), and a trap that stops
        punching teaches nothing. `ConstantBot` deliberately does *not* use this:
        the balance round-robin's whole job is to report what a genuinely pure
        strategy is worth, and under this rule the honest answer is "it voids out".
        """
        if not self._would_void(ctx, action):
            return action
        filler = IDLE if action == "block_high" else "block_high"
        return self._or_idle(ctx, filler)


class RandomBot(Strategy):
    """Uniform choice over the eight actions. Probes the floor."""

    name = "random"

    def decide(self, ctx: Context) -> str:
        return self._respecting_cap(ctx, self.rng.choice(rules.ACTIONS))


class JabBot(Strategy):
    """`jab jab jab block_high`, repeating. A designed trap, not a punching bag
    (spec §11).

    It jabs to the repetition cap and then spends one tick on `block_high` to reset
    the streak (spec §4.6), because a fourth jab would be voided and a bot that
    voids out stops probing anything.

    The trap survives the cap, and sharpens: the obvious answer — always
    `block_high` — now voids from its own fourth tick onward, leaving the blocker
    defenceless against the jabs it meant to block. Winning still requires noticing
    that `hook` into an incoming `jab` is a trade worth net +10 per exchange, and
    now also requires varying the answer.

    Stamina never binds: jab costs 5, block_high 3, and passive regen is 5.
    """

    name = "jab"

    def decide(self, ctx: Context) -> str:
        return self._respecting_cap(ctx, "jab")


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
        # Neither published pattern repeats an action four times in a row, so the
        # cap never fires here — routed through it anyway so that adding a pattern
        # cannot silently introduce a bot that voids out.
        return self._respecting_cap(ctx, self._or_idle(ctx, self.PATTERNS[self.period][idx]))


class FrequencyBot(Strategy):
    """Reads the opponent's action distribution and answers the whole of it.

    Probes whether an agent notices it is being read and starts mixing. Its own
    exploitability is the point — but it has to be exploitable *because it reads*,
    not because of an accident of the history window.

    It counts attacks and defenses in separate pools and picks the answer that
    covers the most of whichever pool is larger, weighted by how often each action
    appeared. Deliberately **not** the single most-used action: the window is only
    ten ticks (spec §6.2), so against anything with a short period the plurality is
    decided by whichever action happens to be sitting at the window's edge, and it
    rotates as the window slides. Against a `jab block_high cross` cycle that made
    the bot flip from correctly dodging every punch to standing up and throwing
    hook on a fixed cadence at exactly tick 12 — the tick the opening `jab` fell
    out of the window — and eating clean hits it had been evading, with nothing
    about the opponent having changed. A distribution moves smoothly where a
    plurality jumps, so the read no longer turns on one count.
    """

    name = "frequency"

    def decide(self, ctx: Context) -> str:
        attacks: dict[str, int] = {}
        defenses: dict[str, int] = {}
        seen = 0
        for entry in ctx.get("history", []):
            action = entry.get("moves", {}).get("opponent")
            if action not in rules.ACTIONS:
                continue
            seen += 1
            if rules.is_attack(action):
                attacks[action] = attacks.get(action, 0) + 1
            elif rules.is_defense(action):
                defenses[action] = defenses.get(action, 0) + 1

        if not seen:
            return self._respecting_cap(ctx, self._or_idle(ctx, "jab"))

        # Guard while the opponent throws at least as often as it guards: eating a
        # punch costs more than missing a punish, so the tie goes to defending.
        if attacks and sum(attacks.values()) >= sum(defenses.values()):
            return self._guard(ctx, attacks)
        return self._punish(ctx, defenses)

    def _guard(self, ctx: Context, attacks: dict[str, int]) -> str:
        """The defense covering the most of the opponent's attacks. Ties prefer a
        dodge — it ripostes — and then `ACTIONS` order, never dict insertion."""
        ranked = sorted(
            DEFENSES,
            key=lambda d: (
                -sum(n for atk, n in attacks.items() if rules.counters(d, atk)),
                not rules.riposte_of(d),
                rules.ACTIONS.index(d),
            ),
        )
        return self._first_playable(ctx, ranked)

    def _punish(self, ctx: Context, defenses: dict[str, int]) -> str:
        """The hardest attack the opponent's guards cover least. An opponent that
        mostly idles covers nothing, so this is `hook` first."""
        ranked = sorted(
            rules.ATTACKS,
            key=lambda atk: (
                sum(n for d, n in defenses.items() if rules.counters(d, atk)),
                -rules.damage_of(atk),
            ),
        )
        return self._first_playable(ctx, ranked)

    def _first_playable(self, ctx: Context, ranked: list[str]) -> str:
        for candidate in ranked:
            if self._affordable(ctx, candidate) and not self._would_void(ctx, candidate):
                return candidate
        return self._respecting_cap(ctx, IDLE)


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
        return self._respecting_cap(ctx, self._or_idle(ctx, choice))


# ─── Fixed strategies used by the Phase 1 balance simulation ───


class ConstantBot(Strategy):
    """Always the same action. The pure-strategy corner of the round-robin.

    Deliberately *not* repeat-aware: the round-robin exists to report what a
    genuinely pure strategy is worth, and under the repetition cap (spec §4.6)
    the honest answer is that it lands three hits and then voids. Making this
    bot dodge the cap would hide the very effect the cap was added to produce.
    """

    def __init__(self, action: str, seed: Optional[int] = None) -> None:
        super().__init__(seed)
        if action not in rules.ACTIONS:
            raise ValueError(f"unknown action: {action!r}")
        self.action = action
        self.name = f"always-{action}"

    def decide(self, ctx: Context) -> str:
        return self._or_idle(ctx, self.action)


class CappedConstantBot(Strategy):
    """One action, played as often as the repetition cap allows.

    The honest successor to `ConstantBot` under spec §4.6: three of the action,
    then one filler to reset the streak, repeating. It is what "a strategy built
    around one action" now means, and it is the strategy the published figures for
    `dodge_right` and `dodge_left` are measured with — a pure `ConstantBot` would
    void out and report the cap's effect rather than the riposte's.
    """

    def __init__(self, action: str, seed: Optional[int] = None) -> None:
        super().__init__(seed)
        if action not in rules.ACTIONS:
            raise ValueError(f"unknown action: {action!r}")
        self.action = action
        self.name = f"mostly-{action}"

    def decide(self, ctx: Context) -> str:
        return self._respecting_cap(ctx, self._or_idle(ctx, self.action))


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
