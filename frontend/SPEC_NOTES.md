# Frontend spec notes

Places where `docs/arena_spec.md` (v0.4) or `docs/skeleton.md` did not determine an
answer the Phase 4 frontend had to pick, plus the two open questions skeleton.md
asked the clip author to settle. **Nothing in `docs/` was edited** — other phases are
being built against those files concurrently. Each item says what was assumed and
what would have to change if the server decides differently.

---

## 1. skeleton.md's upper-arm axis notes contradict the rig (renderer-blocking)

`SKELETON_MAP["LeftArm"]` says:

> `x: swing fwd(+)/back(−). y: twist/roll. z: raise overhead(+)/lower to side(−)`

In `xbot.glb` every bone's rest rotation is identity (verified in
`tools/pose_check.py`), so all bones share the model's axis frame, and the upper-arm
bone runs along ±X. A rotation about **X is therefore the twist along the bone** — it
cannot swing the arm anywhere — and the forward/back swing is **Y** (negative =
forward for the left arm, positive for the right).

The map contradicts itself on this: the `LeftForeArm` entry says `y(−): bend elbow
(curl toward bicep)`, which is only true under the reading above, since the forearm
bone points along the same axis as the upper arm.

**Taken:** the geometric reading (x = twist, y = swing, z = raise/lower). The stated
*ranges* are unchanged and were respected everywhere; only the prose meaning of the
axes differs. `src/renderer/skeleton-map.json` carries the corrected note inline.
If skeleton.md is revised, that file is the one to keep in sync.

## 2. The map has no root motion, and three clips need it

skeleton.md defines rotations only. Two clips cannot be authored without translation:

- **`ko`** must end "on the floor". Lying down is a whole-body rotation of ~88° plus
  an ~87 cm drop of the pelvis; expressing it as a `Hips` rotation would blow through
  the documented `Hips` range of ±35° even though nothing deforms (the Hips is the
  root of the chain, so it moves everything rigidly).
- **every crouch** — `block_low`, `block_high`, the dodges. Bending the knees in pure
  FK *lifts the feet*, it does not lower the hips, so a crouch without a hips
  translation walks the fighter into the air.

**Taken:** two renderer-level keys that are explicitly not joints —
`_root` (Hips translation in rig units, fighter-local) and `_rootRot` (whole-body
rotation pre-multiplied onto the Hips). `_root.y` is normally *solved*, not authored:
`clipFactory` grounds each keyframe so the lowest toe stays on the canvas. `ko` opts
out with `"ground": false` and authors its own fall.

## 3. Open question — does `idle` reuse the stance loop? **No.**

skeleton.md: *"Whether `idle` (the stamina-regen action) reuses the stance loop or
gets a distinct 'catch your breath' clip. Distinct reads better and costs one clip."*

**Decided: distinct**, so there are two clips where skeleton.md counted one:

- `stance` — the seamless 45-frame loop played whenever the clip queue is empty.
- `idle` — the 45-frame *action* clip: guard drops, deep breath, guard back up.

Reason: `idle` is a **choice with a cost and a payoff** (+20 stamina, zero defense,
spec §2/§3). If it looked identical to standing still, the single most legible piece
of strategy in the game — a fighter deciding to recover rather than fight — would be
invisible to spectators, and the stamina bar would move for no visible reason. One
extra clip is a low price for that.

## 4. Open question — does `exhausted` get a visual tell? **Yes.**

skeleton.md: *"It is currently rendered as `idle`; a slumped variant would make the
stamina economy legible to spectators, which is worth something for a platform meant
to be watched."*

**Decided: yes** — a 45-frame `exhausted` clip (hands drop, deep forward slump, heavy
breathing, back to STANCE). It is event-driven, not action-driven: the renderer plays
it whenever the tick carries an `exhausted` event for that corner, whatever
`tick_result.moves` reports (see #6).

Reason: an unaffordable action converting to `idle` is the one outcome where **what
the agent chose and what happened differ**, and a spectator who cannot see it will
read the fighter as passive rather than broke. It also makes the stamina numbers in
§13 ("stamina regen numbers, untested") observable while they are being tuned.

Clip count: skeleton.md says eleven per fighter; this renderer builds **thirteen**
(the eleven, plus `stance` and `exhausted`), each mirrored for `agent_b`.

## 5. `GET /api/matches/{ref}` has no response schema

Spec §8 describes it as *"Status: `lobby | in_progress | paused | finished |
abandoned` + corners (with profiles) + result if finished"* but names no fields.

**Assumed** `{status, match_id?, match_code?, corners?, config?, result?}` where
`corners` matches `lobby_update.corners` and `result` matches `match_end`. Everything
except `status` is read defensively (`src/lib/types.ts: MatchStatusResponse`), so a
server that omits or renames the optional fields degrades rather than breaks — the
WebSocket supplies the same information a moment later.

## 6. `tick_result.moves` is undefined for `no_move` and for stamina conversions

Spec §4.1 converts unaffordable actions to `idle` and absent moves to `no_move`
*before* resolution, but §7's `tick_result.moves` is only ever shown with two real
action names. Unanswered: does `moves` carry the intent, the converted action, the
string `"no_move"`, or nothing at all?

**Taken:** the renderer never depends on it. It picks a clip from the **events**
first — `exhausted` → the exhausted clip, `no_move` → stand still (defenseless) —
and only otherwise looks up `moves[corner]` as a clip name. An unknown or missing
action name is ignored rather than throwing, so a rules-table addition cannot blank
the canvas. The HUD prints whatever `moves` contains verbatim.

## 7. `match_resumed` has no payload schema

Spec §6.4 sends `match_paused` with a full body but only names `match_resumed`.
**Assumed** `{"type": "match_resumed", "tick"?: number}`; the frontend only needs the
type to clear the paused overlay.

## 8. Log `ticks[]` element shape

Spec §7 says the log is `{schema_version, match_start, ticks: [...], match_end}` and
that it "contains the `match_start` payload plus every `tick_result`". It does not say
whether the stored objects keep their `type` field, nor what `match_end` is on an
unfinished match. **Assumed** the tick objects are `tick_result` payloads (with or
without `type`) and `match_end` may be `null`. Replay reads only
`tick`/`moves`/`events`/`state`.

## 9. `POST /api/matches` returns `watch_url`, but its shape is unspecified

This is a static export served from one `index.html`, so it hash-routes: a watch link
is `https://host/#/m/RUMBLE-4F2`. The frontend therefore **builds its own share link**
and ignores `watch_url`. If the server wants to be authoritative about that URL, it
should emit exactly that form.

## 10. No heartbeat on the spectator socket

§7 defines no ping/pong or idle timeout. The frontend treats any close as transient
and reconnects with linear backoff (1 s → 8 s), then re-seeds from
`GET /api/matches/{ref}`. A server that closes idle lobby sockets deliberately will
see clients reconnect forever; if that is not wanted, the spec should say so.

## 11. Two names for the same object

`lobby_update` keys the corner map `corners`; spectator `match_start` keys it
`agents`. Both carry `{name, profile}`. Not a contradiction, just two names — handled,
noted in case it was unintended.

## 12. A copy of the resolution rules lives in the demo fixture

`src/lib/demo.ts` reimplements enough of spec §2–§4 to generate a self-consistent
fake match for `?demo=1`, because the renderer had to be developed and verified
before the server existed. It is **the only duplicate of server logic in this
codebase**, it is imported by nothing on the live path, and it should be deleted once
a real server can be pointed at. If the rules table changes and nobody updates it,
the demo drifts — the demo is a fixture, never a reference.
