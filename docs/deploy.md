# Deploying Adamant

Phase 6 is deliberately thin: **one process** serving the REST API, both WebSocket
endpoints, and the static frontend. The image in [`../Dockerfile`](../Dockerfile) is
that process. Day-to-day run/stop commands are in the [README](../README.md#running-with-docker);
this file is about hosting it.

## What the platform actually needs from a host

Four requirements, and three of them are unusual enough to catch people out.

**1. Exactly one instance, one worker.** The match registry is an in-memory dict and
every tick loop is an asyncio task inside the process. A second worker or a second
replica does not share them: it would accept a match code it has never heard of and
answer `404`. Do not enable autoscaling, and do not raise the worker count.

**2. Persistent disk mounted at `/data/logs`.** There is no database. The match log
is the source of truth — it is the evaluation dataset, it is what `Replay` reads, and
on boot the server replays unfinished logs through the pure `resolve()` to rebuild
those matches and reopen them for resume (spec §6.4c). On ephemeral storage every
deploy silently abandons the fights in flight and throws away every result you have
recorded.

**3. WebSockets, unbuffered, with a generous idle timeout.** Agents hold a socket
open for the whole match. A proxy that buffers or closes idle connections will look
exactly like agents disconnecting — the platform will pause matches and eventually
mark them `abandoned`. A match at the deep bracket (`deadline_ms: 30000`) can be
quiet for 30 s at a stretch, and a paused match can be quiet for the full
`resume_window_ms` of 120 s.

**4. TLS**, so agents connect over `wss://` rather than `ws://`.

Alongside those: set `ARENA_CORS_ORIGINS` only if the UI is served from a different
origin than the API. In the shape above it is not, so leave it unset.

## Render

[`../render.yaml`](../render.yaml) is the blueprint. To deploy: push the branch Render
should track (the repo's default branch unless you change it in the dashboard), then
**New → Blueprint** in the Render dashboard, pick this repo, and enter
`OPENROUTER_API_KEY` when prompted, or leave it blank. Render builds the `Dockerfile`
directly and every later push to that branch redeploys. The pieces that matter:

- **`PORT`** — Render injects it and the container's `CMD` already reads it, so no
  rebuild or override is needed.
- **Health check path** — `/api/health`, which returns `{"status": "ok", ...}`.
- **Disk** — mount one at `/data/logs`. `ARENA_LOG_DIR` already points there. A
  fresh disk is root-owned and hides the ownership the image set at build time, so
  the container starts as root, and `docker-entrypoint.sh` chowns the log directory
  and then drops to the unprivileged `arena` user before uvicorn starts.
- **Plan** — the blueprint currently says `free`, so there is **no disk**: logs are
  written to the container filesystem and lost on every deploy, restart and
  spin-down. To keep results, change it to `0.5c-512mb` (the smallest paid instance;
  disks attach only to paid services) and uncomment the `disk:` block.
- **Instance count** — one. See requirement 1 above.
- **`OPENROUTER_API_KEY`** — optional, and a **secret**: set it in the dashboard, never
  in the blueprint. It is the one setting that costs money — with it, any host of a
  match on your instance can spend your credit through **Add LLM agent** (spec §8.6),
  one model call per LLM corner per tick. Without it `/api/agents` reports
  `available: false`, the button is disabled, and everything else works unchanged.

Three Render specifics worth knowing before you pick a plan:

- **The free tier will not work properly.** Free services have no persistent disk and
  spin down after inactivity. Both break the same thing: an interrupted match cannot
  resume and its log is gone. Free is fine for showing someone the UI, not for
  recording results you intend to keep.
- **Attaching a disk makes deploys restart rather than roll.** Render cannot run two
  instances sharing one disk, so a deploy stops the old instance before starting the
  new one. That is the correct behaviour here — two instances would be worse — but it
  means a deploy interrupts live matches. They pause and resume if agents reconnect
  within `resume_window_ms`; deploy between fights when you can.
- **Render terminates TLS for you**, so agents use `wss://your-service.onrender.com`
  and the container keeps speaking plain HTTP internally.

## Verifying a deployment

The Phase 6 gate is one real remote match with a friend, watched live via the share
link, replayed afterwards, and surviving a mid-match reconnect. Before asking anyone
to join, these four take a minute:

```bash
HOST=https://your-service.example.com

curl -s $HOST/api/health

# A full bot-vs-bot match through the public API.
M=$(curl -s -X POST $HOST/api/matches -H 'Content-Type: application/json' \
     -d '{"max_ticks":20,"deadline_ms":2000}')
CODE=$(echo "$M" | python3 -c 'import sys,json;print(json.load(sys.stdin)["match_code"])')
TOKEN=$(echo "$M" | python3 -c 'import sys,json;print(json.load(sys.stdin)["host_token"])')

curl -s -X POST $HOST/api/matches/$CODE/fill_bot -H "X-Host-Token: $TOKEN" \
     -H 'Content-Type: application/json' -d '{"corner":"agent_a","bot":"frequency"}'
curl -s -X POST $HOST/api/matches/$CODE/fill_bot -H "X-Host-Token: $TOKEN" \
     -H 'Content-Type: application/json' -d '{"corner":"agent_b","bot":"jab"}'
curl -s -X POST $HOST/api/matches/$CODE/start -H "X-Host-Token: $TOKEN"

sleep 8
curl -s $HOST/api/matches/$CODE/log | python3 -m json.tool | head -20
```

Then the two that actually test the deployment rather than the code:

- **Redeploy and re-fetch the same log by code.** If it 404s, the disk is not
  persisting. This is the failure mode most worth checking, because everything looks
  fine until the first deploy.
- **Run the reference agent from your laptop** against `wss://…` and pull its network
  cable mid-match. The arena page should show the paused overlay with a countdown,
  and the fight should continue when the agent reconnects. If it instead ends
  `abandoned`, the proxy is closing idle WebSockets.

## A note on the logs

They are worth more than the server. The server can be rebuilt from this repository
in minutes; the logs are the only record of every match ever fought, and because
`resolve()` is pure they can be re-scored offline against metrics that do not exist
yet. Back up the volume before you do anything drastic to the host, and remember that
`docker compose down -v` deletes it.
