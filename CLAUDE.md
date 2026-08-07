# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

A venv lives at `.venv`. Install with `.venv/bin/pip install -e ".[dev]"`.

| Task | Command |
|---|---|
| All Python tests | `.venv/bin/pytest` |
| Engine only (pure, no sockets, ~3s) | `.venv/bin/pytest server/engine/tests` |
| Server lifecycle / protocol / resume | `.venv/bin/pytest server/tests` |
| Agent and harness | `.venv/bin/pytest agents` |
| One test | `.venv/bin/pytest server/engine/tests/test_table.py::test_no_defense_is_strictly_dominated` |
| Balance round-robin, headless | `.venv/bin/python -m server.engine.simulate` |
| Run the server | `.venv/bin/uvicorn server.main:app --reload` |
| Frontend | `cd frontend && npm run build` (output `frontend/out/`, served by FastAPI) |
| Clip library validation, no browser | `cd frontend && npm run poses` (~12s; `-- --no-mesh` skips the skinned-floor pass) |
| Look at a clip, no browser | `cd frontend && python3 tools/pose_render.py ko jab` → PNG contact sheets in `tools/preview/` (`--view front`, `--trace` for per-frame heights) |
| Reference agent | `.venv/bin/python agents/reference_agent.py --code RUMBLE-4F2 --url ws://localhost:8000` |
| …as a chosen persona | add `--persona aggressor \| counter_puncher \| economist \| pattern_reader` |
| Run in Docker | `docker compose up -d --build` → <http://localhost:8000> |
| Stop Docker | `docker compose down` (keeps match logs) — `-v` also **deletes them** |

Two bare invocations of the reference agent against the **same match code** deliberately pick *different* personas, so filling both corners by hand gives a real fight rather than a mirror match. `--persona` pins one; `--name` and `--model` still override. The persona is part of `prompt_version` (`ref-v2+aggressor-v1`) so two personas can never aggregate into one row.

`ARENA_LOG_DIR`, `ARENA_CORS_ORIGINS`, and `ARENA_LOG_LEVEL` configure the server. `.env` holds `OPENROUTER_API_KEY` (gitignored) — it serves the evaluation harness, not the reference agent, which uses `ANTHROPIC_API_KEY`.

Deployment detail, including Render, is in [`docs/deploy.md`](docs/deploy.md). Two constraints there are not tuneable: **one worker, one instance** (the registry is in-memory and tick loops are asyncio tasks in-process), and **`/data/logs` must be a persistent volume** (it is both the evaluation dataset and the crash-recovery substrate).

**All six phases are implemented. Three gates remain open**, and each needs something no test can supply: real model runs (Phase 3.5 and the Phase 5 evaluation) and a deployed host (Phase 6). See the Build Status table in `docs/plan.md`.

## Never spend API budget without asking

The suite stubs every model client, so `pytest` is free to run. Anything that would call Anthropic or OpenRouter for real — the Phase 3.5 questions, the Phase 5 gate, any harness batch — costs the user money and needs their go-ahead first. The Evaluation Design section of the plan puts a serious comparison at ~100 matches and ~12,000 LLM calls; that is not a decision to make on the user's behalf.

## What ADAM Arena is

A hosted platform where two LLM-powered agents fight under boxing rules. Agents are WebSocket **clients** that dial in with a match code (Kahoot/Jackbox model) — no hosting needed on the agent side. Agents submit **intents** (one of eight action names); the platform owns the action space, rules engine, tick loop, and Three.js renderer.

The unit of evaluation is the **agent** (prompt + model + logic as a bundle), not the model and not inference speed. Ticks resolve simultaneously, so a faster agent gains no in-game advantage. Discrimination between agents comes from **exploiting non-equilibrium opponents** — hence the built-in bot ladder is the measuring instrument, and model-vs-model play is the demo.

## Document hierarchy

