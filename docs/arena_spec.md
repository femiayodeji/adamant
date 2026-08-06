# ADAM Arena — Combat Protocol & Rules Spec (MVP v0.4)

A hosted platform where two LLM agents fight under boxing-style rules. The platform owns the rig, animations, rules engine, and referee logic. Agents own only their decision-making (model + prompt + logic).

**Connection model:** agents are **WebSocket clients that dial into the platform using a match code**. Agent builders run a script from anywhere (laptop, NAT, no hosting needed). Only the platform is publicly hosted. Spectators watch via a share link on the same WebSocket infrastructure.

**What this platform measures:** the **agent** — the bundle of prompt + model + logic — reasoning about combat. Not inference speed. See §12 for how the deadline interacts with that goal.

> **Changes in v0.4** (from the 2026-08-06 documentation review, `docs/review.md`):
> agent profiles are now required at join (§6.1); a missing move is **penalised** rather than converted to `idle` (§4, §5); matches are **pausable and resumable** across infrastructure failure (§6.4); the TKO rule is **removed**; successful dodges now **riposte** for chip damage, which fixes `dodge_right` being a strictly dominated action (§2); tick numbering, `final_state`, draw semantics, and the `history` event encoding are now defined; `max_ticks` and `deadline_ms` are settable per match (§8).

---

## 1. Core Concepts

| Term | Meaning |
|---|---|
| **Match** | One fight between two agents. Created in the UI; identified by a short **match code** (e.g. `RUMBLE-4F2`). |
| **Tick** | One decision round. Platform broadcasts state to both agents; both must submit a move before the deadline; moves resolve simultaneously. |
| **Agent** | A participant-run script/process that connects out to the platform via WebSocket with the match code. Any language. The unit of evaluation is the whole agent — prompt, model, and logic together. |
| **Built-in bot** | Platform-hosted baseline agent (see §11) that can fill an empty corner from the lobby. Runs as an internal client using the same protocol. |
| **Spectator** | Read-only WebSocket connection (the arena page). |
| **Corner** | `agent_a` or `agent_b`. Assigned on join, held for the whole match, reclaimable after a disconnect with a `corner_token`. |

**Design rules:**

- Agents submit *intents* (action names), never animation or physics data.
- The match code is the join credential. The first two agent connections with a valid code take the corners.
- Agents are **stateless by design**: every `state_update` carries everything needed to decide. An agent that keeps no local state between ticks is fully supported, and is unaffected by a pause/resume (§6.4).
- **A missed move is a penalty, not a free action** (§4.4). Silence is never a viable strategy.
- **Infrastructure failure pauses the match; it does not decide it** (§6.4).

### 1.1 Party references

Two vocabularies, deliberately:

- **Agent-facing messages** (§6) are *egocentric*: parties are `"self"` and `"opponent"`. An agent never has to track which corner it is in order to read its own history.
- **Spectator-facing messages and the match log** (§7) are *absolute*: parties are `"agent_a"` and `"agent_b"`.

Event objects (§4.5) are otherwise identical in both. `match_start` tells each agent its corner via `you`, for display and logging.

---

## 2. Action Space

Eight actions. Every `move` message must contain exactly one.

### Attacks

| Action | Damage | Stamina cost | Notes |
|---|---|---|---|
| `jab` | 5 | 5 | Fast probe |
| `cross` | 10 | 10 | Straight power shot |
| `hook` | 15 | 15 | Big committed swing |

### Defenses

| Action | Stamina cost | Counters | On success |
|---|---|---|---|
| `block_high` | 3 | `jab`, `cross` | Negates damage |
| `block_low` | 3 | `hook` | Negates damage |
| `dodge_left` | 8 | `cross`, `hook` | Negates damage **+ riposte 5** |
| `dodge_right` | 8 | `jab` | Negates damage **+ riposte 5** |

**Riposte:** a defense that correctly evades an incoming attack deals **5 damage** back to the attacker, and that 5 is added to the dodger's points. Blocks do not riposte.

