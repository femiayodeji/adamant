"""FastAPI app: REST API, both WebSocket endpoints, and the static frontend.

One process serves everything (plan Phase 6). Nothing in this module computes
combat or owns match rules — it is the shell around `match.Match`, which is itself a
shell around the pure engine.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import bots, matchlog, protocol
from .engine import rules
from .engine.resolver import CORNERS
from .engine.strategies import LADDER, CycleBot
from .match import ABANDONED, FINISHED, LOBBY, LifecycleError, MatchConfig
from .registry import Registry


def _log_level() -> str:
    """Tolerate `info` as well as `INFO`, and never let a typo make the process
    unbootable — `logging.basicConfig` raises on an unknown level, which in a
    container is a crash loop rather than an error message."""
    level = os.environ.get("ARENA_LOG_LEVEL", "INFO").strip().upper()
    if level not in logging.getLevelNamesMapping():
        return "INFO"
    return level


logging.basicConfig(level=_log_level())
logger = logging.getLogger("arena")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend" / "out"

registry = Registry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    recovered = registry.recover()
    registry.start_recovered(recovered)
    registry.start_sweeper()
    try:
        yield
    finally:
        await registry.stop_sweeper()
        await bots.shutdown()


app = FastAPI(title="ADAM Arena", version="0.1.0", lifespan=lifespan)

# CORS is locked to the frontend origin. Unset means same-origin only, which is the
# deployed shape — the API and the static export are served by this one process.
_origins = [o.strip() for o in os.environ.get("ARENA_CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )


def _require_match(ref: str):
    match = registry.get(ref)
    if match is None:
        raise HTTPException(status_code=404, detail="unknown match")
    return match


def _require_host(match, token: Optional[str]):
    """`host_token` is returned only to the creating browser session. Together with
    the per-corner `corner_token`, that is the entire auth model for the MVP."""
    import secrets

    if not token or not secrets.compare_digest(token, match.host_token):
        raise HTTPException(status_code=401, detail="host token required")


def _ws_base(request: Request) -> str:
    scheme = "wss" if request.url.scheme == "https" else "ws"
    return f"{scheme}://{request.url.netloc}"


# ─── REST (spec §8) ───


@app.post("/api/matches")
async def create_match(body: dict[str, Any] = Body(default={})):
    try:
        config = MatchConfig.build(body.get("max_ticks"), body.get("deadline_ms"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    match = registry.create(config)
    return {
        "match_id": match.match_id,
        "match_code": match.match_code,
        "host_token": match.host_token,
        "watch_url": f"/?watch={match.match_code}",
        "config": config.as_dict(),
    }


@app.get("/api/rules")
async def get_rules():
    """The BOXING table, as data (spec §8.5).

    It exists so that nothing outside this repo has to hand-copy a damage number.
    The in-app agent-builder page renders its action table straight from this
    response, and an external agent can read its costs from here instead of
    embedding them — which is the difference between a client that survives a
    balance change and one that silently plays the old game.

    Assembled from `engine.rules` directly, so it cannot drift from the resolver.
    """
    return {
        "combat_type": "boxing",
        "actions": list(rules.ACTIONS),
        "attacks": rules.ATTACKS,
        "defenses": {
            name: {
                "stamina": spec["stamina"],
                "counters": list(spec["counters"]),
                "riposte": spec["riposte"],
            }
            for name, spec in rules.DEFENSES.items()
        },
        "idle": rules.IDLE,
        "economy": {
            "start_hp": rules.START_HP,
            "start_stamina": rules.START_STAMINA,
            "stamina_cap": rules.STAMINA_CAP,
            "passive_regen": rules.PASSIVE_REGEN,
            "idle_regen_bonus": rules.IDLE_REGEN_BONUS,
        },
        "repeat_cap": rules.MAX_CONSECUTIVE_REPEATS,
        "no_move": {
            "penalty": rules.NO_MOVE_PENALTY,
            "causes": list(rules.NO_MOVE_CAUSES),
        },
        "defaults": {
            "max_ticks": rules.DEFAULT_MAX_TICKS,
            "deadline_ms": rules.DEFAULT_DEADLINE_MS,
            "resume_window_ms": rules.DEFAULT_RESUME_WINDOW_MS,
            "max_ticks_range": list(rules.MAX_TICKS_RANGE),
            "deadline_ms_range": list(rules.DEADLINE_MS_RANGE),
        },
        "bots": sorted(LADDER),
    }


def _stored_log(ref: str) -> Optional[dict[str, Any]]:
    """A finished match that has left memory is still fully described by its log,
    which every restart makes the only copy. Resolvable by code as well as by id."""
    path = matchlog.resolve_log_path(ref, registry.log_dir)
    if path is None:
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


@app.get("/api/matches/{ref}")
async def get_match(ref: str):
    match = registry.get(ref)
    if match is not None:
        return match.status_payload()

    stored = _stored_log(ref)
    if stored is None or not stored.get("match_end"):
        raise HTTPException(status_code=404, detail="unknown match")

    # Rebuilt from the log so a share link still resolves after a restart.
    start = stored.get("match_start", {})
    end = stored["match_end"]
    return {
        "match_id": Path(matchlog.resolve_log_path(ref, registry.log_dir)).stem,
        "match_code": start.get("match_code"),
        "status": "abandoned" if end.get("reason") == "abandoned" else "finished",
        "config": start.get("config", {}),
        "corners": start.get("agents", {}),
        "tick": end.get("final_tick", 0),
        "result": {
            "winner": end.get("winner"),
            "reason": end.get("reason"),
            "final_tick": end.get("final_tick"),
            "final_state": end.get("final_state"),
        },
    }


@app.get("/api/matches/{ref}/log")
async def get_log(ref: str):
    """The log powers Replay, so this must be reachable with a match *code* — a
    spectator never sees the match_id."""
    match = registry.get(ref)
    if match is not None:
        return match.log.as_dict()

    stored = _stored_log(ref)
    if stored is None:
        raise HTTPException(status_code=404, detail="unknown match")
    return stored


@app.post("/api/matches/{ref}/start")
async def start_match(ref: str, x_host_token: Optional[str] = Header(default=None)):
    match = _require_match(ref)
    _require_host(match, x_host_token)
    try:
        match.start()
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": match.status, "match_code": match.match_code}


@app.post("/api/matches/{ref}/fill_bot")
async def fill_bot(
    ref: str,
    request: Request,
    body: dict[str, Any] = Body(default={}),
    x_host_token: Optional[str] = Header(default=None),
):
    match = _require_match(ref)
    _require_host(match, x_host_token)

    corner = body.get("corner")
    bot = body.get("bot")
    seed = body.get("seed")
    period = body.get("period")

    if corner not in CORNERS:
        raise HTTPException(status_code=422, detail=f"corner must be one of {list(CORNERS)}")
    if bot not in LADDER:
        raise HTTPException(status_code=422, detail=f"bot must be one of {sorted(LADDER)}")
    if period is not None and bot != "cycle":
        raise HTTPException(status_code=422, detail="period applies to the `cycle` bot only")
    if period is not None and period not in CycleBot.PATTERNS:
        raise HTTPException(
            status_code=422, detail=f"period must be one of {sorted(CycleBot.PATTERNS)}"
        )
    if match.status != LOBBY:
        raise HTTPException(status_code=409, detail=f"match is {match.status}")
    if match.corners[corner] is not None:
        raise HTTPException(status_code=409, detail=f"{corner} is already filled")

    options = {"period": period} if period is not None else {}
    taken = await bots.spawn(
        url=_ws_base(request), code=match.match_code, bot=bot, seed=seed, corner=corner, **options
    )
    match.bot_fills[taken] = {"bot": bot, "seed": seed, **options}
    return {"corner": taken, "bot": bot, "seed": seed, **options}


@app.post("/api/matches/{ref}/rematch")
async def rematch(
    ref: str,
    request: Request,
    x_host_token: Optional[str] = Header(default=None),
):
    """Run it back: a new match with the same config and the same ladder opponents
    (spec §8.4).

    Deliberately a *new* match rather than a reset of the old one. The log is the
    record and one log is one fight — rewinding a finished match in place would
    either overwrite a result or leave two fights sharing an id, and both make the
    evaluation dataset lie. So the host gets a fresh code, and the pair fight again
    under the same conditions with two comparable logs.

    Built-in bots are re-spawned into the corners they held. A human-run agent has
    to dial the new code, which is why the response carries it and the frontend
    walks the host straight into the new lobby.
    """
    source = _require_match(ref)
    _require_host(source, x_host_token)
    if source.status not in (FINISHED, ABANDONED):
        raise HTTPException(status_code=409, detail=f"match is {source.status}")

    # A copy, not the same object: two matches sharing one mutable config is a
    # footgun waiting for the first setting that can be changed after creation.
    match = registry.create(MatchConfig(**source.config.as_dict()))

    refilled: list[dict[str, Any]] = []
    for corner, fill in source.bot_fills.items():
        try:
            taken = await bots.spawn(
                url=_ws_base(request), code=match.match_code, corner=corner, **fill
            )
        except Exception:
            # One bot failing to come back must not cost the host the new match —
            # the corner is simply left open and can be refilled from the lobby.
            logger.exception("rematch could not refill %s with %s", corner, fill.get("bot"))
            continue
        match.bot_fills[taken] = fill
        refilled.append({"corner": taken, **fill})

    return {
        "match_id": match.match_id,
        "match_code": match.match_code,
        "host_token": match.host_token,
        "watch_url": f"/?watch={match.match_code}",
        "config": match.config.as_dict(),
        "rematch_of": source.match_code,
        "bots": refilled,
    }


# ─── Agent WebSocket (spec §6) ───


@app.websocket("/ws/join/{code}")
async def ws_join(websocket: WebSocket, code: str):
    await websocket.accept()

    match = registry.get(code)
    if match is None:
        await websocket.send_json(protocol.error("invalid_code"))
        await websocket.close()
        return

    try:
        first = json.loads(await websocket.receive_text())
    except (json.JSONDecodeError, WebSocketDisconnect):
        await websocket.close()
        return

    try:
        if isinstance(first, dict) and first.get("type") == "resume":
            slot = match.resume(websocket, first.get("corner_token"))
        elif isinstance(first, dict) and first.get("type") == "join":
            slot = match.join(
                websocket,
                first.get("name"),
                first.get("profile"),
                preferred=first.get("corner"),
            )
        else:
            # Not `invalid_code`: the code resolved fine, the client simply opened
            # with the wrong frame. Reporting a good code as invalid sends builders
            # hunting the one thing that is not wrong (spec §6.2).
            raise protocol.JoinError("expected_join")
    except protocol.JoinError as exc:
        await websocket.send_json(protocol.error(exc.reason))
        await websocket.close()
        return

    await websocket.send_json(
        {
            "type": "joined",
            "corner": slot.corner,
            "match_code": match.match_code,
            "corner_token": slot.token,
            # The name the platform actually gave this corner. Usually the one that
            # was asked for; suffixed when the other corner already had it (spec
            # §6.1). An agent that logs its own name has to be told, or its record
            # of the fight disagrees with everyone else's.
            "name": slot.name,
        }
    )

    if match.started_at:
        # A resuming agent may be a freshly restarted process that never saw the
        # original `match_start`, and it needs `config` (its deadline budget) and
        # `opponent_name` before it can decide anything.
        await websocket.send_json(match.agent_match_start(slot.corner))

    await match.broadcast_lobby()

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None  # classified as `malformed`, exactly as spec §4.4 says
            match.submit_move(slot.corner, payload)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("agent socket error", exc_info=True)
    finally:
        match.agent_disconnected(slot.corner, websocket)
        if match.status == LOBBY:
            # Leaving the lobby frees the corner; leaving a live match pauses it.
            if match.corners[slot.corner] is slot:
                match.corners[slot.corner] = None
            await match.broadcast_lobby()


# ─── Spectator WebSocket (spec §7) ───


@app.websocket("/ws/watch/{code}")
async def ws_watch(websocket: WebSocket, code: str):
    await websocket.accept()

    match = registry.get(code)
    if match is None:
        await websocket.send_json(protocol.error("invalid_code"))
        await websocket.close()
        return

    match.spectators.add(websocket)
    try:
        await websocket.send_json(match.lobby_payload())

        # A watch link has to work at any point in the match, not only from the
        # lobby, so bring a late spectator up to date before streaming.
        if match.started_at:
            await websocket.send_json(match.log.match_start or match._spectator_match_start())
            for record in match.log.ticks:
                await websocket.send_json(
                    {**record, "telemetry": protocol.strip_for_spectators(record["telemetry"])}
                )
        if match.log.match_end:
            await websocket.send_json(match.log.match_end)

        while True:
            await websocket.receive_text()  # read-only; inbound frames are ignored
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("spectator socket error", exc_info=True)
    finally:
        match.spectators.discard(websocket)


@app.get("/api/health")
async def health():
    return {"status": "ok", "matches": len(registry)}


# ─── Static frontend ───

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:

    @app.get("/")
    async def no_frontend():
        return JSONResponse(
            {
                "detail": "frontend not built",
                "hint": "cd frontend && npm install && npm run build",
            },
            status_code=503,
        )
