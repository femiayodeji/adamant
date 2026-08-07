"""Batch runner: a loop over matches with concurrency, plus a results CSV.

Because ``resolve()`` is pure and the bots are ordinary WebSocket clients, this
is a script, not a subsystem. The two things it must get right are conventions,
not machinery:

* **corner swapping** -- every comparison is run as a paired set of matches with
  the corners swapped. Resolution is symmetric, so there should be no positional
  effect; the cost is near zero and it forecloses the question.
* **brackets** -- a batch is planned at one ``deadline_ms``. Results carry the
  config so aggregation can refuse to compare across brackets.

``ArenaRunner`` speaks to a live platform and is exercised against a real one in
``agents/tests/test_against_live_platform.py`` — a real server, real sockets, a real
ladder bot, and a stubbed model. ``plan_matches``, ``run_batch``, ``row_from_log``
and the CSV writer are covered separately with a stubbed runner.

What has *not* been exercised is a real model behind ``make_client``: every test
here stubs it, so no API budget is spent by the suite.
"""

from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ..common.decision import make_decider
from ..common.wsloop import run_match
from .analyze import match_metrics, other_corner

DEFAULT_DEADLINE_MS = 5000
DEFAULT_MAX_TICKS = 60
CORNERS = ("agent_a", "agent_b")


@dataclass(frozen=True)
class Opponent:
    name: str  # ladder bot name ("jab", "random", ...) or a model label
    kind: str = "bot"  # "bot" | "model"


@dataclass(frozen=True)
class MatchSpec:
    """One match to run. ``pair_id`` ties the two corner-swapped halves."""

    pair_id: str
    corner: str  # the corner OUR entrant takes
    entrant: str
    opponent: str
    opponent_kind: str
    deadline_ms: int
    max_ticks: int
    seed: int | None
    repeat: int


def plan_matches(
    entrants,
    opponents,
    *,
    repeats: int = 1,
    deadline_ms: int = DEFAULT_DEADLINE_MS,
    max_ticks: int = DEFAULT_MAX_TICKS,
    seed_base: int = 0,
) -> list[MatchSpec]:
    """Every (entrant, opponent, repeat) becomes two corner-swapped matches."""
    specs: list[MatchSpec] = []
    for entrant in entrants:
        entrant_name = getattr(entrant, "name", entrant)
        for opponent in opponents:
            opp = opponent if isinstance(opponent, Opponent) else Opponent(str(opponent))
            for repeat in range(repeats):
                pair_id = f"{entrant_name}|{opp.name}|{deadline_ms}|{repeat}"
                seed = seed_base + repeat
                for corner in CORNERS:
                    specs.append(
                        MatchSpec(
                            pair_id=pair_id,
                            corner=corner,
                            entrant=entrant_name,
                            opponent=opp.name,
                            opponent_kind=opp.kind,
                            deadline_ms=deadline_ms,
                            max_ticks=max_ticks,
                            seed=seed,
                            repeat=repeat,
                        )
                    )
    return specs


async def run_batch(specs, run_one, concurrency: int = 4) -> list[dict]:
    """Run every spec with bounded concurrency; never let one failure kill the batch."""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def guarded(spec: MatchSpec) -> dict:
        async with semaphore:
            try:
                row = await run_one(spec)
            except Exception as exc:  # a crashed match is a data point, not a stop
                row = {"error": f"{type(exc).__name__}: {exc}"}
            merged = asdict(spec)
            merged.update(row or {})
            return merged

    return list(await asyncio.gather(*(guarded(spec) for spec in specs)))


def row_from_log(log: dict, spec: MatchSpec) -> dict:
    """Turn a stored match log into the results row for our entrant's corner."""
    row = match_metrics(log, spec.corner)
    row["entrant"] = spec.entrant
    row["opponent"] = spec.opponent
    return row


# --- CSV -------------------------------------------------------------------

