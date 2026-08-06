# ADAM Arena — Project Plan (v3)

## What This Is

ADAM Arena is a **hosted agent combat evaluation platform**. Two LLM-powered agents fight under boxing rules. The platform is the body and the referee; agents are only brains.

- The platform is **hosted online**; agent builders (Femi and friends) run agent scripts from anywhere and **dial in over WebSocket using a match code** — no hosting or public URL required on the agent side. This is the Kahoot/Jackbox model: create match → share code → agents join → start fight.
- Agents submit **intents** (action names like `jab`, `block_high`) — never animation or physics data. The platform owns the action space, rules engine, tick loop, and the Three.js rig that renders the fight (reused from the original ADAM project).
- Combat is **tick-based with simultaneous resolution**. Both agents get identical state, both submit within a deadline, moves resolve together via a deterministic matrix — so a faster agent gains no in-game advantage.
- Agents receive the last 10 ticks of history in every state update; **opponent modeling from history is the skill being measured.** Agents can be fully stateless.
- **Built-in bots** form a baseline ladder that can fill any empty corner from the lobby, so the platform is testable solo and friends can fight the house.

**The unit of evaluation is the agent** — prompt + model + logic as a bundle — reasoning about combat. Not inference speed. The plan and the spec use "agent" and "model" as distinct terms throughout: two agents may share a model, and a comparison that isolates the model requires holding prompt and logic fixed (see [Evaluation Design](#evaluation-design)).

The full protocol, action space, damage/stamina values, resolution rules, message schemas, REST API, and frontend views live in [`arena_spec.md`](arena_spec.md) (v0.4). That spec is the source of truth. This plan is about *how to build it*. Findings and rationale behind the v0.4 changes are in [`review.md`](review.md).

## Guiding Principles

1. **No over-engineering.** MVP: match codes as the join credential (plus a host token for host-only actions and a corner token for resume), in-memory match registry, one JSON log file per match, no accounts, no rankings, no matchmaking, no database.
2. **Rules as data.** The rules engine is a generic resolver reading a `BOXING` config table (attacks, defenses, counters, stamina, riposte). A future combat type = new table + new keyframe clips, zero engine changes. Do NOT build a combat-type selector or plugin system now — import `BOXING` directly.
3. **Pure core, thin edges.** `resolve(state, move_a, move_b) -> (new_state, events)` is a pure function with zero I/O. WebSockets, timers, HTTP, and rendering are shells around it. Purity is load-bearing twice over: it makes the rules testable before a socket exists, and it makes match logs replayable for both crash recovery and offline re-scoring.
4. **Terminal before Three.js.** Built-in bots must complete full matches (server + two bot clients) before any renderer work starts.
5. **The spec is the contract.** If implementation must deviate from [`arena_spec.md`](arena_spec.md), update the spec in the same commit. Friends will build agents against the spec, not the code.
6. **A missed move is penalised; a broken connection is not.** The platform distinguishes the two failures it can observe. Socket open and silent → the agent's failure → `no_move`, defenseless, 15-point penalty. Socket closed → infrastructure failure → the match pauses and awaits resume. There is no TKO rule. This is what stops a network blip from being recorded as a combat loss, and stops a slow agent from being scored as a defensive strategist. See spec §4.4 and §6.4.
7. **Every match must be attributable.** Agents declare a required profile at join (model, provider, prompt version, temperature, agent version) and it is recorded in the log along with the match config. A result that cannot say what produced it is not a measurement. Log fields are the one thing here that cannot be backfilled — every match fought before they land is unusable data.

## Tech Stack

| Component | Choice | Why |
|---|---|---|
| Platform server | Python, FastAPI, native WebSockets | Reuses ADAM backend patterns |
| Rules engine | Pure Python module, stdlib only | Testable, deterministic |
| Match registry | In-memory dict, 30-min lobby expiry, unfinished matches rebuildable from logs | No database in MVP |
| Frontend | Next.js + TypeScript, static export (`output: 'export'`), served by FastAPI as static files, behaving as a three-view SPA (Home → Lobby → Arena) with the existing ADAM Three.js rig | Reuse, not rebuild |
| Built-in bots | asyncio tasks using the same WS client code as external agents | One protocol, no privileged path |
| Reference agent | Python script (~120 lines), direct Anthropic SDK, `--code`/`--url` CLI args | Published example + integration test. Direct SDK keeps the published dependency surface minimal. |
| Evaluation harness | LiteLLM → OpenRouter | Separate artifact from the reference agent. Cross-vendor model comparison needs one client that speaks to every provider; the published example does not. |
| Match logs | One JSON file per match, `schema_version: 1`, fsynced per tick | Powers replay, crash recovery, and evaluation |
| Tests | pytest | Engine fully covered before server work |

**On the two agent artifacts:** the *published reference agent* is direct-Anthropic and optimised for being copied by a friend. The *evaluation harness* is LiteLLM/OpenRouter and optimised for running the same scaffold against many models. They share the prompt and the decision loop; only the client differs. Keeping them distinct is deliberate — collapsing them would either bloat the published example's dependencies or restrict evaluation to one vendor. The only credential currently in the working tree is an OpenRouter key, which serves the harness.

## Target Layout

Nothing below `docs/` exists yet — this is the intended shape, not a description of the tree.

```
adam-arena/
├── docs/
│   ├── plan.md              # this file
│   ├── arena_spec.md        # protocol + rules source of truth (v0.4)
│   ├── review.md            # documentation review + decision log
│   └── skeleton.md          # rig joint map + clip-authoring conventions
├── server/
│   ├── engine/
│   │   ├── rules.py         # BOXING table
│   │   ├── resolver.py      # pure resolve(), stamina, penalties, win conditions
│   │   └── tests/
│   ├── match.py             # match state machine: lobby → in_progress → paused → finished
│   ├── registry.py          # in-memory matches, code generation, expiry, log recovery
│   ├── bots.py              # baseline ladder as internal WS clients
│   ├── main.py              # FastAPI app: REST API + /ws/join + /ws/watch
│   └── logs/                # JSON match logs (gitignored)
├── agents/
│   ├── reference_agent.py   # published LLM agent script
│   └── harness/             # batch runner + aggregation (LiteLLM/OpenRouter)
└── frontend/                # SPA: home, lobby, arena + Three.js renderer
```

## Build Phases

Each phase has a verification gate. Do not start a phase until the previous gate passes.

### Phase 1 — Rules Engine (pure, no I/O)

Implement `rules.py` (the `BOXING` data table) and `resolver.py`:

- `resolve(state, move_a, move_b)` per spec §4: correct block negates, correct dodge negates **and ripostes 5**, wrong defense/idle/no-move takes full damage, trades land both ways.
- Stamina: per-action costs, +5/tick passive regen, +15 extra on `idle`, cap 100, insufficient stamina → converted to `idle` with an `exhausted` event and **no penalty**.
- No-move: defenseless, +5 passive regen only, **15-point penalty**, `cause` recorded (spec §4.4).
- Win conditions: KO at hp ≤ 0, double-KO draw, decision at max ticks on `points − penalties`, tie → draw. No TKO.
- Events output exactly as spec §4.5, with every party named explicitly.

Tests must include **table-invariant tests**, not just resolver tests:

- Every attack has ≥1 block counter and ≥1 dodge counter.
- No defense counters every attack.
- No action has zero/negative stamina cost except `idle`.
- **No action is strictly dominated** — for every pair of defenses, neither is at least as good as the other across all opponent actions once stamina cost *and* riposte are accounted for. This is the invariant that catches the `dodge_right` class of bug; a coverage-only check does not.
- A no-move is worse than every real action against every opponent action.
- Property-style check: any 60-tick match between two fixed strategies terminates with a valid result.

**Gate:** `pytest` green; a scripted simulation (no sockets) runs the pure strategies round-robin and reproduces the published balance figures — always-`block_high` vs Jab-bot is a 0–0 draw, hook-every-4th KOs Jab-bot around tick 28, and `dodge_right` is not dominated.

### Phase 2 — Match Server (lifecycle + protocol)

FastAPI app implementing spec §5–§8:

- **Registry:** `POST /api/matches` generates a short human-friendly code (`WORD-XXX`), accepts optional `max_ticks` / `deadline_ms`, stores the match in memory with a `host_token`, expires idle lobbies after 30 min.
- **Agent WS** `/ws/join/{code}`: `join` handshake with **required profile** and name validation → `joined` with corner assignment and `corner_token`; `invalid_code` / `match_full` / `profile_required` / `invalid_name` errors; first two valid joins take corners.
- **Lifecycle:** `lobby → in_progress → paused → finished | abandoned`. `POST /api/matches/{ref}/start` (host token, both corners filled) kicks off the tick loop.
- **Host token contract:** host-only REST actions require `X-Host-Token`; missing/invalid → `401`, unknown match → `404`, illegal lifecycle transition → `409`.
- **Match references:** REST endpoints accept **either `match_id` or `match_code`** (spec §8.1) — spectators hold only a code, and without this they cannot fetch a log or use Replay.
- **Tick loop:** 1-indexed ticks, broadcast `state_update` to both agents, resolve early when both moves arrive, apply Principle 6's no-move policy, broadcast `tick_result` to spectators, repeat until KO or `max_ticks`.
- **Pause/resume:** socket close → `match_paused` to the remaining agent and spectators, deadline clock stopped for both; `resume` with `corner_token` → re-issue the same tick with a fresh deadline, discarding any move already submitted for it; `resume_window_ms` exceeded → `abandoned`.
- **Spectator WS** `/ws/watch/{code}`: `lobby_update` on corner changes, then `match_start`, `tick_result`, `match_end`. Strip `reason` and token counts from the live stream.
- **Log:** write `{schema_version, match_start, ticks, match_end}` to `server/logs/{match_id}.json`, fsynced per tick; serve via `GET /api/matches/{ref}/log`.
- **Crash recovery:** on boot, rebuild unfinished matches by replaying their logs through `resolve()` and reopen them for resume (spec §6.4c).

**Gate:** integration test creates a match via REST, connects two mock agent WS clients + one spectator, starts, plays a full match, and asserts the message sequence and final log — including a deliberately-late move (→ no-move + penalty), a malformed move (→ no-move, `cause: malformed`), a dropped-and-reconnected client (→ pause, resume, same tick re-issued, no penalty), and a server restart mid-match (→ state rebuilt from log). Run with a short `deadline_ms` so the suite stays fast.

### Phase 3 — Built-in Bots (baseline ladder)

`bots.py` implements the ladder in spec §11 — `random`, `jab`, `cycle`, `frequency`, `mixer` — as asyncio tasks connecting through the **same public WS protocol**, no privileged internal path. All accept a deterministic seed. `POST /api/matches/{ref}/fill_bot {corner, bot, seed?}` spawns one into a corner.

Start with `random` and `jab`; `cycle`, `frequency`, and `mixer` can follow, but all five are needed before the ladder is a usable instrument.

**Gate:** automated integration test creates a match → fills both corners with bots via REST → starts → completes; asserts `match_end` reason, non-empty tick stream, and persisted log integrity including `schema_version` and both profiles.

**Balance check** (spec §13): run the full bot round-robin and confirm no strategy is degenerate. If always-jab dominates, tune stamina/damage in `rules.py`, mirroring the spec in the same commit. If pure aggression dominates once mixed strategies are in play, reduce trade damage to 50% — and change nothing else. Sweep riposte across 3–5 and settle the value here.

### Phase 3.5 — First LLM Agent (throwaway)

A bare LLM agent — no CLI polish, no published prompt, no documentation — pointed at Jab-bot and Random-bot. Its only job is to answer the questions that decide whether the rest of the plan is worth building:

- Does a real model beat the baselines, and by how much?
- What is the fallback rate at `deadline_ms: 5000`? At 30000?
- Do the no-move penalty economics behave sanely with a real, occasionally-slow client?
- Does the game discriminate at all between a strong model and a weak one?

**This phase exists because everything downstream is keyed to the answers.** The renderer pre-bakes one clip per action name; if balance testing changes the action table, those clips are re-authored. Finding out before Phase 4 costs a day; finding out after costs the animation work.

**Gate:** a written answer to each of the four questions above, backed by logs. If the game does not discriminate, fix the rules table before proceeding — that is a Phase 1 change, and it is much cheaper here than later.

### Phase 4 — Frontend (SPA + renderer)

Three views per spec §9:

1. **Home:** Create Match button + watch-code input.
2. **Lobby:** large copyable match code, ready-to-paste join command, two corner cards (Waiting… → agent name and profile via `lobby_update`), **Add built-in bot** on empty corners, **Start Fight** enabled only when both corners are filled, spectator share link.
3. **Arena:** two mirrored ADAM rigs; pre-bake one keyframe clip per action (8) per fighter plus a hit-react, a riposte, and a KO clip — preload all, no LRU cache needed at this scale. Clip queue so playback stays smooth when ticks resolve faster than clip duration. HUD: HP bars, stamina bars, points, penalties, tick counter. Paused overlay with resume countdown. End banner with winner + reason and a **Replay** button that fetches the log and feeds the same events through the same renderer code on a timer.

Clips are authored against the rig map and conventions in [`skeleton.md`](skeleton.md) — settle the conventions there (clip length, FPS, neutral stance) **before** authoring ten clips.

Keep the UI minimal and clean: one page, three states, no navigation maze, mobile-friendly enough to share a watch link in a group chat.

**Gate:** bot-vs-bot match created, filled, started, watched, and replayed entirely from the browser with no console errors; animations match the event log; a forced disconnect shows the paused overlay and recovers.

### Phase 5 — Reference LLM Agent (published)

`agents/reference_agent.py` (~120 lines): WS client with `--code` and `--url` args, prompt template, direct Anthropic SDK call, strict JSON parsing, required profile declaration.

- LLM call time-boxed to ~70% of `deadline_ms`; on timeout or parse failure fall back to `block_high` **with `telemetry.fallback = true`**.
- Prompt includes: rules summary, current state, 10-tick history, instruction to reply with a single JSON object.
- Emit `telemetry` (latency, tokens, a ≤200-char `reason`) on every move.
- This file doubles as validation that the spec alone is sufficient — if building it requires information not in the spec, the spec is incomplete: fix the spec.

**Gate** (rewritten in v3 — the v2 gates were not measuring what they claimed):

| Criterion | Threshold | Why this and not the old one |
|---|---|---|
| vs **Random-bot** | Wins ≥ 9 of 10 fixed-seed matches, *or* mean HP differential > +30 across 10 | The old "3 of 5" is passed by a coin flip **exactly 50%** of the time — under a null of no skill, P(≥3 of 5) = 0.5. It carried no information. Margin extracts far more signal per match than win count. |
| vs **Jab-bot** | **KO** within 40 ticks in ≥ 4 of 5 matches | The old gate measured rising defensive-action share, which rewards the *drawing* line: always-`block_high` vs Jab-bot ends 100–100 HP, 0–0 points, a draw. It also could only be measured in matches reaching tick 41, which are disproportionately the defensive ones — partly circular. Outcome is unambiguous and actually tests whether the agent found the trade insight. |
| **Fallback rate** | < 10% of ticks at the bracket being tested | A result produced mostly by hedges is not a measurement of the model. |
| **Validity** | 0 `malformed` or `invalid_action` no-moves across the gate runs | Instruction-following is part of the agent bundle being evaluated. |

### Phase 6 — Hosted Deployment (thin)

- Single process (uvicorn) serving API, WS, and static frontend; deploy to any small VPS/PaaS; TLS required (`wss://`).
- CORS locked to the frontend origin; match codes + 30-min expiry are the abuse control for MVP.
- Persist `server/logs/` on a real volume — it is the evaluation dataset and the crash-recovery substrate, not scratch space.
- Smoke test: friend on a different network joins with the reference agent script and completes a match against a built-in bot, including one deliberate disconnect and resume.

**Gate:** one real remote match with a friend, watched live via the share link, replayed after, and surviving a mid-match reconnect.

## Evaluation Design

Not a build phase — the conventions that make match results comparable. Most cost nothing if adopted from Phase 2; all are expensive to retrofit.

**Provenance.** Every match log carries both profiles and the full `config`. Pin `temperature` (0 unless variance is the thing under test), record the resolved model version rather than a floating alias, and version the prompt. Two runs of "the same" agent a month apart are not otherwise comparable, and nothing in the log would reveal it.

**Brackets.** Compare entrants only within the same `deadline_ms` (spec §12). A 5 s standard bracket and a 30 s deep bracket are different experiments, not one leaderboard.

**Corner swapping.** Run comparisons as paired matches with corners swapped. Resolution is symmetric so there should be no positional effect, but the cost is near zero and it forecloses the question.

**Isolating the model.** The default unit of evaluation is the whole agent. To attribute a difference to the *model* specifically, run control-vs-control: the reference agent's prompt and logic held fixed at a pinned `prompt_version`, only the model ID differing. State which comparison a given result is.

**Statistical power.** Distinguishing a 60% win rate from a coin flip at p < 0.05 takes roughly **100 matches**. At 60 ticks × 2 agents that is ~12,000 LLM calls, and at a 5 s deadline a worst-case match is 5 minutes of wall clock — 8+ hours serially before API cost. Plan comparisons around this, and prefer margin-based metrics (HP differential, ticks-to-KO) over win counts, which throw away most of the information in each match.

**Batch running.** `agents/harness/` — a loop over matches with concurrency, plus a results CSV keyed on profile and config. Because `resolve()` is pure and bots are ordinary WS clients, this is a script, not a subsystem. Build it when needed; the logs it consumes must be sufficient from Phase 2.

**Excluding contaminated matches.** Drop `abandoned` matches and any match exceeding the fallback-rate threshold from aggregates. Both are recorded distinctly in the log precisely so this is mechanical.

**Derived metrics worth extracting from logs.** Counter-accuracy (did the chosen defense counter the incoming attack), riposte rate, exploitation speed against each ladder bot, stamina efficiency (damage dealt per stamina spent), no-move rate by `cause`, and instruction-following rate. All are computable offline from stored logs without re-running a model — which is what Principle 3's purity buys.

## Explicitly Out of Scope (do not build)

Accounts/auth beyond match code + host token + corner token, ELO/rankings, tournaments, matchmaking, agent hosting, multiple rulesets or a ruleset selector UI, randomness/crits, database, replay library/browser (only per-match replay), horizontal scaling, live leaderboards.

**Changed in v3:** mid-match reconnects moved *into* scope (Principle 6, spec §6.4). This is the one deliberate scope increase, and it reverses v2's "do not build heartbeat or resume logic." It is justified by the evaluation goal: a match invalidated by a dropped socket is wasted API spend and a corrupted data point. Note it is genuinely new complexity — pause semantics, corner tokens, and log-replay recovery — and it is the highest-risk part of Phase 2.

The analysis layer (rankings, tournaments) stays out of scope, but the **log schema that would feed it is in scope from Phase 2**. That distinction is the whole strategy: build the dataset now, build the analysis whenever.

## Definition of Done (MVP)

1. A friend on another network can join a match with the reference agent script using only the spec + script + a match code, fight a built-in bot or another friend, and everyone can watch live and replay it.
2. [`arena_spec.md`](arena_spec.md) is accurate to the implementation — a stranger could build a working agent from it alone.
3. Engine test suite green, including table-invariant tests and the no-domination check.
4. Match logs replayable from the browser, and sufficient to answer "which agent, which model, under what config, and how did it decide" without re-running anything.
5. A mid-match disconnect pauses and resumes without advantaging either fighter; a server restart mid-match recovers from the log.
6. The reference agent clears the Phase 5 gates, and the ladder produces a defensible ordering of at least three distinct agents.
