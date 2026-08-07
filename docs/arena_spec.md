# ADAM Arena — Combat Protocol & Rules Spec (MVP v0.6)

A hosted platform where two LLM agents fight under boxing-style rules. The platform owns the rig, animations, rules engine, and referee logic. Agents own only their decision-making (model + prompt + logic).

**Connection model:** agents are **WebSocket clients that dial into the platform using a match code**. Agent builders run a script from anywhere (laptop, NAT, no hosting needed). Only the platform is publicly hosted. Spectators watch via a share link on the same WebSocket infrastructure.

**What this platform measures:** the **agent** — the bundle of prompt + model + logic — reasoning about combat. Not inference speed. See §12 for how the deadline interacts with that goal.

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
- **One action is not a strategy** (§4.6). The same move repeated a fourth time in a row is void.
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

> **Why riposte exists.** Without it, `dodge_right` is strictly dominated: `block_high` counters everything `dodge_right` counters *plus* `cross`, at less than half the stamina. A rational agent would never pick it, silently reducing an eight-action space to seven. Simulation confirms both halves: at riposte 0 a `dodge_right` bot loses to Jab-bot on points; at 5 it KOs Jab-bot. See §13 for the balance status of the number itself.

### Other

| Action | Effect |
|---|---|
| `idle` | Regenerates **+15 on top of the +5 passive regen (+20 total)**. No defense — takes full damage if hit. Costs no stamina. |

Every attack has exactly two counters (one block, one dodge). The matrix is arbitrary but published — reading it and predicting the opponent *is* the game.

**No action may be thrown more than three times in a row** (§4.6). This applies to all eight, `idle` included.

---

## 3. Fighter State

Each fighter starts with:

```json
{
  "hp": 100,
  "stamina": 100,
  "points": 0,
  "penalties": 0,
  "last_action": null,
  "repeat_streak": 0
}
```

These six fields are what "fighter state" means throughout this document. They appear together, unchanged, in `state_update.self` and `.opponent` (§6.2), `tick_result.state` (§7), and `match_end.final_state` (§6.2).

- **Stamina regen:** +5 per tick automatically, +15 extra on `idle` (total +20 that tick). Capped at 100. Stamina is never negative.
- **Insufficient stamina:** a submitted action costing more than available stamina is converted to `idle` — including the full `idle` regen — and emits an `exhausted` event. This is *not* a no-move and carries **no penalty**: the agent submitted a legal move on time and simply could not afford it.
- **Points:** all landed damage, including riposte damage (§2), is added to the dealer's points.
- **Penalties:** accumulated from no-moves (§4.4). Points and penalties are tracked separately so that `points` keeps its plain meaning — damage dealt — and the penalty remains auditable in the log.
- **Decision score** (used only when no KO occurs) = `points − penalties`. It may be negative.
- **`last_action` / `repeat_streak`:** the action this fighter last *submitted* and how many times in a row it has now submitted it, for the repetition cap (§4.6). `last_action` is `null` at match start and after a no-move. These are carried in fighter state, rather than left for agents to derive from `history`, so that §1's stateless-by-design promise still holds under a rule that depends on the past: an agent can read straight off `self` that one more repeat would be voided.

---

## 4. Resolution Rules (simultaneous)

Given `move_a` and `move_b`, resolve per attack independently.

### 4.1 Order of operations within a tick

1. Convert absent/invalid moves to `no_move` (§4.4), emitting `no_move` and applying the penalty.
2. Strip fourth-and-later consecutive repeats (§4.6), emitting `void`.
3. Convert unaffordable actions to `idle` (§3), emitting `exhausted`.
4. Deduct stamina costs for both fighters.
5. Resolve both attacks against the opposing action (§4.2), including riposte.
6. Apply passive regen (+5), plus the `idle` bonus (+15) where applicable. Cap at 100.
7. Check win conditions (§5).

**Steps 1–3 are mutually exclusive:** a fighter produces at most one of `no_move`, `void`, or `exhausted` in a given tick. The order between them is load-bearing — the repeat check sits *ahead* of the affordability check, so an action that is both a fourth repeat and unaffordable is reported as `void` and never as `exhausted`. A void action is stripped before it is priced; reversing this would let a fighter dodge the cap by running its stamina down.

Damage is computed from the state at the *start* of the tick, so resolution is genuinely simultaneous: a fighter reduced to 0 HP still lands the attack it threw that tick.

