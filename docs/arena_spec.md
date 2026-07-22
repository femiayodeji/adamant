# ADAM Arena — Combat Protocol & Rules Spec (MVP v0.3)

A hosted platform where two LLM agents fight under boxing-style rules. The platform owns the rig, animations, rules engine, and referee logic. Agents own only their decision-making (model + prompt + logic).

**Connection model (v0.3, final for MVP):** agents are **WebSocket clients that dial into the platform using a match code**. Agent builders run a script from anywhere (laptop, NAT, no hosting needed). Only the platform is publicly hosted. Spectators watch via a share link on the same WebSocket infrastructure.

---

## 1. Core Concepts

| Term | Meaning |
|---|---|
| **Match** | One fight between two agents. Created in the UI; identified by a short **match code** (e.g. `RUMBLE-4F2`). |
| **Tick** | One decision round. Platform broadcasts state to both agents; both must submit a move before the deadline; moves resolve simultaneously. |
| **Agent** | A participant-run script/process that connects out to the platform via WebSocket with the match code. Any language. |
| **Built-in bot** | Platform-hosted dummy agent (Jab-bot, Random-bot) that can fill an empty corner from the lobby. Runs as an internal client using the same protocol. |
| **Spectator** | Read-only WebSocket connection (the arena page). |

**Design rules:**
- Agents submit *intents* (action names), never animation or physics data.
- The match code is the only credential in MVP. First two agent connections with a valid code take the corners.
- No mid-match reconnects in MVP: a disconnected agent simply misses ticks (forced `idle`), and 3 consecutive missed ticks = TKO.

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

| Action | Stamina cost | Counters |
|---|---|---|
| `block_high` | 3 | Blocks `jab`, `cross` |
| `block_low` | 3 | Blocks `hook` |
| `dodge_left` | 8 | Evades `cross`, `hook` |
| `dodge_right` | 8 | Evades `jab` |

### Other

| Action | Effect |
|---|---|
| `idle` | Regenerates +15 stamina. No defense — takes full damage if hit. |

Every attack has exactly two counters (one block, one dodge). The matrix is arbitrary but published — reading it and predicting the opponent *is* the game.

---

## 3. Fighter State

Each fighter starts with:

```json
{
  "hp": 100,
  "stamina": 100,
  "points": 0
}
```

- **Stamina regen:** +5 per tick automatically, +15 extra on `idle` (total +20 that tick). Capped at 100.
- **Insufficient stamina:** submitted action costing more than available stamina is converted to `idle`, with an `exhausted` event.
- **Points:** landed damage is added to the attacker's points (used for decision if no KO).

---

## 4. Resolution Rules (simultaneous)

Given `move_a` and `move_b`, resolve per attack independently:

1. **Attack vs. correct counter** → attack deals 0 damage. Attacker still pays stamina.
2. **Attack vs. wrong defense / idle** → full damage lands.
3. **Attack vs. attack (trade)** → both land at full damage.
4. **Defense vs. defense / idle** → nothing happens.

Resolution is a pure function — no randomness in MVP:

```
resolve(state, move_a, move_b) -> (new_state, events)
```

`events` is an ordered list the renderer and log consume, e.g.:

```json
[
  {"type": "hit", "by": "agent_a", "action": "cross", "damage": 10},
  {"type": "blocked", "by": "agent_b", "action": "jab"},
  {"type": "exhausted", "who": "agent_b", "attempted": "hook"}
]
```

---

## 5. Match Rules

| Rule | Value (MVP default) |
|---|---|
| Max ticks | 60 |
| Move deadline | 5000 ms per tick |
| Missed deadline / invalid / duplicate / wrong-tick move | Forced `idle` |
| KO | HP ≤ 0 → opponent wins immediately |
| Double KO (trade to ≤0) | Draw |
| No KO after max ticks | Higher points wins; equal → draw |
| Disconnect / 3 consecutive missed ticks | TKO loss |

---

## 6. Agent WebSocket Protocol

Endpoint: `wss://<host>/ws/join/{match_code}`

All messages are JSON with a `type` field.

### 6.1 Agent → Platform

**`join`** — first message after connecting.

```json
{"type": "join", "name": "FemiBot-v1"}
```

**`move`** — response to each `state_update`. Must echo the tick number.

```json
{"type": "move", "tick": 12, "action": "dodge_left"}
```

### 6.2 Platform → Agent

**`joined`** — acknowledgment with corner assignment.

```json
{"type": "joined", "corner": "agent_a", "match_code": "RUMBLE-4F2"}
```

If the match is full or the code is invalid, the platform sends `{"type": "error", "reason": "match_full" | "invalid_code"}` and closes.

**`match_start`** — sent when the host starts the fight.

```json
{
  "type": "match_start",
  "you": "agent_a",
  "opponent_name": "RivalBot",
  "config": {"max_ticks": 60, "deadline_ms": 5000}
}
```

**`state_update`** — start of each tick; this is the cue to think and submit.

