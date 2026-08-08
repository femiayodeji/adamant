"""Match state machine and tick loop (spec §5, §6, §7).

    lobby → in_progress → paused → finished | abandoned

The pause/resume path is the highest-risk part of the server, and it exists for one
reason: a match invalidated by a dropped socket is wasted API spend and a corrupted
data point. The rule it implements is plan Principle 6 — *a missed move is
penalised; a broken connection is not*:

  socket open, nothing by the deadline → the agent's failure → no_move + penalty
  socket closed                        → infrastructure failure → pause and wait

Nothing in here computes combat. All of that is `engine.resolve()`, called from one
place below.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import protocol
from .engine import rules
from .engine.resolver import (
    CORNERS,
    Corner,
    MatchResult,
    MatchState,
    Move,
    NoMove,
    check_end,
    initial_state,
    resolve,
)
from .matchlog import MatchLog

logger = logging.getLogger("arena.match")

LOBBY = "lobby"
IN_PROGRESS = "in_progress"
PAUSED = "paused"
FINISHED = "finished"
ABANDONED = "abandoned"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class LifecycleError(Exception):
    """An illegal transition — starting a match twice, or with an empty corner."""


@dataclass
class MatchConfig:
    max_ticks: int = rules.DEFAULT_MAX_TICKS
    deadline_ms: int = rules.DEFAULT_DEADLINE_MS
    resume_window_ms: int = rules.DEFAULT_RESUME_WINDOW_MS

    def as_dict(self) -> dict[str, int]:
        return {
            "max_ticks": self.max_ticks,
            "deadline_ms": self.deadline_ms,
            "resume_window_ms": self.resume_window_ms,
        }

    @classmethod
    def build(cls, max_ticks: Any = None, deadline_ms: Any = None) -> "MatchConfig":
        config = cls()
        if max_ticks is not None:
            low, high = rules.MAX_TICKS_RANGE
            if not isinstance(max_ticks, int) or not low <= max_ticks <= high:
                raise ValueError(f"max_ticks must be an integer in [{low}, {high}]")
            config.max_ticks = max_ticks
        if deadline_ms is not None:
            low, high = rules.DEADLINE_MS_RANGE
            if not isinstance(deadline_ms, int) or not low <= deadline_ms <= high:
                raise ValueError(f"deadline_ms must be an integer in [{low}, {high}]")
            config.deadline_ms = deadline_ms
        return config


@dataclass
class CornerSlot:
    corner: Corner
    name: str
    profile: dict[str, Any]
    token: str
    ws: Any = None  # None means disconnected — the match pauses, it does not resolve
    is_bot: bool = False

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "profile": self.profile}


class Match:
    def __init__(
        self,
        match_id: str,
        match_code: str,
        host_token: str,
        config: Optional[MatchConfig] = None,
        log_dir: Optional[Path] = None,
    ) -> None:
        self.match_id = match_id
        self.match_code = match_code
        self.host_token = host_token
        self.config = config or MatchConfig()

        self.status = LOBBY
        self.state: MatchState = initial_state()
        self.result: Optional[MatchResult] = None
        self.corners: dict[Corner, Optional[CornerSlot]] = {"agent_a": None, "agent_b": None}
        self.spectators: set[Any] = set()
        self.history: list[dict[str, Any]] = []
        self.created_at = time.monotonic()
        self.started_at: Optional[str] = None

        # What the host asked `fill_bot` for, per corner. Kept because a rematch has
        # to reproduce the same ladder opponent, and the slot itself cannot say: a bot
        # joins over the public protocol like anything else, so all the corner knows is
        # the name and profile it declared. Requests, not connections — a bot that
        # never came up leaves nothing here.
        self.bot_fills: dict[Corner, dict[str, Any]] = {}

        self.log = MatchLog(match_id, log_dir=log_dir)
        self._task: Optional[asyncio.Task] = None

        # Per-tick collection state.
        self._current_tick = 0
        self._pending: dict[Corner, str] = {}
        self._telemetry: dict[Corner, dict[str, Any]] = {}
        self._rejections: dict[Corner, str] = {}
        self._wake = asyncio.Event()
        self._resume_wake = asyncio.Event()

        # Monotonic deadline of the resume window while paused, so a spectator who
        # arrives or reconnects mid-pause can be told how much of it is left.
        self._pause_deadline: Optional[float] = None

    # ─── lobby ───

    def touch(self) -> None:
        self.created_at = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.created_at

    def join(
        self,
        ws: Any,
        name: Any,
        profile: Any,
        preferred: Optional[str] = None,
        is_bot: bool = False,
    ) -> CornerSlot:
        """First two valid connections with a valid code take the corners.

        `preferred` is an optional request for a specific corner, honoured when that
        corner is free and otherwise ignored. It is available to every agent on the
        public protocol — the lobby's "add a bot to *this* corner" button needs it,
        and giving bots a private way to do the same would be exactly the privileged
        path spec §11 rules out.
        """
        if self.status != LOBBY:
            raise protocol.JoinError("match_full")

        clean_name = self._distinct_name(protocol.validate_name(name))
        clean_profile = protocol.validate_profile(profile)

        order = list(CORNERS)
        if preferred in CORNERS and self.corners[preferred] is None:
            order = [preferred] + [c for c in CORNERS if c != preferred]

        for corner in order:
            if self.corners[corner] is None:
                slot = CornerSlot(
                    corner=corner,
                    name=clean_name,
                    profile=clean_profile,
                    token=f"ct_{secrets.token_hex(6)}",
                    ws=ws,
                    is_bot=is_bot,
                )
                self.corners[corner] = slot
                self.touch()
                self._persist_recovery()
                return slot

        raise protocol.JoinError("match_full")

    def _distinct_name(self, name: str) -> str:
        """Suffix a name the other corner is already using, so the two fighters can
        never share one (spec §6.1).

        Filling both corners from the same ladder rung — two `jab` bots, or the
        reference agent run twice — otherwise puts two `Jab-bot`s in the ring, and
        every line of the event log, every HUD panel and every row of the analysed
        log becomes ambiguous about which one it means. Renaming is the only fix
        that keeps working after the fact: a log is read long after the match, when
        "the one in the blue corner" is no longer something the reader can see.

        The later joiner is the one renamed, so a corner that is already filled
        never changes name under a spectator who is watching the lobby. Compared
        case-insensitively — `jab-bot` beside `Jab-bot` is exactly the confusion
        this exists to prevent — and the result is truncated to stay inside the
        name rules of §6.1, so it survives the same validation it just passed.
        """
        taken = {slot.name.casefold() for slot in self.corners.values() if slot is not None}
        if name.casefold() not in taken:
            return name
        # Two corners, so one suffix is almost always enough; the second covers a
        # joiner literally called "Jab-bot 2" arriving beside "Jab-bot 2".
        for n in (2, 3, 4):
            suffix = f" {n}"
            candidate = f"{name[: protocol.MAX_NAME - len(suffix)].rstrip()}{suffix}"
            if candidate.casefold() not in taken:
                return candidate
        return name

    def resume(self, ws: Any, token: Any) -> CornerSlot:
        """Reclaim a corner after a disconnect. The match code alone is deliberately
        insufficient — anyone holding the share link could otherwise seize a corner
        mid-match."""
        for corner in CORNERS:
            slot = self.corners[corner]
            if slot is not None and token and secrets.compare_digest(slot.token, str(token)):
                slot.ws = ws
                self._resume_wake.set()
                self._wake.set()
                return slot
        raise protocol.JoinError("invalid_token")

    def agent_disconnected(self, corner: Corner, ws: Any) -> None:
        slot = self.corners[corner]
        if slot is None or slot.ws is not ws:
            # A stale close from a socket that has already been replaced by a resume.
            return
        slot.ws = None
        self._wake.set()
        self._resume_wake.set()

    @property
    def filled(self) -> bool:
        return all(self.corners[c] is not None for c in CORNERS)

    def _missing(self) -> list[Corner]:
        return [c for c in CORNERS if self.corners[c] is None or self.corners[c].ws is None]

    def missing_corners(self) -> list[Corner]:
        """The corners with no live socket. Public for the spectator catch-up."""
        return self._missing()

    def lobby_payload(self) -> dict[str, Any]:
        return {
            "type": "lobby_update",
            "corners": {
                c: (self.corners[c].public() if self.corners[c] else None) for c in CORNERS
            },
        }

    def status_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "match_id": self.match_id,
            "match_code": self.match_code,
            "status": self.status,
            "config": self.config.as_dict(),
            "corners": {
                c: (self.corners[c].public() if self.corners[c] else None) for c in CORNERS
            },
            "tick": self.state.tick,
        }
        if self.result is not None:
            payload["result"] = {
                **self.result.as_dict(),
                "final_tick": self.state.tick,
                "final_state": self.state.as_dict(),
            }
        return payload

    # ─── moves ───

    def submit_move(self, corner: Corner, payload: Any) -> None:
        """Accept or reject one inbound `move` (spec §4.4).

        A rejected submission does *not* immediately lock in a no-move: the agent may
        retry until the deadline, and only the last rejection reason is recorded if
        nothing valid ever arrives. A duplicate is dropped outright — the first valid
        move is kept — so `duplicate` never becomes a no-move cause. A move for a tick
        that has already resolved is dropped the same way (`protocol.STALE`).
        """
        if self.status != IN_PROGRESS:
            return
        if corner in self._pending:
            return  # duplicate: the first is kept

        action, cause = protocol.classify_move(payload, self._current_tick)
        if cause == protocol.STALE:
            return  # a late answer to a tick that is already in the log
        if cause is not None:
            self._rejections[corner] = cause
            return

        self._pending[corner] = action
        self._telemetry[corner] = protocol.sanitise_telemetry(
            payload.get("telemetry") if isinstance(payload, dict) else None
        )
        self._wake.set()

    # ─── transport ───

    async def _send(self, ws: Any, message: dict[str, Any]) -> None:
        if ws is None:
            return
        try:
            await ws.send_json(message)
        except Exception:
            # A send failure is a disconnect we have not been told about yet. The
            # receive loop will confirm it; nothing here should raise into the tick
            # loop, or one dead spectator would end the fight.
            logger.debug("send failed", exc_info=True)

    async def _to_agents(self, message: dict[str, Any]) -> None:
        await asyncio.gather(
            *(self._send(self.corners[c].ws, message) for c in CORNERS if self.corners[c])
        )

    async def _to_spectators(self, message: dict[str, Any]) -> None:
        await asyncio.gather(*(self._send(ws, message) for ws in list(self.spectators)))

    async def _to_all(self, message: dict[str, Any]) -> None:
        await self._to_agents(message)
        await self._to_spectators(message)

    async def broadcast_lobby(self) -> None:
        await self._to_spectators(self.lobby_payload())

    # ─── lifecycle ───

    def start(self) -> None:
        if self.status != LOBBY:
            raise LifecycleError(f"cannot start a match in state {self.status!r}")
        if not self.filled:
            raise LifecycleError("both corners must be filled before starting")
        # Set synchronously, before the loop is scheduled: a client that polls
        # `GET /api/matches/{ref}` immediately after `start` must not see `lobby`
        # and conclude the start failed.
        self.status = IN_PROGRESS
        self._task = asyncio.create_task(self._run(), name=f"match-{self.match_id}")

    def start_recovered(self) -> None:
        """Resume a match rebuilt from its log after a restart.

        Deliberately not `start()`: `match_start` was already emitted and logged
        before the crash, and re-emitting it would corrupt the log's provenance
        block. The loop picks up at `state.tick + 1`, finds both corners
        disconnected, and pauses — which is exactly the state a resume expects.
        """
        if self._task is not None:
            raise LifecycleError("match already running")
        self.status = IN_PROGRESS
        self._task = asyncio.create_task(
            self._run_recovered(), name=f"match-{self.match_id}-recovered"
        )

    async def _run_recovered(self) -> None:
        try:
            result = await self._tick_loop()
            await self._finish(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("recovered match %s crashed", self.match_id)
            raise

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    def agent_match_start(self, corner: Corner) -> dict[str, Any]:
        other: Corner = "agent_b" if corner == "agent_a" else "agent_a"
        return {
            "type": "match_start",
            "you": corner,
            "opponent_name": self.corners[other].name if self.corners[other] else None,
            "config": self.config.as_dict(),
        }

    def _spectator_match_start(self) -> dict[str, Any]:
        return {
            "type": "match_start",
            "schema_version": protocol.SCHEMA_VERSION,
            "match_code": self.match_code,
            "agents": {c: self.corners[c].public() for c in CORNERS},
            "config": self.config.as_dict(),
            "started_at": self.started_at,
        }

    async def _run(self) -> None:
        try:
            self.started_at = utcnow()
            self.log.start(self._spectator_match_start())
            self._persist_recovery()

            for corner in CORNERS:
                await self._send(self.corners[corner].ws, self.agent_match_start(corner))
            await self._to_spectators(self._spectator_match_start())

            result = await self._tick_loop()
            await self._finish(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("match %s crashed", self.match_id)
            raise

    async def _tick_loop(self) -> MatchResult:
        tick = self.state.tick + 1
        while tick <= self.config.max_ticks:
            resolved = await self._play_tick(tick)

            if not resolved:
                if not await self._pause_and_await_resume(tick):
                    return self._abandon_result()
                continue  # same tick, fresh deadline

            result = check_end(self.state, self.config.max_ticks)
            if result is not None:
                return result
            tick += 1

        # check_end fires on the final tick, so this is defensive only.
        return check_end(self.state, self.config.max_ticks) or MatchResult(None, "draw")

    async def _play_tick(self, tick: int) -> bool:
        """Run one tick. Returns False if the match must pause instead of resolving.

        Any move already submitted for this tick is discarded on entry, so a
        re-issued tick after a resume genuinely starts over for both corners.
        """
        self._current_tick = tick
        self._pending.clear()
        self._telemetry.clear()
        self._rejections.clear()
        self._wake.clear()

        if self._missing():
            return False

        for corner in CORNERS:
            await self._send(
                self.corners[corner].ws,
                protocol.state_update(
                    corner, tick, self.config.deadline_ms, self.state, self.history
                ),
            )

        deadline = time.monotonic() + self.config.deadline_ms / 1000
        while True:
            if self._missing():
                return False
            if len(self._pending) == len(CORNERS):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            self._wake.clear()

        # A socket that closed on the deadline boundary is still infrastructure.
        if self._missing():
            return False

        moves: dict[Corner, Move] = {}
        for corner in CORNERS:
            if corner in self._pending:
                moves[corner] = self._pending[corner]
            else:
                moves[corner] = NoMove(self._rejections.get(corner, "timeout"))

        self.state, events = resolve(self.state, moves["agent_a"], moves["agent_b"])

        submitted = {
            c: (moves[c] if isinstance(moves[c], str) else None) for c in CORNERS
        }
        telemetry = {c: self._telemetry.get(c, {}) for c in CORNERS}
        record = protocol.tick_result(tick, submitted, events, self.state, telemetry)

        self.history.append({"tick": tick, "moves": submitted, "events": events})
        self.log.append_tick(record)
        await self._to_spectators(
            {**record, "telemetry": protocol.strip_for_spectators(telemetry)}
        )
        return True

    def paused_payload(self, tick: int, missing: Corner, resume_window_ms: int) -> dict[str, Any]:
        return {
            "type": "match_paused",
            "tick": tick,
            "missing": missing,
            "resume_window_ms": resume_window_ms,
        }

    def resume_window_left_ms(self) -> int:
        """How much of the resume window is still to run; the whole of it if not paused.

        `match_paused` is broadcast once, at the moment the corner drops, and never
        again — so a spectator who opens the watch link mid-pause, or whose own socket
        blipped during one, needs the frame replayed in the catch-up to get the overlay
        at all. Replaying it with the *configured* window would show a countdown longer
        than the one actually running.
        """
        if self._pause_deadline is None:
            return self.config.resume_window_ms
        return max(0, math.ceil((self._pause_deadline - time.monotonic()) * 1000))

    async def _pause_and_await_resume(self, tick: int) -> bool:
        """Stop the deadline clock for *both* corners and wait for the missing one.

        Stopping it for both is the point: freezing only the absent corner would hand
        the present one free thinking time, which is the asymmetry this is meant to
        avoid rather than create.
        """
        self.status = PAUSED
        announced: set[Corner] = set()

        deadline = time.monotonic() + self.config.resume_window_ms / 1000
        self._pause_deadline = deadline
        while True:
            missing = self._missing()
            for corner in missing:
                if corner not in announced:
                    announced.add(corner)
                    await self._to_all(
                        self.paused_payload(tick, corner, self.config.resume_window_ms)
                    )

            if not missing:
                self.status = IN_PROGRESS
                self._pause_deadline = None
                await self._to_all({"type": "match_resumed", "tick": tick})
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._pause_deadline = None
                return False

            self._resume_wake.clear()
            try:
                await asyncio.wait_for(self._resume_wake.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False

    def _abandon_result(self) -> MatchResult:
        """The remaining fighter is named as winner; if both are gone, winner is
        null. `abandoned` exists so infrastructure failure stays distinguishable from
        combat in the log, and can be excluded from evaluation aggregates."""
        missing = set(self._missing())
        remaining = [c for c in CORNERS if c not in missing]
        winner: Optional[Corner] = remaining[0] if len(remaining) == 1 else None
        return MatchResult(winner=winner, reason="abandoned")

    async def _finish(self, result: MatchResult) -> None:
        self.result = result
        self.status = ABANDONED if result.reason == "abandoned" else FINISHED
        payload = protocol.match_end(result, self.state.tick, self.state)
        self.log.finish(payload)
        await self._to_all(payload)

    # ─── recovery ───

    def _persist_recovery(self) -> None:
        """Tokens must survive a restart or nobody can reclaim their corner — which
        would turn every server restart into an abandoned match."""
        if self.status == LOBBY and not self.filled:
            return
        self.log.write_recovery(
            {
                "match_id": self.match_id,
                "match_code": self.match_code,
                "host_token": self.host_token,
                "config": self.config.as_dict(),
                "corners": {
                    c: (
                        {
                            "name": self.corners[c].name,
                            "profile": self.corners[c].profile,
                            "token": self.corners[c].token,
                            "is_bot": self.corners[c].is_bot,
                        }
                        if self.corners[c]
                        else None
                    )
                    for c in CORNERS
                },
            }
        )