### 4.2 Attack outcomes

| Situation | Outcome |
|---|---|
| Attack vs. correct **block** | 0 damage. Attacker still pays stamina. `blocked` event. |
| Attack vs. correct **dodge** | 0 damage. Defender ripostes 5 to the attacker, added to defender's points. `dodged` event. |
| Attack vs. wrong defense, `idle`, `no_move`, or a **voided** action | Full damage lands. `hit` event. |
| Attack vs. attack (trade) | Both land at full damage. Two `hit` events. |
| Defense vs. defense, `idle`, `no_move`, or a **voided** action | Nothing happens. No riposte — a riposte requires an incoming attack to evade. |
| **Voided** attack vs. anything | Nothing happens. The attack does not exist (§4.6). |

A voided action resolves exactly as if the fighter had thrown nothing: it deals no damage, negates nothing, and never ripostes. It differs from a no-move only in what it costs (§4.6).

### 4.3 Purity

Resolution is a pure function — no randomness, no I/O:

```
resolve(state, move_a, move_b) -> (new_state, events)
```

This is load-bearing beyond testability: because it is deterministic, a stored match log can be replayed to reconstruct exact state (used for crash recovery, §6.4c) and re-scored offline against metrics that did not exist when the match was fought.

### 4.4 No-move

A fighter has made a **no-move** for tick *N* when, by the deadline, the platform has not received a valid `move` for tick *N* on an open connection. Rejection reasons, each logged distinctly:

| `cause` | Meaning |
|---|---|
| `timeout` | Connection open, nothing arrived before the deadline |
| `malformed` | Not valid JSON, or not a `move` message |
| `invalid_action` | Action name not in the eight of §2 |
| `wrong_tick` | `tick` field does not match the current tick |
| `duplicate` | A move for this tick was already accepted (the first is kept) |

**A rejected submission does not immediately lock in a no-move.** The agent keeps the remainder of its deadline to send something valid, and only if nothing valid ever arrives does the tick become a no-move — carrying the *last* rejection reason rather than `timeout`, because "the agent cannot follow the output format" and "the agent was slow" are different findings about an agent. An agent that sends `{"action": "punch"}` and then corrects itself to `jab` within the deadline has thrown a jab and is not penalised.

`duplicate` is therefore the one reason that never appears as a no-move cause: a duplicate exists only when a valid move was already accepted, so by definition that tick is not a no-move. The duplicate is discarded, the first move stands, and no penalty applies.

A no-move resolves as follows:

- The fighter is **defenseless**: it takes full damage from any incoming attack and deals none.
- It receives **only the +5 passive regen** — never the `idle` bonus.
- It accrues a **penalty of 15** (see §11 for why 15).

A no-move is therefore strictly worse than every real action, including a badly-chosen one. There is never an incentive to stay silent, and a slow agent cannot be scored as a defensive strategist.

**A closed connection is not a no-move.** If the socket is closed, the tick does not resolve at all — the match pauses (§6.4). This split is what separates *the agent failed* from *the infrastructure failed*.

### 4.5 Event schema

`events` is an ordered list that the renderer, the log, and the agent-facing `history` all consume. Every event names its parties explicitly — no subject is ever left to be inferred.

**Ordering is the order of operations in §4.1**, which is stable and worth relying on: the conversion events (`no_move`, `void`, `exhausted` — at most one per fighter) in corner order first, then attack outcomes in corner order (`agent_a`'s attack, then `agent_b`'s), then any `ko`. The list below illustrates the event *shapes*, not a sequence any single tick would produce.

```json
[
  {"type": "hit",       "attacker": "agent_a", "defender": "agent_b", "action": "cross", "damage": 10},
  {"type": "blocked",   "attacker": "agent_b", "defender": "agent_a", "action": "jab",   "defense": "block_high"},
  {"type": "dodged",    "attacker": "agent_a", "defender": "agent_b", "action": "hook",  "defense": "dodge_left", "riposte": 5},
  {"type": "exhausted", "who": "agent_b", "attempted": "hook", "stamina": 4},
  {"type": "void",      "who": "agent_a", "action": "jab", "streak": 4},
  {"type": "no_move",   "who": "agent_b", "cause": "timeout", "penalty": 15},
  {"type": "ko",        "who": "agent_b"}
]
```

In agent-facing messages the party values are `"self"` and `"opponent"` instead of corner IDs (§1.1). Field sets are otherwise identical.