RESULT_FIELDS = (
    "pair_id",
    "repeat",
    "seed",
    "corner",
    "entrant",
    "opponent",
    "opponent_kind",
    "deadline_ms",
    "max_ticks",
    "persona",
    "prompt_version",
    "agent_version",
    "resolved_model",
    "temperature",
    "match_code",
    "reason",
    "winner",
    "won",
    "abandoned",
    "final_tick",
    "ticks_played",
    "hp_self",
    "hp_opponent",
    "hp_diff",
    "points_self",
    "points_opponent",
    "penalties_self",
    "decision_score",
    "ticks_to_ko",
    "damage_dealt",
    "damage_taken",
    "riposte_damage",
    "stamina_spent",
    "stamina_efficiency",
    "counter_opportunities",
    "counter_accuracy",
    "attacks_faced",
    "dodges_landed",
    "blocks_landed",
    "riposte_rate",
    "no_moves",
    "no_move_rate",
    "no_move_by_cause",
    "voids",
    "void_rate",
    "instruction_following_rate",
    "fallback_rate",
    "mean_latency_ms",
    "tokens_in",
    "tokens_out",
    "action_counts",
    "error",
)


def write_csv(rows, path, fields=RESULT_FIELDS) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (json.dumps(value) if isinstance(value, (dict, list)) else value)
                    for key, value in row.items()
                }
            )
    return path


# --- Live platform ---------------------------------------------------------


@dataclass
class ArenaEndpoints:
    http_url: str  # e.g. https://arena.example.com
    ws_url: str  # e.g. wss://arena.example.com


class ArenaRunner:
    """Drive one match end to end against a live platform.

    Corner swapping works because ``fill_bot`` places the opponent in the opposite
    corner and ``join`` carries a corner preference (spec §6.1); without that the
    corners would fall out of join order and could not be controlled.
    """

    def __init__(self, endpoints: ArenaEndpoints, make_client, http=None, name="HarnessBot"):
        self.endpoints = endpoints
        self.make_client = make_client  # (spec) -> LiteLLMClient
        self.http = http
        self.name = name

    async def _request(self, method, path, *, json_body=None, host_token=None):
        if self.http is None:
            import httpx

            self.http = httpx.AsyncClient(base_url=self.endpoints.http_url, timeout=30.0)
        headers = {"X-Host-Token": host_token} if host_token else {}
        response = await self.http.request(method, path, json=json_body, headers=headers)
        response.raise_for_status()
        return response.json() if response.content else {}

    async def run_one(self, spec: MatchSpec) -> dict:
        match = await self._request(
            "POST",
            "/api/matches",
            json_body={"max_ticks": spec.max_ticks, "deadline_ms": spec.deadline_ms},
        )
        ref, host_token = match["match_code"], match["host_token"]

        if spec.opponent_kind == "bot":
            # Fill the bot into the *other* corner first; our agent then takes
            # the corner this spec asks for. That is how corner swapping works.
            await self._request(
                "POST",
                f"/api/matches/{ref}/fill_bot",
                json_body={
                    "corner": other_corner(spec.corner),
                    "bot": spec.opponent,
                    **({"seed": spec.seed} if spec.seed is not None else {}),
                },
                host_token=host_token,
            )

        client = self.make_client(spec)
        agent = asyncio.create_task(
            run_match(
                self.endpoints.ws_url,
                ref,
                self.name,
                client.profile(),
                # A client that declares a persona pins its system prompt for the
                # whole match; anything else gets the neutral one.
                make_decider(client, system=getattr(client, "system_prompt", None)),
            )
        )
        await self._wait_for_corners(ref)
        await self._request("POST", f"/api/matches/{ref}/start", host_token=host_token)
        await agent

        log = await self._request("GET", f"/api/matches/{ref}/log")
        row = row_from_log(log, spec)
        row["match_id"] = match.get("match_id")
        return row

    async def _wait_for_corners(self, ref, timeout_s: float = 30.0, poll_s: float = 0.25):
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            status = await self._request("GET", f"/api/matches/{ref}")
            corners = status.get("corners") or {}
            if all(corners.get(corner) for corner in CORNERS):
                return
            await asyncio.sleep(poll_s)
        raise TimeoutError(f"corners never filled for {ref}")
