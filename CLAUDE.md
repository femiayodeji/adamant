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
| Reference agent | `.venv/bin/python agents/reference_agent.py --code RUMBLE-4F2 --url ws://localhost:8000` |
| …as a chosen persona | add `--persona aggressor \| counter_puncher \| economist \| pattern_reader` |
| Run in Docker | `docker compose up -d --build` → <http://localhost:8000> |
| Stop Docker | `docker compose down` (keeps match logs) — `-v` also **deletes them** |

Deployment detail, including Render, is in [`docs/deploy.md`](docs/deploy.md). Two constraints there are not tuneable: **one worker, one instance** (the registry is in-memory and tick loops are asyncio tasks in-process), and **`/data/logs` must be a persistent volume** (it is both the evaluation dataset and the crash-recovery substrate).

Two bare invocations of the reference agent against the **same match code** deliberately pick *different* personas, so filling both corners by hand gives a real fight rather than a mirror match. `--persona` pins one; `--name` and `--model` still override. The persona is part of `prompt_version` (`ref-v2+aggressor-v1`) so two personas can never aggregate into one row.

`ARENA_LOG_DIR`, `ARENA_CORS_ORIGINS`, and `ARENA_LOG_LEVEL` configure the server. `.env` holds `OPENROUTER_API_KEY` (gitignored) — it serves the evaluation harness, not the reference agent, which uses `ANTHROPIC_API_KEY`.

**Phases 1–3 and 5 are built and gated; see the Build Status table in `docs/plan.md` for what is verified and what is not.** The gates that remain open all need something no test can supply: real model runs (3.5, 5) and a deployed host (6).

## Never spend API budget without asking

The suite stubs every model client, so `pytest` is free to run. Anything that would call Anthropic or OpenRouter for real — the Phase 3.5 questions, the Phase 5 gate, any harness batch — costs the user money and needs their go-ahead first. The Evaluation Design section of the plan puts a serious comparison at ~100 matches and ~12,000 LLM calls; that is not a decision to make on the user's behalf.

## What ADAM Arena is

A hosted platform where two LLM-powered agents fight under boxing rules. Agents are WebSocket **clients** that dial in with a match code (Kahoot/Jackbox model) — no hosting needed on the agent side. Agents submit **intents** (one of eight action names); the platform owns the action space, rules engine, tick loop, and Three.js renderer.

The unit of evaluation is the **agent** (prompt + model + logic as a bundle), not the model and not inference speed. Ticks resolve simultaneously, so a faster agent gains no in-game advantage. Discrimination between agents comes from **exploiting non-equilibrium opponents** — hence the built-in bot ladder is the measuring instrument, and model-vs-model play is the demo.

## Document hierarchy

- **`docs/agent_guide.md` — the front door for agent builders.** The 5-minute path, the rules explained rather than specified, the protocol with real captured payloads, writing an agent in any language, and testing free against the bot ladder. Point friends here; point *implementers* at the spec. It explains and links — it never restates a number that could drift.
- **`docs/arena_spec.md` (v0.6) — the contract.** Protocol, action space, damage/stamina values, resolution rules, message schemas, REST API, frontend views. Friends will build agents against this spec, not against the code. If implementation must deviate, **update the spec in the same commit**.
- **`docs/plan.md` (v3)** — how to build it: principles, tech stack, six gated phases, evaluation design, explicit non-goals.
- **`docs/review.md`** — the decision log. Findings are already applied to the other docs; this file records *why*, with a traceability table mapping each finding to where it landed. Read it before re-litigating a design choice — most obvious objections were raised and answered here on 2026-08-06.
- **`docs/skeleton.md`** — rig joint map and clip-authoring conventions. Consumed only by the Phase 4 renderer; orthogonal to rules and protocol.

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

## Working rules

- **Terminal before Three.js.** Bots must complete full matches (server + two bot clients) before any renderer work starts. Phase 3.5 (a throwaway LLM agent) exists specifically to catch rules-table changes *before* eleven animation clips are authored against those action names — clip name **is** the action name, with no mapping table.
- **Phases are gated.** Do not start a phase until the previous gate passes. Gates are written into `docs/plan.md` and are specific — e.g. Phase 1 requires table-invariant tests, including a **no-strictly-dominated-action** check (a coverage-only check misses the `dodge_right` bug that motivated riposte).
- **Out of scope, do not build:** accounts/auth beyond the three tokens, ELO/rankings, tournaments, matchmaking, agent hosting, ruleset selector, randomness/crits, database, replay library, horizontal scaling, leaderboards. The *log schema* that would feed an analysis layer is in scope from Phase 2; the analysis layer is not.
- **Settled vs. open tuning** is tracked in spec §13. The Phase 3 balance check has run and been re-run under the cap: always-`jab` is not dominant, so that trigger did not fire, and riposte stays at 5. Two things stay open — whether aggression dominates *once mixed strategies are in play* (`mostly-hook` is joint first, which is not dominance; if it ever is, reduce trade damage to 50% **and change nothing else**), and whether the repetition cap shifts the mixed-strategy equilibrium, which has only been checked against the bot ladder.
- **Built-in bots are repeat-aware and must stay that way.** A bot that voided out would stop probing what it exists to probe. `test_balance.py::test_no_bot_in_the_ladder_ever_voids_a_move` guards this. `ConstantBot` in the round-robin is the deliberate exception — its voiding *is* the measurement.
- **The published balance figures are load-bearing tests**, not documentation. `test_balance.py` asserts the 0–0 Jab-bot draw (now against `mostly-block_high`), the KO of a *literal* always-`block_high` at tick 30, the tick-28 hook-every-4th KO, and the tick-26 `dodge_right` KO; `test_server.py` re-asserts the tick-28 figure over real sockets. Re-derive them with `python -m server.engine.simulate` — never hand-edit. A rules change that moves them should move the spec too, deliberately.
- `/doc-review` (`.claude/commands/doc-review.md`) reviews the docs and writes results to `docs/review.md`.
