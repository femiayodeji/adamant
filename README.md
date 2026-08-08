# Adamant

*Adamant rules. Adaptive agents.*

Adamant is a combat evaluation platform for LLM agents. Two agents connect to a live match, each seeing the same fixed action set, the same deterministic resolution rules, and a rolling history of every move exchanged so far. Every tick, they choose: attack, block, or dodge — and a correct read is rewarded, a bad guess is punished, immediately and visibly.

The name is the design philosophy. The rules engine is adamant — deterministic, unbending, the same for every agent, every match. What's being measured is whether the agents can adapt: spot a pattern, exploit it, and adjust when the opponent stops being predictable. It's a small, legible arena for a hard question — can an agent actually learn its opponent, or is it just guessing well?

*The rules don't move. The agents have to.*

Agents are **WebSocket clients that dial in with a match code** — run a script from a laptop behind NAT, no hosting or public URL needed. Create a match, share the code, agents join, start the fight. Built-in bots can fill any empty corner, so the whole thing is testable solo.

The unit of evaluation is the **agent** — prompt + model + logic as a bundle — reasoning about combat. Not inference speed: ticks resolve simultaneously, so a faster agent gains no in-game advantage.

- **[`docs/agent_guide.md`](docs/agent_guide.md)** — **build an agent.** Start here if you have a match code and want to fight: the 5-minute path, the rules explained, the protocol with real payloads, and how to test against the built-in bots for free.
- **[`docs/adamant_spec.md`](docs/adamant_spec.md)** — the contract. Protocol, action space, damage and stamina values, resolution rules, message schemas, REST API. Build an agent from this alone.
- **[`docs/plan.md`](docs/plan.md)** — what is still unfinished, and the conventions that make match results comparable.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Optional: the browser UI. Without it the API and both WebSockets still work.
cd frontend && npm install && npm run build && cd ..

.venv/bin/uvicorn server.main:app --reload
```

Then open <http://127.0.0.1:8000>, or drive it from the command line:

```bash
# Create a match
curl -s -X POST localhost:8000/api/matches | tee /tmp/match.json

CODE=$(jq -r .match_code /tmp/match.json)
HOST=$(jq -r .host_token /tmp/match.json)

# Fill both corners with built-in bots and start.
# The JSON content type is required — without it FastAPI reads the body as a form
# and rejects it with a 422.
curl -s -X POST localhost:8000/api/matches/$CODE/fill_bot \
  -H "X-Host-Token: $HOST" -H "Content-Type: application/json" \
  -d '{"corner":"agent_a","bot":"frequency","seed":1}'
curl -s -X POST localhost:8000/api/matches/$CODE/fill_bot \
  -H "X-Host-Token: $HOST" -H "Content-Type: application/json" \
  -d '{"corner":"agent_b","bot":"jab"}'
curl -s -X POST localhost:8000/api/matches/$CODE/start -H "X-Host-Token: $HOST"

# Watch it back
curl -s localhost:8000/api/matches/$CODE/log | jq '.match_end'
```

To fight it yourself with an LLM agent:

```bash
.venv/bin/pip install -r agents/requirements.txt
export ANTHROPIC_API_KEY=...
.venv/bin/python agents/reference_agent.py --code $CODE --url ws://localhost:8000
```

Or use OpenRouter, and any model it fronts. `agents/openrouter_app` is a separate app
with three ready-made fighters — a model and a fighting style bundled together — that
can fill one corner or both:

```bash
echo 'OPENROUTER_API_KEY=...' >> .env       # gitignored
.venv/bin/python -m agents.openrouter_app --list
.venv/bin/python -m agents.openrouter_app --code $CODE --agent bulldozer --agent cipher
```

The same three are reachable from the lobby's **Add LLM agent** button, or over REST
(`GET /api/agents`, `POST /api/matches/{ref}/fill_agent`), which runs them inside the
server against its own key. Either way it **costs money**: one model call per LLM
corner per tick.

## Tests

```bash
.venv/bin/pytest                      # everything (~30s)
.venv/bin/pytest server/engine/tests  # the pure rules engine, no sockets
.venv/bin/pytest server/tests         # lifecycle, protocol, pause/resume, recovery
.venv/bin/pytest agents               # the published agent and the harness
.venv/bin/pytest server/engine/tests/test_table.py::test_no_defense_is_strictly_dominated
```

Balance simulation — the whole pure-strategy round-robin, no server needed:

```bash
.venv/bin/python -m server.engine.simulate
```

## Layout

```
server/
  engine/        pure rules: rules.py (the boxing table), resolver.py, strategies.py, simulate.py
  match.py       lifecycle state machine and tick loop, incl. pause/resume
  registry.py    in-memory matches, code generation, expiry, crash recovery
  matchlog.py    one JSON file per match — replay, recovery, and evaluation substrate
  protocol.py    message validation and construction
  corner_client.py  the socket shell every platform-run agent uses
  bots.py        the baseline ladder as ordinary WebSocket clients
  llm_agents.py  the OpenRouter roster, spawned into a corner by the host
  main.py        FastAPI: REST + /ws/join + /ws/watch + static frontend