This is the economic difference between blocks and dodges. Blocks are cheap and safe; dodges cost more, and are only worth it when you read the attack correctly — the payoff for prediction, which is the skill this platform measures.

> **Why riposte exists.** Without it, `dodge_right` is strictly dominated: `block_high` counters everything `dodge_right` counters *plus* `cross`, at less than half the stamina. A rational agent would never pick it, silently reducing an eight-action space to seven. Simulation of the v0.3 table confirms this (a `dodge_right` bot loses to Jab-bot on points; with riposte it KOs Jab-bot). See §11 for the balance status of the number itself.

### Other

| Action | Effect |
|---|---|
| `idle` | Regenerates **+15 on top of the +5 passive regen (+20 total)**. No defense — takes full damage if hit. Costs no stamina. |

Every attack has exactly two counters (one block, one dodge). The matrix is arbitrary but published — reading it and predicting the opponent *is* the game.

---

## 3. Fighter State

Each fighter starts with:

```json
{
  "hp": 100,
  "stamina": 100,
  "points": 0,
  "penalties": 0
}
```

- **Stamina regen:** +5 per tick automatically, +15 extra on `idle` (total +20 that tick). Capped at 100. Stamina is never negative.
- **Insufficient stamina:** a submitted action costing more than available stamina is converted to `idle` — including the full `idle` regen — and emits an `exhausted` event. This is *not* a no-move and carries **no penalty**: the agent submitted a legal move on time and simply could not afford it.
- **Points:** all landed damage, including riposte damage (§2), is added to the dealer's points.
- **Penalties:** accumulated from no-moves (§4.4). Points and penalties are tracked separately so that `points` keeps its plain meaning — damage dealt — and the penalty remains auditable in the log.
- **Decision score** (used only when no KO occurs) = `points − penalties`. It may be negative.

---

## 4. Resolution Rules (simultaneous)

Given `move_a` and `move_b`, resolve per attack independently.

### 4.1 Order of operations within a tick

1. Convert unaffordable actions to `idle` (§3), emitting `exhausted`.
2. Convert absent/invalid moves to `no_move` (§4.4), emitting `no_move` and applying the penalty.
3. Deduct stamina costs for both fighters.
4. Resolve both attacks against the opposing action (§4.2), including riposte.
5. Apply passive regen (+5), plus the `idle` bonus (+15) where applicable. Cap at 100.
6. Check win conditions (§5).

Damage is computed from the state at the *start* of the tick, so resolution is genuinely simultaneous: a fighter reduced to 0 HP still lands the attack it threw that tick.

### 4.2 Attack outcomes

| Situation | Outcome |
|---|---|
| Attack vs. correct **block** | 0 damage. Attacker still pays stamina. `blocked` event. |
| Attack vs. correct **dodge** | 0 damage. Defender ripostes 5 to the attacker, added to defender's points. `dodged` event. |
| Attack vs. wrong defense, `idle`, or `no_move` | Full damage lands. `hit` event. |
| Attack vs. attack (trade) | Both land at full damage. Two `hit` events. |
| Defense vs. defense, `idle`, or `no_move` | Nothing happens. No riposte — a riposte requires an incoming attack to evade. |

### 4.3 Purity

Resolution is a pure function — no randomness, no I/O:

```
resolve(state, move_a, move_b) -> (new_state, events)
```

This is load-bearing beyond testability: because it is deterministic, a stored match log can be replayed to reconstruct exact state (used for crash recovery, §6.4c) and re-scored offline against metrics that did not exist when the match was fought.

### 4.4 No-move

A fighter has made a **no-move** for tick *N* when, by the deadline, the platform has not received a valid `move` for tick *N* on an open connection. Causes, each logged distinctly:

| `cause` | Meaning |
|---|---|
| `timeout` | Connection open, nothing arrived before the deadline |
| `malformed` | Not valid JSON, or not a `move` message |
| `invalid_action` | Action name not in the eight of §2 |
| `wrong_tick` | `tick` field does not match the current tick |
| `duplicate` | A move for this tick was already accepted (the first is kept) |

A no-move resolves as follows:

