# Frontend spec notes

Places where `docs/adamant_spec.md` does not determine an answer the frontend has to
pick. Each says what was assumed and what would have to change if the server decides
differently.

Clip-authoring decisions that were open during Phase 4 — root motion, the fourteen-clip
roster, `idle` vs `stance`, the `exhausted` and `void` tells, and the upper-arm axis
correction — are settled and now live in [`docs/skeleton.md`](../docs/skeleton.md).
Findings since written into the spec are not repeated here.

## 1. `GET /api/matches/{ref}` has no response schema

Spec §8 describes it as *"Status: `lobby | in_progress | paused | finished |
abandoned` + corners (with profiles) + result if finished"* but names no fields.

**Assumed** `{status, match_id?, match_code?, corners?, config?, result?}` where
`corners` matches `lobby_update.corners` and `result` matches `match_end`. Everything
except `status` is read defensively (`src/lib/types.ts: MatchStatusResponse`), so a
server that omits or renames the optional fields degrades rather than breaks — the
WebSocket supplies the same information a moment later.

## 2. `match_resumed` has no payload schema

Spec §6.4 sends `match_paused` with a full body but only names `match_resumed`.
**Assumed** `{"type": "match_resumed", "tick"?: number}`; the frontend needs only the
type, to clear the paused overlay.

## 3. Log `ticks[]` element shape

Spec §7 says the log is `{schema_version, match_start, ticks: [...], match_end}` and
that it "contains the `match_start` payload plus every `tick_result`". It does not say
whether the stored objects keep their `type` field, nor what `match_end` is on an
unfinished match. **Assumed** the tick objects are `tick_result` payloads (with or
without `type`) and `match_end` may be `null`. Replay reads only
`tick`/`moves`/`events`/`state`.

## 4. `POST /api/matches` returns `watch_url`, but its shape is unspecified

This is a static export served from one `index.html`, so it hash-routes: a watch link
is `https://host/#/m/RUMBLE-4F2`. The frontend therefore **builds its own share link**
and ignores `watch_url`. If the server wants to be authoritative about that URL, it
should emit exactly that form.

## 5. No heartbeat on the spectator socket

§7 defines no ping/pong or idle timeout. The frontend treats any close as transient
and reconnects with linear backoff (1 s → 8 s). A server that deliberately closes idle
lobby sockets will see clients reconnect forever; if that is not wanted, the spec should
say so. An `error` frame is the one close it does *not* retry — the code will never
resolve, so there is nothing to come back to.

It does **not** re-seed from `GET /api/matches/{ref}` on a reconnect; that fetch happens
once, when the feed mounts. It does not need to, because every socket replays the match
from tick 1 and the client keeps a high-water mark on the tick number.

## 6. Two names for the same object

`lobby_update` keys the corner map `corners`; spectator `match_start` keys it
`agents`. Both carry `{name, profile}`. Not a contradiction, just two names — handled,
noted in case it was unintended.