---

### 4.6 Repetition cap

**A fighter may submit the same action at most three times consecutively. The fourth and every further consecutive repeat is void.**

```
tick:    1     2     3     4     5     6     7
submit:  jab   jab   jab   jab   jab   cross jab
result:  ok    ok    ok    VOID  VOID  ok    ok
streak:  1     2     3     4     5     1     1
```

The rules, exactly:

- The streak counts what was **submitted**, not what it resolved as. An action converted to `idle` for want of stamina (§3) still counts as the action submitted.
- A **voided move still extends the streak.** Spamming is not a way out of the cap.
- Submitting a **different** action resets the streak to 1.
- A **no-move breaks the streak** — `last_action` becomes `null` and `repeat_streak` becomes 0. There was no submission, so there is nothing to have repeated.
- It applies to **all eight actions**, `idle` included.

A void costs:

| | Voided action | No-move (§4.4), for contrast |
|---|---|---|
| Deals damage | No | No |
| Defends / ripostes | No — defenceless | No — defenceless |
| Stamina | **Deducted** (clamped at the fighter's remaining stamina) | None spent |
| Regen | +5 passive only, never the `idle` bonus | +5 passive only |
| Penalty | **None** | 15 |
| Reported in `tick_result.moves` | The action submitted | `null` |

Spam is punished **economically, not punitively**: a void is a legal action submitted on time, so it never touches `penalties` and never appears as a no-move. This keeps the no-move statistics meaning only what they say — an agent that spams and an agent that goes silent are different findings, and the log must be able to tell them apart.

> **Why this rule exists.** Without it, a single action can be an entire strategy. Reading the opponent and predicting their next move is the skill this platform is built to measure (§2, §11), and that skill is sidestepped entirely by an agent that picks one move and holds it — there is nothing to read, and nothing being read. The cap of three is a floor on how much variety a fight contains: at minimum, every fourth tick is a real decision.
>
> **Why it does not break statelessness.** The cap depends on history, which looks like it forces agents to remember something. It does not: `self.last_action` and `self.repeat_streak` arrive in every `state_update` (§3, §6.2), so an agent can see the cap coming from the current message alone. A pause and resume (§6.4) re-issues the same `state_update` carrying the same streak, so the cap is invisible to a correctly-built agent in exactly the way everything else is.

---

## 5. Match Rules

**Ticks are 1-indexed.** A match runs ticks `1 … max_ticks` inclusive.

| Rule | Value (MVP default) |
|---|---|
| Max ticks | 60 |
| Move deadline | 5000 ms per tick (configurable per match, §8) |
| Missing / invalid / duplicate / wrong-tick move | `no_move`: defenseless, +5 regen only, **penalty 15** (§4.4) |
| Max consecutive repeats of one action | **3.** The 4th and later are void: stripped to nothing, stamina still paid, no penalty (§4.6) |
| Unaffordable move | Converted to `idle` with full idle regen, no penalty (§3) |
| KO | HP ≤ 0 → opponent wins immediately |
| Double KO (trade to ≤ 0) | Draw |
| No KO after `max_ticks` | Higher **decision score** (`points − penalties`) wins; equal → draw |
| Disconnect | Match **pauses** and awaits resume (§6.4) — it is not a loss |
| Resume window exceeded | Match ends `abandoned` (§5.1) |

There is **no TKO rule**, deliberately. An agent that stops deciding is already defenseless and accruing penalties, so it loses on damage or on the decision score without a special case — and its absence is what stops a network failure from being recorded as a combat loss.

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
  "corner": "agent_a",
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

  **Names are unique within a match.** If the other corner is already using the name — compared case-insensitively — the *joining* agent's is suffixed: `Jab-bot` beside `Jab-bot` becomes `Jab-bot 2`, truncated if needed to stay inside the 24 characters above. The corner that was already filled never changes name, so a spectator watching the lobby never sees one rename itself.

  > Filling both corners from the same ladder rung — two `jab` bots, or the reference agent run twice — otherwise puts two identically-named fighters in the ring, and every line of the event log, every HUD panel and every row of an analysed log becomes ambiguous about which one it means. Renaming is the only fix that still works after the fact: a log is read long after the match, when "the one in the blue corner" is no longer something the reader can see. **Read your name back from `joined`** rather than assuming you got the one you asked for.

- **`corner`** — **optional.** A request for a specific corner, honoured when that corner is free and silently ignored otherwise; omit it to take whichever corner is free. Always read the corner you actually got from `joined`, never from what you asked for.

  > This exists because the lobby's **Add built-in bot** button sits on a specific corner card, and filling `agent_b` while `agent_a` is still empty has to work. It is on the public protocol rather than being a platform-internal mechanism precisely so that built-in bots need no privileged path (§11) — any agent may use it.

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
  "corner_token": "ct_8f21c0a4",
  "name": "FemiBot-v1"
}
```

`name` is the name the platform gave this corner — the one that was asked for, unless the other corner already had it (§6.1). It is what spectators, the opponent and the log will call you, so log *this* rather than what you sent.

**Keep `corner_token`.** It is the only way to reclaim this corner after a dropped connection; the match code alone is insufficient, since anyone holding the share link could otherwise seize a corner mid-match.

Errors, each followed by close: `{"type":"error","reason":"invalid_code" | "expected_join" | "match_full" | "profile_required" | "invalid_name" | "invalid_token"}`.

`expected_join` means the first frame on the socket was neither `join` nor `resume`. It is distinct from `invalid_code` deliberately: the code resolved fine and reporting it as invalid sends a builder hunting the one thing that is not wrong.

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
  "self":     {"hp": 85, "stamina": 60, "points": 30, "penalties": 0, "last_action": "jab",        "repeat_streak": 3},
  "opponent": {"hp": 70, "stamina": 40, "points": 15, "penalties": 0, "last_action": "block_high", "repeat_streak": 1},
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
    "agent_a": {"hp": 100, "stamina": 66,  "points": 105, "penalties": 0, "last_action": "hook",       "repeat_streak": 1},
    "agent_b": {"hp": -5,  "stamina": 100, "points": 0,   "penalties": 0, "last_action": "block_high", "repeat_streak": 1}
  }
}
```

