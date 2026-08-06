"""The agent side of the WebSocket protocol (spec 6).

An agent is a WebSocket *client*: it dials the platform with a match code, joins
with a name and a required profile, answers every ``state_update`` with a
``move``, and stops at ``match_end``.

The agent keeps **no state between ticks**. Everything needed to decide arrives
in the ``state_update``, which is what makes a pause/resume invisible: a
re-issued ``state_update`` for a tick we already answered is simply answered
again, with the newer message treated as authoritative (spec 6.3).
"""

from __future__ import annotations

import asyncio
import json
import re

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

#: Display names are restricted so the one free-text channel between adversaries
#: cannot carry a prompt injection (spec 6.1).
NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{1,24}$")

DEFAULT_DEADLINE_MS = 5000


class ProtocolError(RuntimeError):
    """The platform rejected us, or sent something we cannot act on."""


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError(f"name must match {NAME_RE.pattern!r}, got {name!r}")
    return name


def join_url(url: str, code: str) -> str:
    return f"{url.rstrip('/')}/ws/join/{code}"


def _noop(*_args) -> None:
    pass


async def _play(uri, name, profile, decide, session, log):
    """One socket's worth of match. Returns the ``match_end`` payload, or None
    if the socket closed before the match finished."""
    async with connect(uri) as ws:
        if session.get("corner_token"):
            await ws.send(json.dumps({"type": "resume", "corner_token": session["corner_token"]}))
        else:
            await ws.send(json.dumps({"type": "join", "name": name, "profile": profile}))

        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")

            if kind == "joined":
                session["corner"] = msg.get("corner")
                session["corner_token"] = msg.get("corner_token")
                log(f"joined {msg.get('match_code')} as {session['corner']}")

            elif kind == "error":
                raise ProtocolError(msg.get("reason", "unknown"))

            elif kind == "match_start":
                session["config"] = msg.get("config", {}) or {}
                session["opponent_name"] = msg.get("opponent_name")
                session["corner"] = msg.get("you", session.get("corner"))
                log(f"match start vs {session['opponent_name']} config={session['config']}")

            elif kind == "state_update":
                config = session.get("config", {})
                deadline_ms = (
                    msg.get("deadline_ms") or config.get("deadline_ms") or DEFAULT_DEADLINE_MS
                )
                decision = await decide(
                    msg,
                    session.get("opponent_name"),
                    deadline_ms,
                    config.get("max_ticks"),
                )
                await ws.send(
                    json.dumps(
                        {
                            "type": "move",
                            "tick": msg["tick"],
                            "action": decision.action,
                            "telemetry": decision.telemetry(),
                        }
                    )
                )
                log(
                    f"tick {msg['tick']}: {decision.action} "
                    f"({decision.latency_ms} ms, fallback={decision.fallback})"
                )

            elif kind == "match_paused":
                # Nothing to re-sync: we hold no state between ticks. The clock
                # is stopped for both corners until the missing corner returns.
                log(f"paused at tick {msg.get('tick')}, missing {msg.get('missing')}")

            elif kind == "match_resumed":
                log("resumed; the same tick will be re-issued with a fresh deadline")

            elif kind == "match_end":
                log(f"match end: winner={msg.get('winner')} reason={msg.get('reason')}")
                return msg

    return None


async def run_match(
    url: str,
    code: str,
    name: str,
    profile: dict,
    decide,
    *,
    max_reconnects: int = 2,
    reconnect_delay_s: float = 1.0,
    log=_noop,
) -> dict | None:
    """Play one match to ``match_end``; return that payload (or None).

    ``decide(state, opponent_name, deadline_ms, max_ticks) -> Decision``.

    A dropped socket is infrastructure failure, not a lost fight: the platform
    pauses the match and waits, so we reconnect and reclaim the corner with the
    ``corner_token`` from ``joined`` (spec 6.4).
    """
    validate_name(name)
    if not profile:
        raise ValueError("profile is required at join (spec 6.1)")
    uri = join_url(url, code)
    session: dict = {"corner_token": None, "config": {}, "opponent_name": None}

    for attempt in range(max_reconnects + 1):
        try:
            end = await _play(uri, name, profile, decide, session, log)
        except (ConnectionClosed, OSError) as exc:
            log(f"connection lost: {exc!r}")
            end = None
        if end is not None:
            return end
        if session["corner_token"] is None or attempt == max_reconnects:
            break
        log(f"reconnecting to reclaim {session.get('corner')}")
        await asyncio.sleep(reconnect_delay_s)
    return None
