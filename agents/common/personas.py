"""Named fighting styles, so one script can field more than one fighter.

Running ``reference_agent.py`` twice to fill both corners of a match used to
produce two identical agents with the same name: a mirror match, which is a bad
demo (you cannot tell the fighters apart) and a worthless simulation (there is
nothing to discriminate). A persona varies the display name and the strategic
half of the system prompt, and is recorded in the join ``profile`` so that a
match log can say which style fought it -- profile fields cannot be backfilled,
so a persona that is not in the profile is invisible to evaluation.

**A persona is a change of strategy, never of protocol.** The rules summary, the
deadline discipline, the strict-JSON output contract and the eight action names
are identical for all of them; ``build_system_prompt`` always emits the output
contract last, after the persona's guidance, so style cannot displace compliance.

Selection: ``--persona`` picks one. Unspecified, one is chosen at random from
those not already claimed for the same match code, so filling both corners from
two bare invocations gives two different fighters rather than a collision.
"""

from __future__ import annotations

import json
import random
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .prompt import PROMPT_VERSION


@dataclass(frozen=True)
class Persona:
    """One fighting style.

    ``name`` is the display name and must satisfy ``wsloop.NAME_RE``. ``version``
    is bumped whenever ``guidance`` changes, because comparing two matches across
    a silent prompt change is not a measurement.
    """

    key: str
    name: str
    version: str
    tagline: str
    guidance: str


_PERSONAS = (
    Persona(
        key="aggressor",
        name="Bulldozer",
        version="v1",
        tagline="pressure fighter: forward, always, and happy to trade",
        guidance="""\
YOUR STYLE: pressure fighter. You win by being the one who is punching.

Attack by default; defend only when you have a specific reason to expect an
attack you can counter exactly. A trade is a good outcome for you, not a bad
one - if you throw hook into their jab you take 5 and deal 15, and you can
afford that exchange all day. Hunt the moments the opponent is committed to
defending or has just idled, because those are free damage.

Your risk is stamina, not damage: hook costs 15 and regen is only 5 per tick, so
mix in jab (5) to keep punching rather than stalling out, and use cross (10) as
the middle option when a hook is too expensive but a jab is too small. Rotate
your punches - jab, cross and hook are three different actions, so varying them
keeps the pressure on and the repetition cap never touches you. A cross also
beats the block_low they put up for your hook. Never defend three ticks running.\
""",
    ),
    Persona(
        key="counter_puncher",
        name="Ricochet",
        version="v1",
        tagline="counter-puncher: invite the shot, dodge it, make them pay",
        guidance="""\
YOUR STYLE: counter-puncher. You want them to swing first.

The dodges are your scoring shots: dodge_left beats cross and hook, dodge_right
beats jab, and each one negates the damage AND ripostes 5 back. Blocks only
cancel; dodges pay. Read which attack is coming from their recent pattern and
their stamina, then pick the dodge that answers it. A wrong dodge takes full
damage, so guess with evidence, not habit.

Prefer dodge_right when they have been throwing the cheap shot and dodge_left
when they have the stamina for the big one; those two dodges between them answer
every attack in the game. When you genuinely cannot read them, block_high costs 3
and keeps you watching, which is better than spending 8 on a coin flip. Punish
stalls: an opponent who idles or defends is not threatening you, and that tick is
the one to attack into. Rotate dodge_left, dodge_right and block_high so the
repetition cap never catches you standing still.\
""",
    ),
    Persona(
        key="economist",
        name="Metronome",
        version="v1",
        tagline="stamina economist: spend nothing cheaply, punish the exhausted",
        guidance="""\
YOUR STYLE: stamina economist. You win the long match on the balance sheet.

Every action has a price and regen is only +5 a tick. Blocks cost 3 and are
cheaper than the regen, so guarding is net positive stamina; idle banks +20 but
leaves you defenseless, so take it when they cannot reach you or cannot afford
to. Track the opponent's stamina as carefully as their HP: below 15 they cannot
hook, below 10 they cannot cross, and below 5 they cannot even jab. An opponent
who tries an action they cannot afford is converted to idle and stands wide open.

That is your kill window - bank early, then spend a run of hooks when they have
nothing left to answer with. Do not confuse economy with passivity: a match where
you deal no damage is a draw, not a win, and the repetition cap means you cannot
simply hold one block. Alternate block_high and block_low while you wait.\
""",
    ),
    Persona(
        key="pattern_reader",
        name="Cipher",
        version="v1",
        tagline="pattern reader: the history window is the whole game",
        guidance="""\
YOUR STYLE: pattern reader. You beat the opponent's habits, not the opponent.

Work the recent-ticks history first, every tick, before you consider your own
plan. Look for a cycle (does their move depend on the tick number?), a trigger
(do they react to what you did last tick?), or a bias (one action far more often
than the rest). Most opponents, bots especially, are more repetitive than they
look, and one confirmed read is worth more than any amount of general caution.

The classic pattern is a run of jab followed by one block_high to reset the
streak - if you see a jab, jab, jab, block_high shape, you know both the tick to
punish and the tick to guard. Note that block_high is the answer to a jab, so an
opponent doing this is telling you their whole plan.

The repetition cap hands you a second, harder read for free: an opponent sitting
at repeat_streak 3 must change action this tick or be voided and defenseless. If
they change, you know what they will NOT play; if they do not, they are a free
target - attack. State the pattern you think you have found in your reason field
so it can be checked. Until you have a read, stay cheap and keep watching; once
you have one, commit to the exact counter.\
""",
    ),
)