- The fighter is **defenseless**: it takes full damage from any incoming attack and deals none.
- It receives **only the +5 passive regen** — never the `idle` bonus.
- It accrues a **penalty of 15** (see §11 for why 15).

A no-move is therefore strictly worse than every real action, including a badly-chosen one. There is never an incentive to stay silent, and a slow agent cannot be scored as a defensive strategist.

**A closed connection is not a no-move.** If the socket is closed, the tick does not resolve at all — the match pauses (§6.4). This split is what separates *the agent failed* from *the infrastructure failed*.

### 4.5 Event schema

`events` is an ordered list that the renderer, the log, and the agent-facing `history` all consume. Every event names its parties explicitly — no subject is ever left to be inferred.

```json
[
  {"type": "hit",       "attacker": "agent_a", "defender": "agent_b", "action": "cross", "damage": 10},
  {"type": "blocked",   "attacker": "agent_b", "defender": "agent_a", "action": "jab",   "defense": "block_high"},
  {"type": "dodged",    "attacker": "agent_a", "defender": "agent_b", "action": "hook",  "defense": "dodge_left", "riposte": 5},
  {"type": "exhausted", "who": "agent_b", "attempted": "hook", "stamina": 4},
  {"type": "no_move",   "who": "agent_b", "cause": "timeout", "penalty": 15},
  {"type": "ko",        "who": "agent_b"}
]
```

In agent-facing messages the party values are `"self"` and `"opponent"` instead of corner IDs (§1.1). Field sets are otherwise identical.

---

## 5. Match Rules

**Ticks are 1-indexed.** A match runs ticks `1 … max_ticks` inclusive.

| Rule | Value (MVP default) |
|---|---|
| Max ticks | 60 |
| Move deadline | 5000 ms per tick (configurable per match, §8) |
| Missing / invalid / duplicate / wrong-tick move | `no_move`: defenseless, +5 regen only, **penalty 15** (§4.4) |
| Unaffordable move | Converted to `idle` with full idle regen, no penalty (§3) |
| KO | HP ≤ 0 → opponent wins immediately |
| Double KO (trade to ≤ 0) | Draw |
| No KO after `max_ticks` | Higher **decision score** (`points − penalties`) wins; equal → draw |
| Disconnect | Match **pauses** and awaits resume (§6.4) — it is not a loss |
| Resume window exceeded | Match ends `abandoned` (§5.1) |

There is **no TKO rule.** It was removed in v0.4 as redundant: an agent that stops deciding is already defenseless and accruing penalties, so it loses on damage or on the decision score without a special case. Removing it also stops network failures from being recorded as combat losses.

### 5.1 End reasons

| `reason` | `winner` | Meaning |
|---|---|---|
| `ko` | corner | Opponent's HP hit 0 |
| `points` | corner | `max_ticks` reached, higher decision score |
| `draw` | **`null`** | Double KO, or equal decision score at `max_ticks`, or both corners abandoned |
| `abandoned` | corner or `null` | A corner failed to return within the resume window (§6.4). The remaining fighter is named as `winner`; if both are gone, `winner` is `null`. |

`winner` is **`null`** — not `"draw"`, not absent — whenever no single corner won. Clients may rely on the key always being present.

`abandoned` exists specifically so that infrastructure failure is distinguishable from combat in the log, and can be excluded from evaluation aggregates.

---

## 6. Agent WebSocket Protocol

Endpoint: `wss://<host>/ws/join/{match_code}`

All messages are JSON with a `type` field.

### 6.1 Agent → Platform

**`join`** — first message after connecting.

```json
{
  "type": "join",
  "name": "FemiBot-v1",
  "profile": {
    "model": "claude-opus-5",
    "provider": "anthropic",
    "prompt_version": "ref-v1",
    "temperature": 0,
    "agent_version": "femibot@1.2.0"
  }
}
```

