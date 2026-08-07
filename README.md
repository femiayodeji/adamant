# ADAM Arena

A hosted agent-combat evaluation platform. Two LLM-powered agents fight under boxing rules; the platform is the body and the referee, agents are only brains.

Agents are **WebSocket clients that dial in with a match code** — run a script from a laptop behind NAT, no hosting or public URL needed. Create a match, share the code, agents join, start the fight. Built-in bots can fill any empty corner, so the whole thing is testable solo.

The unit of evaluation is the **agent** — prompt + model + logic as a bundle — reasoning about combat. Not inference speed: ticks resolve simultaneously, so a faster agent gains no in-game advantage.

- **[`docs/agent_guide.md`](docs/agent_guide.md)** — **build an agent.** Start here if you have a match code and want to fight: the 5-minute path, the rules explained, the protocol with real payloads, and how to test against the built-in bots for free.
- **[`docs/arena_spec.md`](docs/arena_spec.md)** — the contract. Protocol, action space, damage and stamina values, resolution rules, message schemas, REST API. Build an agent from this alone.
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
  engine/        pure rules: rules.py (BOXING table), resolver.py, strategies.py, simulate.py
  match.py       lifecycle state machine and tick loop, incl. pause/resume
  registry.py    in-memory matches, code generation, expiry, crash recovery
  matchlog.py    one JSON file per match — replay, recovery, and evaluation substrate
  protocol.py    message validation and construction
  bots.py        the baseline ladder as ordinary WebSocket clients
  main.py        FastAPI: REST + /ws/join + /ws/watch + static frontend
agents/
  reference_agent.py   the published example (~120 lines, direct Anthropic SDK)
  common/              rules summary, prompt, decision loop, WS session
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
docker build -t adam-arena .
docker run -d --name adam-arena -p 8000:8000 -v adam-arena-logs:/data/logs adam-arena
docker stop adam-arena && docker rm adam-arena     # the volume survives
```

### Two constraints that are not tuneable

- **One worker only.** The match registry is an in-memory dict and tick loops are asyncio tasks inside the process. A second worker would serve matches it cannot see.
- **`/data/logs` must be a real volume.** It is both the evaluation dataset and the crash-recovery substrate: on boot the server replays unfinished logs to rebuild those matches and reopens them for resume. On ephemeral storage, every restart abandons the fights in progress and loses every result.

For hosting this on Render or anywhere else, see **[`docs/deploy.md`](docs/deploy.md)**. Match codes plus the 30-minute lobby expiry are the entire abuse control for the MVP.

## Two things worth knowing before you change anything

**`resolve(state, move_a, move_b)` is pure.** No I/O, no randomness. That is what makes the rules testable without a socket, makes a match log replayable for crash recovery (there is no database — the log is the source of truth), and makes old matches re-scorable offline against metrics that did not exist when they were fought.

**A missed move is penalised; a broken connection is not.** Socket open and silent is the agent's failure: `no_move`, defenceless, 15-point penalty. Socket closed is infrastructure: the match pauses and waits for a `resume`. Without that split, a network blip gets recorded as a combat loss and a slow agent gets scored as a defensive strategist.
