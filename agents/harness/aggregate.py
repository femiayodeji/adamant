"""Aggregation that obeys the evaluation conventions in docs/plan.md.

Three rules are enforced here rather than left to discipline:

1. **Brackets.** Never compare across different ``deadline_ms``. A 5 s standard
   bracket and a 30 s deep bracket are different experiments; mixing them
   reports "the shallow model fights better" when the finding is that the deep
   one ran out of clock.
2. **Contamination.** ``abandoned`` matches and matches over the fallback-rate
   threshold are excluded, and the exclusions are reported, not silently
   dropped.
3. **Margins over win counts.** HP differential and ticks-to-KO carry far more
   information per match than a win/loss bit; win rate is reported last.
"""

from __future__ import annotations

from collections import Counter, defaultdict

#: A result produced mostly by hedges is not a measurement of the model
#: (Phase 5 gate: fallback rate < 10% of ticks).
DEFAULT_FALLBACK_LIMIT = 0.10

#: The identity of a comparison cell: profile + config.
DEFAULT_GROUP_FIELDS = (
    "deadline_ms",
    "max_ticks",
    "entrant",
    "opponent",
    "prompt_version",
    "agent_version",
    "resolved_model",
    "temperature",
)


class BracketError(ValueError):
    """Rows from more than one deadline bracket were compared."""


def brackets(rows) -> set:
    return {row.get("deadline_ms") for row in rows}


def require_single_bracket(rows) -> None:
    found = brackets(rows)
    if len(found) > 1:
        raise BracketError(
            f"refusing to compare across deadline brackets: {sorted(map(str, found))}"
        )


def exclusion_reason(row, fallback_limit: float = DEFAULT_FALLBACK_LIMIT) -> str | None:
    if row.get("abandoned") or row.get("reason") == "abandoned":
        return "abandoned"
    if row.get("error"):
        return "error"
    rate = row.get("fallback_rate")
    if rate is not None and rate > fallback_limit:
        return "fallback_rate"
    return None


def partition(rows, fallback_limit: float = DEFAULT_FALLBACK_LIMIT):
    """Split rows into (kept, {reason: [rows]})."""
    kept: list[dict] = []
    dropped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        reason = exclusion_reason(row, fallback_limit)
        if reason:
            dropped[reason].append(row)
        else:
            kept.append(row)
    return kept, dict(dropped)


def _mean(values):
    values = [v for v in values if v is not None]
    return (sum(values) / len(values)) if values else None


def aggregate(
    rows,
    fallback_limit: float = DEFAULT_FALLBACK_LIMIT,
    group_fields=DEFAULT_GROUP_FIELDS,
) -> list[dict]:
    """Summarise per (config, profile, opponent) cell. Margin metrics first."""
    kept, dropped = partition(rows, fallback_limit)
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in kept:
        grouped[tuple(row.get(f) for f in group_fields)].append(row)

    summaries: list[dict] = []
    for key, group in sorted(grouped.items(), key=lambda kv: [str(x) for x in kv[0]]):
        corners = Counter(r.get("corner") for r in group)
        kos = [r["ticks_to_ko"] for r in group if r.get("ticks_to_ko")]
        summary = dict(zip(group_fields, key))
        summary.update(
            {
                "matches": len(group),
                # Margin-based: these are the numbers to read.
                "mean_hp_diff": _mean(r.get("hp_diff") for r in group),
                "mean_ticks_to_ko": _mean(kos),
                "ko_rate": len(kos) / len(group),
                "mean_decision_score": _mean(r.get("decision_score") for r in group),
                # Behavioural.
                "mean_counter_accuracy": _mean(r.get("counter_accuracy") for r in group),
                "mean_riposte_rate": _mean(r.get("riposte_rate") for r in group),
                "mean_stamina_efficiency": _mean(r.get("stamina_efficiency") for r in group),
                "mean_instruction_following": _mean(
                    r.get("instruction_following_rate") for r in group
                ),
                "mean_no_move_rate": _mean(r.get("no_move_rate") for r in group),
                "mean_fallback_rate": _mean(r.get("fallback_rate") for r in group),
                # Corner balance: paired matches should be 50/50.
                "matches_agent_a": corners.get("agent_a", 0),
                "matches_agent_b": corners.get("agent_b", 0),
                "corner_balanced": corners.get("agent_a", 0) == corners.get("agent_b", 0),
                # Secondary, and lossy: reported last on purpose.
                "win_rate": _mean(1.0 if r.get("won") else 0.0 for r in group),
                "excluded_total": sum(len(v) for v in dropped.values()),
            }
        )
        summaries.append(summary)
    return summaries


def summarise_exclusions(rows, fallback_limit: float = DEFAULT_FALLBACK_LIMIT) -> dict:
    _, dropped = partition(rows, fallback_limit)
    return {reason: len(group) for reason, group in dropped.items()}