- **`name`** — display name. Must match `^[A-Za-z0-9 _-]{1,24}$`; otherwise `{"type":"error","reason":"invalid_name"}` and close.

  > The name is rendered to spectators and sent to the opponent as `opponent_name`, where it reaches the opponent's prompt. It is the only free-text channel between adversaries — everything else crossing the boundary is drawn from the closed vocabulary of eight action names and numeric state. Restricting the character set closes a prompt-injection vector that would otherwise let an agent win matches for reasons unrelated to combat. If you later want injection-resistance to be an evaluation axis, relax this deliberately and document it as in-bounds.

- **`profile`** — **required.** Provenance for the match log. Missing or empty → `{"type":"error","reason":"profile_required"}` and close.

  The platform stores it verbatim and never parses, validates, or acts on it; any additional keys are preserved. All fields are **self-reported and unverified**, which is adequate for a friends-and-benchmarks arena and is *not* a basis for public leaderboard claims.

  Recommended keys: `model`, `provider`, `prompt_version`, `temperature`, `agent_version`. A non-LLM agent should still declare a profile (e.g. `{"model": "none", "agent_version": "heuristic@1.0"}`).

**`move`** — response to each `state_update`. Must echo the tick number.

```json
{
  "type": "move",
  "tick": 12,
  "action": "dodge_left",
  "telemetry": {
    "latency_ms": 820,
    "fallback": false,
    "reason": "opponent threw hook on 9 and 11; dodge_left covers it and ripostes",
    "tokens_in": 640,
    "tokens_out": 25
  }
}
```

`telemetry` is **optional and never affects resolution** — the platform acts only on `action`. It is logged verbatim and is what makes a match log analysable rather than merely replayable.

| Field | Purpose |
|---|---|
| `latency_ms` | Decision wall clock. The only number that reveals whether the deadline is binding. |
| `fallback` | `true` if this action came from a local default rather than a completed decision. See §10. |
| `reason` | ≤ 200 chars. Never shown to the opponent, never rendered live. Turns a log into a readable transcript of both agents' thinking, gradeable directly. |
| `tokens_in` / `tokens_out` | Cost per decision. |

**`resume`** — reclaim a corner after a disconnect (§6.4).

```json
{"type": "resume", "corner_token": "ct_8f21c0a4"}
```

### 6.2 Platform → Agent

**`joined`** — acknowledgment with corner assignment.

```json
{
  "type": "joined",
  "corner": "agent_a",
  "match_code": "RUMBLE-4F2",
  "corner_token": "ct_8f21c0a4"
}
```

**Keep `corner_token`.** It is the only way to reclaim this corner after a dropped connection; the match code alone is insufficient, since anyone holding the share link could otherwise seize a corner mid-match.

Errors, each followed by close: `{"type":"error","reason":"invalid_code" | "match_full" | "profile_required" | "invalid_name" | "invalid_token"}`.

**`match_start`** — sent when the host starts the fight.

```json
{
  "type": "match_start",
  "you": "agent_a",
  "opponent_name": "RivalBot",
  "config": {"max_ticks": 60, "deadline_ms": 5000, "resume_window_ms": 120000}
}
```

**`state_update`** — start of each tick; this is the cue to think and submit.

```json
{
  "type": "state_update",
  "tick": 12,
  "deadline_ms": 5000,
  "self":     {"hp": 85, "stamina": 60, "points": 30, "penalties": 0},
  "opponent": {"hp": 70, "stamina": 40, "points": 15, "penalties": 0},
  "history": [
    {
      "tick": 11,
      "moves": {"self": "jab", "opponent": "block_high"},
      "events": [
        {"type": "blocked", "attacker": "self", "defender": "opponent", "action": "jab", "defense": "block_high"}
      ]
    },
    {
      "tick": 10,
      "moves": {"self": "idle", "opponent": "hook"},
      "events": [
        {"type": "hit", "attacker": "opponent", "defender": "self", "action": "hook", "damage": 15}
      ]
    }
  ]
}
```

`history` contains the **last 10 ticks, most recent first**, using the §4.5 event objects with egocentric party references. Opponent modeling from this history is the intended skill dimension — agents need no local state.

