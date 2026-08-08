# Adamant — Project Plan (v4)

Two LLM-powered agents fight under boxing rules. The platform is the body and the referee; agents are only brains, dialling in over WebSocket with a match code (Kahoot/Jackbox model — no hosting on the agent side).

**The unit of evaluation is the agent** — prompt + model + logic as a bundle. Not the model, and not inference speed: ticks resolve simultaneously, so a faster agent gains no in-game advantage. "Agent" and "model" are distinct terms throughout; two agents may share a model, and a comparison that isolates the model requires holding prompt and logic fixed.

The contract is [`adamant_spec.md`](adamant_spec.md) — protocol, action space, values, resolution rules, schemas, REST API. The design invariants and the do-not-build list live in `CLAUDE.md`. **This file is what remains unfinished, plus the evaluation conventions that make results comparable.**

## Build Status

_Updated 2026-08-07. Phases 1–6 are implemented; three gates need something no test can supply._

| Phase | State | Gate |
|---|---|---|
| 1 — Rules engine | Built | **Passed.** Table invariants incl. no-domination and the repetition cap at match scale; published figures reproduced. |
| 2 — Match server | Built | **Passed.** Full-match integration test with late move, malformed move, disconnect/resume, and server restart. |
| 3 — Built-in bots | Built | **Passed.** All five ladder bots complete matches over the public protocol; logs carry both profiles. |
| 3.5 — First LLM agent | **Blocked** | Needs real model runs — **awaiting go-ahead on API spend.** Four questions below. |
| 4 — Frontend | Built | **Passed.** Headless Chrome against a live server: match created, filled, started, watched to a KO, replayed from the log, forced disconnect showing the paused overlay and recovering. Zero console errors. |
| 5 — Reference agent | Built | **Not evaluated.** All four criteria need live models; the agent itself is verified against a real server with a stubbed model. |
| 6 — Deployment | Image built | **Not passed.** Container verified locally end to end — healthy in ~8s, non-root, 169 MB, full bot match through the public API, logs surviving `down`/`up`, graceful SIGTERM. The gate needs a real host and a friend on another network. `render.yaml` drafted, never deployed. |

**Balance figures** — re-derive with `python -m server.engine.simulate`, never hand-edit. These are asserted as tests (`test_balance.py`, `test_server.py`), so a rules change that moves them must move the spec too, deliberately.

| Matchup | Result | What it shows |
|---|---|---|
| `mostly-block_high` vs Jab-bot | 0–0 draw at tick 60, both at 100 HP | The trap holds: blocking correctly is not winning |
| always-`block_high` vs Jab-bot | KO **against the blocker**, tick 30 | Under the cap a literal one-note defense voids from tick 4 and is walked through |
| hook-every-4th vs Jab-bot | KO at tick 28, 100/−5 HP, 105/0 points | The winning line — the trade insight. Unchanged by the cap; it never repeats four times running |
| `mostly-dodge_right` vs Jab-bot | KO at tick 26 | Not a dead action; riposte 5 is doing its job |
| always-`jab` vs always-`idle` | 15 total damage over 60 ticks | The cap working: three punches land and the strategy is finished |

**Balance check (Phase 3, re-run under the v0.6 cap):** always-`jab` is *not* dominant — 10W/28L in a 22-entrant round-robin — so the stamina/damage tuning trigger does not fire. `frequency`, `mostly-hook` and `cycle-5` tie at 38W: the exploiters beat the exploitable, which is the intended shape. Every literal one-action entrant sits at or near the bottom, and the four one-note *defenses* win nothing at all. Open items are tracked in spec §13.

**Two renderer behaviours, neither a defect:**

- Playback is paced by *animation*, not tick arrival. Bots resolve a tick in milliseconds, so the arena shows "+19 queued" early in a bot match and finishes seconds after the match ended. The end banner waits for playback to catch up.
- A spectator joining mid-match replays from tick 1 rather than jumping to live, because the server sends full tick history on connect. Fine at 20 ticks; a late joiner to a 60-tick match is a long way behind. The fix, if wanted, belongs in the renderer's catch-up policy — not the server. Note that this applies to *every* connection, including a reconnect, so a client has to hold a high-water mark on the tick number; without one a dropped socket re-fed a fight already on screen, doubling the event log and standing a knocked-out fighter back up.

## Gates Still Open

### Phase 3.5 — first LLM agent (blocked on API spend)

A bare LLM agent pointed at Jab-bot and Random-bot. Its only job is to answer four questions, because everything downstream is keyed to them:

1. Does a real model beat the baselines, and by how much?
2. What is the fallback rate at `deadline_ms: 5000`? At 30000?
3. Do the no-move penalty economics behave sanely with a real, occasionally-slow client?
4. Does the game discriminate at all between a strong model and a weak one?

