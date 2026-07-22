# ADAM Arena — Project Plan (v2)

## What This Is

ADAM Arena is a **hosted agent combat evaluation platform**. Two LLM-powered agents fight under boxing rules. The platform is the body and the referee; agents are only brains.

- The platform is **hosted online**; agent builders (Femi and friends) run agent scripts from anywhere and **dial in over WebSocket using a match code** — no hosting or public URL required on the agent side. This is the Kahoot/Jackbox model: create match → share code → agents join → start fight.
- Agents submit **intents** (action names like `jab`, `block_high`) — never animation or physics data. The platform owns the action space, rules engine, tick loop, and the Three.js rig that renders the fight (reused from the original ADAM project).
- Combat is **tick-based with simultaneous resolution**. Both agents get identical state, both submit within a deadline, moves resolve together via a deterministic matrix — so the evaluation measures strategy, not inference latency.
- Agents receive the last 10 ticks of history in every state update; **opponent modeling from history is the skill being measured.** Agents can be fully stateless.
- **Built-in bots** (Jab-bot, Random-bot) can fill any empty corner from the lobby, so the platform is testable solo and friends can fight the house.

The full protocol, action space, damage/stamina values, resolution rules, message schemas, REST API, and frontend views live in `adam-arena-spec.md` (v0.3). That spec is the source of truth. This plan is about *how to build it*.

## Guiding Principles

1. **No over-engineering.** MVP: match codes as the only credential (plus a host token for host-only actions), in-memory match registry (a dict), one JSON log file per match, no accounts, no rankings, no matchmaking, no database.
2. **Rules as data.** The rules engine is a generic resolver reading a `BOXING` config table (attacks, defenses, counters, stamina). A future combat type = new table + new keyframe clips, zero engine changes. Do NOT build a combat-type selector or plugin system now — import `BOXING` directly.
3. **Pure core, thin edges.** `resolve(state, move_a, move_b) -> (new_state, events)` is a pure function with zero I/O. WebSockets, timers, HTTP, and rendering are shells around it.
4. **Terminal before Three.js.** Built-in bots must complete full matches (server + two bot clients) before any renderer work starts.
5. **The spec is the contract.** If implementation must deviate from `adam-arena-spec.md`, update the spec in the same commit. Friends will build agents against the spec, not the code.
6. **Bounded connection handling.** No mid-match reconnects. Missed deadline / invalid / duplicate / wrong-tick move → forced `idle`. Three consecutive missed ticks → TKO. Disconnection is not a special case — it just produces missed ticks. Do not build heartbeat or resume logic.

## Tech Stack

| Component | Choice | Why |
|---|---|---|
| Platform server | Python, FastAPI, native WebSockets | Reuses ADAM backend patterns |
| Rules engine | Pure Python module, stdlib only | Testable, deterministic |
| Match registry | In-memory dict, 30-min lobby expiry | No database in MVP |
| Frontend | Single-page app: Home → Lobby → Arena; Three.js + existing ADAM rig | Reuse, not rebuild |
| Built-in bots | asyncio tasks using the same WS client code as external agents | One protocol, no privileged path |
| Reference agent | Python script (~100 lines), Anthropic API, `--code`/`--url` CLI args | Published example + integration test |
| Match logs | One JSON file per match | Powers replay |
| Tests | pytest | Engine fully covered before server work |

- **Frontend**: Next.js with TypeScript, built as a static export (`output: 'export'`), served by FastAPI as static files
- **Backend**: FastAPI (Python), managed as a `uv` project
- **Testing agents AI**: optional LiteLLM → OpenRouter (Cerebras for fast inference) path for local experiments and benchmarks; the published reference agent remains direct Anthropic for minimal dependencies


## Repository Layout

```
adam-arena/
├── docs/plan.md             # this file
├── adam-arena-spec.md       # protocol + rules source of truth (v0.3)
├── server/
│   ├── engine/
│   │   ├── rules.py         # BOXING table
│   │   ├── resolver.py      # pure resolve(), stamina, win conditions
│   │   └── tests/
│   ├── match.py             # match state machine: lobby → in_progress → finished
│   ├── registry.py          # in-memory matches, code generation, expiry
│   ├── bots.py              # Jab-bot, Random-bot as internal WS clients
│   ├── main.py              # FastAPI app: REST API + /ws/join + /ws/watch
│   └── logs/                # JSON match logs (gitignored)
├── agents/
│   └── reference_agent.py   # published LLM agent script (built last)
└── frontend/                # SPA: home, lobby, arena + Three.js renderer
```

## Build Phases

Each phase has a verification gate. Do not start a phase until the previous gate passes.

### Phase 1 — Rules Engine (pure, no I/O)

Implement `rules.py` (the `BOXING` data table) and `resolver.py`:
- `resolve(state, move_a, move_b)` per spec Section 4: correct counter negates, wrong defense/idle takes full damage, trades land both ways.
- Stamina: per-action costs, +5/tick passive regen, +15 extra on `idle`, cap 100, insufficient stamina → converted to `idle` with an `exhausted` event.
- Win conditions: KO at hp ≤ 0, double-KO draw, points decision at max ticks (60), tie → draw.
- Events output exactly as spec Section 4 (renderer and logs consume it).

Tests must include **table-invariant tests**, not just resolver tests:
- Every attack has ≥1 block counter and ≥1 dodge counter.
- No defense counters every attack.
- No action has zero/negative stamina cost except `idle`.
- Property-style check: any 60-tick match between two fixed strategies terminates with a valid result.

