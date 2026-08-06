"""Offline analysis of stored match logs. No model is ever re-run.

Everything here reads ``server/logs/{match_id}.json``:
``{schema_version, match_start, ticks, match_end}``. Because ``resolve()`` is
pure and the log is the full event stream, old matches can be re-scored against
metrics that did not exist when they were fought.

Metrics per corner:
  counter accuracy   -- did the defense we chose counter the incoming attack
  riposte rate       -- share of incoming attacks converted into riposte damage
  exploitation speed -- ticks to KO against each ladder bot
  stamina efficiency -- damage dealt per point of stamina spent
  no-move rate       -- broken down by the cause the platform logged
  instruction-following -- share of ticks free of malformed/invalid_action
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from ..common.rules import ATTACKS, COUNTERS, DEFENSES, RIPOSTE_DAMAGE, STAMINA_COST

CORNERS = ("agent_a", "agent_b")

#: no-move causes that mean the agent broke the contract, as opposed to being
#: slow or disconnected. These drive the instruction-following rate.
INVALID_CAUSES = ("malformed", "invalid_action")


def other_corner(corner: str) -> str:
    return "agent_b" if corner == "agent_a" else "agent_a"


def load_log(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def config_of(log: dict) -> dict:
    return ((log.get("match_start") or {}).get("config") or {})


def agent_entry(log: dict, corner: str) -> dict:
    return ((log.get("match_start") or {}).get("agents") or {}).get(corner) or {}


#: A non-LLM agent declares ``{"model": "none", ...}`` (spec 6.1), so "none" is
#: an absent model, not a label.
_NULL_MODELS = ("", "none", "null")


def label_of(log: dict, corner: str) -> str:
    """A short, comparable name for a corner: resolved model, else agent version."""
    entry = agent_entry(log, corner)
    profile = entry.get("profile") or {}

    def usable(value):
        return value if (value and str(value).strip().lower() not in _NULL_MODELS) else None

    return (
        usable(profile.get("resolved_model"))
        or usable(profile.get("model"))
        or usable(profile.get("agent_version"))
        or usable(entry.get("name"))
        or corner
    )


def _safe_div(numerator: float, denominator: float):
    return (numerator / denominator) if denominator else None


def match_metrics(log: dict, corner: str) -> dict:
    """Derived metrics for one corner of one match."""
    opp = other_corner(corner)
    ticks = log.get("ticks") or []
    end = log.get("match_end") or {}
    cfg = config_of(log)
    profile = (agent_entry(log, corner).get("profile") or {})

    damage_dealt = damage_taken = riposte_damage = stamina_spent = 0
    counter_opportunities = counter_correct = 0
    attacks_faced = dodges_landed = blocks_landed = 0
    no_move_by_cause: Counter[str] = Counter()
    fallback_ticks = telemetry_ticks = 0
    latencies: list[int] = []
    tokens_in = tokens_out = 0
    actions: Counter[str] = Counter()

    for tick in ticks:
        moves = tick.get("moves") or {}
        events = tick.get("events") or []
        mine, theirs = moves.get(corner), moves.get(opp)
        exhausted = {e.get("who") for e in events if e.get("type") == "exhausted"}
        no_moved = {e.get("who") for e in events if e.get("type") == "no_move"}

        if mine and corner not in no_moved:
            actions[mine] += 1
            # An unaffordable action is converted to idle, so it costs nothing.
            if corner not in exhausted:
                stamina_spent += STAMINA_COST.get(mine, 0)

        if theirs in ATTACKS and opp not in exhausted and opp not in no_moved:
            attacks_faced += 1
            if mine in DEFENSES and corner not in no_moved:
                counter_opportunities += 1
                if mine in COUNTERS[theirs]:
                    counter_correct += 1

        for event in events:
            kind = event.get("type")
            if kind == "hit":
                if event.get("attacker") == corner:
                    damage_dealt += event.get("damage", 0)
                elif event.get("defender") == corner:
                    damage_taken += event.get("damage", 0)
            elif kind == "dodged" and event.get("defender") == corner:
                dodges_landed += 1
                riposte = event.get("riposte", RIPOSTE_DAMAGE) or 0
                damage_dealt += riposte
                riposte_damage += riposte
            elif kind == "blocked" and event.get("defender") == corner:
                blocks_landed += 1
            elif kind == "no_move" and event.get("who") == corner:
                no_move_by_cause[event.get("cause", "unknown")] += 1

        telemetry = ((tick.get("telemetry") or {}).get(corner)) or {}
        if telemetry:
            telemetry_ticks += 1
            if telemetry.get("fallback"):
                fallback_ticks += 1
            if telemetry.get("latency_ms") is not None:
                latencies.append(telemetry["latency_ms"])
            tokens_in += telemetry.get("tokens_in") or 0
            tokens_out += telemetry.get("tokens_out") or 0

    final_state = end.get("final_state") or {}
    mine_final = final_state.get(corner) or {}
    opp_final = final_state.get(opp) or {}
    reason = end.get("reason")
    winner = end.get("winner")
    invalid = sum(no_move_by_cause[c] for c in INVALID_CAUSES)
    n_ticks = len(ticks)

    return {
        "match_code": (log.get("match_start") or {}).get("match_code"),
        "corner": corner,
        "entrant": label_of(log, corner),
        "opponent": label_of(log, opp),
        "prompt_version": profile.get("prompt_version"),
        "agent_version": profile.get("agent_version"),
        "resolved_model": profile.get("resolved_model") or profile.get("model"),
        "temperature": profile.get("temperature"),
        "deadline_ms": cfg.get("deadline_ms"),
        "max_ticks": cfg.get("max_ticks"),
        "reason": reason,
        "winner": winner,
        "won": (winner == corner) if winner is not None else False,
        "abandoned": reason == "abandoned",
        "final_tick": end.get("final_tick"),
        "ticks_played": n_ticks,
        "hp_self": mine_final.get("hp"),
        "hp_opponent": opp_final.get("hp"),
        "hp_diff": (
            None
            if mine_final.get("hp") is None or opp_final.get("hp") is None
            else mine_final["hp"] - opp_final["hp"]
        ),
        "points_self": mine_final.get("points"),
        "points_opponent": opp_final.get("points"),
        "penalties_self": mine_final.get("penalties"),
        "decision_score": (
            None
            if mine_final.get("points") is None
            else mine_final.get("points", 0) - mine_final.get("penalties", 0)
        ),
        "ticks_to_ko": end.get("final_tick") if (reason == "ko" and winner == corner) else None,
        "damage_dealt": damage_dealt,
        "damage_taken": damage_taken,
        "riposte_damage": riposte_damage,
        "stamina_spent": stamina_spent,
        "stamina_efficiency": _safe_div(damage_dealt, stamina_spent),
        "counter_opportunities": counter_opportunities,
        "counter_accuracy": _safe_div(counter_correct, counter_opportunities),
        "attacks_faced": attacks_faced,
        "dodges_landed": dodges_landed,
        "blocks_landed": blocks_landed,
        "riposte_rate": _safe_div(dodges_landed, attacks_faced),
        "no_moves": sum(no_move_by_cause.values()),
        "no_move_rate": _safe_div(sum(no_move_by_cause.values()), n_ticks),
        "no_move_by_cause": dict(no_move_by_cause),
        "instruction_following_rate": _safe_div(n_ticks - invalid, n_ticks),
        "fallback_rate": _safe_div(fallback_ticks, telemetry_ticks),
        "mean_latency_ms": _safe_div(sum(latencies), len(latencies)),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "action_counts": dict(actions),
    }


def analyze_log(log: dict) -> list[dict]:
    """Metrics for both corners of one match."""
    return [match_metrics(log, corner) for corner in CORNERS]


def analyze_logs(paths) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        rows.extend(analyze_log(load_log(path)))
    return rows


def exploitation_speed(rows) -> dict[tuple[str, str], dict]:
    """Mean ticks-to-KO per (entrant, opponent), plus the KO rate.

    Against a ladder bot this is the discriminating number: *how fast* an agent
    exploits a fixed non-equilibrium opponent, not whether it eventually did.
    """
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("abandoned"):
            continue
        grouped[(row["entrant"], row["opponent"])].append(row)

    out: dict[tuple[str, str], dict] = {}
    for key, group in grouped.items():
        kos = [r["ticks_to_ko"] for r in group if r.get("ticks_to_ko")]
        out[key] = {
            "matches": len(group),
            "kos": len(kos),
            "ko_rate": len(kos) / len(group),
            "mean_ticks_to_ko": (sum(kos) / len(kos)) if kos else None,
            "mean_hp_diff": _safe_div(
                sum(r["hp_diff"] for r in group if r.get("hp_diff") is not None),
                sum(1 for r in group if r.get("hp_diff") is not None),
            ),
        }
    return out


def no_move_breakdown(rows) -> Counter:
    total: Counter[str] = Counter()
    for row in rows:
        total.update(row.get("no_move_by_cause") or {})
    return total