These are the real numbers from the hook-every-4th-vs-Jab-bot line (§11), reproducible with `python -m server.engine.simulate`. HP goes **below zero** on a KO — the winning attack lands in full, so clients must not assume a floor of 0.

`final_state` is keyed by corner and carries the same fields as §3 for both fighters — the same shape as `tick_result.state` (§7). It is absolute, not egocentric, so that the agent-facing and spectator-facing `match_end` are byte-identical and one parser serves both. `winner` is `null` on a draw (§5.1).

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
2. The disconnected builder reconnects to `/ws/join/{match_code}` and sends `resume` with the `corner_token` from `joined`. A bad or unknown token → `{"type":"error","reason":"invalid_token"}`. On success the platform replies `joined`, then **re-sends `match_start`** — a resuming agent is often a freshly restarted process that never saw the original, and it cannot budget its thinking time without `config.deadline_ms`.
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
    "agent_a": {"hp": 85, "stamina": 55, "points": 40, "penalties": 0, "last_action": "cross",       "repeat_streak": 1},
    "agent_b": {"hp": 60, "stamina": 37, "points": 15, "penalties": 0, "last_action": "dodge_right", "repeat_streak": 1}
  },
  "telemetry": {
    "agent_a": {"latency_ms": 820, "fallback": false},
    "agent_b": {"latency_ms": 40,  "fallback": false}
  }
}
```

`moves` carries the action each fighter **submitted**, or **`null`** for a no-move — the accompanying `no_move` event gives the cause. An action converted to `idle` for want of stamina (§3), or voided as a repeat (§4.6), is still reported as the action submitted, with an `exhausted` or `void` event recording what became of it; storing the submission rather than its outcome is what keeps a log a genuine re-run of the rules rather than a replay of their results.

Renderers therefore cannot read `moves` alone. A `null` has no clip. A voided action **must not play its strike clip** — the punch did not happen — so a renderer has to check the tick's events before animating, and should show the action visibly failing instead.

*Worked example, continuing from the `state_update` in §6.2:* `agent_a` was on `repeat_streak: 3` with `jab`, so a fourth jab would have been void (§4.6) — it throws `cross` instead (10 damage, 10 stamina), which resets its streak to 1. `agent_b` picks `dodge_right`, which counters `jab` only, so the cross lands for full damage and there is no riposte. `agent_b` HP 70 − 10 = 60. `agent_a` points 30 + 10 = 40. Stamina: `agent_a` 60 − 10 + 5 = 55; `agent_b` 40 − 8 + 5 = 37. Points reconcile against damage taken throughout: `agent_a`'s 40 equals `agent_b`'s 100 − 60, and `agent_b`'s 15 equals `agent_a`'s 100 − 85.

`telemetry` is echoed for the HUD and the log; `reason` and token counts are **stripped from the live spectator stream** (they remain in the log) so that in-flight reasoning is never visible to a watching opponent.

**`match_paused`** / **`match_resumed`** — as in §6.4.

**`match_end`** — byte-identical to §6.2.

**Replay:** the match log contains the `match_start` payload plus every `tick_result` and the `match_end`. Replay = the frontend fetching the log via REST and feeding the same events through the same renderer code on a timer. No separate replay system.

**Log format:** one JSON file per match, `{schema_version, match_start, ticks: [...], match_end}`. `schema_version` starts at `1` and appears in every file so replay parsing stays forward-compatible. The log is also the crash-recovery substrate (§6.4c) and the evaluation dataset — it is written append-only and fsynced per tick.

---

## 8. Platform REST API (frontend-facing)

| Endpoint | Purpose |
|---|---|
| `POST /api/matches` | Creates a match. Optional body `{"max_ticks": 60, "deadline_ms": 5000}`. Returns `{match_id, match_code, host_token, watch_url, config}` |
| `POST /api/matches/{ref}/start` | Host starts the fight. Rejected unless both corners are filled. |
| `POST /api/matches/{ref}/fill_bot` | Body: `{corner, bot: "jab" \| "random" \| "cycle" \| "frequency" \| "mixer", seed?: int, period?: 3 \| 5}`. Spawns a built-in bot (§11) as an internal client in that corner. `period` applies to `cycle` only (default 3); supplying it for any other bot, or with a value other than 3 or 5, is a `422`. |
| `POST /api/matches/{ref}/rematch` | Host runs it back. Creates a **new** match with the same config and re-fills any built-in bots into the corners they held. Returns `{match_id, match_code, host_token, watch_url, config, rematch_of, bots}`. Rejected with `409` unless the source match is `finished` or `abandoned`. |
| `GET /api/matches/{ref}` | Status: `lobby \| in_progress \| paused \| finished \| abandoned` + corners (with profiles) + result if finished |
| `GET /api/matches/{ref}/log` | Full match log JSON (used for replay) |
| `GET /api/rules` | The rules table as data (§8.5). No authentication — it is public reference for agent builders. |

### 8.1 Match references

`{ref}` accepts **either the `match_id` or the `match_code`.** Both resolve to the same match. Match codes are matched **case-insensitively**, so a code typed in lower case from a share link still resolves.

Request bodies are JSON and must be sent with `Content-Type: application/json`; without it the body is read as a form and rejected with a `422`.

This matters: spectators arrive holding only a match code from a share link, while `match_id` is returned solely to the creating browser. If the read endpoints were keyed by ID alone, a spectator could not fetch the log and therefore could not use Replay at all.

### 8.2 Host authentication

Host-only actions (`start`, `fill_bot`, `rematch`) require the header:

```
X-Host-Token: <host_token>
```

Conditions are checked in the order listed — an unknown match is a `404` whether or not a token was supplied, since match codes are shared publicly via watch links and their existence is not a secret worth protecting.

| Condition | Status |
|---|---|
| Unknown match reference | `404` |
| Missing or invalid token | `401` |
| Valid token, illegal lifecycle transition (e.g. starting a match already in progress, or with an empty corner) | `409` |
| Success | `200` |

`host_token` is returned only to the creating browser session. This is the entire auth model for MVP, alongside the per-corner `corner_token` used for resume (§6.4).

### 8.3 Match configuration

`max_ticks` and `deadline_ms` are settable at creation and are **the same for both corners** in a given match. They are echoed in `match_start` and recorded in the log, so every result carries the conditions it was produced under.

Defaults: `max_ticks: 60`, `deadline_ms: 5000`, `resume_window_ms: 120000`. Accepted ranges: `max_ticks` 1–200, `deadline_ms` 500–120000.

`deadline_ms` being configurable is what keeps the platform honest about what it measures — see §12.

### 8.4 Rematch

A rematch is a **new match with a new code**, not a reset of the old one. One log is one fight: rewinding a finished match in place would either overwrite a result or leave two fights sharing a `match_id`, and either makes the evaluation dataset lie. Two rematches are therefore two comparable logs fought under identical config.

Built-in bots are re-spawned into the corners they held, with the same `bot`, `seed` and `period`, so a ladder run is one click. An externally-run agent must dial the new code — the response carries it, and the host's browser moves straight into the new lobby. A bot that fails to come back leaves its corner open rather than failing the whole call.

### 8.5 Rules as data

`GET /api/rules` serves the `BOXING` table (§2, §3, §4.4, §4.6) assembled from the same module the resolver reads, plus the configuration defaults of §8.3 and the ladder of §11:

```json
{
  "combat_type": "boxing",
  "actions": ["jab", "cross", "hook", "block_high", "block_low", "dodge_left", "dodge_right", "idle"],
  "attacks": {"jab": {"damage": 5, "stamina": 5}, "…": {}},
  "defenses": {"block_high": {"stamina": 3, "counters": ["jab", "cross"], "riposte": 0}, "…": {}},
  "idle": "idle",
  "economy": {"start_hp": 100, "start_stamina": 100, "stamina_cap": 100, "passive_regen": 5, "idle_regen_bonus": 15},
  "repeat_cap": 3,
  "no_move": {"penalty": 15, "causes": ["timeout", "malformed", "invalid_action", "wrong_tick", "duplicate"]},
  "defaults": {"max_ticks": 60, "deadline_ms": 5000, "resume_window_ms": 120000, "max_ticks_range": [1, 200], "deadline_ms_range": [500, 120000]},
  "bots": ["cycle", "frequency", "jab", "mixer", "random"]
}
```

It exists so nothing outside this repo has to hand-copy a damage number. The in-app agent-builder page (§9) renders its action table from this response rather than restating it, and an agent that reads its costs from here survives a balance change instead of silently playing the old game.

---

## 9. Frontend (single-page app)

Four views, one page. Simple, clean, effective — no navigation maze.

1. **Home:** one primary button — **Create Match** — plus a "Watch a match" code input and a link to **Build an agent**.
2. **Lobby** (after create): the match code displayed large with a copy button and the join command shown ready to paste (`python agent.py --code RUMBLE-4F2 --url wss://…`); two corner cards that flip from *Waiting…* to the agent's name **and profile** (model, prompt version) on connect; **Add built-in bot** button on each empty corner; **Start Fight** enabled only when both corners are filled. Share link for spectators.
3. **Arena:** Three.js canvas with both fighters; HUD with HP bars, stamina bars, points, penalties, tick counter, and each fighter's **repeat streak** (§4.6) so a watcher can see the cap coming and read a void when it lands; a **sound** toggle on the canvas; a **paused** overlay when a corner drops (§6.4), showing the resume countdown; end-of-match result banner with the winner and reason, **Replay**, and **Rematch** for the host (§8.4); the **event log** beside the canvas; and a replay transport with a video export.
4. **Build an agent** (`#/build`): the protocol, the action table, and copy-paste starters, for someone writing a client against §6 without the repo in front of them. Reached from Home. Its action, economy and no-move numbers are rendered from `GET /api/rules` (§8.5), never restated.

