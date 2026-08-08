"""Roster fighters, spawned by the host from the lobby (spec §8.6).

The three fighters of `agents/openrouter_app` are ordinary WebSocket clients. This
module lets the *host* start one without a terminal: `POST /api/matches/{ref}/fill_agent`
lands here, a fighter dials `/ws/join/{code}` exactly as a guest would, and the
lobby card flips. Nothing about the match, the log or the resolver can tell the
difference — the same invariant that governs the ladder bots (spec §11).

Two things are deliberately not hidden:

**It spends money.** Every tick is one model call per LLM corner, against the
host's `OPENROUTER_API_KEY`. The endpoint is therefore unavailable with no key
configured rather than silently falling back to something cheaper, and the lobby
says so before the button is pressed.

**The agent code is not duplicated here.** The roster, the prompt, the decision
loop and the OpenRouter client all live under `agents/`, and this module imports
them. A fighter that behaved differently depending on whether the host or its
author started it would make every log the platform records suspect.

The import of `agents` is soft: the package sits beside `server/` in the repo and
in the image, but the platform must still boot without it — a missing roster
disables one button, it does not take the arena down.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .corner_client import CornerClient, launch

logger = logging.getLogger("arena.llm")

UNAVAILABLE_NO_PACKAGE = "the agents package is not installed beside the server"
UNAVAILABLE_NO_KEY = "OPENROUTER_API_KEY is not configured on the server"


class ModelUnreachable(RuntimeError):
    """The roster fighter's model could not be called, so the corner was not taken.

    Carries the provider's own message: "insufficient credits" and "invalid key" need
    different actions from the host, and only the provider knows which it is.
    """


async def preflight(model_client: Any) -> None:
    """Confirm a model client can reach its model. Raises with the provider's reason.

    Skipped for a client without a `preflight` method, which is how the test suite's
    stubs stay two lines long.
    """
    probe = getattr(model_client, "preflight", None)
    if probe is None:
        return
    await probe()


def _roster_module():
    """The fighter roster, or None if `agents` is not importable."""
    try:
        from agents.openrouter_app import roster
    except Exception:  # pragma: no cover - only when the package is absent
        logger.debug("agents.openrouter_app is not importable", exc_info=True)
        return None
    return roster


def _have_key() -> bool:
    try:
        from agents.common.openrouter import have_api_key
    except Exception:  # pragma: no cover - only when the package is absent
        return False
    return have_api_key()


def roster() -> list[dict[str, Any]]:
    """The roster as data. Listed even with no key, so the lobby can show what
    *would* be available and say what is missing."""
    module = _roster_module()
    return module.describe() if module else []


def min_deadline_ms() -> int:
    """The shortest `deadline_ms` the roster can answer inside, or 0 if unknown."""
    module = _roster_module()
    return int(getattr(module, "MIN_DEADLINE_MS", 0) or 0) if module else 0


def availability() -> tuple[bool, Optional[str]]:
    """`(available, reason_if_not)` — what `GET /api/agents` reports."""
    if _roster_module() is None:
        return False, UNAVAILABLE_NO_PACKAGE
    if not _have_key():
        return False, UNAVAILABLE_NO_KEY
    return True, None


def build_model_client(fighter, model: Optional[str] = None):
    """The OpenRouter client for one fighter. Patched out in tests, which is how
    the suite fights whole matches against this code path for free."""
    from agents.openrouter_app.fighter import build_client

    return build_client(fighter, model=model)


class LLMAgentClient(CornerClient):
    """A roster fighter in a corner: the shared socket shell, with a model behind
    `decide` instead of a pure strategy."""

    def __init__(
        self,
        url: str,
        code: str,
        fighter,
        model: Optional[str] = None,
        corner: Optional[str] = None,
        model_client: Any = None,
    ) -> None:
        from agents.common.decision import make_decider

        self.fighter = fighter
        self.model_client = model_client or build_model_client(fighter, model)
        self._decide = make_decider(self.model_client, system=fighter.system_prompt())
        super().__init__(
            url=url,
            code=code,
            name=fighter.name,
            profile=fighter.profile(
                model=getattr(self.model_client, "model", None) or fighter.model
            ),
            corner=corner,
        )

    async def decide(self, message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Never raises: `decision.decide` hedges on a timeout, an unparseable
        reply or a transport error, and marks the move `fallback`. A provider
        outage costs this corner a defensive tick, not the match."""
        decision = await self._decide(
            message,
            self.opponent_name,
            message.get("deadline_ms") or self.config.get("deadline_ms"),
            self.config.get("max_ticks"),
        )
        return decision.action, decision.telemetry()

    def unrecoverable(self) -> Optional[str]:
        """Why this corner can never decide again, if that has happened.

        A bad key or an unpayable account fails the same way on every remaining
        tick. Hedging through all of them yields a full match of `block_high`
        against `block_high` — no damage, no events, a 0–0 draw, and a log whose
        `match_start` names a model that never answered once. Better to drop the
        corner and let the match pause on it (spec §6.4), which is what an
        infrastructure failure is supposed to look like.
        """
        return getattr(self.model_client, "permanent_error", None)

    async def aclose(self) -> None:
        await self.model_client.aclose()


async def spawn(
    url: str,
    code: str,
    agent: str,
    corner: Optional[str] = None,
    model: Optional[str] = None,
    **_ignored: Any,
) -> str:
    """Fill a corner with a roster fighter. Returns the corner it took.

    `_ignored` swallows the extra keys a stored fill may carry (`kind`), so a
    rematch can replay a fill dict straight back into this function.
    """
    module = _roster_module()
    if module is None:
        raise RuntimeError(UNAVAILABLE_NO_PACKAGE)

    fighter = module.get_fighter(agent)  # ValueError on an unknown key
    client = LLMAgentClient(url=url, code=code, fighter=fighter, model=model, corner=corner)

    # Check the model answers *before* taking the corner. `availability()` can only
    # see whether a key is configured, and a key that is present but cannot pay looks
    # identical to a working one until the first tick — at which point the corner is
    # already filled, the host has started the fight, and every tick hedges.
    try:
        await preflight(client.model_client)
    except Exception as exc:
        await client.aclose()
        raise ModelUnreachable(str(exc)) from exc

    taken = await client.handshake()
    launch(client, name=f"llm-{agent}-{code}")
    return taken
