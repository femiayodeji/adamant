# Spec notes from building the agents

Findings from writing `agents/reference_agent.py` and `agents/harness/` against
`docs/arena_spec.md` v0.4 and the Evaluation Design section of `docs/plan.md`.

The reference agent is supposed to double as a check that the spec alone is
sufficient. It mostly is: the protocol, the action table, and the resolution
matrix were enough to build a working client without guessing. The items below
are where I had to make a decision the spec does not make for me. **I did not
edit the spec** — every one of these is a proposal for whoever owns it.

Ordered roughly by how much they affect evaluation.

---

## 1. `temperature` cannot be pinned on the default model (blocks a plan convention)

Plan, Evaluation Design: *"Pin `temperature` (0 unless variance is the thing
under test)"*. Spec §6.1 lists `temperature` among the recommended profile keys.

`claude-opus-5` — the plan's own default model for the reference agent — **rejects
`temperature`, `top_p`, and `top_k` outright with HTTP 400**. There is no
sampling knob to pin. The reference agent therefore sends no temperature and
reports `"temperature": null` in its profile rather than reporting a number it
never sent.

What replaces it on that model family is `output_config.effort` and the thinking
mode, which is what actually determines how much reasoning was bought. Suggested
amendment: make the recommended profile keys
`{model, provider, prompt_version, agent_version}` plus *"whatever sampling or
effort controls were set, verbatim"* — and add an `effort`/`thinking` example.
Otherwise every log from this model family has a null in the one field the
evaluation conventions lean on.

The harness keeps `temperature` pinnable per model (`ModelSpec.temperature`), and
treats `None` as "do not send the parameter", because some OpenRouter models
accept it and some reject it.

## 2. The resolved model version is unknowable at join time

Plan: *"record the resolved model version rather than a floating alias"*.

`profile` is sent as the **first message on the socket** (§6.1), before any model
call has been made, so the agent can only report the alias it intends to use. The
resolved id (`response.model`) is not known until the first decision, one tick
later.

Three ways out, none of them currently in the spec:

- allow a `profile_update` agent→platform message, merged into the stored profile;
- carry `resolved_model` in `telemetry` on each `move` (cheap: it is already a
  per-move object, and the log keeps telemetry verbatim);
- have the platform record whatever the agent puts in `profile` and accept that
  the alias is all you get.

The harness fills `resolved_model` into its profile after the first call, which
helps for the *second* match onwards and not at all for the first. This is worth
fixing before matches are fought, because §7 says log fields cannot be backfilled.

## 3. Corners are assigned by join order, so corner swapping is racy

Plan: *"Run comparisons as paired matches with corners swapped."* §1 says the
first two agent connections with a valid code take the corners.

- Against a **built-in bot** this works: `fill_bot` names a corner, so filling the
  bot into the other corner first leaves ours deterministic. That is what
  `ArenaRunner.run_one` does.
- For **model vs. model** there is no way to choose a corner other than winning a
  race to connect. Over a network, with two agent processes, corner assignment is
  not reproducible — which is exactly what the corner-swapping convention exists
  to control for.

Suggested amendment: let `join` carry an optional `"corner"` request (rejected
with `corner_taken` if unavailable), or let `fill_bot`-style reservation apply to
external agents too.

## 4. A reconnecting agent is not told the match config or the opponent's name

§6.4b: on resume the platform sends `match_resumed`, then re-issues the same
`state_update`. It does **not** re-send `match_start`.

An agent that reconnects *in the same process* still holds `config` and
`opponent_name`. An agent whose process died and was restarted — the exact case
`corner_token` exists for — has neither. It can still play (`state_update` carries
`deadline_ms`), but it loses `max_ticks`, `resume_window_ms`, and the opponent's
name, all of which feed the prompt.

Suggested amendment: have `match_resumed` carry `you`, `opponent_name`, and
`config`, or simply re-send `match_start` before `match_resumed`. Cheap, and it
makes "agents are stateless by design" true across process restarts rather than
only across pauses.

## 5. `tick_result.moves` is undefined for no-moves and for exhausted actions

§7 shows `"moves": {"agent_a": "cross", "agent_b": "dodge_right"}`. Two cases are
not covered, and both matter to offline metrics:

