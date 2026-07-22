# ADAM Arena — Documentation Review

_Review date: 2026-07-22 · Docs reviewed: `plan.md`, `arena_spec.md` (v0.3), `skeleton.md`_

## Summary

The docs are in good shape. `plan.md` is a disciplined build plan with real verification gates, and `arena_spec.md` covers the action space, resolution rules, both WebSocket protocols, and the REST API. No code exists yet, so the docs are currently the entire project.

The spec's stated bar is "a stranger could build a working agent from it alone." It is close, but not there yet: a few things an agent author must parse are underspecified, and one worked example is internally inconsistent. Those are the findings below, in priority order.

| Doc | State |
|---|---|
| `plan.md` | Strong. Phased, gated, explicit non-goals. Minor internal tensions. |
| `arena_spec.md` | Strong structure; several protocol gaps that block the "stranger builds an agent" test. |
| `skeleton.md` | Accurate rig map, but orphaned — no prose, and nothing references it. |

---

## Blocking for "an agent builder can work from the spec alone"

### 1. `history` uses an undocumented event encoding different from everything else
Section 4 defines events as objects:
```json
{"type": "hit", "by": "agent_a", "action": "cross", "damage": 10}
```
but the `state_update` `history` (§6.2) uses opaque strings:
```json
{"tick": 10, "self": "idle", "opponent": "hook", "events": ["hit:15"]}
```
`"hit:15"` and `"blocked"` are never defined. Critically, **the string form drops who the event applies to** — in that example the reader must infer from `self: "idle"` that the 15 landed on self. Since history-based opponent modeling is explicitly "the skill being measured," this is the single most important thing to nail down.

**Fix:** either reuse the Section 4 object form in `history`, or define the mini-format exhaustively (grammar, full token list, and how the subject is determined).

### 2. `final_state` is unspecified, and `winner` on a draw is undefined
```json
{"type": "match_end", "winner": "agent_a", "reason": "ko | points | tko | draw", "final_state": {"...": "..."}}
```
`final_state` is a literal placeholder. And on a draw, `winner` has no defined value — `null`, `"draw"`, or absent are all plausible, and each breaks a different client. `match_end` is also the message the replay UI and the log parser both key off.

**Fix:** give `final_state` a concrete schema (presumably the same shape as `tick_result.state`) and state that `winner` is `null` when `reason` is `"draw"`.

### 3. Tick numbering is never defined
Agents "must echo the tick number" (§6.1) and moves for the wrong tick are forced to `idle` (§5) — so an off-by-one here is a silent loss. The spec never says whether the first tick is `0` or `1`, nor how `max_ticks: 60` maps onto that range.

**Fix:** one sentence — "ticks are 1-indexed; a match runs ticks 1…max_ticks."

### 4. Host-token mechanics live in the plan, not the spec
`plan.md` (line 89) specifies the `X-Host-Token` header, `401` on missing/invalid, `409` on illegal transition. `arena_spec.md` §8 only says "requires `host_token`" — no header name, no status codes. By Principle 5 the spec is the contract, so the contract is missing the part a frontend must implement.

**Fix:** move the header name and status codes into §8.

### 5. Spectators hold a code, but the REST API takes an `id`
`GET /api/matches/{id}` and `/api/matches/{id}/log` are keyed by `match_id`, while the WS endpoints and the share link are keyed by `match_code`. `match_id` is only ever returned to the creating browser. As written, **a spectator who followed a share link cannot fetch the log — so they cannot use Replay**, which Phase 4's gate requires.

**Fix:** accept the match code on those endpoints, or state that `match_id` and `match_code` are the same value.

---

## Spec correctness and balance

### 6. `dodge_right` is strictly dominated — it is never the right move
| Defense | Cost | Counters |
|---|---|---|
| `block_high` | 3 | `jab`, `cross` |
| `block_low` | 3 | `hook` |
| `dodge_left` | 8 | `cross`, `hook` |
| `dodge_right` | **8** | **`jab` only** |

`block_high` counters everything `dodge_right` counters, plus `cross`, at **less than half the stamina cost**. A rational agent never picks `dodge_right`, which quietly reduces the eight-action space to seven. `block_low` survives only because `dodge_left` costs more; in general the cheap blocks dominate the economy.