> **The window size is a difficulty knob, not an implementation detail.** Ten ticks bounds what structure is detectable: a period-12 pattern is invisible by construction, and an opponent that adapts on a longer horizon cannot be fully characterised. Ten is the MVP default because it keeps prompts small and matches the length of a typical exchange. Change it deliberately, and record it — it defines the difficulty of the skill being measured.

**`match_paused`** / **`match_resumed`** — see §6.4.

**`match_end`**

```json
{
  "type": "match_end",
  "winner": "agent_a",
  "reason": "ko",
  "final_tick": 28,
  "final_state": {
    "agent_a": {"hp": 65, "stamina": 66, "points": 105, "penalties": 0},
    "agent_b": {"hp": 0,  "stamina": 40, "points": 35,  "penalties": 15}
  }
}
```

`final_state` is keyed by corner and carries the same four fields as §3 for both fighters — the same shape as `tick_result.state` (§7). It is absolute, not egocentric, so that the agent-facing and spectator-facing `match_end` are byte-identical and one parser serves both. `winner` is `null` on a draw (§5.1).

### 6.3 Tick sequencing

1. Platform sends `state_update` for tick *N* to both agents simultaneously.
2. Each agent replies with `move` for tick *N* before `deadline_ms`.
3. The tick resolves as soon as **both** moves arrive, or the deadline expires — whichever is first.
4. Non-arrivals become no-moves (§4.4); a closed socket pauses instead (§6.4).
5. Platform broadcasts `tick_result` to spectators, then sends `state_update` for tick *N+1*.

An agent that receives a `state_update` for a tick it has already answered should treat the newer message as authoritative and answer again — this is what a resume looks like from the inside (§6.4).

### 6.4 Disconnection, pause, and resume

Infrastructure failure must not decide a fight. It also must not advantage either fighter.

**a. Fault classification.** The platform distinguishes the two failure modes it can actually observe:

| Observation | Classification | Consequence |
|---|---|---|
| Socket **open**, no valid move by the deadline | The agent's failure | `no_move` + penalty (§4.4); the tick resolves normally |
| Socket **closed** | Infrastructure failure | The tick does **not** resolve; the match pauses |

**b. Pause and resume.** On a corner's socket closing mid-match:

1. The platform sends `{"type": "match_paused", "tick": 12, "missing": "agent_b", "resume_window_ms": 120000}` to the **remaining agent and all spectators**, and stops the deadline clock for **both** corners.
2. The disconnected builder reconnects to `/ws/join/{match_code}` and sends `resume` with the `corner_token` from `joined`. A bad or unknown token → `{"type":"error","reason":"invalid_token"}`.
3. On success the platform sends `match_resumed`, then **re-issues the same `state_update` for the same tick** to both agents with a full fresh deadline. The tick number does not advance and no penalty is applied.
4. Any move already submitted for that tick by the connected agent is **discarded**; both agents answer the re-issued tick.
5. If nobody returns within `resume_window_ms` (default 120 000), the match ends `abandoned` (§5.1).

Because agents are stateless by design (§1), a pause is invisible to a correctly-built agent: it simply receives a `state_update` and answers it. Nothing needs re-syncing.

> **Known limitation — residual asymmetry.** The connected agent has already seen that tick's state and may have finished reasoning about it before the pause; re-issuing hands it additional wall clock on a position it has already considered. Discarding its submitted move (step 4) narrows this but does not close it. The effect is bounded and we accept it; it is recorded here rather than claimed solved. Matches ending `abandoned` should be excluded from evaluation aggregates for the same reason.

**c. Platform restart.** "Resumable" covers the server too. Because `resolve()` is pure and deterministic (§4.3) and the match log is an append-only event stream (§7), replaying a log reconstructs match state exactly. Recovery is therefore: fsync the log after every tick, and on boot rebuild any unfinished match from its log and reopen it for resume. **No database is required** — the log is the source of truth.

---

## 7. Spectator WebSocket Protocol

Endpoint: `wss://<host>/ws/watch/{match_code}` — read-only.

**`lobby_update`** — sent on join and whenever corners change.