**Gate:** `pytest` green; a scripted simulation (no sockets) runs always-jab vs. always-block_high for 60 ticks and produces a sane points decision.

### Phase 2 — Match Server (lifecycle + protocol)

FastAPI app implementing spec Sections 6–8:
- **Registry:** `POST /api/matches` generates a short human-friendly code (`WORD-XXX`), stores the match in memory with a `host_token`, expires idle lobbies after 30 min.
- **Agent WS** `/ws/join/{code}`: `join` handshake → `joined` with corner assignment; `match_full` / `invalid_code` errors; first two valid joins take corners.
- **Lifecycle:** `lobby → in_progress → finished`. `POST /api/matches/{id}/start` (host token required, both corners filled) kicks off the tick loop.
- **Host token contract:** host-only REST actions require `X-Host-Token: <token>` header; missing/invalid token returns `401`; valid token but illegal lifecycle transition returns `409`.
- **Tick loop:** broadcast `state_update` to both agents, 5000 ms deadline, resolve early when both moves arrive, enforce Principle 6's forced-idle/TKO policies, broadcast `tick_result` to spectators, repeat until KO/points/TKO.
- **Spectator WS** `/ws/watch/{code}`: `lobby_update` on corner changes, then `match_start`, `tick_result`, `match_end`.
- **Log:** write full match log (match_start payload, every tick_result, match_end) to `server/logs/{match_id}.json`; serve via `GET /api/matches/{id}/log`.
- **Log schema:** include `schema_version` in every log file (start at `1`) so replay parsing stays forward-compatible.

**Gate:** integration test creates a match via REST, connects two mock agent WS clients + one spectator, starts, plays a full match, asserts the message sequence (including a deliberately-late move → forced idle, and a dropped client → TKO) and the final log file.

### Phase 3 — Built-in Bots

`bots.py`: Jab-bot (always `jab`) and Random-bot (random valid action), implemented as asyncio tasks that connect through the **same public WS protocol** — no privileged internal path. `POST /api/matches/{id}/fill_bot {corner, bot}` spawns one into a corner. Random-bot must accept an optional deterministic seed for tests.

**Gate:** automated integration test creates a match → fills both corners with bots via REST → starts → completes full match; asserts `match_end` reason, non-empty tick stream, and persisted log integrity (including `schema_version`). **Balance check:** if always-jab dominates everything, tune stamina/damage in `rules.py` (mirroring the spec in the same commit). Reduce trade damage to 50% only if aggression proves dominant — change nothing else.

### Phase 4 — Frontend (SPA + renderer)

Three views per spec Section 9:
1. **Home:** Create Match button + watch-code input.
2. **Lobby:** large copyable match code, ready-to-paste join command, two corner cards (Waiting… → agent name via `lobby_update`), **Add built-in bot** on empty corners, **Start Fight** enabled only when both corners are filled, spectator share link.
3. **Arena:** two mirrored ADAM rigs; pre-bake one keyframe clip per action (8) per fighter plus a hit-react and a KO clip — preload all, no LRU cache needed at this scale. Clip queue so playback stays smooth when ticks resolve faster than clip duration. HUD: HP bars, stamina bars, points, tick counter. End banner with winner + reason and a **Replay** button that fetches the log and feeds the same events through the same renderer code on a timer.

Keep the UI minimal and clean: one page, three states, no navigation maze, mobile-friendly enough to share a watch link in a group chat.

**Gate:** bot-vs-bot match created, filled, started, watched, and replayed entirely from the browser with no console errors; animations match the event log.

### Phase 5 — Reference LLM Agent

`agents/reference_agent.py` (~100 lines): WS client with `--code` and `--url` args, prompt template, Anthropic API call, strict JSON parsing.
- LLM call hard-timeout at 3.5 s; on timeout or parse failure fall back to `block_high`.
- Prompt includes: rules summary, current state, 10-tick history, instruction to reply with a single JSON object.
- This file doubles as validation that the spec alone is sufficient — if building it requires information not in the spec, the spec is incomplete: fix the spec.

**Gate:** reference agent beats Random-bot in at least 3/5 matches (fixed seeds), and versus Jab-bot shows adaptation by increasing defensive-action share by at least 20% between ticks 1–20 and ticks 41–60 in at least 3/5 matches.

### Phase 6 — Hosted Deployment (thin)

- Single process (uvicorn) serving API, WS, and static frontend; deploy to any small VPS/PaaS; TLS required (`wss://`).
- CORS locked to the frontend origin; match codes + 30-min expiry are the abuse control for MVP.
- Smoke test: friend on a different network joins with the reference agent script and completes a match against a built-in bot.

**Gate:** one real remote match with a friend, watched live via the share link, replayed after.

## Explicitly Out of Scope (do not build)

Accounts/auth beyond match code + host token, ELO/rankings, tournaments, matchmaking, agent hosting, mid-match reconnects, multiple rulesets or a ruleset selector UI, randomness/crits, database, replay library/browser (only per-match replay), horizontal scaling.

## Definition of Done (MVP)

1. A friend on another network can join a match with the reference agent script using only the spec + script + a match code, fight a built-in bot or another friend, and everyone can watch live and replay it.
2. `adam-arena-spec.md` is accurate to the implementation — a stranger could build a working agent from it alone.
3. Engine test suite green, including table-invariant tests.
4. Match logs replayable from the browser.