The matrix is allowed to be "arbitrary but published," but a dead action is different from an arbitrary one. This is adjacent to §11's "stamina regen numbers: untested" — worth resolving in the doc before Phase 1 hardcodes it, e.g. by giving dodges something blocks lack (a counter-window, chip-damage immunity, or a lower cost).

### 7. Does a stamina-forced `idle` earn the `idle` regen bonus?
§3 says an unaffordable action "is converted to `idle`, with an `exhausted` event," and separately that `idle` grants +15 on top of the +5 passive. Whether a *forced* idle grants that +15 is undefined — and it materially changes how fast an exhausted fighter recovers. The same question applies to §5's forced idles (missed deadline, invalid, duplicate, wrong tick).

**Fix:** state it once, covering both causes. (Recommend: yes — forced idle is idle. Simplest rule, and it keeps `resolve()` free of special cases.)

### 8. The `idle` row understates regen
§2's action table says `idle` "Regenerates +15 stamina"; §3 says +15 *extra*, +20 total. Read alone, the table is wrong.

**Fix:** "+15 on top of the +5 passive regen (+20 total)."

### 9. Points don't match damage in the §6.2 example
Points are defined as cumulative landed damage. In the `state_update` example, `self.points` is 25 but the opponent has taken 30 damage (`hp: 70`). The `tick_result` example inherits the discrepancy (agent_a `points: 35`, agent_b at `hp: 60` = 40 taken). Everything else in those examples reconciles exactly — HP, stamina costs, and regen all check out — so this reads as a typo, but a spec's worked examples are what implementers test against.

### 10. Double-TKO is undefined
Double-KO resolves to a draw (§5), but if both agents miss three consecutive ticks — very likely if the host's network drops, since both get force-idled by the same event — the outcome isn't specified. One line resolves it.

### 11. `config` is sent, but nothing can set it
`match_start` carries `{"max_ticks": 60, "deadline_ms": 5000}` as if configurable, yet `POST /api/matches` takes no body and no override is described. This also makes Phase 2's dropped-client gate expensive: 3 missed ticks × 5000 ms = 15 s of wall clock per integration run.

**Fix:** either accept optional overrides at match creation, or state that `config` is informational and constant in MVP — and give tests an internal way to shrink the deadline.

### 12. §11 "Open Questions" mostly aren't questions
Three of the four bullets are decisions already made (tick pacing, the trade-damage contingency, code expiry). Only "stamina regen numbers" is genuinely open. Retitle to "Deferred Tuning Decisions" and move the settled ones into the body, so nobody re-litigates them at build time.

---

## `plan.md` findings

### 13. The spec filename in the plan does not exist
`plan.md` refers to **`adam-arena-spec.md`** at the repo root (lines 13, 21, 47, 138). The actual file is **`docs/arena_spec.md`**. Four references, all wrong, including the one inside the Definition of Done. Pick one name and fix the other side — Principle 5 makes this consistency load-bearing.

### 14. Phase 5's gate may be unreachable as written
The gate compares defensive-action share in **ticks 41–60** against ticks 1–20, versus Jab-bot. But Jab-bot sustains `jab` indefinitely (cost 5, regen +5) for 5 damage a tick, so any match where the reference agent doesn't defend heavily ends by KO around tick 20. Matches that reach tick 41 are already ones where the agent defended — the measurement is partly circular, and a mediocre agent fails by dying rather than by not adapting.

**Fix:** measure first-half vs. second-half of the *actual* tick count, and make survival a separate criterion.

### 15. The frontend is described two ways
The Tech Stack table (line 31) says "Single-page app … Three.js + existing ADAM rig"; the bullet at line 37 says "Next.js with TypeScript, static export." Compatible, but the reader has to reconcile them. State it once: "Next.js + TypeScript, static export, behaving as a three-view SPA."

### 16. Reference-agent provider vs. the credential on hand
The plan puts the reference agent on the **Anthropic API directly** (lines 33, 39, 116), while the only credential in the working tree is an **OpenRouter** key. That's consistent with the "optional LiteLLM → OpenRouter for local experiments" path, but confirm the direction before Phase 5, so the published example and the one you actually test with don't diverge.

