'use client';

/**
 * View 3 of spec 9: the canvas, the HUD, the paused overlay, the end banner, and
 * Replay.
 *
 * Replay is not a second renderer. It fetches the stored log (spec 8, `GET
 * /api/matches/{ref}/log`, keyed by the match CODE so a spectator can use it too)
 * and pushes the very same `tick_result` objects into the very same
 * `ArenaRenderer.pushTick`, paced by the renderer's own backlog.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, getLog } from '@/lib/api';
import type { ArenaEvent, Corner, FighterState, MatchLog } from '@/lib/types';
import type { MatchView } from '@/lib/useMatchFeed';
import { ArenaRenderer, type HudSnapshot } from '@/renderer/arenaRenderer';
import type { TickSink } from '@/renderer/tickSink';

const FULL: FighterState = { hp: 100, stamina: 100, points: 0, penalties: 0 };
const MAX_TICKER = 6;

interface ArenaProps {
  code: string | null;
  view: MatchView;
  sink: TickSink;
  demo: boolean;
  demoLog: () => MatchLog | null;
}

export function Arena({ code, view, sink, demo, demoLog }: ArenaProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rendererRef = useRef<ArenaRenderer | null>(null);
  const replayTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const [hud, setHud] = useState<HudSnapshot | null>(null);
  const [ticker, setTicker] = useState<{ tick: number; event: ArenaEvent }[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [replaying, setReplaying] = useState(false);
  const [replayError, setReplayError] = useState<string | null>(null);
  const [drained, setDrained] = useState(false);

  const onHud = useCallback((snapshot: HudSnapshot) => {
    setHud(snapshot);
    if (snapshot.events.length > 0) {
      setTicker((rows) =>
        [...snapshot.events.map((event) => ({ tick: snapshot.tick, event })), ...rows].slice(
          0,
          MAX_TICKER,
        ),
      );
    }
  }, []);

  /* ------------------------------------------------------- renderer life */

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let cancelled = false;

    ArenaRenderer.create(canvas, { onHud })
      .then((renderer) => {
        if (cancelled) {
          renderer.dispose();
          return;
        }
        rendererRef.current = renderer;
        sink.attach(renderer);
        setReady(true);
      })
      .catch((err: Error) => {
        if (!cancelled) setLoadError(err.message);
      });

    return () => {
      cancelled = true;
      sink.detach();
      rendererRef.current?.dispose();
      rendererRef.current = null;
      if (replayTimer.current) clearInterval(replayTimer.current);
    };
  }, [onHud, sink]);

  // Stop burning GPU on a backgrounded tab; the queue picks up where it left off.
  useEffect(() => {
    const onVisibility = () =>
      rendererRef.current?.setPaused(document.visibilityState === 'hidden');
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, []);

  // The banner waits for the animation to catch up with the result — the last
  // clips are usually still playing when `match_end` lands.
  useEffect(() => {
    if (view.phase !== 'ended' || replaying) {
      setDrained(false);
      return;
    }
    const id = setInterval(() => {
      if (rendererRef.current?.idle !== false) setDrained(true);
    }, 250);
    return () => clearInterval(id);
  }, [view.phase, replaying]);

  /* ------------------------------------------------------------- replay */

  const startReplay = useCallback(async () => {
    const renderer = rendererRef.current;
    if (!renderer) return;
    setReplayError(null);
    let log: MatchLog | null = null;
    try {
      log = demo ? demoLog() : code ? await getLog(code) : null;
    } catch (err) {
      setReplayError(err instanceof ApiError ? err.message : String(err));
      return;
    }
    if (!log || log.ticks.length === 0) {
      setReplayError('No log to replay yet.');
      return;
    }

    renderer.reset();
    setTicker([]);
    setHud(null);
    setReplaying(true);

    let index = 0;
    if (replayTimer.current) clearInterval(replayTimer.current);
    replayTimer.current = setInterval(() => {
      const active = rendererRef.current;
      if (!active) return;
      if (index >= log.ticks.length) {
        if (active.idle) {
          if (replayTimer.current) clearInterval(replayTimer.current);
          replayTimer.current = null;
          setReplaying(false);
        }
        return;
      }
      // Backpressure instead of a fixed frame budget: push only when the renderer
      // has room, so replay plays at animation speed rather than log speed.
      if (active.backlog < 2) active.pushTick(log.ticks[index++]);
    }, 120);
  }, [code, demo, demoLog]);

  /* --------------------------------------------------------------- view */

  const names: Record<Corner, string> = {
    agent_a: view.corners.agent_a?.name ?? 'Corner A',
    agent_b: view.corners.agent_b?.name ?? 'Corner B',
  };
  const state = hud?.state ?? view.end?.final_state ?? { agent_a: FULL, agent_b: FULL };
  const maxTicks = view.config?.max_ticks ?? 60;
  const showEnd = view.phase === 'ended' && drained && !replaying && view.end;

  return (
    <div className="stack">
      <div className="stage">
        <canvas ref={canvasRef} />

        <div className="hud">
          <div className="hud-top">
            <FighterPanel corner="agent_a" name={names.agent_a} state={state.agent_a} />
            <div className="tickbox">
              <div className="n mono">
                {hud?.tick ?? 0}
                <span className="muted" style={{ fontSize: '0.7rem' }}>
                  /{maxTicks}
                </span>
              </div>
              <div className="l">tick</div>
            </div>
            <FighterPanel corner="agent_b" name={names.agent_b} state={state.agent_b} />
          </div>

          <div className="moves">
            {hud?.moves.agent_a && <span className="move a mono">{hud.moves.agent_a}</span>}
            {hud?.moves.agent_b && <span className="move b mono">{hud.moves.agent_b}</span>}
            {replaying && <span className="move mono">replay</span>}
            {(hud?.backlog ?? 0) > 2 && (
              <span className="move mono muted">+{hud?.backlog} queued</span>
            )}
          </div>
        </div>

        {!ready && !loadError && (
          <div className="overlay">
            <div className="box">
              <div className="title">Loading the rig…</div>
              <small className="hint">xbot.glb, thirteen procedural clips</small>
            </div>
          </div>
        )}

        {loadError && (
          <div className="overlay">
            <div className="box">
              <div className="title">Renderer failed</div>
              <div className="error">{loadError}</div>
            </div>
          </div>
        )}

        {view.phase === 'paused' && view.paused && <PausedOverlay view={view} />}

        {showEnd && view.end && (
          <div className="overlay">
            <div className="box">
              <div className="title">
                {view.end.winner ? `${names[view.end.winner]} wins` : 'Draw'}
              </div>
              <div className="row" style={{ justifyContent: 'center' }}>
                <span className="chip">by {view.end.reason}</span>
                <span className="chip">tick {view.end.final_tick}</span>
                <span className="chip">
                  {score(view.end.final_state.agent_a)} — {score(view.end.final_state.agent_b)}{' '}
                  decision score
                </span>
              </div>
              <button className="primary big" onClick={startReplay}>
                Replay
              </button>
              {replayError && <div className="error">{replayError}</div>}
            </div>
          </div>
        )}
      </div>

      <div className="card">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <h2>Event log</h2>
          {view.phase === 'ended' && !showEnd && !replaying && (
            <button onClick={startReplay}>Replay</button>
          )}
        </div>
        <div className="ticker" style={{ marginTop: '0.6rem' }}>
          {ticker.length === 0 && <span className="line">Waiting for the first tick…</span>}
          {ticker.map((row, i) => (
            <span className="line" key={`${row.tick}-${i}`}>
              <b className="mono">t{row.tick}</b> <EventLine event={row.event} names={names} />
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

function FighterPanel({
  corner,
  name,
  state,
}: {
  corner: Corner;
  name: string;
  state: FighterState;
}) {
  const side = corner === 'agent_a' ? 'a' : 'b';
  return (
    <div className={`hud-panel ${side}`}>
      <div className="hud-name">{name}</div>
      <div className={`bar hp ${side}`}>
        <span style={{ width: `${clamp(state.hp)}%` }} />
      </div>
      <div className="bar stam">
        <span style={{ width: `${clamp(state.stamina)}%` }} />
      </div>
      <div className="hud-stats mono">
        <span>hp {Math.max(0, state.hp)}</span>
        <span>st {Math.max(0, state.stamina)}</span>
        <span>pts {state.points}</span>
        {state.penalties > 0 && <span style={{ color: '#ff9a6c' }}>pen {state.penalties}</span>}
      </div>
    </div>
  );
}

function PausedOverlay({ view }: { view: MatchView }) {
  const paused = view.paused!;
  const [left, setLeft] = useState(paused.resume_window_ms);

  useEffect(() => {
    setLeft(paused.resume_window_ms);
    const started = Date.now();
    const id = setInterval(
      () => setLeft(Math.max(0, paused.resume_window_ms - (Date.now() - started))),
      250,
    );
    return () => clearInterval(id);
  }, [paused.resume_window_ms, paused.tick, paused.missing]);

  return (
    <div className="overlay">
      <div className="box">
        <div className="title">Paused</div>
        <p className="lede">
          {paused.missing === 'agent_a' ? 'Corner A' : 'Corner B'} dropped its connection
          on tick {paused.tick}. The clock is stopped for both fighters.
        </p>
        <div className="countdown mono">{Math.ceil(left / 1000)}s</div>
        <small className="hint">
          Infrastructure failure pauses a match; it never decides one. If nobody returns
          in time the match ends <code>abandoned</code>.
        </small>
      </div>
    </div>
  );
}

function EventLine({
  event,
  names,
}: {
  event: ArenaEvent;
  names: Record<Corner, string>;
}) {
  const who = (corner: Corner) => <b>{names[corner]}</b>;
  switch (event.type) {
    case 'hit':
      return (
        <span className="ev-hit">
          {who(event.attacker)} lands {event.action} for {event.damage}
        </span>
      );
    case 'blocked':
      return (
        <span className="ev-blocked">
          {who(event.defender)} blocks {event.action} with {event.defense}
        </span>
      );
    case 'dodged':
      return (
        <span className="ev-dodged">
          {who(event.defender)} slips {event.action} with {event.defense}
          {event.riposte ? ` and ripostes ${event.riposte}` : ''}
        </span>
      );
    case 'exhausted':
      return (
        <span className="ev-exhausted">
          {who(event.who)} could not afford {event.attempted} ({event.stamina} stamina)
        </span>
      );
    case 'no_move':
      return (
        <span className="ev-no_move">
          {who(event.who)} made no move ({event.cause}) — penalty {event.penalty}
        </span>
      );
    case 'ko':
      return <span className="ev-ko">{who(event.who)} is knocked out</span>;
    default:
      return null;
  }
}

const clamp = (value: number) => Math.max(0, Math.min(100, value));
const score = (state: FighterState) => state.points - state.penalties;