**Event log.** Every event of the whole fight, in fight order, in a box that scrolls. Nothing is dropped and nothing is windowed out — the log is the readable record of the match, and a reviewer stepping through a replay has to be able to read back through it. It follows the fight automatically only while the reader is already at the end, so scrolling up to re-read an exchange is never yanked away by the next tick.

It sits **beside the canvas** on a wide screen and below it on a narrow one. Side by side is what makes the log and the fight one instrument rather than two: a reviewer scrubbing the replay watches the marked line move down the log without the page scrolling, and the fight is never pushed off-screen by the record of what happened in it. Beside the canvas the log is bounded twice — by the height of the canvas column and by the height of the window — and scrolls inside whichever is smaller, so it can never run off the bottom of the screen.

**Headroom for the HUD.** The HUD floats over the canvas as DOM, so the renderer must be told it is there: the camera frames the fighters *plus* a reserved band across the top, rather than framing them to the full viewport and letting the gauges sit on their heads. It is a fraction of the picture, not a pixel offset, so it holds at every stage size and aspect ratio.

**Replay.** Fetches the stored log (§8) and pushes the same `tick_result` objects through the same renderer — no second implementation. On top of that it exposes a transport:

| Control | Behaviour |
|---|---|
| Slider | One step per tick, spanning the fight. Dragging **jumps** the scene rather than fast-forwarding to it. |
| Step / first / last | Single-tick moves, for frame-accurate review. |
| Play / pause | At the end of the fight, Play restarts from the top. |
| Speed | 0.25× / 0.5× / 1× / 2× / 4×, applied to both the feed rate and the clip time scale. |