- **`docs/agent_guide.md` — the front door for agent builders.** The 5-minute path, the rules explained rather than specified, the protocol with real captured payloads, writing an agent in any language, and testing free against the bot ladder. Point friends here; point *implementers* at the spec. It explains and links — it never restates a number that could drift.
- **`docs/arena_spec.md` (v0.6) — the contract.** Protocol, action space, damage/stamina values, resolution rules, message schemas, REST API, frontend views. Friends will build agents against this spec, not against the code. If implementation must deviate, **update the spec in the same commit.** §13 is the live list of settled-vs-open tuning decisions — read it before re-litigating one.
- **`docs/plan.md` (v4)** — what is *not* finished: the three open gates, and the evaluation conventions that make results comparable. Completed build instructions have been removed; the code and the spec are the record.
- **`docs/skeleton.md`** — rig joint map and clip-authoring conventions, including the fourteen-clip roster. Consumed only by the renderer; orthogonal to rules and protocol.
- **`agents/SPEC_NOTES.md`, `frontend/SPEC_NOTES.md`** — gaps found building against the spec that are **still open**. Anything since written into the spec has been deleted from them; the spec is the record.

Docs describe the system as it is now. When something is implemented and its rationale lives here or in the spec, delete the plan that described it rather than leaving a second account to drift.

## Architecture invariants

These are load-bearing across multiple documents; violating one breaks something non-obvious.

**Pure core, thin edges.** `resolve(state, move_a, move_b) -> (new_state, events)` has zero I/O and zero randomness. Purity buys three things at once: testability before a socket exists, crash recovery by replaying the log (spec §6.4c — there is no database; the log *is* the source of truth), and offline re-scoring of old matches against metrics that did not exist when they were fought. Any proposal that costs `resolve()` its purity — e.g. a next-tick damage bonus — is a last resort.

**A missed move is penalised; a broken connection is not.** Socket open and silent → the agent's failure → `no_move`, defenseless, +5 regen only, 15-point penalty, `cause` logged. Socket closed → infrastructure failure → the match *pauses* and awaits `resume` with a `corner_token`. There is no TKO rule. This split is what stops a network blip from being recorded as a combat loss and a slow agent from being scored as a defensive strategist.

**One action is not a strategy.** The same action may be submitted at most **3 times in a row**; the 4th and every further consecutive repeat is **void** — stripped to nothing, defenceless that tick, stamina still paid, **no penalty** (spec §4.6). Spam is punished economically, never punitively, so `penalties` keeps meaning only no-moves and the log can tell "this agent spams" from "this agent went silent". Fighter state carries `last_action` and `repeat_streak` on the wire for this: a rule that depends on the past would otherwise break the stateless-by-design promise that pause/resume relies on. Three ordering facts are load-bearing — the repeat check runs **before** the affordability check (so a void never also reports `exhausted`), a **void still extends the streak** (or the cap is a rate limit, not a cap), and the streak counts what was **submitted**, not what it resolved as.

**Rules as data.** The engine is a generic resolver reading a `BOXING` config table. A future combat type = new table + new clips. Do **not** build a combat-type selector or plugin system — import `BOXING` directly.

**Bots use the public protocol.** Built-in bots are asyncio tasks connecting through `/ws/join/{code}` like any external agent. No privileged internal path — baselines and contenders must be measured through identical machinery.

**Two party vocabularies, deliberately.** Agent-facing messages are egocentric (`self` / `opponent`); spectator messages and the log are absolute (`agent_a` / `agent_b`). `match_end` is absolute in both so one parser serves both.

**Every match must be attributable.** Agent `profile` is required at join and stored verbatim; `telemetry` (latency, tokens, `reason`, `fallback`) rides on every move. Log fields are the one thing that cannot be backfilled — matches fought before they land are unusable data. Strip `reason` and token counts from the live spectator stream only; keep them in the log.

**REST `{ref}` accepts match_id *or* match_code.** Spectators hold only a code; keying read endpoints by ID alone makes Replay impossible for them.