### 17. The repo layout reads as description, not intent
`server/`, `agents/`, and `frontend/` (lines 44–61) don't exist yet, and the layout places the spec at the root, where it isn't. Label it **Target Layout**.

---

## `skeleton.md` findings

### 18. It is a `.md` file containing only Python
No heading, no prose, no fenced code block — it renders as an undifferentiated wall of text in any markdown viewer, and the `#` comments carrying the actual instructions are the least visible part. Wrap the dict in a ```python fence and add a short intro: which rig it maps (Mixamo Xbot, T-pose rest), that rotations are degree offsets from rest, and what consumes it.

### 19. Nothing references it
`plan.md` Phase 4 calls for "one keyframe clip per action (8) per fighter plus a hit-react and a KO clip" — exactly what this map exists to drive — yet neither `plan.md` nor `arena_spec.md` mentions `skeleton.md`. Link it from Phase 4 so it isn't rediscovered late.

### 20. No clip-authoring conventions
The per-joint ranges and plain-English notes are genuinely good, and the mirroring is internally consistent (`LeftForeArm.y` `[-145, 0]` vs `RightForeArm.y` `[0, 145]`, shoulder `z` signs, thigh spread signs all check out). What's missing is the layer above: clip duration and FPS, keyframe count per clip, how a clip binds to an action name, and whether the neutral pose for boxing is the T-pose or a stance. Settle that before authoring 10 clips, not after.

---

## Correction to the previous review

The prior version of this file listed a **critical** issue: "`.env` is tracked by git and present in the initial commit." That is not the case — the repository **has no commits at all** (`git log` reports no commits on `main`; `git ls-files` is empty), and `.gitignore` already covers `.env` and `.env.*`. The key has never entered git history.

It remains true that a live `OPENROUTER_API_KEY` sits in the working tree. The real precaution is simply not to force-add it (`git add -f`), and to commit a placeholder `.env.example` alongside. Rotation is prudent hygiene here, not an emergency.

---

## What's working well

- **The engine seam is right.** `resolve(state, move_a, move_b) -> (new_state, events)` as a pure, I/O-free function makes the entire rules layer testable before a socket exists.
- **The gates are testable, not aspirational.** Especially the table-invariant tests (every attack has a block *and* a dodge counter; no defense counters everything) — those are what catch a bad matrix edit six months out. Note that finding 6 is exactly the kind of thing a *cost*-aware invariant would also catch.
- **Scope discipline.** An explicit "do not build" list (line 133) plus Principle 1 is the main reason this plan is likely to ship.
- **Bounded connection handling** (Principle 6) collapses disconnects, timeouts, and malformed moves into a single rule — a lot of avoided complexity for one paragraph.
- **The spec is structurally complete.** Action matrix, resolution, both protocols, REST, and frontend views are all present and in a sensible order; the findings above are gaps inside a sound frame, not missing sections.
- **The examples are arithmetically checked.** In the `tick_result` example every stamina and HP figure reconciles against the prior `state_update` (60−10+5 = 55; 40−8+5 = 37; 70−10 = 60). Only the points figures drift (finding 9).

---

## Recommended order

1. Fix findings **1–5** — they are what stands between the spec and its own Definition of Done (#2, "a stranger could build a working agent from it alone").
2. Resolve **6 and 7** before writing `rules.py`; both get baked into the `BOXING` table and the resolver's shape.
3. Sweep the smaller spec corrections (**8–12**) in one pass.
4. Fix the **spec filename** (13) and relabel the target layout (17) — cheap, and 13 breaks the Definition of Done.
5. Revisit the **Phase 5 gate** (14) before you need it.
6. Give `skeleton.md` a header, a code fence, and a link from Phase 4 (18–20).
7. Then start **Phase 1** — zero dependencies, crisp spec, concrete gate.

## Bottom line

The plan is ready to build against. The spec is one editing pass away from being ready for someone *else* to build against — and that gap, not the code, is the current critical path. Separately: `statics/index.html` is still 0 bytes, so the frontend entrypoint is a placeholder rather than a starting point.