```json
{
  "type": "lobby_update",
  "corners": {
    "agent_a": {"name": "FemiBot-v1", "profile": {"model": "claude-opus-5", "provider": "anthropic"}},
    "agent_b": null
  }
}
```

**`match_start`**

```json
{
  "type": "match_start",
  "schema_version": 1,
  "match_code": "RUMBLE-4F2",
  "agents": {
    "agent_a": {"name": "FemiBot-v1", "profile": {"model": "claude-opus-5", "provider": "anthropic", "prompt_version": "ref-v1", "temperature": 0}},
    "agent_b": {"name": "Jab-bot",    "profile": {"model": "none", "agent_version": "jabbot@1.0"}}
  },
  "config": {"max_ticks": 60, "deadline_ms": 5000, "resume_window_ms": 120000},
  "started_at": "2026-08-06T14:03:11Z"
}
```

**`tick_result`** — the renderer plays animations from this.

```json
{
  "type": "tick_result",
  "tick": 12,
  "moves": {"agent_a": "cross", "agent_b": "dodge_right"},
  "events": [
    {"type": "hit", "attacker": "agent_a", "defender": "agent_b", "action": "cross", "damage": 10}
  ],
  "state": {
    "agent_a": {"hp": 85, "stamina": 55, "points": 40, "penalties": 0},
    "agent_b": {"hp": 60, "stamina": 37, "points": 15, "penalties": 0}
  },
  "telemetry": {
    "agent_a": {"latency_ms": 820, "fallback": false},
    "agent_b": {"latency_ms": 40,  "fallback": false}
  }
}
```

*Worked example, continuing from the `state_update` in §6.2:* `agent_a` throws `cross` (10 damage, 10 stamina); `agent_b` picks `dodge_right`, which counters `jab` only, so the cross lands for full damage and there is no riposte. `agent_b` HP 70 − 10 = 60. `agent_a` points 30 + 10 = 40. Stamina: `agent_a` 60 − 10 + 5 = 55; `agent_b` 40 − 8 + 5 = 37. Points reconcile against damage taken throughout: `agent_a`'s 40 equals `agent_b`'s 100 − 60, and `agent_b`'s 15 equals `agent_a`'s 100 − 85.

`telemetry` is echoed for the HUD and the log; `reason` and token counts are **stripped from the live spectator stream** (they remain in the log) so that in-flight reasoning is never visible to a watching opponent.

**`match_paused`** / **`match_resumed`** — as in §6.4.

**`match_end`** — byte-identical to §6.2.

**Replay:** the match log contains the `match_start` payload plus every `tick_result` and the `match_end`. Replay = the frontend fetching the log via REST and feeding the same events through the same renderer code on a timer. No separate replay system.

**Log format:** one JSON file per match, `{schema_version, match_start, ticks: [...], match_end}`. `schema_version` starts at `1` and appears in every file so replay parsing stays forward-compatible. The log is also the crash-recovery substrate (§6.4c) and the evaluation dataset — it is written append-only and fsynced per tick.

---

## 8. Platform REST API (frontend-facing)

| Endpoint | Purpose |
|---|---|
| `POST /api/matches` | Creates a match. Optional body `{"max_ticks": 60, "deadline_ms": 5000}`. Returns `{match_id, match_code, host_token, watch_url}` |
| `POST /api/matches/{ref}/start` | Host starts the fight. Rejected unless both corners are filled. |
| `POST /api/matches/{ref}/fill_bot` | Body: `{corner, bot: "jab" \| "random" \| "cycle" \| "frequency" \| "mixer", seed?: int}`. Spawns a built-in bot (§11) as an internal client in that corner. |
| `GET /api/matches/{ref}` | Status: `lobby \| in_progress \| paused \| finished \| abandoned` + corners (with profiles) + result if finished |
| `GET /api/matches/{ref}/log` | Full match log JSON (used for replay) |

### 8.1 Match references

`{ref}` accepts **either the `match_id` or the `match_code`.** Both resolve to the same match.

This matters: spectators arrive holding only a match code from a share link, while `match_id` is returned solely to the creating browser. If the read endpoints were keyed by ID alone, a spectator could not fetch the log and therefore could not use Replay at all.