**Clip name IS the action name.** The eight action clips map 1:1 to the eight action names with no mapping table, so a rules-table rename is a clip rename. The other six (`stance`, `hit_react`, `riposte`, `ko`, `exhausted`, `void`) are named after events and states and are reached from `tick.events`, never from `tick.moves` — which is what keeps that 1:1 rule intact. A renderer must check events before animating: a voided action must never play its strike clip.

**The fighters wear their corner.** Both are the same rig loaded twice and `xbot.glb` dresses it in one salmon body material, one darker joint material and no textures at all — so out of the file the two corners are the same colour. `Fighter.skin()` puts the corner colour on the albedo: body tinted and darkened, joint material mixed almost to black so knees and elbows stay legible. Rim lights, deck band, post cap and HUD carry the same colour, but they cannot carry it alone — at the moment a punch lands the silhouettes overlap and a rim light is exactly what is occluded. `src/renderer/corners.ts` is the single definition; `--a` / `--b` in `globals.css` is the one copy that cannot import it.

**Clip grounding is checked on the mesh, not on joints.** `pose_check.py` check 7 CPU-skins the real geometry and samples every other frame of every clip, because the two ways a clip goes through the floor are both invisible to joint-at-keyframe checks: joints sit inside the body, and three.js interpolates *between* keyframes on an arc the endpoints don't describe. That combination hid a 14 cm foot-through-the-deck in `ko` behind five clean keyframes. The same pass asserts the opposite failure for a terminal clip — a KO that stops *above* the deck is a body sinking to a halt in mid air. A clip with `"ground": false` owns its leg fold — see `docs/skeleton.md`, "The floor is not a keyframe property".

**One rig, three consumers, one implementation of the maths.** `clipFactory.ts` plays the clips; `tools/pose_check.py` validates them and `tools/pose_render.py` draws them, both on `tools/rig.py`. The two Python tools share a module rather than a copy for the same reason the spec is the contract: a validator that disagrees with the renderer about the grounding solve, the `_fist` expansion or the slerp is worse than no validator. The TypeScript copy is unavoidable and is the one to change first.

**Replay seeks are free because state is absolute.** `tick_result.state` carries the full picture per tick (spec §7), so the scrub slider jumps straight to a tick instead of replaying up to it, and the event log is re-derived from the ticks up to the slider. The only history a jump reconstructs by hand is who is already on the floor, read from that tick's HP.

## Working rules

- **Phases are gated.** Do not start a phase until the previous gate passes. Phase 3.5 (a throwaway LLM agent) exists specifically to catch rules-table changes *before* clips are authored against those action names.
- **Out of scope, do not build:** accounts/auth beyond the three tokens, ELO/rankings, tournaments, matchmaking, agent hosting, ruleset selector, randomness/crits, database, replay library, horizontal scaling, leaderboards. The *log schema* that would feed an analysis layer is in scope; the analysis layer is not.
- **The published balance figures are load-bearing tests**, not documentation. `test_balance.py` asserts the 0–0 Jab-bot draw (against `mostly-block_high`), the KO of a *literal* always-`block_high` at tick 30, the tick-28 hook-every-4th KO, and the tick-26 `dodge_right` KO; `test_server.py` re-asserts the tick-28 figure over real sockets. Re-derive them with `python -m server.engine.simulate` — never hand-edit. A rules change that moves them should move the spec too, deliberately.
- **Built-in bots are repeat-aware and must stay that way.** A bot that voided out would stop probing what it exists to probe. `test_balance.py::test_no_bot_in_the_ladder_ever_voids_a_move` guards this. `ConstantBot` in the round-robin is the deliberate exception — its voiding *is* the measurement.
- **`frontend/src/lib/demo.ts` is the one duplicate of server logic** in the repo — a hand-copy of the resolution rules backing `?demo=1`. It can drift silently. Change it with `server/engine/rules.py` or delete it.
- `/doc-review` (`.claude/commands/doc-review.md`) reviews the docs and writes results to `docs/doc_review.md`.
