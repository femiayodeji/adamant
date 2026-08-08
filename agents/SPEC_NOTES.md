# Spec notes from building the agents

Open gaps found while writing `agents/reference_agent.py` and `agents/harness/`
against `docs/adamant_spec.md`. The reference agent doubles as a check that the spec
alone is sufficient; it mostly is. These are the places where I still have to make
a decision the spec does not make for me — **proposals for whoever owns the spec**,
not edits to it.

Findings that have since been written into the spec are not repeated here; the
spec is the record. Ordered by how much they affect evaluation.

## 1. `temperature` cannot be pinned on the default model

Plan, Evaluation Design: *"Pin `temperature`"*. Spec §6.1 lists it among the
recommended profile keys.

`claude-opus-5` — the reference agent's own default — **rejects `temperature`,
`top_p` and `top_k` with HTTP 400**. There is no sampling knob to pin. The agent
sends none and reports `"temperature": null` rather than a number it never sent.
What actually governs reasoning spend on that family is `output_config.effort` and
the thinking mode.

**Suggested:** make the recommended keys `{model, provider, prompt_version,
agent_version}` plus *"whatever sampling or effort controls were set, verbatim"*,
with an `effort`/`thinking` example. Otherwise every log from this model family
carries a null in the one field the evaluation conventions lean on.

The harness keeps `temperature` pinnable per model (`ModelSpec.temperature`) and
treats `None` as "do not send the parameter", because some OpenRouter models
accept it and some reject it.

## 2. The resolved model version is unknowable at join time

Plan: *"record the resolved model version rather than a floating alias"*. But
`profile` is the **first message on the socket** (§6.1), sent before any model call,
so an agent can only report the alias it intends to use. The resolved id
(`response.model`) is not known until the first decision, one tick later.

**Suggested**, none currently in the spec: allow a `profile_update` message merged
into the stored profile; or carry `resolved_model` in per-move `telemetry`, which
the log already keeps verbatim; or state that the alias is all you get.

The harness fills `resolved_model` in after the first call, which helps from the
*second* match onward and not at all for the first. Worth settling before matches
are fought in anger — §7 log fields cannot be backfilled.

## 3. Optional telemetry makes the fallback-rate gate unmeasurable

§6.1: *"`telemetry` is optional and never affects resolution."* Phase 5 gate:
*"Fallback rate < 10% of ticks."*

An agent that sends no telemetry has no measurable fallback rate, and aggregation
cannot tell "never hedged" from "never reported". `aggregate.exclusion_reason`
currently treats a missing `fallback_rate` as *not* contaminated — the charitable
reading, and probably the wrong one.

**Suggested:** keep `telemetry` optional, but state that a match with no `fallback`
telemetry is ineligible for the gate — or make `fallback` the one required field.

## 4. `{"model": "none"}` is a sentinel that reads as a real value

§6.1 suggests a non-LLM agent declare `{"model": "none", "agent_version":
"heuristic@1.0"}`. Anything that labels a corner by `profile.model` then labels
every built-in bot `none` and the whole ladder collapses into one bucket. This bug
hit `analyze.label_of`, which now special-cases `"none"`.

**Suggested:** use `"model": null` in the example, or say outright that `"none"` is
a sentinel meaning "no model".

## 5. The standard bracket excludes thinking models more sharply than §12 implies

§10 says to time-box the model call to ~70% of `deadline_ms`; §12 calls
`deadline_ms` a cap on permitted reasoning. Concretely, at the 5000 ms default that
is a 3.5 s budget, and `claude-opus-5` has adaptive thinking **on by default** — so
the standard bracket does not merely constrain reasoning, it makes the default
configuration of a current frontier model unusable. The reference agent disables
thinking below `DEEP_BRACKET_MS` (15000) and lets the model think at or above it.

Not a defect — §12 predicts exactly this — but the spec would serve a builder
better if it said outright: *in the standard bracket turn extended thinking off, in
the deep bracket turn it on, and never compare the two.*

## 6. Smaller things

- **Name regex.** `^[A-Za-z0-9 _-]{1,24}$` admits an all-whitespace name (`"    "`),
  which renders as an empty corner label. Harmless, easy to reject.
- **`fill_bot` timing.** §8 does not say whether a bot may be filled before any
  external agent has joined. The harness assumes yes — it must, to pin its own
  corner for the corner-swapping convention.
- **Strict JSON parsing.** §10 says to fall back on "parse failure" without defining
  it. This agent is deliberately strict: exactly one JSON object, no markdown
  fences, no leading prose. A fenced reply is a fallback and shows up in the
  instruction-following rate. A more forgiving parser would raise measured scores
  for every model without improving any agent, so strictness is the honest default
  — but it is a choice, and stating it is what makes two builders' numbers mean the
  same thing.

## Settled deliberately, not a gap

An action the **model chose** is sent as submitted even when it will void under the
repetition cap (§4.6). The prompt states the cap and reports the current streak, so
walking into it is a decision the agent made; repairing it locally would launder
the instruction-following the platform exists to measure. Only the *fallback* hedge
is cap-aware (`rules.fallback_action`), because a hedge that voids is the inverse
of a hedge — see spec §10, which now specifies the two-action fallback.
