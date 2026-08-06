"""Hand-built match logs in the spec 7 format, for the offline analysis tests.

Arithmetic is kept internally consistent so the fixtures read like real logs:
``sample_log`` is a five-tick match where agent_a lands a hook trade (15) and one
riposte (5) for 20 points, takes 15, and drops one tick to a malformed move.
"""

from __future__ import annotations

ENTRANT_PROFILE = {
    "model": "anthropic/claude-opus-5",
    "resolved_model": "anthropic/claude-opus-5-20260101",
    "provider": "openrouter",
    "prompt_version": "ref-v1",
    "temperature": None,
    "agent_version": "harness@1.0.0",
}

BOT_PROFILE = {"model": "none", "agent_version": "jabbot@1.0"}


def _telemetry(latency=800, fallback=False, tokens_in=600, tokens_out=20):
    return {
        "latency_ms": latency,
        "fallback": fallback,
        "reason": "test",
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def match_start(deadline_ms=5000, max_ticks=60, match_code="RUMBLE-4F2"):
    return {
        "type": "match_start",
        "schema_version": 1,
        "match_code": match_code,
        "agents": {
            "agent_a": {"name": "HarnessBot", "profile": dict(ENTRANT_PROFILE)},
            "agent_b": {"name": "Jab-bot", "profile": dict(BOT_PROFILE)},
        },
        "config": {
            "max_ticks": max_ticks,
            "deadline_ms": deadline_ms,
            "resume_window_ms": 120000,
        },
        "started_at": "2026-08-06T14:03:11Z",
    }


def _tick(tick, move_a, move_b, events, telemetry_a=None):
    return {
        "type": "tick_result",
        "tick": tick,
        "moves": {"agent_a": move_a, "agent_b": move_b},
        "events": events,
        "state": {},
        "telemetry": {
            "agent_a": telemetry_a or _telemetry(),
            "agent_b": {"latency_ms": 2, "fallback": False},
        },
    }


def sample_log(deadline_ms=5000):
    """Five ticks against Jab-bot: block, trade, dodge+riposte, wrong block, no-move."""
    ticks = [
        _tick(1, "block_high", "jab", [
            {"type": "blocked", "attacker": "agent_b", "defender": "agent_a",
             "action": "jab", "defense": "block_high"},
        ]),
        _tick(2, "hook", "jab", [
            {"type": "hit", "attacker": "agent_a", "defender": "agent_b",
             "action": "hook", "damage": 15},
            {"type": "hit", "attacker": "agent_b", "defender": "agent_a",
             "action": "jab", "damage": 5},
        ]),
        _tick(3, "dodge_right", "jab", [
            {"type": "dodged", "attacker": "agent_b", "defender": "agent_a",
             "action": "jab", "defense": "dodge_right", "riposte": 5},
        ]),
        _tick(4, "block_low", "jab", [
            {"type": "hit", "attacker": "agent_b", "defender": "agent_a",
             "action": "jab", "damage": 5},
        ]),
        _tick(5, None, "jab", [
            {"type": "no_move", "who": "agent_a", "cause": "malformed", "penalty": 15},
            {"type": "hit", "attacker": "agent_b", "defender": "agent_a",
             "action": "jab", "damage": 5},
        ], telemetry_a=_telemetry(latency=4900, fallback=True, tokens_in=610, tokens_out=0)),
    ]
    return {
        "schema_version": 1,
        "match_start": match_start(deadline_ms=deadline_ms),
        "ticks": ticks,
        "match_end": {
            "type": "match_end",
            "winner": "agent_b",
            "reason": "points",
            "final_tick": 5,
            "final_state": {
                "agent_a": {"hp": 85, "stamina": 60, "points": 20, "penalties": 15},
                "agent_b": {"hp": 80, "stamina": 70, "points": 15, "penalties": 0},
            },
        },
    }


def ko_log(final_tick=28, winner="agent_a", deadline_ms=5000):
    """A short KO log: enough to exercise ticks-to-KO and exploitation speed."""
    ticks = [
        _tick(t, "hook", "jab", [
            {"type": "hit", "attacker": "agent_a", "defender": "agent_b",
             "action": "hook", "damage": 15},
            {"type": "hit", "attacker": "agent_b", "defender": "agent_a",
             "action": "jab", "damage": 5},
        ])
        for t in range(1, 4)
    ]
    return {
        "schema_version": 1,
        "match_start": match_start(deadline_ms=deadline_ms),
        "ticks": ticks,
        "match_end": {
            "type": "match_end",
            "winner": winner,
            "reason": "ko",
            "final_tick": final_tick,
            "final_state": {
                "agent_a": {"hp": 70, "stamina": 40, "points": 100, "penalties": 0},
                "agent_b": {"hp": 0, "stamina": 55, "points": 30, "penalties": 0},
            },
        },
    }


def abandoned_log(deadline_ms=5000):
    log = ko_log(deadline_ms=deadline_ms)
    log["match_end"] = {
        "type": "match_end",
        "winner": "agent_a",
        "reason": "abandoned",
        "final_tick": 3,
        "final_state": log["match_end"]["final_state"],
    }
    return log