A jump is cheap because `tick_result.state` is **absolute** (§7): any single tick is the whole picture, so seeking never has to replay the ticks before it to reconstruct HP, stamina, points, or the repeat streak. The one piece of history a jump must restore by hand is whether a fighter is already on the floor, which it reads from the HP in the tick it lands on. The event log is re-derived from the ticks up to the slider position, so scrubbing backwards shortens it.

**Sound.** Five synthesised impacts — a punch landing, a block, a whiff, the repeat cap's buzzer, and a KO with the bell on the end of it — dispatched from the same `tick.events` branches that fire the sparks and the shake, so what a viewer hears can never describe a different fight from what they see. A voided action makes the buzzer and never the punch, for the same reason it plays the `void` clip and never the strike clip. There are no audio files: the sounds are oscillators and one buffer of noise, so the static export carries no assets and a phone downloads nothing extra.

It is **off by default and the toggle is the only way on**. A share link opens straight into a live fight, and browsers will not start audio outside a user gesture in any case; the choice is remembered per browser. Nothing about it reaches the engine, the log, or either agent — sound is a spectator affordance and is not part of the protocol.

**Video export.** *Download video* records a replay to a file, **with the match audio in it regardless of the toggle**. The toggle mutes this browser tab; it does not mute the file, and a replay someone downloads to send on should arrive with the punches in it. The audio is tapped off the mix bus ahead of the mute control and married to the canvas frames before recording starts, so a muted page and a sounded file are the same recording. A browser with no Web Audio records video alone and the UI says so. The HUD is DOM rather than WebGL, so a raw canvas capture would have no scoreboard; the frame and a repainted scoreboard are composited into a second canvas, and that is what `MediaRecorder` encodes. Recording always runs at 1× whatever speed the viewer was reviewing at. MP4/H.264 is used where the browser can encode it *with an audio track beside it*, WebM/Opus otherwise — sound outranks the container, so a browser that writes H.264 but refuses AAC produces a WebM with the fight in it rather than a silent MP4. The file is named with the extension it actually is, and the UI says which was produced, and whether it has sound, rather than promising an MP4 and writing something else into it.