- **No-move.** Is the value `null`, the string `"no_move"`, or the invalid action
  the agent submitted? For `cause: malformed` there may be no action name at all.
- **Unaffordable action converted to `idle` (§3).** Does `moves` record the
  attempted action or the `idle` it became? The `exhausted` event carries
  `attempted`, which suggests `moves` holds the conversion — but it is not stated.

`agents/harness/analyze.py` therefore derives everything from the **events**, not
from `moves`: a tick with a `no_move` event for a corner is not counted as an
answered tick, and a tick with an `exhausted` event costs that corner 0 stamina.
Stating the convention in §7 would make stamina-efficiency and counter-accuracy
numbers comparable between anyone's analysis code and mine.

## 6. The log's telemetry shape is implied, not specified

§6.1 defines `telemetry` on a `move`; §7 shows a *spectator* `tick_result` with a
reduced `telemetry` object and says `reason` and token counts are stripped "from
the live spectator stream (they remain in the log)". The log's own shape is only
described as "every `tick_result`".

I assumed the log stores the **full** telemetry under
`ticks[i].telemetry[corner]`, keyed by corner. Two Phase 5 gate criteria
(fallback rate, instruction-following) are computed from exactly that, so it is
worth one explicit sentence in §7.

## 7. Optional telemetry makes the fallback-rate gate unmeasurable

§6.1: *"`telemetry` is optional and never affects resolution."* Phase 5 gate:
*"Fallback rate < 10% of ticks."*

An agent that sends no telemetry has no measurable fallback rate, and the
aggregation cannot tell "never hedged" from "never reported". Right now
`aggregate.exclusion_reason` treats a missing `fallback_rate` as *not*
contaminated, which is the charitable reading and is probably wrong.

Suggested amendment: keep `telemetry` optional, but state that a match with no
`fallback` telemetry is ineligible for the gate — or make `fallback` the one
required telemetry field.

## 8. `{"model": "none"}` is a sentinel that reads as a real value

§6.1 suggests a non-LLM agent declare `{"model": "none", "agent_version": "heuristic@1.0"}`.
Anything that labels a corner by `profile.model` then labels every built-in bot
`none`, and the bot ladder collapses into one bucket. I hit this exact bug in
`analyze.label_of` and had to special-case `"none"`.

Suggested amendment: use `"model": null` in the example, or say explicitly that
`"none"` is a sentinel meaning "no model".

## 9. The standard bracket excludes thinking models more sharply than §12 implies

§10 says to time-box the model call to ~70% of `deadline_ms`; §12 says
`deadline_ms` is "a cap on how much reasoning an entrant is permitted to do".

Concretely, at the 5000 ms default that is a 3.5 s budget, and `claude-opus-5`
has adaptive thinking **on by default** — the standard bracket does not merely
constrain reasoning, it makes the default configuration of a current frontier
model unusable. The reference agent handles this by explicitly disabling thinking
below `DEEP_BRACKET_MS` (15000) and letting the model think at or above it.

Not a defect — §12 predicts precisely this — but the spec would be more useful to
a builder if it said outright: *in the standard bracket, turn extended thinking
off; in the deep bracket, turn it on, and never compare the two.*

## 10. Smaller things

- **Name regex.** `^[A-Za-z0-9 _-]{1,24}$` admits an all-whitespace name
  (`"    "`), which renders as an empty corner label. Harmless, easy to reject.
- **`resume` handshake.** §6.4b says a successful `resume` is answered with
  `match_resumed`. It does not say whether `joined` (and a refreshed
  `corner_token`) is sent as well. The agent loop tolerates either.
- **`fill_bot` timing.** §8 does not say whether a bot may be filled before any
  external agent has joined. The harness assumes yes — it must, to pin its own
  corner (finding 3).
- **Strict JSON parsing.** §10 says to fall back on "parse failure" without
  defining it. This agent is deliberately strict: exactly one JSON object, no
  markdown fences, no leading prose. A fenced reply is a fallback, and shows up in
  the instruction-following rate. A more forgiving parser would raise measured
  scores for every model without improving any agent, so strictness is the
  honest default — but it is a choice, and it should be stated so that two
  builders' numbers mean the same thing.
