"""Match logs — replay substrate, crash-recovery substrate, and evaluation dataset.

There is no database. Because `resolve()` is pure and deterministic and the log is
an event stream, replaying a log reconstructs match state exactly (spec §6.4c). That
single property is what lets the MVP skip persistence entirely.

Two files per match:

  `{match_id}.json`           the log proper — served over REST, safe to publish
  `{match_id}.recovery.json`  host and corner tokens, never served

Tokens live in a sidecar rather than in the log because `GET /api/matches/{ref}/log`
hands the log to any spectator holding a share link. Putting `host_token` in there
would let a spectator start matches and fill corners.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from .engine.resolver import MatchState, Move, NoMove, initial_state, resolve
from .protocol import SCHEMA_VERSION

# `ARENA_LOG_DIR` exists so a deployment can point this at a mounted volume. The
# logs are the evaluation dataset and the crash-recovery substrate, so in any real
# deployment they must outlive the container.
LOG_DIR = Path(os.environ.get("ARENA_LOG_DIR") or (Path(__file__).resolve().parent / "logs"))


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write-temp, fsync, rename. A crash mid-write must never leave a truncated log
    behind, because the log is the only copy of the match."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    # Rename durability needs the *directory* fsynced too, or a crash can lose the
    # entry even though the file's contents were flushed.
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class MatchLog:
    """`{schema_version, match_start, ticks, match_end}`, fsynced per tick."""

    def __init__(self, match_id: str, log_dir: Optional[Path] = None) -> None:
        self.match_id = match_id
        self.dir = Path(log_dir) if log_dir else LOG_DIR
        self.path = self.dir / f"{match_id}.json"
        self.recovery_path = self.dir / f"{match_id}.recovery.json"
        self.match_start: dict[str, Any] = {}
        self.ticks: list[dict[str, Any]] = []
        self.match_end: Optional[dict[str, Any]] = None

    # -- writing ---------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "match_start": self.match_start,
            "ticks": self.ticks,
            "match_end": self.match_end,
        }

    def flush(self) -> None:
        _atomic_write(self.path, self.as_dict())

    def start(self, payload: dict[str, Any]) -> None:
        self.match_start = payload
        self.flush()

    def append_tick(self, payload: dict[str, Any]) -> None:
        self.ticks.append(payload)
        self.flush()

    def finish(self, payload: dict[str, Any]) -> None:
        self.match_end = payload
        self.flush()

    def write_recovery(self, tokens: dict[str, Any]) -> None:
        _atomic_write(self.recovery_path, tokens)

    def read_recovery(self) -> dict[str, Any]:
        if not self.recovery_path.exists():
            return {}
        return json.loads(self.recovery_path.read_text())

    # -- reading ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "MatchLog":
        data = json.loads(Path(path).read_text())
        log = cls(match_id=Path(path).stem, log_dir=Path(path).parent)
        log.match_start = data.get("match_start", {})
        log.ticks = data.get("ticks", [])
        log.match_end = data.get("match_end")
        return log

    @property
    def finished(self) -> bool:
        return self.match_end is not None


def moves_from_tick(record: dict[str, Any]) -> tuple[Move, Move]:
    """Reconstruct the two `Move`s that produced a logged tick.

    `moves` holds the action the agent *submitted*, or null for a no-move; the
    `no_move` event carries the cause. An `exhausted` conversion is deliberately not
    baked in — the log stores what was submitted and `resolve()` re-derives the
    conversion, which keeps replay a genuine re-run of the rules rather than a
    replay of their output.
    """
    causes = {
        event["who"]: event.get("cause", "timeout")
        for event in record.get("events", [])
        if event.get("type") == "no_move"
    }

    def one(corner: str) -> Move:
        action = record.get("moves", {}).get(corner)
        if action is None:
            return NoMove(causes.get(corner, "timeout"))
        return action

    return one("agent_a"), one("agent_b")


def replay(log: MatchLog) -> MatchState:
    """Rebuild exact match state by re-running every logged tick through the engine.

    Used for crash recovery on boot and by the offline evaluation tooling. If this
    ever disagrees with the state recorded in the log, either the engine changed
    under a stored log or the log is corrupt — both are worth failing loudly for.
    """
    state = initial_state()
    for record in log.ticks:
        move_a, move_b = moves_from_tick(record)
        state, _ = resolve(state, move_a, move_b)

        recorded = record.get("state")
        if recorded and recorded != state.as_dict():
            raise ValueError(
                f"replay diverged from log at tick {record.get('tick')}: "
                f"recorded {recorded}, recomputed {state.as_dict()}"
            )
    return state


def resolve_log_path(ref: str, log_dir: Optional[Path] = None) -> Optional[Path]:
    """Find a stored log by **either** match_id or match_code (spec §8.1).

    Logs are named by match_id, but a spectator only ever holds a code from a share
    link. Once a finished match has left the in-memory registry — which every
    restart causes — resolving by ID alone would 404 their Replay link, so the code
    has to be resolvable from disk too.

    Sidecars are checked first because they are tiny; full logs are the fallback for
    the case where sidecars have been cleaned up.
    """
    directory = Path(log_dir) if log_dir else LOG_DIR
    if not directory.exists():
        return None

    direct = directory / f"{ref}.json"
    if direct.exists() and not ref.endswith(".recovery"):
        return direct

    wanted = ref.upper()

    for sidecar in sorted(directory.glob("*.recovery.json")):
        try:
            data = json.loads(sidecar.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if str(data.get("match_code", "")).upper() == wanted:
            candidate = directory / f"{data.get('match_id')}.json"
            if candidate.exists():
                return candidate

    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".recovery.json"):
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if str(data.get("match_start", {}).get("match_code", "")).upper() == wanted:
            return path

    return None


def unfinished_logs(log_dir: Optional[Path] = None) -> list[MatchLog]:
    """Every match that was in progress when the process died."""
    directory = Path(log_dir) if log_dir else LOG_DIR
    if not directory.exists():
        return []
    found = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".recovery.json"):
            continue
        try:
            log = MatchLog.load(path)
        except (json.JSONDecodeError, OSError):
            continue
        if not log.finished and log.match_start:
            found.append(log)
    return found
