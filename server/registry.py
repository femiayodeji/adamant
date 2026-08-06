"""In-memory match registry with log-based recovery (spec §13, §6.4c).

No database in the MVP. Live matches are a dict; the durable half is the match log,
which is sufficient to rebuild anything unfinished after a restart. Lobbies expire
after 30 minutes of inactivity — together with the match code that is the entire
abuse control for the MVP.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from pathlib import Path
from typing import Iterator, Optional

from .match import LOBBY, PAUSED, CornerSlot, Match, MatchConfig
from .matchlog import MatchLog, replay, unfinished_logs

logger = logging.getLogger("arena.registry")

LOBBY_TTL_SECONDS = 30 * 60
SWEEP_INTERVAL_SECONDS = 60

# Human-friendly and readable over a voice call or in a group chat.
WORDS = (
    "RUMBLE", "BOUT", "CLINCH", "ROUND", "CORNER", "GLOVE", "CANVAS", "TITLE",
    "SPAR", "RING", "BELL", "PUNCH", "GUARD", "SOUTHPAW", "COMBO", "FEINT",
)
# No 0/O or 1/I — codes get read aloud and retyped.
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


class Registry:
    def __init__(self, log_dir: Optional[Path] = None) -> None:
        self._matches: dict[str, Match] = {}
        self._by_code: dict[str, str] = {}
        self._log_dir = log_dir
        self._sweeper: Optional[asyncio.Task] = None

    @property
    def log_dir(self) -> Optional[Path]:
        """Where logs are read and written. None means `matchlog.LOG_DIR`."""
        return self._log_dir

    # ─── lookup ───

    def get(self, ref: str) -> Optional[Match]:
        """`{ref}` is either a match_id or a match_code (spec §8.1).

        Spectators arrive holding only a code from a share link; keying the read
        endpoints by ID alone would leave them unable to fetch a log, and therefore
        unable to use Replay at all.
        """
        if ref in self._matches:
            return self._matches[ref]
        match_id = self._by_code.get(ref.upper())
        return self._matches.get(match_id) if match_id else None

    def __iter__(self) -> Iterator[Match]:
        return iter(list(self._matches.values()))

    def __len__(self) -> int:
        return len(self._matches)

    # ─── creation ───

    def _new_code(self) -> str:
        for _ in range(100):
            word = secrets.choice(WORDS)
            suffix = "".join(secrets.choice(ALPHABET) for _ in range(3))
            code = f"{word}-{suffix}"
            if code not in self._by_code:
                return code
        raise RuntimeError("could not allocate an unused match code")

    def create(self, config: Optional[MatchConfig] = None) -> Match:
        match = Match(
            match_id=uuid.uuid4().hex,
            match_code=self._new_code(),
            host_token=f"ht_{secrets.token_urlsafe(16)}",
            config=config or MatchConfig(),
            log_dir=self._log_dir,
        )
        self._matches[match.match_id] = match
        self._by_code[match.match_code] = match.match_id
        return match

    def remove(self, match_id: str) -> None:
        match = self._matches.pop(match_id, None)
        if match is not None:
            self._by_code.pop(match.match_code, None)

    # ─── expiry ───

    def sweep(self) -> list[str]:
        """Drop idle lobbies. Matches that ever started are left alone — their log is
        the record and recovery may still want them."""
        expired = [
            m.match_id
            for m in list(self._matches.values())
            if m.status == LOBBY and m.idle_seconds > LOBBY_TTL_SECONDS
        ]
        for match_id in expired:
            self.remove(match_id)
        return expired

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                for match_id in self.sweep():
                    logger.info("expired idle lobby %s", match_id)
            except Exception:
                logger.exception("lobby sweep failed")

    def start_sweeper(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_forever(), name="lobby-sweeper")

    async def stop_sweeper(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except asyncio.CancelledError:
                pass
            self._sweeper = None

    # ─── crash recovery ───

    def recover(self) -> list[Match]:
        """Rebuild every unfinished match by replaying its log through `resolve()`
        and reopen it for resume.

        This works only because resolution is pure and deterministic: the log is an
        append-only event stream, so re-running it reconstructs exact state. Without
        that property this would need a database.
        """
        recovered: list[Match] = []
        for log in unfinished_logs(self._log_dir):
            try:
                match = self._rebuild(log)
            except Exception:
                logger.exception("could not recover match from %s", log.path)
                continue
            if match is not None:
                recovered.append(match)
                logger.info(
                    "recovered match %s at tick %d, awaiting resume",
                    match.match_code,
                    match.state.tick,
                )
        return recovered

    def _rebuild(self, log: MatchLog) -> Optional[Match]:
        tokens = log.read_recovery()
        if not tokens:
            logger.warning("no recovery sidecar for %s; cannot reopen for resume", log.match_id)
            return None

        config = MatchConfig(**tokens.get("config", {}))
        match = Match(
            match_id=tokens["match_id"],
            match_code=tokens["match_code"],
            host_token=tokens["host_token"],
            config=config,
            log_dir=self._log_dir,
        )
        match.log = log
        match.state = replay(log)
        match.started_at = log.match_start.get("started_at")
        match.history = [
            {"tick": t["tick"], "moves": t["moves"], "events": t["events"]} for t in log.ticks
        ]

        for corner, saved in (tokens.get("corners") or {}).items():
            if saved:
                match.corners[corner] = CornerSlot(
                    corner=corner,
                    name=saved["name"],
                    profile=saved["profile"],
                    token=saved["token"],
                    ws=None,  # everyone is disconnected after a restart
                    is_bot=saved.get("is_bot", False),
                )

        match.status = PAUSED
        self._matches[match.match_id] = match
        self._by_code[match.match_code] = match.match_id
        return match

    def start_recovered(self, matches: list[Match]) -> None:
        for match in matches:
            match.start_recovered()