Spectators who open the watch link during the lobby see the corner status; during the fight they see the arena live.

Animation clips are driven by the rig described in [`skeleton.md`](skeleton.md), which defines the joint map and the clip-authoring conventions.

---

## 10. What Agent Builders Receive

[`agent_guide.md`](agent_guide.md) is the front door for agent builders — it explains what this section specifies, with captured payloads and runnable examples. What a builder needs from *this* document is three things:

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

**A fixed fallback must be a *pair*, not a single action, because of the repetition cap (§4.6).** A fallback fires on consecutive ticks by construction — a slow model is slow repeatedly — so four hedges in a row would emit four identical `block_high` submissions, the fourth of which is void. That leaves the agent defenceless with the stamina already spent, at exactly the moment it is least able to notice: the precise inverse of a hedge.

Use the first of `block_high`, `block_low` that would not void, read off `self.last_action` and `self.repeat_streak` in the `state_update`. This needs no model call and no local state, and it provably never voids — a fighter has only one `last_action`, so both cannot be capped at once. Both cost 3 stamina, below the +5 passive regen, so an unbounded run of hedges cannot exhaust the fighter either. The steady state is `block_high ×3, block_low, block_high ×3, …`.

Note what the fallback is and is not. Sending a hedge is **strictly better than sending nothing** — a no-move is defenseless and costs 15 penalty points (§4.4), so hedging is a legitimate strategic choice, not a loophole. But a hedge is a real move with real consequences, and it is not a way to be scored as a deliberate defensive strategist. Set `telemetry.fallback = true` when it happens; a high fallback rate is the signal that a model is running out of clock rather than choosing to block.

---

## 11. Built-in Bots (baseline ladder)

Bots connect through the **same public WebSocket protocol** as external agents — no privileged internal path — so baselines and contenders are measured through identical machinery. All are deterministic given a seed.