### 8.2 Host authentication

Host-only actions (`start`, `fill_bot`) require the header:

```
X-Host-Token: <host_token>
```

| Condition | Status |
|---|---|
| Missing or invalid token | `401` |
| Unknown match reference | `404` |
| Valid token, illegal lifecycle transition (e.g. starting a match already in progress, or with an empty corner) | `409` |
| Success | `200` |

`host_token` is returned only to the creating browser session. This is the entire auth model for MVP, alongside the per-corner `corner_token` used for resume (§6.4).

### 8.3 Match configuration

`max_ticks` and `deadline_ms` are settable at creation and are **the same for both corners** in a given match. They are echoed in `match_start` and recorded in the log, so every result carries the conditions it was produced under.

Defaults: `max_ticks: 60`, `deadline_ms: 5000`, `resume_window_ms: 120000`. Accepted ranges: `max_ticks` 1–200, `deadline_ms` 500–120000.

`deadline_ms` being configurable is what keeps the platform honest about what it measures — see §12.

---

## 9. Frontend (single-page app)

Three views, one page. Simple, clean, effective — no navigation maze.

1. **Home:** one primary button — **Create Match** — plus a "Watch a match" code input.
2. **Lobby** (after create): the match code displayed large with a copy button and the join command shown ready to paste (`python agent.py --code RUMBLE-4F2 --url wss://…`); two corner cards that flip from *Waiting…* to the agent's name **and profile** (model, prompt version) on connect; **Add built-in bot** button on each empty corner; **Start Fight** enabled only when both corners are filled. Share link for spectators.
3. **Arena:** Three.js canvas with both fighters; HUD with HP bars, stamina bars, points, penalties, and tick counter; a **paused** overlay when a corner drops (§6.4), showing the resume countdown; end-of-match result banner with the winner and reason; **Replay** button that re-runs the fight from the log through the same renderer.

Spectators who open the watch link during the lobby see the corner status; during the fight they see the arena live.

Animation clips are driven by the rig described in [`skeleton.md`](skeleton.md), which defines the joint map and the clip-authoring conventions.

---

## 10. What Agent Builders Receive

The public "build an agent" doc is just three things:

1. The action table and resolution matrix (§2–§4)
2. The agent WebSocket protocol (§6)
3. The reference agent script (~120 lines of Python) they can copy and modify.

Reference agent decision loop:

```python
async for msg in ws:
    data = json.loads(msg)
    if data["type"] == "state_update":
        action, meta = await decide(data)   # LLM call with prompt + state + history
        await ws.send(json.dumps({
            "type": "move", "tick": data["tick"], "action": action, "telemetry": meta
        }))
```

Usage: `python agent.py --code RUMBLE-4F2 --url wss://arena.example.com`

**`decide()` must always return a valid action within the deadline.** Time-box the LLM call to roughly 70% of `deadline_ms` and fall back to a hardcoded default (`block_high`) on timeout or parse failure.

Note what the fallback is and is not, under v0.4 rules. Sending a hedge is **strictly better than sending nothing** — a no-move is defenseless and costs 15 penalty points (§4.4), so hedging is a legitimate strategic choice, not a loophole. But a hedge is a real move with real consequences, and it is no longer a way to be scored as a deliberate defensive strategist. Set `telemetry.fallback = true` when it happens; a high fallback rate is the signal that a model is running out of clock rather than choosing to block.

---

## 11. Built-in Bots (baseline ladder)

Bots connect through the **same public WebSocket protocol** as external agents — no privileged internal path — so baselines and contenders are measured through identical machinery. All are deterministic given a seed.

| Bot | Behaviour | What it probes |
|---|---|---|
| `random` | Uniform choice over the eight actions | Floor |
| `jab` | Always `jab` | Whether the agent finds the trade line (see below) |
| `cycle` | Fixed period-3 or period-5 pattern | Pattern detection inside the 10-tick history window |
| `frequency` | Counters the opponent's most-used action so far | Whether the agent notices it is being read, and mixes |
| `mixer` | Near-equilibrium randomised mix | Ceiling — nobody should reliably beat it |

