"""Message validation and construction for both WebSocket protocols (spec §6, §7).

Kept separate from the match state machine so the wire format is testable on its
own, and so there is exactly one place where the egocentric/absolute split (spec
§1.1) is applied.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .engine import rules
from .engine.resolver import Corner, Event, MatchState, to_egocentric

SCHEMA_VERSION = 1

MAX_NAME = 24
NAME_PATTERN = re.compile(rf"^[A-Za-z0-9 _-]{{1,{MAX_NAME}}}$")


class JoinError(Exception):
    """Carries the `reason` for the error frame that precedes the close."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_name(name: Any) -> str:
    """Names are the only free-text channel between adversaries — everything else
    crossing the boundary is drawn from the closed vocabulary of eight action names
    and numeric state. Restricting the character set closes a prompt-injection
    vector that would otherwise let an agent win for reasons unrelated to combat.
    """
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise JoinError("invalid_name")
    return name


def validate_profile(profile: Any) -> dict[str, Any]:
    """Required, and stored verbatim. A result that cannot say what produced it is
    not a measurement, and this is the one log field that cannot be backfilled."""
    if not isinstance(profile, dict) or not profile:
        raise JoinError("profile_required")
    return dict(profile)


#: Not one of the no-move causes of spec §4.4, and deliberately so: a move for a
#: tick that has already resolved is a late answer to a closed question, and it is
#: dropped like a duplicate rather than recorded against the tick now open. A slow
#: agent's overdue move for tick N lands during tick N+1, so counting it as
#: `wrong_tick` relabelled every no-move after the first as a protocol error —
#: turning "this agent is slow" into "this agent cannot echo a tick number", which
#: is a different finding about the agent and the reason the causes are separate.
STALE = "stale"


def classify_move(payload: Any, expected_tick: int) -> tuple[Optional[str], Optional[str]]:
    """Validate an inbound `move`. Returns `(action, rejection_cause)`.

    Exactly one of the two is non-None. Causes match spec §4.4 and are logged
    distinctly, because "the agent was slow" and "the agent cannot follow the
    output format" are different findings about an agent. The one cause that is not
    from §4.4 is `STALE`, which the caller drops instead of scoring.
    """
    if not isinstance(payload, dict) or payload.get("type") != "move":
        return None, "malformed"
    tick = payload.get("tick")
    if tick != expected_tick:
        if isinstance(tick, int) and not isinstance(tick, bool) and tick < expected_tick:
            return None, STALE
        return None, "wrong_tick"
    action = payload.get("action")
    if action not in rules.ACTIONS:
        return None, "invalid_action"
    return action, None


def sanitise_telemetry(telemetry: Any) -> dict[str, Any]:
    """Telemetry is optional and never affects resolution — the platform acts only
    on `action`. It is logged verbatim, with `reason` clipped to the documented
    200-char budget so one agent cannot bloat every log line."""
    if not isinstance(telemetry, dict):
        return {}
    clean = dict(telemetry)
    reason = clean.get("reason")
    if isinstance(reason, str):
        clean["reason"] = reason[:200]
    elif reason is not None:
        clean.pop("reason", None)
    return clean


def strip_for_spectators(telemetry: dict[str, Any]) -> dict[str, Any]:
    """`reason` and token counts stay out of the live spectator stream so that
    in-flight reasoning is never visible to a watching opponent. They remain in the
    log, which is what makes a match gradeable after the fact (spec §7)."""
    return {
        corner: {
            k: v
            for k, v in (values or {}).items()
            if k not in ("reason", "tokens_in", "tokens_out")
        }
        for corner, values in telemetry.items()
    }


# ─── Outbound frames ───


def state_update(
    corner: Corner,
    tick: int,
    deadline_ms: int,
    state: MatchState,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Everything needed to decide, egocentric, with the last 10 ticks most recent
    first. Agents are stateless by design — this is what makes a pause invisible to
    a correctly built one."""
    other: Corner = "agent_b" if corner == "agent_a" else "agent_a"
    window = [
        {
            "tick": record["tick"],
            "moves": {
                "self": record["moves"][corner],
                "opponent": record["moves"][other],
            },
            "events": to_egocentric(record["events"], corner),
        }
        for record in reversed(history[-10:])
    ]
    return {
        "type": "state_update",
        "tick": tick,
        "deadline_ms": deadline_ms,
        "self": state.fighter(corner).as_dict(),
        "opponent": state.fighter(other).as_dict(),
        "history": window,
    }


def tick_result(
    tick: int,
    moves: dict[str, Optional[str]],
    events: list[Event],
    state: MatchState,
    telemetry: dict[str, Any],
) -> dict[str, Any]:
    """Absolute party references — this feeds spectators and the log (spec §1.1)."""
    return {
        "type": "tick_result",
        "tick": tick,
        "moves": moves,
        "events": events,
        "state": state.as_dict(),
        "telemetry": telemetry,
    }


def match_end(result, final_tick: int, state: MatchState) -> dict[str, Any]:
    """Byte-identical for agents and spectators, so one parser serves both. `winner`
    is null — not "draw", not absent — whenever no single corner won (spec §5.1)."""
    return {
        "type": "match_end",
        "winner": result.winner,
        "reason": result.reason,
        "final_tick": final_tick,
        "final_state": state.as_dict(),
    }


def error(reason: str) -> dict[str, str]:
    return {"type": "error", "reason": reason}