| Bot | Behaviour | What it probes |
|---|---|---|
| `random` | Uniform choice over the eight actions | Floor |
| `jab` | `jab` to the repetition cap, then one `block_high` to reset the streak | Whether the agent finds the trade line (see below) |
| `cycle` | Fixed period-3 or period-5 pattern | Pattern detection inside the 10-tick history window |
| `frequency` | Counters the opponent's most-used action so far | Whether the agent notices it is being read, and mixes |
| `mixer` | Near-equilibrium randomised mix | Ceiling — nobody should reliably beat it |

The ladder, not model-vs-model play, is the measuring instrument. This game is at heart a resource-constrained simultaneous matrix game: two agents both playing near-equilibrium score alike regardless of how strong they are, so discrimination comes almost entirely from **exploiting a non-equilibrium opponent**. Model-vs-model matches are the demo; the ladder is the evaluation.

**Every ladder bot is repeat-aware** (§4.6): each one checks `self.repeat_streak` and inserts a streak-resetting move rather than submitting an action it knows will be voided. This is not a privileged path — it reads the same `state_update` fields any external agent gets. It is necessary because a bot that voided out would stop probing the thing it exists to probe, and the ladder is the measuring instrument.

**Jab-bot is a designed trap.** The obvious answer — meeting every `jab` with `block_high` — takes zero damage but also deals zero, and both fighters finish at 100 HP and 0 points: a **draw**. Winning requires noticing that `hook` into an incoming `jab` is a trade (§4.2), dealing 15 and taking 5 for a net +10 per exchange; hooking every fourth tick and blocking otherwise KOs Jab-bot at tick 28. Any gate against Jab-bot should therefore measure **the outcome, not the defensive-action share** — rewarding defensiveness rewards the drawing line.

The repetition cap sharpens the trap rather than dissolving it: a *literal* always-`block_high` voids from its own fourth tick onward and is knocked out at tick 30. The drawing line still exists, but only for an agent that blocks the jabs *and* varies enough to stay legal — a strictly harder thing to find than the naive answer, which now loses outright.

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
- **A void is punished economically, not with a penalty (§4.6).** Adding a penalty was considered and rejected: it would fold "this agent spams" into the same number as "this agent goes silent", and those are different findings about an agent. Forfeiting the tick and paying the stamina is already strictly worse than any legal alternative, since any non-repeated action costs the same or less and actually resolves.

- **Repetition cap = 3.** Two would force a change every other tick, which is less a strategic constraint than a metronome, and would break `cycle-3` — a pattern bot the ladder needs. Four or more is barely a constraint at all inside a 10-tick history window. Three leaves room for a deliberate two- or three-punch combination while guaranteeing that at minimum every fourth tick is a real decision.
- **The streak counts submissions, and a void extends it.** The alternative — resetting the streak on a void — makes the cap self-clearing: an agent could throw the same action forever, losing only every fourth tick. That is a rate limit, not a cap, and it does not force the variety the rule exists to force.

Genuinely open, to be resolved by the balance gate in Phase 3:

- **Riposte damage (currently 5).** Simulation shows 0 leaves `dodge_right` strictly dominated (a dead action), 3 makes it marginal against Jab-bot (KO at tick 35, near the stamina limit), and 5 makes it decisive (KO at tick 20). 5 is provisional. Alternatives to riposte, if it proves too strong: give dodges a lower stamina cost, or grant a next-tick damage bonus instead of immediate chip damage — the latter costs `resolve()` its purity and should be a last resort.
- **Stamina regen numbers.** Untested. Tune after the built-in-bot phase — always-jab must not be dominant.
- **Aggression balance.** Round-robin simulation currently ranks `mostly-hook` joint first, level with `frequency` and `cycle-5`, and ahead of `mixer`. This is expected — pure defenses cannot win without riposte — but joint-first is not dominance, so the trigger for halving trade damage has still not fired. It needs re-checking against mixed strategies once real agents play.
- **Whether the cap changes the equilibrium.** The repetition cap (§4.6) has been checked only against the bot ladder, where it behaves as intended: no ladder bot voids and one-note strategies collapse. Whether it shifts the mixed-strategy equilibrium — a near-equilibrium mixer repeats by chance roughly once every few hundred ticks, so it should not — is unverified against real agents.
- **History window (10 ticks).** See §6.2. Defensible as an MVP default; revisit once the `cycle` and `frequency` bots exist, since the window bounds what either can teach.
