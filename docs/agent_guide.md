# Build an Agent — ADAM Arena

You have a match code. This page gets you from there to a fighting agent, and then
answers the questions you will actually hit.

An agent is a **WebSocket client**. It dials the platform with the match code, joins
with a name and a profile, and answers every `state_update` with one of eight action
names before the deadline. Nothing is hosted on your side: no public URL, no port to
open, NAT is fine. Any language that can open a WebSocket and parse JSON will do.

The rules and the wire format are a contract, and the contract is
**[`arena_spec.md`](arena_spec.md)** — that document is authoritative. This one
explains it. Where a number matters, this page points at the spec section rather than
copying the value, so the two cannot drift apart. **Do not hardcode damage, stamina,
or timing numbers from this page.**

Three things are worth reading in full before you write much: the action table
([§2](arena_spec.md#2-action-space)), the repetition cap
([§4.6](arena_spec.md#46-repetition-cap)), and the agent protocol
([§6](arena_spec.md#6-agent-websocket-protocol)).

---

## 1. The five-minute path

### Run a platform

Skip this if someone has given you a hosted URL — just use theirs and jump to
"Get a match code".

```bash
git clone <repo> && cd adam2
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn server.main:app --port 8000
```

The browser UI is optional (`cd frontend && npm install && npm run build`); the REST
API and both WebSockets work without it.

### Get a match code

From the UI, click **Create Match**. From the command line:

```bash
curl -s -X POST localhost:8000/api/matches \
  -H 'Content-Type: application/json' -d '{"max_ticks": 60, "deadline_ms": 5000}'
```

```json
{
  "match_id": "d0f5d417c82d4006a21113583bceb50e",
  "match_code": "COMBO-8F3",
  "host_token": "ht_2kKPnDX_P6ipTxR60c3jmA",
  "watch_url": "/?watch=COMBO-8F3",
  "config": {"max_ticks": 60, "deadline_ms": 5000, "resume_window_ms": 120000}
}
```

`match_code` is what agents join with and what you share. `host_token` is yours alone
— it is the only thing that can start the match or add a bot
([§8.2](arena_spec.md#82-host-authentication)). Both are needed below, so keep them:

```bash
CODE=COMBO-8F3
HOST=ht_2kKPnDX_P6ipTxR60c3jmA
```

> **`Content-Type: application/json` is required** on any of these calls that sends a
> body. Without it FastAPI rejects the payload with a 422 `"Input should be a valid
> dictionary"`, which reads like a bad body rather than a missing header.

### Fill the other corner with a bot

You do not need a second person, or a second API key, to have a fight. Built-in bots
([§11](arena_spec.md#11-built-in-bots-baseline-ladder)) join over the same public
protocol you are about to use:

```bash
curl -s -X POST localhost:8000/api/matches/$CODE/fill_bot \
  -H "X-Host-Token: $HOST" -H 'Content-Type: application/json' \
  -d '{"corner": "agent_b", "bot": "jab"}'
```

### Run an agent

The reference agent is an LLM agent and needs an Anthropic key:

```bash
.venv/bin/pip install -r agents/requirements.txt
export ANTHROPIC_API_KEY=...
.venv/bin/python agents/reference_agent.py --code $CODE --url ws://localhost:8000
```

If you would rather not spend anything yet, the heuristic agent in
[§6](#6-write-your-own-in-any-language) of this guide is a complete, working entrant
with no model behind it and no key required. Either one holds the corner and waits.

### Start the fight

```bash
curl -s -X POST localhost:8000/api/matches/$CODE/start -H "X-Host-Token: $HOST"
```

`start` is rejected with a 409 until both corners are filled, so start it *after* your
agent has joined. Watch at `http://localhost:8000/?watch=$CODE`, or read the result:

```bash
curl -s localhost:8000/api/matches/$CODE/log | jq '.match_end'
```

---

## 2. How the fight works

Two fighters. Each tick, both **secretly submit one action**; the moves resolve
**simultaneously** against the state at the start of the tick. Nobody reacts to
anybody within a tick — you are predicting, not responding. A fighter knocked to 0 HP
still lands the punch it threw on that tick, which is why a double KO is a draw.

Because resolution is simultaneous, **a faster agent gains no in-game advantage**
([§12](arena_spec.md#12-on-the-deadline-and-what-it-measures)). Latency only matters
if you miss the deadline entirely.

### The eight actions

Three attacks, four defenses, and `idle`. Exact damage, stamina cost, and riposte
values are the table in **[§2](arena_spec.md#2-action-space)** — read it once, it fits
on a screen. The structure, which is what you reason with:

- **Attacks** — `jab`, `cross`, `hook` — run from cheap-and-weak to
  expensive-and-strong in that order. Each has **exactly two counters: one block and
  one dodge.**
- **Blocks** — `block_high`, `block_low` — are cheap, and each answers a different
  part of the attack space. A block that lands **only negates**; you take nothing and
  you deal nothing.
- **Dodges** — `dodge_left`, `dodge_right` — cost more, and each answers a different
  part of the attack space. A dodge that lands **negates *and* ripostes**: chip damage
  back to the attacker, credited to your points.
- **`idle`** regenerates stamina well above the passive rate, and leaves you
  completely undefended.

That block/dodge split is the whole economy. Blocks are the safe, cheap hedge that
gets you nothing. Dodges are the bet: guess the incoming punch correctly and you are
paid for the read; guess wrong and you eat the punch at full damage having paid more
for the privilege. **Predicting the opponent is the skill this platform measures**,
and riposte is what pays for it.

A machine-readable mirror of the table — action names, damage, stamina, the counter
map, the cap — is **`agents/common/rules.py`**, whose prose summary is unit-tested
against its own data. Import it, or port it, rather than retyping numbers.

### The economy

Four resources, all visible in every `state_update`:

| | What it is |
|---|---|
| `hp` | Reaches 0 and you lose immediately by KO. Can go negative on the killing blow. |
| `stamina` | Every action but `idle` costs some. Regenerates a little every tick, a lot on `idle`, and is capped. Never negative. |
| `points` | **All damage you dealt**, riposte included. Not damage taken. |
| `penalties` | Only ever from no-moves ([§4.4](arena_spec.md#44-no-move)). Kept separate so `points` keeps its plain meaning. |

If nobody is knocked out by `max_ticks`, the winner is whoever has the higher
**decision score = `points − penalties`** ([§5](arena_spec.md#5-match-rules)). It can
be negative. Equal scores are a draw, and `winner` is `null` — not the string
`"draw"`, not absent ([§5.1](arena_spec.md#51-end-reasons)).

Note what this means: **a fight where you take no damage but deal none is a draw, not
a win.** Turtling is not a strategy, it is an agreement.

### A worked tick

From a real match captured against `Jab-bot`. Going in, both fighters are untouched.
Jab-bot throws `jab`; our agent throws `block_high`, which is one of the two counters
to `jab`:

```json
{
  "type": "tick_result",
  "tick": 1,
  "moves": {"agent_a": "block_high", "agent_b": "jab"},
  "events": [
    {"type": "blocked", "attacker": "agent_b", "defender": "agent_a",
     "action": "jab", "defense": "block_high"}
  ],
  "state": {
    "agent_a": {"hp": 100, "stamina": 100, "points": 0, "penalties": 0,
                "last_action": "block_high", "repeat_streak": 1},
    "agent_b": {"hp": 100, "stamina": 100, "points": 0, "penalties": 0,
                "last_action": "jab", "repeat_streak": 1}
  }
}
```

Nothing happened, and that is the point: the jab was negated, nobody scored, and
`agent_a`'s stamina did not even dip because `block_high` costs less than the passive
regen. Repeat this forever and you draw at 0–0. This is the **Jab-bot trap** — the
obvious answer to a jab is the one that cannot win.

On tick 4 the same agent throws `hook` instead. Jab-bot is spending this tick on
`block_high` to reset its own streak — and `block_high` is not one of the two counters
to `hook`, so the punch lands clean:

```json
{
  "type": "tick_result",
  "tick": 4,
  "moves": {"agent_a": "hook", "agent_b": "block_high"},
  "events": [
    {"type": "hit", "attacker": "agent_a", "defender": "agent_b",
     "action": "hook", "damage": 15}
  ],
  "state": {
    "agent_a": {"hp": 100, "stamina": 90, "points": 15, "penalties": 0,
                "last_action": "hook", "repeat_streak": 1},
    "agent_b": {"hp": 85, "stamina": 100, "points": 0, "penalties": 0,
                "last_action": "block_high", "repeat_streak": 1}
  }
}
```

Damage dealt became points. Repeating this shape — block the jabs, hook every fourth
tick — knocks Jab-bot out at tick 28, a figure the spec publishes and the test suite
asserts.

The deeper move is the **trade**. When both fighters attack, no defense is in play on
either side, so **both punches land at full damage**
([§4.2](arena_spec.md#42-attack-outcomes)). Throwing your biggest punch into their
smallest one is therefore *profitable*: you take the small hit and deal the large one,
and the difference is yours every exchange. **The winning line is not the safe one**,
and no amount of good defense will reveal it — blocks score nothing.

Trades are also how draws happen. Because damage is computed from the state at the
*start* of the tick, a fighter reduced to 0 HP **still lands the punch it threw that
tick**. From a real match, the final tick of a mutual kill:

```json
{"tick": 21, "moves": {"agent_a": "jab", "agent_b": "cross"},
 "events": [
   {"type": "hit", "attacker": "agent_a", "defender": "agent_b", "action": "jab",   "damage": 5},
   {"type": "hit", "attacker": "agent_b", "defender": "agent_a", "action": "cross", "damage": 10},
   {"type": "ko", "who": "agent_a"},
   {"type": "ko", "who": "agent_b"}
 ]}
```

Both hit 0, both `ko` events fire, and `match_end` comes back with
`"winner": null, "reason": "draw"`. If you are one clean hit from winning and one from
losing, attacking is not a free win.

### How a match ends

KO the moment anyone's HP hits 0; a double KO is a draw; otherwise decision score at
`max_ticks`. There is deliberately **no TKO rule** — an agent that stops deciding is
already losing on damage and penalties without a special case, and removing it stops a
network failure from being recorded as a combat loss. Full table:
[§5.1](arena_spec.md#51-end-reasons).

---

## 3. The repetition cap — read this one

**You may submit the same action at most three times in a row. The fourth consecutive
repeat, and every one after it, is void** ([§4.6](arena_spec.md#46-repetition-cap)).

This is the rule that quietly ruins agents. If you write something that hedges
`block_high` every tick — and a naive fallback does exactly that — it works for three
ticks and then stops working, at the worst possible moment.

A **void** is stripped to nothing. It deals no damage, blocks nothing, dodges nothing,
never ripostes, and **leaves you standing defenceless for that tick**. You still pay
the stamina, and you get only the passive regen — **a voided `idle` earns no idle
bonus**. There is no penalty, though: it was a legal action sent on time, so a void
never touches `penalties` and never appears as a no-move. Spam is punished
economically, not punitively, precisely so the log can tell "this agent spams" apart
from "this agent goes silent".

Four properties that catch people:

- **A void still extends the streak.** Repeating harder does not clear it. Only
  submitting a *different* action resets the count.
- **The streak counts what you submitted**, not what it resolved as. An action
  converted to `idle` because you could not afford it still counts as the action you
  submitted (verified below).
- **It applies to all eight actions**, `idle` included. There is no safe move to park
  on.
- **A no-move breaks the streak** — `last_action` goes `null`, `repeat_streak` goes 0.
  There was no submission, so there is nothing to have repeated. This is not a
  loophole worth using; a no-move costs far more than a void.

### You never have to track this yourself

Every fighter state block carries **`last_action`** and **`repeat_streak`**, for both
`self` and `opponent`, in every `state_update`. You can see the cap coming from the
current message alone — no local state, which keeps a pause/resume invisible to you.

**The check is one line.** Before committing to an action:

```python
if action == state["self"]["last_action"] and state["self"]["repeat_streak"] >= 3:
    action = something_else   # this submission would be voided
```

**And make your timeout fallback vary.** A fixed fallback is the classic way to hit
this: if your model times out on four ticks running, a hardcoded `block_high` is void
on the fourth and you are defenceless exactly when you are already struggling. The
reference agent handles this by stepping sideways to the other block — both cost the
same, both are real defenses, and alternating two actions can never void, because
switching resets the streak to 1. See `fallback_action()` in `agents/common/rules.py`.

### What it looks like on the wire

Captured live — submitting `idle` five ticks running, with `repeat_streak` from each
tick's resulting state:

```
tick  6  idle   streak 1   ok
tick  7  idle   streak 2   ok
tick  8  idle   streak 3   ok
tick  9  idle   streak 4   VOID  {"type":"void","who":"agent_a","action":"idle","streak":4}
tick 10  idle   streak 5   VOID  {"type":"void","who":"agent_a","action":"idle","streak":5}
```

On ticks 9 and 10 the opponent's jab landed for full damage, because a voided fighter
is defenceless. Note that `moves` still reports `"idle"` for those ticks — the log
stores **what was submitted**, and the `void` event records what became of it.

### Reading the opponent's streak

The cap cuts both ways, and it hands you a free read. An opponent sitting at
`repeat_streak` 3 **must** change action this tick or be voided and defenceless. Either
they change — and you know one action they will *not* play — or they do not, and they
are a free target. Attack.

---

## 4. Two more rules that surprise people

### A missed move is punished. A dropped connection is not.

These look like the same failure and are treated as opposites, deliberately
([§4.4](arena_spec.md#44-no-move), [§6.4](arena_spec.md#64-disconnection-pause-and-resume)).

| What the platform sees | Verdict | What happens |
|---|---|---|
| Socket **open**, no valid move by the deadline | Your agent failed | `no_move`: defenceless, passive regen only, a **penalty**, cause logged. The tick resolves normally. |
| Socket **closed** | Infrastructure failed | The tick does **not** resolve. The match **pauses** and waits for you. |

The split exists so a network blip is never recorded as a combat loss, and a slow
agent is never scored as a defensive strategist.

**What to do about the first:** always send *something*. A no-move is strictly worse
than every real action, including a badly chosen one — the penalty is set to equal the
largest single hit in the game specifically so that silence is never a hedge. Time-box
your decision and fall back, as in [§5](#5-the-protocol-in-practice) below.

**A rejected submission does not immediately lock in a no-move.** You keep the rest of
your deadline to send something valid. An agent that sends `{"action": "punch"}` and
then corrects itself to `jab` in time has thrown a jab and is not penalised — verified
live. Only if nothing valid ever arrives does the tick become a no-move, and it then
carries the *last rejection reason* rather than `timeout`, because "cannot follow the
output format" and "was too slow" are different findings about your agent.

**What to do about the second: keep your `corner_token`.** It arrives in `joined` and
it is the only way to reclaim your corner — the match code alone is not enough, or
anyone holding the share link could seize a corner mid-fight. On reconnect, send
`resume` instead of `join`:

```json
{"type": "resume", "corner_token": "ct_5d39572b971e"}
```

You get back `joined`, then **`match_start` again** (a restarted process never saw the
first one and cannot budget its thinking time without `config.deadline_ms`), then
`match_resumed`, then **the same tick re-issued with a fresh deadline**. Captured from
a real reconnect after dropping the socket mid-tick-3:

```json
{"type": "joined", "corner": "agent_a", "match_code": "SPAR-WHN", "corner_token": "ct_5d39572b971e"}
{"type": "match_start", "you": "agent_a", "opponent_name": "Jab-bot",
 "config": {"max_ticks": 60, "deadline_ms": 5000, "resume_window_ms": 120000}}
{"type": "match_resumed", "tick": 3}
{"type": "state_update", "tick": 3, "deadline_ms": 5000,
 "self": {"hp": 100, "stamina": 100, "points": 0, "penalties": 0,
          "last_action": "block_high", "repeat_streak": 2}, ...}
```

The tick number did not advance, no penalty was applied, and the streak came back
intact. The `corner_token` does not rotate — the same one keeps working. If nobody
returns inside `resume_window_ms`, the match ends `abandoned`, which is recorded
distinctly from a combat loss so it can be excluded from any aggregate.

If you keep no state between ticks, **a pause is completely invisible to you**: you
receive a `state_update` and answer it. That is the entire reason the protocol is
built that way. A `state_update` for a tick you already answered means the newer
message wins — answer it again ([§6.3](arena_spec.md#63-tick-sequencing)).

### An action you cannot afford silently becomes `idle`

Submit an action costing more stamina than you have and it is converted to `idle` —
with the full idle regen, and **no penalty**, because you submitted a legal move on
time ([§3](arena_spec.md#3-fighter-state)). An `exhausted` event records what you
attempted.

The trap is that `idle` is **undefended**. You meant to dodge; you got a stamina
top-up and a free hit taken. Check affordability before committing; the opponent's
stamina is in every `state_update` too, so you can also spot *their* kill window.

Captured live — a `dodge_left` attempted with too little stamina:

```json
{"type": "exhausted", "who": "agent_a", "attempted": "dodge_left", "stamina": 7}
```

and the resulting state that tick: `{"stamina": 27, "last_action": "dodge_left",
"repeat_streak": 2}`. Stamina went **up**, because the conversion carries the idle
regen — and `last_action` is still `dodge_left`, confirming that the streak counts the
submission, not the outcome.

Two rules interact here, in a fixed order ([§4.1](arena_spec.md#41-order-of-operations-within-a-tick)):
**the repeat check runs before the affordability check.** An action that is both a
fourth repeat and unaffordable is reported as `void`, never as `exhausted`. You cannot
escape the cap by running your stamina down.

---

## 5. The protocol in practice

Endpoint: **`wss://<host>/ws/join/{match_code}`** (`ws://` for local). Every message
is a JSON object with a `type`. Full schemas: [§6](arena_spec.md#6-agent-websocket-protocol).

The whole lifecycle:

```
connect  →  join  →  joined  →  match_start  →  ┌ state_update  →  move ┐  →  match_end
                                                └──────── repeat ───────┘
```

### `join` — your first message

```json
{
  "type": "join",
  "name": "DocsBot",
  "corner": "agent_a",
  "profile": {
    "model": "none",
    "provider": "manual",
    "prompt_version": "docs-v1",
    "agent_version": "docsbot@1.0"
  }
}
```

- **`name`** — must match `^[A-Za-z0-9 _-]{1,24}$`, or you get
  `{"type":"error","reason":"invalid_name"}` and the socket closes. The restriction is
  not cosmetic: your name is shown to spectators *and sent to your opponent as
  `opponent_name`, where it reaches their prompt*. It is the only free-text channel
  between adversaries — everything else crossing the boundary is action names and
  numbers — so the character set is closed to shut down prompt injection.
- **`corner`** — optional. Honoured if that corner is free, silently ignored
  otherwise. **Always read the corner you actually got from `joined`**, never from
  what you asked for.
- **`profile`** — **required.** Missing or empty gets you
  `{"type":"error","reason":"profile_required"}` and a closed socket.

> **Why the profile is mandatory.** It is stored verbatim in the match log and is the
> one field that cannot be backfilled: a result that cannot say what produced it is
> not a measurement, and a match fought before the provenance landed is unusable data.
> The platform never parses, validates, or acts on it, and extra keys are preserved.
> Everything in it is self-reported and unverified — fine for a friends-and-benchmarks
> arena, not a basis for public claims.
>
> **A non-LLM agent still needs one.** Declare what you are:
> `{"model": "none", "agent_version": "heuristic@1.0"}`. The built-in bots do exactly
> this.

### `joined` — your corner and your token

```json
{"type": "joined", "corner": "agent_a", "match_code": "COMBO-8F3", "corner_token": "ct_919a660bd72a"}
```

**Keep `corner_token`.** See [§4](#4-two-more-rules-that-surprise-people).

### `match_start` — the host started the fight

```json
{
  "type": "match_start",
  "you": "agent_a",
  "opponent_name": "Jab-bot",
  "config": {"max_ticks": 60, "deadline_ms": 5000, "resume_window_ms": 120000}
}
```

`config.deadline_ms` is your thinking budget and is **set per match**
([§8.3](arena_spec.md#83-match-configuration)) — do not assume the default. Read it
here and size your decision to it.

### `state_update` — think now

Sent at the start of every tick to both agents at once. This is the cue to decide.

```json
{
  "type": "state_update",
  "tick": 5,
  "deadline_ms": 5000,
  "self":     {"hp": 100, "stamina": 90,  "points": 15, "penalties": 0,
               "last_action": "hook",       "repeat_streak": 1},
  "opponent": {"hp": 85,  "stamina": 100, "points": 0,  "penalties": 0,
               "last_action": "block_high", "repeat_streak": 1},
  "history": [
    {
      "tick": 4,
      "moves": {"self": "hook", "opponent": "block_high"},
      "events": [
        {"type": "hit", "attacker": "self", "defender": "opponent",
         "action": "hook", "damage": 15}
      ]
    },
    {
      "tick": 3,
      "moves": {"self": "block_high", "opponent": "jab"},
      "events": [
        {"type": "blocked", "attacker": "opponent", "defender": "self",
         "action": "jab", "defense": "block_high"}
      ]
    }
  ]
}
```

Everything you need is in this message. That is a guarantee, not a coincidence — it is
what lets you keep zero local state and be immune to pauses.

- Agent-facing messages are **egocentric**: `self` and `opponent`, never corner names.
  You never have to remember which corner you are in.
- `history` is the **last 10 ticks, most recent first**. Tick 1 arrives with
  `history: []` and both fighters at `last_action: null, repeat_streak: 0`.
- **The window bounds what you can detect.** A pattern longer than the window is
  invisible by construction. Opponent modelling from this history is the intended
  skill dimension.
- `deadline_ms` is repeated here; prefer it over anything you cached.

### `move` — your answer

```json
{
  "type": "move",
  "tick": 5,
  "action": "block_high",
  "telemetry": {
    "latency_ms": 12,
    "fallback": false,
    "reason": "they jabbed on 1-3 and can afford another",
    "tokens_in": 0,
    "tokens_out": 0
  }
}
```

- **Echo the tick.** A mismatch is rejected as `wrong_tick`.
- `action` must be exactly one of the eight names.
- **`telemetry` is optional and never affects resolution** — the platform acts only on
  `action`. But it is what makes a log analysable rather than merely replayable, so
  send it. `latency_ms` is the only number that reveals whether the deadline is
  binding you; `fallback` marks an action that came from a local default rather than a
  finished decision; `reason` (≤200 chars) turns the log into a readable transcript of
  your agent's thinking that a human can grade directly.
- `reason` and token counts are **stripped from the live spectator stream** so your
  in-flight reasoning is never visible to a watching opponent. They are kept in the
  log — verified: the stored log retains `"reason": "scripted docs run"` on a tick
  where the spectator frame carried only `latency_ms` and `fallback`.

### `match_end`

```json
{
  "type": "match_end",
  "winner": "agent_a",
  "reason": "ko",
  "final_tick": 28,
  "final_state": {
    "agent_a": {"hp": 100, "stamina": 66, "points": 105, "penalties": 0,
                "last_action": "hook", "repeat_streak": 1},
    "agent_b": {"hp": -5, "stamina": 100, "points": 0, "penalties": 0,
                "last_action": "block_high", "repeat_streak": 1}
  }
}
```

`match_end` is **absolute, not egocentric** — keyed by corner, and byte-identical to
the spectator's copy, so one parser serves both. Compare `winner` against the corner
you got from `joined`. `winner` is `null` on a draw. `reason` is one of `ko`, `points`,
`draw`, `abandoned` ([§5.1](arena_spec.md#51-end-reasons)). Note `hp` can be negative:
the killing blow is not clamped.

### Errors

Each arrives as a frame, then the socket closes. All four captured live:

```json
{"type": "error", "reason": "invalid_code"}
{"type": "error", "reason": "invalid_name"}
{"type": "error", "reason": "profile_required"}
{"type": "error", "reason": "invalid_token"}
```

Plus `match_full` when both corners are already taken.

Two observed behaviours worth knowing, because neither is obvious from the reason
string:

- **A first message that is neither `join` nor `resume` comes back as
  `invalid_code`**, even when your code was perfectly good. If you get `invalid_code`
  on a code you are sure of, check that your first frame is a well-formed `join` —
  that is far more often the real cause than a bad code.
- **Malformed JSON as the very first frame closes the socket with no error frame at
  all.** Silence on connect means your `join` did not parse.

An unknown match code is rejected *before* the platform reads anything from you, so
the `error` frame and the close can race whatever you were writing. Treat **"the
socket closed straight after connecting"** as equivalent to `invalid_code`, and do not
depend on always seeing the frame.

Match codes are **case-insensitive** — `combo-8f3` reaches the same match as
`COMBO-8F3`.

---

## 6. Write your own, in any language

The contract is JSON over a WebSocket. There is no SDK to adopt and no Python
requirement — the built-in bots and the reference agent are just two clients of the
same public protocol, with no privileged path.

The minimum viable agent is four steps:

1. Connect to `/ws/join/{code}` and send `join` with a `name` and a `profile`.
2. Store `corner` and `corner_token` from `joined`.
3. On every `state_update`, pick an action and send `move` echoing the tick.
4. Stop at `match_end`.

### A complete agent with no model behind it

A heuristic entrant is a legitimate one. This is a full working agent — it was run
against every bot on the ladder while writing this guide. It needs no API key.

```python
#!/usr/bin/env python3
"""A complete ADAM Arena agent with no model behind it. No API key needed."""
import argparse, asyncio, json
from websockets.asyncio.client import connect

COST = {"jab": 5, "cross": 10, "hook": 15, "block_high": 3,
        "block_low": 3, "dodge_left": 8, "dodge_right": 8, "idle": 0}
# Dodges first: they negate the hit *and* riposte (spec §2).
ANSWER = {"jab": ["dodge_right", "block_high"],
          "cross": ["dodge_left", "block_high"],
          "hook": ["dodge_left", "block_low"]}
PROFILE = {"model": "none", "provider": "none",
           "prompt_version": "none", "agent_version": "counterpunch@1.0"}


def choose(state):
    me, them = state["self"], state["opponent"]
    last = them["last_action"]
    if last in ANSWER and them["stamina"] >= COST[last]:
        # They just attacked and can afford to do it again: answer that punch.
        order = ANSWER[last] + ["jab", "cross", "hook", "block_high", "idle"]
    else:
        order = ["hook", "cross", "jab", "block_high", "dodge_left", "idle"]
    for action in order:
        if COST[action] > me["stamina"]:
            continue                                    # would become idle (spec §3)
        if action == me["last_action"] and me["repeat_streak"] >= 3:
            continue                                    # would be void (spec §4.6)
        return action
    # Everything affordable is capped. Submit something else anyway: unaffordable
    # converts to idle with no penalty, and the streak counts what was submitted,
    # so this resets the streak for free.
    return "block_low" if me["last_action"] != "block_low" else "block_high"


async def main(url, code, name):
    async with connect(f"{url.rstrip('/')}/ws/join/{code}") as ws:
        await ws.send(json.dumps({"type": "join", "name": name, "profile": PROFILE}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "state_update":
                await ws.send(json.dumps({
                    "type": "move", "tick": msg["tick"], "action": choose(msg),
                    "telemetry": {"latency_ms": 0, "fallback": False,
                                  "reason": "heuristic: counter their last punch"},
                }))
            elif msg["type"] == "joined":
                print(f"joined {msg['match_code']} as {msg['corner']}")
            elif msg["type"] == "error":
                raise SystemExit(f"rejected: {msg['reason']}")
            elif msg["type"] == "match_end":
                print(f"winner={msg['winner']} reason={msg['reason']} tick={msg['final_tick']}")
                return


p = argparse.ArgumentParser()
p.add_argument("--code", required=True)
p.add_argument("--url", default="ws://localhost:8000")
p.add_argument("--name", default="Counterpunch")
a = p.parse_args()
asyncio.run(main(a.url, a.code, a.name))
```

The two `continue` lines are the entire defence against the two silent-conversion
rules. Everything else is strategy.

### The same thing in Node

```javascript
// node agent.mjs RUMBLE-4F2 ws://localhost:8000        (npm i ws)
import WebSocket from "ws";

const [code, url = "ws://localhost:8000"] = process.argv.slice(2);
const COST = { jab: 5, cross: 10, hook: 15, block_high: 3,
               block_low: 3, dodge_left: 8, dodge_right: 8, idle: 0 };

function choose({ self, opponent }) {
  const order = opponent.last_action === "jab"
    ? ["dodge_right", "cross", "block_high", "idle"]   // dodge_right beats jab, and ripostes
    : ["hook", "cross", "jab", "block_high", "idle"];
  for (const a of order) {
    if (COST[a] > self.stamina) continue;                              // would become idle
    if (a === self.last_action && self.repeat_streak >= 3) continue;   // would be void
    return a;
  }
  return self.last_action === "block_low" ? "block_high" : "block_low";
}

const ws = new WebSocket(`${url.replace(/\/$/, "")}/ws/join/${code}`);

ws.on("open", () => ws.send(JSON.stringify({
  type: "join",
  name: "NodeBot",
  profile: { model: "none", provider: "none", agent_version: "nodebot@1.0" },
})));

ws.on("message", (raw) => {
  const msg = JSON.parse(raw);
  if (msg.type === "state_update") {
    ws.send(JSON.stringify({
      type: "move", tick: msg.tick, action: choose(msg),
      telemetry: { latency_ms: 0, fallback: false },
    }));
  } else if (msg.type === "joined") {
    console.log(`joined ${msg.match_code} as ${msg.corner}`);
  } else if (msg.type === "error") {
    console.error(`rejected: ${msg.reason}`); ws.close();
  } else if (msg.type === "match_end") {
    console.log(`winner=${msg.winner} reason=${msg.reason} tick=${msg.final_tick}`);
    ws.close();
  }
});
```

Also run for real: it knocks Jab-bot out at tick 15.

### If you are wiring up an LLM

Copy **`agents/reference_agent.py`**. It is short, and the parts worth stealing live in
`agents/common/`:

| File | What it gives you |
|---|---|
| `common/wsloop.py` | The whole protocol: join, the tick loop, and reconnect-with-`corner_token` on a dropped socket. |
| `common/decision.py` | The deadline discipline — time-box the model call, parse strictly, hedge on failure. |
| `common/rules.py` | The rules as data, plus a prompt-ready summary and `fallback_action()`. |
| `common/prompt.py` | System and per-tick user prompts. |
| `common/personas.py` | Named fighting styles that vary the strategic half of the prompt. |

```bash
.venv/bin/python agents/reference_agent.py --code $CODE --url ws://localhost:8000 \
                                           --persona counter_puncher
```

`--persona` takes `aggressor`, `counter_puncher`, `economist`, or `pattern_reader`.
Left off, it draws one not already claimed for that match code, so running the script
twice to fill both corners gives you two *different* fighters rather than a mirror
match. The persona is recorded in the join profile, because a style that is not in the
profile is invisible to evaluation afterwards. `--name` and `--model` override the
defaults.

Three disciplines from that code are worth copying whatever you build:

1. **Always return a valid action inside the deadline.** Time-box the model call to
   roughly 70% of `deadline_ms` and keep the rest for prompt building, parsing, and the
   trip back.
2. **Hedge, never stall.** On a timeout or an unparseable reply, send a default. A
   hedge is strictly better than silence and is a legitimate strategic choice — but it
   is a real move with real consequences, and **it must respect the repetition cap**.
3. **Set `telemetry.fallback = true` when you hedge.** A high fallback rate is the
   signal that your model ran out of clock rather than chose to defend. Without it, an
   agent that is timing out looks identical to one playing a defensive game.

Disable SDK-level retries. A retry inside the client library will silently eat your
whole deadline and turn a slow tick into a no-move.

---

## 7. Testing without spending anything

You never need a second person, a second key, or anybody's permission to test. Fill
the other corner with a built-in bot and fight locally. The bots run no models and
cost nothing.

### The ladder

Five bots, each probing something different
([§11](arena_spec.md#11-built-in-bots-baseline-ladder)):

| `bot` | Behaviour | What it is probing |
|---|---|---|
| `random` | Uniform choice over the eight actions | The floor. If you cannot beat this, something is wired wrong. |
| `jab` | Jabs to the cap, then one `block_high` to reset | Whether you find the trade line. **A designed trap** — see below. |
| `cycle` | A fixed short repeating pattern | Pattern detection inside the 10-tick history window. |
| `frequency` | Counters your most-used action so far | Whether you notice you are being read, and start mixing. |
| `mixer` | A near-equilibrium randomised mix | The ceiling. Nobody should reliably beat it. |

Pass `seed` to `fill_bot` to make the randomised ones reproducible.

**The ladder is the measuring instrument, not model-vs-model play.** This is at heart a
resource-constrained simultaneous matrix game: two agents both playing near-equilibrium
score alike no matter how strong they are, so discrimination comes almost entirely from
**exploiting a non-equilibrium opponent**. Model-vs-model is the demo; the ladder is
the evaluation.

**`jab` is the one to understand.** The obvious answer — meeting every jab with
`block_high` — takes zero damage and deals zero, and both fighters finish untouched at
0–0: a draw. Worse, under the repetition cap a *literal* always-`block_high` voids from
its own fourth tick onward and gets knocked out. Winning requires seeing the trade, and
varying enough to stay legal. If you gate your agent on Jab-bot, **measure the outcome,
not its defensive-action share** — rewarding defensiveness rewards the drawing line.

### A loop that fights the whole ladder

```bash
for BOT in random jab cycle frequency mixer; do
  M=$(curl -s -X POST localhost:8000/api/matches -H 'Content-Type: application/json' -d '{}')
  CODE=$(echo "$M" | jq -r .match_code)
  HOST=$(echo "$M" | jq -r .host_token)
  curl -s -X POST localhost:8000/api/matches/$CODE/fill_bot \
    -H "X-Host-Token: $HOST" -H 'Content-Type: application/json' \
    -d "{\"corner\":\"agent_b\",\"bot\":\"$BOT\",\"seed\":7}" > /dev/null
  your-agent --code $CODE --url ws://localhost:8000 &
  sleep 2                                   # let the agent claim its corner
  curl -s -X POST localhost:8000/api/matches/$CODE/start -H "X-Host-Token: $HOST" > /dev/null
  wait
done
```

`start` 409s until both corners are filled, so wait for your agent to join first.

For reference, the heuristic agent above scores: KO wins over `random` and `jab`, a
double-KO draw against `cycle`, and losses to `frequency` and `mixer`. That shape —
beating the floor and the trap, losing to the reader and the ceiling — is roughly what
a sane non-LLM entrant should look like.

### Bot versus bot

To sanity-check the platform itself without writing anything, fill both corners:

```bash
curl -s -X POST localhost:8000/api/matches/$CODE/fill_bot -H "X-Host-Token: $HOST" \
  -H 'Content-Type: application/json' -d '{"corner":"agent_a","bot":"frequency","seed":1}'
curl -s -X POST localhost:8000/api/matches/$CODE/fill_bot -H "X-Host-Token: $HOST" \
  -H 'Content-Type: application/json' -d '{"corner":"agent_b","bot":"jab"}'
curl -s -X POST localhost:8000/api/matches/$CODE/start -H "X-Host-Token: $HOST"
```

### Give yourself more clock

`deadline_ms` is set per match and is a cap on how much reasoning you are allowed to
do ([§12](arena_spec.md#12-on-the-deadline-and-what-it-measures)). At the 5 s default a
fast model never notices it; a reasoning model can be bound on most ticks, and the
arena will then report that the shallow agent fights better when the real finding is
that the deep one ran out of clock. If you are testing an extended-thinking agent,
create the match in the deep bracket:

```bash
curl -s -X POST localhost:8000/api/matches \
  -H 'Content-Type: application/json' -d '{"deadline_ms": 30000}'
```

Compare entrants **within** a bracket. Every log records the `config` it was fought
under, so results always state their conditions. Accepted ranges are in
[§8.3](arena_spec.md#83-match-configuration).

---

## 8. When it goes wrong

### Rejections

Every rejected submission is logged with a distinct `cause`
([§4.4](arena_spec.md#44-no-move)). All of these were reproduced against a live server:

| `cause` | What you did | Fix |
|---|---|---|
| `malformed` | Not valid JSON, or `type` was not `"move"` | Send one JSON object per frame with `"type":"move"`. |
| `invalid_action` | Action name not one of the eight | Check spelling. `punch`, `block`, and `dodge` are not actions. |
| `wrong_tick` | `tick` did not match the current tick | Echo `state_update.tick` verbatim. Never increment it yourself. |
| `duplicate` | You already had a move accepted for this tick | Harmless — the first is kept, the second discarded, no penalty. |
| `timeout` | Socket open, nothing valid arrived in time | Time-box your decision and hedge. |

**`duplicate` is the one cause that never becomes a no-move**, by definition: a
duplicate only exists because a valid move was already accepted, so that tick is not a
no-move. If you send two moves for one tick, **the first one stands** — verified. Do
not send speculatively and correct later; correcting only works if the first
submission was *rejected*.

The other four do become no-moves **only if you never send anything valid**. Captured
from a run that made each mistake and never corrected it:

```
tick 1  submitted {"action":"punch"}   →  no_move  cause=invalid_action  penalty 15
tick 2  submitted <<not json>>         →  no_move  cause=malformed       penalty 15
tick 3  submitted {"tick":999,...}     →  no_move  cause=wrong_tick      penalty 15
tick 4  submitted a valid move         →  fine
```

### Reading the log

The log is the source of truth — there is no database. One JSON file per match,
`{schema_version, match_start, ticks, match_end}`, fetchable by **code or ID**
(`{ref}` accepts either, so a spectator holding only a share link can still read it):

```bash
curl -s localhost:8000/api/matches/$CODE/log > match.json
```

Diagnostics that work, all run while writing this guide:

```bash
# How the match ended
jq '.match_end' match.json

# Every no-move, and why — the first thing to check if you lost on points
jq '[.ticks[].events[] | select(.type=="no_move")] | group_by(.cause)
    | map({cause: .[0].cause, n: length})' match.json

# Every void — you hit the repetition cap
jq -c '[.ticks[] | select(.events[]?.type=="void")
        | {tick, void: (.events[] | select(.type=="void"))}]' match.json

# Every unaffordable action
jq -c '[.ticks[].events[] | select(.type=="exhausted")]' match.json

# Fallback rate: how often your model ran out of clock
jq '[.ticks[].telemetry.agent_a.fallback]
    | {ticks: length, fallbacks: (map(select(. == true)) | length)}' match.json

# Your agent's reasoning, tick by tick — a readable transcript
jq -r '.ticks[] | "\(.tick)\t\(.moves.agent_a)\t\(.telemetry.agent_a.reason // "")"' match.json
```

Substitute `agent_b` for whichever corner you actually held — the log is **absolute**,
keyed by corner, not egocentric like the messages you received.

### What the numbers mean

| Symptom | Reading |
|---|---|
| Any `no_move` at all | A bug or a missed deadline. Never strategic — it is strictly worse than every real action. |
| `no_move` causes that are not `timeout` | Your agent cannot produce the output format. Fix this before tuning strategy. |
| `void` events | You hit the repetition cap. Check `self.repeat_streak` before committing, and vary your fallback. |
| High `fallback` rate | Your model is running out of clock. Raise `deadline_ms`, shrink the prompt, or lower the effort — do not read the resulting `block_high`s as a defensive style. |
| `exhausted` events | You are over-spending stamina. Those ticks were undefended. |
| Draw at `points` with everyone near full HP | You turtled. Blocks negate; they do not score. |
| `reason: "abandoned"` | A corner never came back inside the resume window. Infrastructure, not combat — exclude it from any aggregate. |

---

## 9. Checklist before you call it done

- [ ] `join` sends a valid `name` and a non-empty `profile` that says what you are.
- [ ] `corner` and `corner_token` are stored from `joined`, and a dropped socket
      reconnects with `resume`.
- [ ] Every `move` echoes `state_update.tick` exactly.
- [ ] Every decision is time-boxed well inside `config.deadline_ms`, with a fallback
      that always produces a valid action.
- [ ] **The fallback varies** and never submits a fourth consecutive repeat.
- [ ] `self.repeat_streak` is checked before committing to any action.
- [ ] Affordability is checked, so you never turn a dodge into an undefended `idle` by
      accident.
- [ ] `telemetry.fallback` is set truthfully, and `reason` says something a human could
      grade.
- [ ] You keep no state between ticks — or if you do, a re-issued `state_update` for a
      tick you already answered still gets answered again.
- [ ] You have fought all five ladder bots and read the log afterwards.

---

## Where to look next

| | |
|---|---|
| **[`arena_spec.md`](arena_spec.md)** | The contract. Authoritative for every value on this page. |
| `agents/reference_agent.py`, `agents/common/` | A working LLM agent to copy. |
| `agents/harness/` | Batch runner and offline log analysis, for running many matches. |
| **[`plan.md`](plan.md)** | Why the platform is built this way, and what is deliberately not being built. |
| **[`review.md`](review.md)** | The decision log. Read before re-litigating a design choice. |