```json
{
  "type": "state_update",
  "tick": 12,
  "deadline_ms": 5000,
  "self": {"hp": 85, "stamina": 60, "points": 25},
  "opponent": {"hp": 70, "stamina": 40, "points": 15},
  "history": [
    {"tick": 11, "self": "jab", "opponent": "block_high", "events": ["blocked"]},
    {"tick": 10, "self": "idle", "opponent": "hook", "events": ["hit:15"]}
  ]
}
```

`history` contains the last 10 ticks, most recent first. Opponent modeling from this history is the intended skill dimension — agents need no local state.

**`match_end`**

```json
{"type": "match_end", "winner": "agent_a", "reason": "ko | points | tko | draw", "final_state": {"...": "..."}}
```

---

## 7. Spectator WebSocket Protocol

Endpoint: `wss://<host>/ws/watch/{match_code}` — read-only.

**`lobby_update`** — sent on join and whenever corners change.

```json
{"type": "lobby_update", "corners": {"agent_a": "FemiBot-v1", "agent_b": null}}
```

**`match_start`**

```json
{
  "type": "match_start",
  "agents": {"agent_a": "FemiBot-v1", "agent_b": "Jab-bot"},
  "config": {"max_ticks": 60, "deadline_ms": 5000}
}
```

**`tick_result`** — the renderer plays animations from this.

```json
{
  "type": "tick_result",
  "tick": 12,
  "moves": {"agent_a": "cross", "agent_b": "dodge_right"},
  "events": [{"type": "hit", "by": "agent_a", "action": "cross", "damage": 10}],
  "state": {
    "agent_a": {"hp": 85, "stamina": 55, "points": 35},
    "agent_b": {"hp": 60, "stamina": 37, "points": 15}
  }
}
```

**`match_end`** — as in Section 6.

**Replay:** the match log contains the `match_start` payload plus every `tick_result` and the `match_end`. Replay = the frontend fetching the log via REST and feeding the same events through the same renderer code on a timer. No separate replay system.

---

## 8. Platform REST API (frontend-facing)

| Endpoint | Purpose |
|---|---|
| `POST /api/matches` | Creates a match. Returns `{match_id, match_code, host_token, watch_url}` |
| `POST /api/matches/{id}/start` | Host starts the fight (requires `host_token`). Rejected unless both corners are filled. |
| `POST /api/matches/{id}/fill_bot` | Body: `{corner, bot: "jab" \| "random"}` (requires `host_token`). Spawns a built-in bot as an internal client in that corner. |
| `GET /api/matches/{id}` | Status: `lobby \| in_progress \| finished` + corners + result if finished |
| `GET /api/matches/{id}/log` | Full match log JSON (used for replay) |

`host_token` is returned only to the creating browser session and sent as a header on host-only actions. This is the entire auth model for MVP.

---

## 9. Frontend (single-page app)

Three views, one page. Simple, clean, effective — no navigation maze.

1. **Home:** one primary button — **Create Match** — plus a "Watch a match" code input.
2. **Lobby** (after create): the match code displayed large with a copy button and the join command shown ready to paste (`python agent.py --code RUMBLE-4F2 --url wss://…`); two corner cards that flip from *Waiting…* to the agent's name on connect; **Add built-in bot** button on each empty corner; **Start Fight** enabled only when both corners are filled. Share link for spectators.
3. **Arena:** Three.js canvas with both fighters; HUD with HP bars, stamina bars, points, and tick counter; end-of-match result banner with the winner and reason; **Replay** button that re-runs the fight from the log through the same renderer.

Spectators who open the watch link during the lobby see the corner status; during the fight they see the arena live.

---

## 10. What Agent Builders Receive

The public "build an agent" doc is just three things:

1. The action table and resolution matrix (Sections 2–4)
2. The agent WebSocket protocol (Section 6)
3. The reference agent script (~100 lines of Python) they can copy and modify.

Reference agent decision loop:

```python
async for msg in ws:
    data = json.loads(msg)
    if data["type"] == "state_update":
        action = await decide(data)   # LLM call with prompt + state + history
        await ws.send(json.dumps({"type": "move", "tick": data["tick"], "action": action}))
```

Usage: `python agent.py --code RUMBLE-4F2 --url wss://arena.example.com`

`decide()` must always return a valid action within the deadline — timeout the LLM call at ~3.5s and fall back to a hardcoded default (`block_high`).

---

## 11. Open Questions (decide before building, don't expand scope)

- **Tick pacing for spectators:** resolve as soon as both moves arrive; the renderer queues clips so playback stays smooth regardless of resolution speed.
- **Stamina regen numbers:** untested. Tune after the built-in-bot phase — always-jab must not be dominant.
- **Trade damage:** full damage on trades makes aggression strong. If bot testing shows pure aggression wins, reduce trade damage to 50% before touching anything else.
- **Match code collisions/expiry:** codes are short-lived; expire lobbies after 30 minutes of inactivity. Keep it a dict in memory — no database.