agents/
  reference_agent.py   the published example (~120 lines, direct Anthropic SDK)
  common/              rules summary, prompt, decision loop, WS session, OpenRouter client
  openrouter_app/      three ready-made fighters on OpenRouter, runnable standalone
  harness/             batch runner, aggregation, offline log analysis (LiteLLM/OpenRouter)
frontend/        Next.js static export: home, lobby, arena + the Three.js renderer
```

## Running with Docker

The image is the whole platform — API, both WebSockets, and the built frontend — in one process. Nothing else to install.

```bash
docker compose up -d --build      # build and start
docker compose ps                 # is it healthy?
docker compose logs -f            # follow the logs (Ctrl-C just stops following)
```

Then open <http://localhost:8000>. First build takes a few minutes (it compiles the frontend); afterwards it is cached.

### Stopping

| What you want | Command | Match logs |
|---|---|---|
| Pause it, keep everything | `docker compose stop` | kept |
| Start it again | `docker compose start` | kept |
| Restart in place | `docker compose restart` | kept |
| Stop and remove the container | `docker compose down` | **kept** |
| Stop and wipe the match logs too | `docker compose down -v` | **destroyed** |

`docker compose down` is the normal way to stop. It removes the container and network but leaves the `arena-logs` volume alone, so finished matches stay replayable and interrupted ones still resume on the next `up`.

**`docker compose down -v` deletes your match logs.** Those logs are the evaluation dataset and the only record of every fight — there is no database backing them up. Only use `-v` when you actually want a clean slate.

Stopping is graceful: the container gets 30 seconds to finish the tick in flight and fsync the log before it is killed.

### Configuration

Set these in `docker-compose.yml` or the environment:

| Variable | Default | What it does |
|---|---|---|
| `ARENA_PORT` | `8000` | Host port to publish on. `ARENA_PORT=9000 docker compose up -d` to move it. |
| `PORT` | `8000` | Port *inside* the container. Hosts that assign one (Render, Fly, Cloud Run) set this themselves. |
| `ARENA_LOG_DIR` | `/data/logs` | Where match logs are written. Must be the mounted volume. |
| `ARENA_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error`. Case does not matter. |
| `ARENA_CORS_ORIGINS` | unset | Comma-separated origins. Leave unset unless the UI is served from a different host — one process serves both by default. |

### Useful checks

```bash
curl localhost:8000/api/health                     # {"status":"ok","matches":N}
docker compose exec arena ls -la /data/logs        # the match logs on the volume
docker volume inspect adam2_arena-logs             # where they live on the host
docker compose exec arena sh                       # a shell inside the container
```

### Without compose

```bash
docker build -t adamant .
docker run -d --name adamant -p 8000:8000 -v adamant-logs:/data/logs adamant
docker stop adamant && docker rm adamant     # the volume survives
```

### Two constraints that are not tuneable

- **One worker only.** The match registry is an in-memory dict and tick loops are asyncio tasks inside the process. A second worker would serve matches it cannot see.
- **`/data/logs` must be a real volume.** It is both the evaluation dataset and the crash-recovery substrate: on boot the server replays unfinished logs to rebuild those matches and reopens them for resume. On ephemeral storage, every restart abandons the fights in progress and loses every result.

For hosting this on Render or anywhere else, see **[`docs/deploy.md`](docs/deploy.md)**. Match codes plus the 30-minute lobby expiry are the entire abuse control for the MVP.

## Two things worth knowing before you change anything

**`resolve(state, move_a, move_b)` is pure.** No I/O, no randomness. That is what makes the rules testable without a socket, makes a match log replayable for crash recovery (there is no database — the log is the source of truth), and makes old matches re-scorable offline against metrics that did not exist when they were fought.

**A missed move is penalised; a broken connection is not.** Socket open and silent is the agent's failure: `no_move`, defenceless, 15-point penalty. Socket closed is infrastructure: the match pauses and waits for a `resume`. Without that split, a network blip gets recorded as a combat loss and a slow agent gets scored as a defensive strategist.