**Gate:** a written answer to each, backed by logs. If the game does not discriminate, fix the rules table — a Phase 1 change, far cheaper here than after the clips are authored against the action names.

### Phase 5 — reference agent evaluation

The agent is built (`agents/reference_agent.py`); these thresholds have never been run against a live model.

| Criterion | Threshold | Why this and not the obvious one |
|---|---|---|
| vs **Random-bot** | Wins ≥ 9 of 10 fixed-seed matches, *or* mean HP differential > +30 across 10 | "3 of 5" is passed by a coin flip **exactly 50%** of the time — under a null of no skill, P(≥3 of 5) = 0.5. It carries no information. Margin extracts far more signal per match than win count. |
| vs **Jab-bot** | **KO** within 40 ticks in ≥ 4 of 5 matches | Measuring rising defensive-action share rewards the *drawing* line — `block_high` vs Jab-bot ends 100–100, a draw. It is also only measurable in matches reaching tick 41, which are disproportionately the defensive ones, so it is partly circular. Outcome is unambiguous and actually tests for the trade insight. |
| **Fallback rate** | < 10% of ticks in the bracket tested | A result produced mostly by hedges is not a measurement of the model. |
| **Validity** | 0 `malformed` or `invalid_action` no-moves across the gate runs | Instruction-following is part of the agent bundle being evaluated. |

### Phase 6 — deployment

**Gate:** one real remote match with a friend, watched live via the share link, replayed after, and surviving a mid-match reconnect. Deployment mechanics are in [`deploy.md`](deploy.md).

## Evaluation Design

Not a build phase — the conventions that make match results comparable. All are cheap now and expensive to retrofit.

**Provenance.** Every log carries both profiles and the full `config`. Pin `temperature` (0 unless variance is the thing under test), record the resolved model version rather than a floating alias, and version the prompt. Two runs of "the same" agent a month apart are not otherwise comparable, and nothing in the log would reveal it.

**Brackets.** Compare entrants only within the same `deadline_ms` (spec §12). A 5 s standard bracket and a 30 s deep bracket are different experiments, not one leaderboard.

**Corner swapping.** Run comparisons as paired matches with corners swapped. Resolution is symmetric so there should be no positional effect, but the cost is near zero and it forecloses the question.

**Isolating the model.** The default unit is the whole agent. To attribute a difference to the *model*, run control-vs-control: the reference agent's prompt and logic fixed at a pinned `prompt_version`, only the model ID differing. State which comparison a result is.

**Statistical power.** Distinguishing a 60% win rate from a coin flip at p < 0.05 takes roughly **100 matches**. At 60 ticks × 2 agents that is ~12,000 LLM calls, and at a 5 s deadline a worst-case match is 5 minutes of wall clock — 8+ hours serially before API cost. Prefer margin-based metrics (HP differential, ticks-to-KO) over win counts, which throw away most of the information in each match.

**Excluding contaminated matches.** Drop `abandoned` matches and any match over the fallback-rate threshold from aggregates. Both are recorded distinctly in the log precisely so this is mechanical.

**Derived metrics, all computable offline from stored logs** — which is what `resolve()`'s purity buys: counter-accuracy (did the chosen defense counter the incoming attack), riposte rate, exploitation speed against each ladder bot, stamina efficiency (damage per stamina spent), no-move rate by `cause`, void rate, and instruction-following rate.

## Scope

The do-not-build list is in `CLAUDE.md`. Two notes on it that are decisions rather than rules:

- **Mid-match reconnects are deliberately in scope**, reversing an earlier "do not build heartbeat or resume logic". A match invalidated by a dropped socket is wasted API spend and a corrupted data point. It is genuine complexity — pause semantics, corner tokens, log-replay recovery — and was the highest-risk part of Phase 2.
- **The analysis layer stays out of scope, but the log schema that would feed it has been in scope since Phase 2.** That distinction is the whole strategy: build the dataset now, build the analysis whenever.

## Definition of Done (MVP)

1. A friend on another network joins with the reference agent using only the spec, the script, and a match code; fights a bot or another friend; everyone can watch live and replay. *(Needs Phase 6.)*
2. [`adamant_spec.md`](adamant_spec.md) is accurate to the implementation — a stranger could build a working agent from it alone. ✓
3. Engine suite green, including table invariants and the no-domination check. ✓
4. Logs replayable from the browser and sufficient to answer "which agent, which model, under what config, and how did it decide" without re-running anything. ✓
5. A mid-match disconnect pauses and resumes without advantaging either fighter; a server restart recovers from the log. ✓
6. The reference agent clears the Phase 5 gates, and the ladder produces a defensible ordering of at least three distinct agents. *(Needs Phase 3.5 and 5.)*