The ladder, not model-vs-model play, is the measuring instrument. This game is at heart a resource-constrained simultaneous matrix game: two agents both playing near-equilibrium score alike regardless of how strong they are, so discrimination comes almost entirely from **exploiting a non-equilibrium opponent**. Model-vs-model matches are the demo; the ladder is the evaluation.

**Jab-bot is a designed trap.** The obvious answer — always `block_high`, which counters `jab` — takes zero damage but also deals zero, and both fighters finish at 100 HP and 0 points: a **draw**. Winning requires noticing that `hook` into an incoming `jab` is a trade (§4.2), dealing 15 and taking 5 for a net +10 per exchange; hooking every fourth tick and blocking otherwise KOs Jab-bot around tick 28. Any gate against Jab-bot should therefore measure **the outcome, not the defensive-action share** — rewarding defensiveness rewards the drawing line.

---

## 12. On the deadline and what it measures

Simultaneous resolution means a faster agent gains **no in-game advantage**: both sides see identical state, both submit blind, both resolve together. Relative latency is fully neutralised, by design.

Absolute latency is a different matter and should not be glossed. Reasoning capability is bought with tokens, and tokens cost wall clock, so `deadline_ms` is a **cap on how much reasoning an entrant is permitted to do**. For a fast non-reasoning model, 5 s never binds and the cap is invisible. For an extended-thinking model working a genuine game-theory problem, it can bind on most ticks — and under §4.4 an overrun is now penalised rather than absorbed. Run a shallow fast model against a deep slow one under a tight deadline and the arena will report that the shallow one fights better, when the finding is really that the deep one ran out of clock.

This is why `deadline_ms` is settable per match (§8.3) rather than fixed:

| Bracket | `deadline_ms` | Use |
|---|---|---|
| **Standard** | 5000 | Demos, party mode, fast-model comparison. The default. |
| **Deep** | 30000+ | Reasoning models, where thinking budget is the variable under test. |

Same engine, same rules, same protocol. Compare entrants **within** a bracket, and record the bracket alongside the profile — every log already carries `config`, so every result states the conditions it was produced under.

---

## 13. Deferred Tuning Decisions

Settled, recorded here so they are not re-litigated at build time:

- **Tick pacing for spectators:** resolve as soon as both moves arrive; the renderer queues clips so playback stays smooth regardless of resolution speed.
- **Trade damage:** full damage on both sides. If bot testing shows pure aggression dominant, reduce trade damage to 50% **before touching anything else**.
- **Match code collisions/expiry:** codes are short-lived; lobbies expire after 30 minutes of inactivity. In-memory registry, with unfinished matches recoverable from their logs (§6.4c).
- **No-move penalty = 15.** Chosen to equal `hook` damage, the largest single hit in the game, so that no-move is worse than any real move in every branch — including throwing a `hook` that gets countered. A materially smaller penalty would make silence a viable hedge when an agent is unsure.

Genuinely open, to be resolved by the balance gate in Phase 3:

- **Riposte damage (currently 5).** Simulation shows 0 leaves `dodge_right` strictly dominated (a dead action), 3 makes it marginal against Jab-bot (KO at tick 35, near the stamina limit), and 5 makes it decisive (KO at tick 20). 5 is provisional. Alternatives to riposte, if it proves too strong: give dodges a lower stamina cost, or grant a next-tick damage bonus instead of immediate chip damage — the latter costs `resolve()` its purity and should be a last resort.
- **Stamina regen numbers.** Untested. Tune after the built-in-bot phase — always-jab must not be dominant.
- **Aggression balance.** Round-robin simulation of pure strategies currently ranks `hook` first. This is expected — pure defenses cannot win without riposte — but it needs re-checking against mixed strategies once real agents play. This is the trigger condition for the trade-damage reduction above.
- **History window (10 ticks).** See §6.2. Defensible as an MVP default; revisit once the `cycle` and `frequency` bots exist, since the window bounds what either can teach.
