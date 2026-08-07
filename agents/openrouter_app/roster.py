"""Three fighters. A fighter is a model, a style, and a name — bundled.

The unit of evaluation in this arena is the **agent**: prompt + model + logic as
one thing (CLAUDE.md). So the roster does not offer a model list and a persona
list to mix at the call site; it offers three finished fighters, each of which
is one point in that space and can be pointed at a match code by name.

Each pairs an OpenRouter model with one of the personas in
``agents/common/personas.py``, so the three differ in *both* halves of what makes
an agent — a different model reading a different strategy — and a match between
any two of them is a real fight rather than a mirror.

Model ids are OpenRouter ids and are the only part of this file with a shelf
life. ``--model`` overrides one for a single run, and whatever id is used ends up
in the profile, which is what the match log keeps.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.personas import Persona, get_persona
from ..common.personas import prompt_version as persona_prompt_version
from ..common.prompt import build_system_prompt
from ..common.wsloop import NAME_RE

APP_VERSION = "openrouter-app@1.0.0"

#: A five-second deadline leaves the model roughly 3.5 s (spec §12, and
#: ``decision.TIME_BUDGET_FRACTION``), which is not enough for extended thinking.
#: Kept low and excluded from the reply for every fighter; OpenRouter drops it for
#: models with no reasoning mode.
LOW_REASONING = {"effort": "low", "exclude": True}

#: Room for the reasoning a model does anyway plus one small JSON object. Cutting
#: this too fine is how a reasoning model returns an empty string and hedges every
#: tick — a fallback loop that looks like a bad agent but is a bad token budget.
MAX_TOKENS = 1024

#: Not the 0.0 the harness pins. That is right for a measurement, where a varying
#: sampler is noise between two rows; here it would mean two runs of the same
#: fighter against the same bot fight the identical match tick for tick. This is a
#: demo roster, so it samples — and the value is in the profile either way.
TEMPERATURE = 0.3

#: "Caller said nothing", as distinct from an explicit ``None``.
_KEEP: object = object()


@dataclass(frozen=True)
class Fighter:
    """One entrant: a display name, an OpenRouter model, and a fighting style."""

    key: str
    name: str
    model: str
    persona_key: str
    blurb: str
    temperature: float | None = TEMPERATURE
    max_tokens: int = MAX_TOKENS

    @property
    def persona(self) -> Persona:
        return get_persona(self.persona_key)  # type: ignore[return-value]

    def system_prompt(self) -> str:
        """Rules, instructions, this fighter's style, then the output contract."""
        return build_system_prompt(self.persona)

    def profile(self, model: str | None = None, temperature: object = _KEEP) -> dict:
        """Provenance for the match log (spec §6.1). Required at join.

        Everything that could make two runs of "the same" fighter different lives
        here — the model, the sampler, the prompt identity including the persona,
        and the app version — because profile fields cannot be backfilled and a
        match fought before they landed is unusable data.
        """
        return {
            "model": model or self.model,
            "provider": "openrouter",
            "agent": self.key,
            "persona": self.persona_key,
            "prompt_version": persona_prompt_version(self.persona),
            # `None` is a real value here -- it means "the parameter was not sent",
            # which some models require -- so the default is a sentinel and not None.
            "temperature": self.temperature if temperature is _KEEP else temperature,
            "agent_version": APP_VERSION,
        }


ROSTER: tuple[Fighter, ...] = (
    Fighter(
        key="bulldozer",
        name="Bulldozer",
        model="anthropic/claude-haiku-4.5",
        persona_key="aggressor",
        blurb="Claude Haiku 4.5 pressure fighter — forward, always, happy to trade.",
    ),
    Fighter(
        key="ricochet",
        name="Ricochet",
        model="google/gemini-3.6-flash",
        persona_key="counter_puncher",
        blurb="Gemini 3.6 Flash counter-puncher — invites the shot, dodges, ripostes.",
    ),
    Fighter(
        key="cipher",
        name="Cipher",
        model="openai/gpt-5.4-mini",
        persona_key="pattern_reader",
        blurb="GPT-5.4 mini pattern reader — plays your habits, not you.",
    ),
)

FIGHTERS: dict[str, Fighter] = {fighter.key: fighter for fighter in ROSTER}
FIGHTER_KEYS: tuple[str, ...] = tuple(FIGHTERS)


def get_fighter(key: str) -> Fighter:
    """Look one up by key. Raises on a typo rather than guessing."""
    try:
        return FIGHTERS[key]
    except KeyError:
        raise ValueError(
            f"unknown agent {key!r}; choose one of: {', '.join(FIGHTER_KEYS)}"
        ) from None


def describe() -> list[dict]:
    """The roster as data — what ``GET /api/agents`` serves and ``--list`` prints."""
    return [
        {
            "key": fighter.key,
            "name": fighter.name,
            "model": fighter.model,
            "persona": fighter.persona_key,
            "blurb": fighter.blurb,
        }
        for fighter in ROSTER
    ]


# A name the platform would reject is a fighter that cannot enter the ring, and the
# only place that would surface is a rejected join at the worst possible moment.
for _fighter in ROSTER:
    if not NAME_RE.match(_fighter.name):
        raise ValueError(f"fighter name {_fighter.name!r} must match {NAME_RE.pattern!r}")