PERSONAS: dict[str, Persona] = {persona.key: persona for persona in _PERSONAS}
PERSONA_KEYS: tuple[str, ...] = tuple(PERSONAS)


def get_persona(key: str | None) -> Persona | None:
    """Look up a persona, or ``None`` for the neutral agent. Raises on a typo."""
    if not key:
        return None
    try:
        return PERSONAS[key]
    except KeyError:
        raise ValueError(
            f"unknown persona {key!r}; choose one of: {', '.join(PERSONA_KEYS)}"
        ) from None


def prompt_version(persona: Persona | None) -> str:
    """The prompt identity for the profile: base prompt text plus persona text.

    Two personas are two different prompts and must never aggregate into one
    cell, so the persona is part of the version rather than a note beside it.
    """
    if persona is None:
        return PROMPT_VERSION
    return f"{PROMPT_VERSION}+{persona.key}-{persona.version}"


# --- Picking one -----------------------------------------------------------

#: Which personas have been used for which match code recently. This exists for
#: one reason: two bare invocations of the script filling the two corners of the
#: same match must not both draw the same fighter. It is a convenience for local
#: simulation, never a correctness dependency -- every read and write is
#: best-effort, and a missing or unwritable file just means an unbiased draw.
CLAIM_FILE = Path(tempfile.gettempdir()) / "adam_arena_personas.json"

#: Long enough to cover setting up a match by hand, short enough that yesterday's
#: codes never constrain today's draw.
CLAIM_TTL_S = 3600


def _load_claims(path: Path, now: float) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        code: entry
        for code, entry in data.items()
        if isinstance(entry, dict) and now - float(entry.get("at") or 0) < CLAIM_TTL_S
    }


def _claim(path: Path, code: str, key: str, now: float) -> None:
    claims = _load_claims(path, now)
    entry = claims.setdefault(code, {"at": now, "keys": []})
    entry["at"] = now
    if key not in entry["keys"]:
        entry["keys"].append(key)
    try:
        path.write_text(json.dumps(claims), encoding="utf-8")
    except OSError:
        pass  # a persona we cannot remember is still a persona we can fight with


def pick_persona(
    key: str | None = None,
    *,
    code: str | None = None,
    rng=None,
    claim_file: Path | None = None,
) -> Persona:
    """Return the requested persona, or draw one that is free for this match.

    With ``code`` supplied, personas already drawn for that code are skipped, so
    the second corner of a match differs from the first. Two processes racing on
    the same code could still collide; this is a simulation aid, not a lock.
    """
    explicit = get_persona(key)
    if explicit is not None:
        return explicit

    path = claim_file or CLAIM_FILE
    now = time.time()
    pool = list(_PERSONAS)
    if code:
        taken = set((_load_claims(path, now).get(code) or {}).get("keys") or [])
        pool = [persona for persona in pool if persona.key not in taken] or pool

    chosen = (rng or random).choice(pool)
    if code:
        _claim(path, code, chosen.key, now)
    return chosen
