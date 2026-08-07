'use client';

/**
 * View 3 of spec §9: the canvas, the HUD, the paused overlay, the end banner, the
 * event log, and Replay.
 *
 * Replay is not a second renderer. It fetches the stored log (spec §8, `GET
 * /api/matches/{ref}/log`, keyed by the match CODE so a spectator can use it too)
 * and pushes the very same `tick_result` objects into the very same
 * `ArenaRenderer.pushTick`, paced by the renderer's own backlog.
 *
 * The transport — scrub, step, speed — rests on one property of the wire format:
 * `tick_result.state` is ABSOLUTE, so any single tick is the whole picture and a
 * jump never has to replay what came before it to reconstruct anything. The one
 * exception is whether a fighter is already on the floor, which `scrubTo` restores
 * from the HP in that tick.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ApiError, getLog } from '@/lib/api';
import {
  CORNERS,
  MAX_CONSECUTIVE_REPEATS,
  type ArenaEvent,
  type Corner,
  type FighterState,
  type MatchEnd,
  type MatchLog,
  type TickResult,
} from '@/lib/types';
import type { MatchView } from '@/lib/useMatchFeed';
import { ArenaRenderer, type HudSnapshot } from '@/renderer/arenaRenderer';
import { pickVideoFormat, ReplayRecorder, saveBlob } from '@/renderer/recorder';
import type { TickSink } from '@/renderer/tickSink';
import { paintOverlay, type OverlayData } from '@/renderer/videoOverlay';

const FULL: FighterState = {
  hp: 100,
  stamina: 100,
  points: 0,
  penalties: 0,
  last_action: null,
  repeat_streak: 0,
};

const SPEEDS = [0.25, 0.5, 1, 2, 4] as const;
/** How long the end card is held on screen at the end of a recorded replay. */
const END_CARD_MS = 1800;

/** Where the sound preference is remembered. Per browser, not per match: a viewer
 *  who muted the last fight should not have to mute the next one. */
const SOUND_KEY = 'adam-arena.sound';

interface LogRow {
  tick: number;
  event: ArenaEvent;
}

/** Flatten ticks to log rows in fight order — the same shape the live feed builds. */
function rowsOf(ticks: TickResult[]): LogRow[] {
  const rows: LogRow[] = [];
  for (const tick of ticks) {
    for (const event of tick.events ?? []) rows.push({ tick: tick.tick, event });
  }
  return rows;
}

/** Corners already down at `index` — what a jump has to restore. */
function downAt(ticks: TickResult[], index: number): Corner[] {
  const state = ticks[index]?.state;
  if (!state) return [];
  return CORNERS.filter((corner) => (state[corner]?.hp ?? 100) <= 0);
}

interface ArenaProps {
  code: string | null;
  view: MatchView;
  sink: TickSink;
  demo: boolean;
  demoLog: () => MatchLog | null;
}

interface Transport {
  playing: boolean;
  speed: number;
  /** Index of the tick currently on screen. */
  position: number;
  total: number;
}

export function Arena({ code, view, sink, demo, demoLog }: ArenaProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rendererRef = useRef<ArenaRenderer | null>(null);

  /** The loaded log, plus the cursor of what has been fed to the renderer. Held in
   *  a ref because the playback interval must not be torn down on every position
   *  change — only on play/pause and speed. */
  const session = useRef<{ ticks: TickResult[]; end: MatchEnd | null } | null>(null);
  const feed = useRef(0);
  const recorder = useRef<ReplayRecorder | null>(null);
  const overlay = useRef<OverlayData | null>(null);
  const logBox = useRef<HTMLDivElement | null>(null);
  const stickToEnd = useRef(true);

  const [hud, setHud] = useState<HudSnapshot | null>(null);
  const [liveRows, setLiveRows] = useState<LogRow[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [transport, setTransport] = useState<Transport | null>(null);
  const [replayError, setReplayError] = useState<string | null>(null);
  const [recording, setRecording] = useState<'running' | null>(null);
  const [videoNote, setVideoNote] = useState<string | null>(null);
  const [drained, setDrained] = useState(false);
  /** Starts false on every render path, including the prerendered one — reading
   *  the stored preference here instead would make the server's HTML and the
   *  client's first render disagree. It is restored in an effect below. */
  const [sound, setSound] = useState(false);

  const replaying = transport !== null;

  const onHud = useCallback((snapshot: HudSnapshot) => {
    setHud(snapshot);
    if (overlay.current) {
      overlay.current = {
        ...overlay.current,
        tick: snapshot.tick,
        state: snapshot.state,
        events: snapshot.events,
      };
    }
    // While replaying, the log is derived from the scrub position instead — a
    // jump backwards has to shorten it, which an append-only list cannot do.
    if (session.current) {
      const at = session.current.ticks.findIndex((t) => t.tick === snapshot.tick);
      if (at >= 0) {
        setTransport((prev) =>
          !prev || prev.position === at ? prev : { ...prev, position: at },
        );
      }
      return;
    }
    if (snapshot.events.length > 0) {
      setLiveRows((rows) => [
        ...rows,
        ...snapshot.events.map((event) => ({ tick: snapshot.tick, event })),
      ]);
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
      recorder.current?.dispose();
      recorder.current = null;
      rendererRef.current?.dispose();
      rendererRef.current = null;
      // There is no renderer any more, so anything keyed on `ready` — the sound
      // preference below — has to be re-applied to whatever replaces it.
      setReady(false);
    };
  }, [onHud, sink]);

  // Stop burning GPU on a backgrounded tab; the queue picks up where it left off.
  // Not while recording, though — a paused renderer emits duplicate frames and the
  // capture would show the fight freezing.
  useEffect(() => {
    const onVisibility = () =>
      rendererRef.current?.setPaused(!recording && document.visibilityState === 'hidden');
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, [recording]);

  /* --------------------------------------------------------------- sound */

  // The stored preference, applied after mount. A viewer who turned sound on
  // last time gets it back, but the browser still holds the audio silent until
  // they touch the page — see `sound.ts`, which resumes on the first gesture.
  useEffect(() => {
    try {
      if (window.localStorage.getItem(SOUND_KEY) === 'on') setSound(true);
    } catch {
      // Private-mode Safari throws on localStorage. Sound just stays off.
    }
  }, []);

  // Also runs when `ready` flips: the renderer is built asynchronously, so a
  // restored preference usually lands before there is anything to tell.
  useEffect(() => {
    rendererRef.current?.setSoundEnabled(sound);
  }, [sound, ready]);

  const toggleSound = useCallback(() => {
    setSound((on) => {
      const next = !on;
      try {
        window.localStorage.setItem(SOUND_KEY, next ? 'on' : 'off');
      } catch {
        // Not being able to remember the choice is no reason not to honour it.
      }
      return next;
    });
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

  /* --------------------------------------------------------------- names */

  const names: Record<Corner, string> = useMemo(
    () => ({
      agent_a: view.corners.agent_a?.name ?? 'Corner A',
      agent_b: view.corners.agent_b?.name ?? 'Corner B',
    }),
    [view.corners],
  );
  const maxTicks = view.config?.max_ticks ?? 60;

  /* -------------------------------------------------------------- replay */

  const loadLog = useCallback(async (): Promise<MatchLog | null> => {
    setReplayError(null);
    try {
      const log = demo ? demoLog() : code ? await getLog(code) : null;
      if (!log || log.ticks.length === 0) {
        setReplayError('No log to replay yet.');
        return null;
      }
      return log;
    } catch (err) {
      setReplayError(err instanceof ApiError ? err.message : String(err));
      return null;
    }
  }, [code, demo, demoLog]);

  const begin = useCallback(
    (log: MatchLog, speed: number) => {
      const renderer = rendererRef.current;
      if (!renderer) return;
      session.current = { ticks: log.ticks, end: log.match_end };
      feed.current = 0;
      renderer.reset();
      renderer.setSpeed(speed);
      setHud(null);
      setTransport({ playing: true, speed, position: 0, total: log.ticks.length });
      stickToEnd.current = true;
    },
    [],
  );

  const startReplay = useCallback(async () => {
    const log = await loadLog();
    if (log) begin(log, 1);
  }, [begin, loadLog]);

  const exitReplay = useCallback(() => {
    session.current = null;
    feed.current = 0;
    setTransport(null);
    const renderer = rendererRef.current;
    renderer?.setSpeed(1);
    renderer?.reset();
    setHud(null);
  }, []);

  const seek = useCallback((index: number) => {
    const active = session.current;
    const renderer = rendererRef.current;
    if (!active || !renderer) return;
    const at = Math.max(0, Math.min(index, active.ticks.length - 1));
    feed.current = at + 1;
    renderer.scrubTo(active.ticks[at], downAt(active.ticks, at));
    setTransport((prev) => (prev ? { ...prev, position: at } : prev));
  }, []);

  const setSpeed = useCallback((speed: number) => {
    rendererRef.current?.setSpeed(speed);
    setTransport((prev) => (prev ? { ...prev, speed } : prev));
  }, []);

  /** Play/pause — and, at the end of the fight, Play means "run it again". A restart
   *  rewinds the feed rather than seeking, so tick 1 is animated instead of being
   *  posed statically the way a scrub target is. */
  const togglePlay = useCallback(() => {
    if (!transport) return;
    if (!transport.playing && transport.position >= transport.total - 1) {
      rendererRef.current?.reset();
      feed.current = 0;
      setHud(null);
      stickToEnd.current = true;
      setTransport((prev) => (prev ? { ...prev, playing: true, position: 0 } : prev));
      return;
    }
    setTransport((prev) => (prev ? { ...prev, playing: !prev.playing } : prev));
  }, [transport]);

  /* The playback pump. Feeds the renderer only when it has room, so the fight
   * plays at animation speed rather than log speed; `speed` scales both the pump
   * period and the renderer's own clip time scale. */
  const finish = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (!transport?.playing) return;
    const period = Math.max(25, Math.round(110 / transport.speed));
    const id = setInterval(() => {
      const renderer = rendererRef.current;
      const active = session.current;
      if (!renderer || !active) return;

      if (feed.current < active.ticks.length) {
        if (renderer.backlog < 2) renderer.pushTick(active.ticks[feed.current++]);
        return;
      }
      if (renderer.idle) {
        setTransport((prev) =>
          prev ? { ...prev, playing: false, position: prev.total - 1 } : prev,
        );
        finish.current?.();
      }
    }, period);
    return () => clearInterval(id);
  }, [transport?.playing, transport?.speed]);

  /* ------------------------------------------------------- video capture */

  const format = useMemo(() => (ready ? pickVideoFormat() : null), [ready]);

  const downloadVideo = useCallback(async () => {
    const renderer = rendererRef.current;
    const canvas = canvasRef.current;
    if (!renderer || !canvas) return;
    if (!format) {
      setReplayError('This browser cannot record video.');
      return;
    }
    const log = await loadLog();
    if (!log) return;

    overlay.current = {
      code,
      names,
      state: { agent_a: FULL, agent_b: FULL },
      tick: 0,
      maxTicks,
      events: [],
      end: null,
    };

    let capture: ReplayRecorder;
    try {
      capture = new ReplayRecorder(
        canvas,
        (ctx, w, h) => overlay.current && paintOverlay(ctx, w, h, overlay.current),
        format,
      );
    } catch (err) {
      setReplayError(String(err));
      return;
    }
    recorder.current = capture;
    setVideoNote(null);
    setRecording('running');

    // Recording is always at 1x: the file should play back as the fight was fought,
    // whatever speed the viewer happened to be reviewing at.
    begin(log, 1);
    renderer.setFrameHook(capture.composite);
    capture.start();

    finish.current = () => {
      finish.current = null;
      if (overlay.current && log.match_end) {
        overlay.current = { ...overlay.current, end: log.match_end };
      }
      // Hold the end card, then cut. The frame hook stays attached for it.
      window.setTimeout(async () => {
        renderer.setFrameHook(null);
        const blob = await capture.stop();
        capture.dispose();
        recorder.current = null;
        overlay.current = null;
        setRecording(null);
        if (blob.size === 0) {
          setReplayError('The recording came back empty.');
          return;
        }
        saveBlob(blob, `${code ?? 'adam-arena'}-replay.${capture.format.ext}`);
        setVideoNote(
          capture.format.ext === 'mp4'
            ? `Saved ${(blob.size / 1e6).toFixed(1)} MB as MP4.`
            : `Saved ${(blob.size / 1e6).toFixed(1)} MB as WebM — this browser has no MP4 encoder.`,
        );
      }, END_CARD_MS);
    };
  }, [begin, code, format, loadLog, maxTicks, names]);

  /**
   * Watchdog.
   *
   * A capture is only as alive as the render loop feeding it: `composite()` runs
   * from the renderer's frame hook, so if `requestAnimationFrame` stops — a
   * throttled background tab, a lost GPU context, a headless browser with no
   * compositor — no frames reach the encoder, the replay never advances, and the
   * transport sits on "Recording…" with no way out. Give up loudly instead.
   */
  useEffect(() => {
    if (recording !== 'running') return;
    let stalledFor = 0;
    let seen = -1;
    const id = setInterval(() => {
      if (feed.current !== seen) {
        seen = feed.current;
        stalledFor = 0;
        return;
      }
      stalledFor += 1;
      if (stalledFor < 6) return; // 30 s of no progress
      clearInterval(id);
      finish.current = null;
      rendererRef.current?.setFrameHook(null);
      recorder.current?.dispose();
      recorder.current = null;
      overlay.current = null;
      setRecording(null);
      setTransport((prev) => (prev ? { ...prev, playing: false } : prev));
      setReplayError(
        'Recording stalled — the page stopped drawing frames. Keep this tab in the ' +
          'foreground and try again.',
      );
    }, 5000);
    return () => clearInterval(id);
  }, [recording]);

  /* ----------------------------------------------------------- event log */

  const rows = useMemo(() => {
    if (transport && session.current) {
      return rowsOf(session.current.ticks.slice(0, transport.position + 1));
    }
    return liveRows;
  }, [transport, liveRows]);

  // Follow the fight, but never yank the view away from someone reading back.
  useEffect(() => {
    const box = logBox.current;
    if (!box || !stickToEnd.current) return;
    box.scrollTop = box.scrollHeight;
  }, [rows]);

  const onLogScroll = useCallback(() => {
    const box = logBox.current;
    if (!box) return;
    stickToEnd.current = box.scrollHeight - box.scrollTop - box.clientHeight < 28;
  }, []);

  /* --------------------------------------------------------------- view */

  const state = hud?.state ?? view.end?.final_state ?? { agent_a: FULL, agent_b: FULL };
  /** The tick on the canvas right now — the log marks its rows so the reader can
   *  see where on the timeline the picture is. */
  const currentTick = hud?.tick ?? -1;
  const showEnd = view.phase === 'ended' && drained && !replaying && view.end;
  const canReplay = view.phase === 'ended' || demo;

  return (
    <div className="stack">
      <div className="stage">
        <canvas ref={canvasRef} />

        <div className="hud">
          <div className="hud-top">
            <FighterPanel corner="agent_a" name={names.agent_a} state={state.agent_a} />
            <div className="tickbox">
              <div className="n mono">
                {String(hud?.tick ?? 0).padStart(2, '0')}
                <span className="of">/{maxTicks}</span>
              </div>
              <div className="l">tick</div>
            </div>
            <FighterPanel corner="agent_b" name={names.agent_b} state={state.agent_b} />
          </div>

          <div className="moves">
            {hud?.moves.agent_a && (
              <MoveChip corner="agent_a" move={hud.moves.agent_a} events={hud.events} />
            )}
            {hud?.moves.agent_b && (
              <MoveChip corner="agent_b" move={hud.moves.agent_b} events={hud.events} />
            )}
            {replaying && !recording && <span className="move mono">replay</span>}
            {recording && <span className="move mono rec">● recording</span>}
            {(hud?.backlog ?? 0) > 2 && (
              <span className="move mono muted">+{hud?.backlog} queued</span>
            )}
          </div>
        </div>

        <Callouts hud={hud} />

        {/* Outside `.hud`, which is `pointer-events: none` and is a two-row layout
            with nowhere to put a third thing. */}
        <button
          type="button"
          className={`sound-toggle${sound ? ' on' : ''}`}
          onClick={toggleSound}
          aria-pressed={sound}
          title={
            sound
              ? 'Mute the fight'
              : 'Sound: punches, blocks, the repeat-cap buzzer and the bell'
          }
        >
          <SpeakerIcon on={sound} />
          <span className="mono">{sound ? 'sound' : 'muted'}</span>
        </button>

        {!ready && !loadError && (
          <div className="overlay">
            <div className="box">
              <div className="title">Loading the rig…</div>
              <small className="hint">xbot.glb, fourteen procedural clips</small>
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

      {transport && (
        <ReplayBar
          transport={transport}
          recording={recording !== null}
          canRecord={format !== null}
          ticks={session.current?.ticks ?? []}
          onSeek={seek}
          onSpeed={setSpeed}
          onToggle={togglePlay}
          onExit={exitReplay}
          onDownload={downloadVideo}
        />
      )}

      <div className="card">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <h2>Event log</h2>
          <div className="row">
            <span className="chip mono muted">{rows.length} events</span>
            {canReplay && !replaying && <button onClick={startReplay}>Replay</button>}
            {canReplay && !replaying && format && (
              <button onClick={downloadVideo}>Download video</button>
            )}
          </div>
        </div>
        {videoNote && <small className="hint">{videoNote}</small>}
        {replayError && !showEnd && <div className="error">{replayError}</div>}
        <div className="ticker" ref={logBox} onScroll={onLogScroll} style={{ marginTop: '0.6rem' }}>
          {rows.length === 0 && <span className="line">Waiting for the first tick…</span>}
          {rows.map((row, i) => (
            <span
              className={`line${row.tick === currentTick ? ' now' : ''}`}
              key={`${row.tick}-${i}`}
            >
              <b className="mono">t{row.tick}</b> <EventLine event={row.event} names={names} />
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

/** The speaker on the sound toggle. Inline rather than a font or an emoji, so it
 *  takes the button's own colour in both states and never arrives late. */
function SpeakerIcon({ on }: { on: boolean }) {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false">
      <path
        d="M7.2 2.4 3.9 5.2H1.6v5.6h2.3l3.3 2.8z"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="1.1"
        strokeLinejoin="round"
      />
      {on ? (
        <g fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round">
          <path d="M10.1 5.6a3.4 3.4 0 0 1 0 4.8" />
          <path d="M12.3 3.5a6.5 6.5 0 0 1 0 9" />
        </g>
      ) : (
        <g fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
          <path d="M10.4 6.1 14 9.9" />
          <path d="M14 6.1 10.4 9.9" />
        </g>
      )}
    </svg>
  );
}

/**
 * The replay transport. The slider is the fight timeline: one step per tick, and
 * dragging it jumps the scene rather than fast-forwarding through it.
 */
function ReplayBar({
  transport,
  recording,
  canRecord,
  ticks,
  onSeek,
  onSpeed,
  onToggle,
  onExit,
  onDownload,
}: {
  transport: Transport;
  recording: boolean;
  canRecord: boolean;
  ticks: TickResult[];
  onSeek: (index: number) => void;
  onSpeed: (speed: number) => void;
  onToggle: () => void;
  onExit: () => void;
  onDownload: () => void;
}) {
  const last = Math.max(0, transport.total - 1);
  const shown = ticks[transport.position]?.tick ?? transport.position + 1;
  const finalTick = ticks[last]?.tick ?? transport.total;

  return (
    <div className="card replaybar">
      <div className="row">
        <button onClick={() => onSeek(0)} disabled={recording} title="Back to the first tick">
          ⏮
        </button>
        <button
          onClick={() => onSeek(transport.position - 1)}
          disabled={recording || transport.position <= 0}
          title="Previous tick"
        >
          ◀
        </button>
        <button className="primary" onClick={onToggle} disabled={recording}>
          {transport.playing ? '❚❚ Pause' : '▶ Play'}
        </button>
        <button
          onClick={() => onSeek(transport.position + 1)}
          disabled={recording || transport.position >= last}
          title="Next tick"
        >
          ▶
        </button>
        <button onClick={() => onSeek(last)} disabled={recording} title="Jump to the last tick">
          ⏭
        </button>

        <span className="chip mono">
          tick {String(shown).padStart(2, '0')}/{finalTick}
        </span>

        <span className="spacer" />

        <label className="speed">
          <span className="sr-only">Playback speed</span>
          <select
            value={transport.speed}
            disabled={recording}
            onChange={(e) => onSpeed(Number(e.target.value))}
          >
            {SPEEDS.map((s) => (
              <option key={s} value={s}>
                {s}×
              </option>
            ))}
          </select>
        </label>
        {canRecord && (
          <button onClick={onDownload} disabled={recording}>
            {recording ? 'Recording…' : 'Download video'}
          </button>
        )}
        <button onClick={onExit} disabled={recording}>
          Close
        </button>
      </div>

      <input
        className="scrub"
        type="range"
        min={0}
        max={last}
        step={1}
        value={transport.position}
        disabled={recording}
        aria-label={`Fight timeline, tick ${shown} of ${finalTick}`}
        onChange={(e) => onSeek(Number(e.target.value))}
      />
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
      <div className="hud-head">
        <span className={`badge ${side}`} aria-hidden>
          {side.toUpperCase()}
        </span>
        <span className="hud-name">{name}</span>
      </div>
      <Gauge kind="hp" side={side} label="HP" value={state.hp} />
      <Gauge kind="st" side={side} label="ST" value={state.stamina} />
      <div className="hud-stats mono">
        <span>
          PTS <b>{state.points}</b>
        </span>
        {state.penalties > 0 && (
          <span className="pen">
            PEN <b>{state.penalties}</b>
          </span>
        )}
        <StreakPips state={state} />
      </div>
    </div>
  );
}

/**
 * A notched readout rather than a smooth bar: twenty segments of five points each,
 * so a spectator can count damage off the gauge. The number is always printed
 * beside it — the bar is the glance, the digits are the truth.
 */
function Gauge({
  kind,
  side,
  label,
  value,
}: {
  kind: 'hp' | 'st';
  side: 'a' | 'b';
  label: string;
  value: number;
}) {
  const shown = Math.max(0, Math.round(value));
  return (
    <div className="gauge">
      <span className="g-label mono">{label}</span>
      <div
        className={`bar ${kind} ${side}${shown <= 25 ? ' low' : ''}`}
        role="meter"
        aria-label={`${label === 'HP' ? 'hit points' : 'stamina'} ${shown} of 100`}
        aria-valuenow={shown}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <span className="fill" style={{ width: `${clamp(value)}%` }} />
        <span className="notches" aria-hidden />
      </div>
      <span className="g-num mono">{shown}</span>
    </div>
  );
}

/**
 * The repeat cap made visible (spec §4.6). Three pips fill as the same action is
 * resubmitted; the third goes hot because the next repeat is the one that voids.
 * Never colour alone — the count is printed as `n/3`, and a void reads as the word.
 */
function StreakPips({ state }: { state: FighterState }) {
  const cap = MAX_CONSECUTIVE_REPEATS;
  const streak = state.repeat_streak ?? 0;
  const action = state.last_action ?? null;
  const voided = streak > cap;
  const lit = Math.min(streak, cap);
  const detail = action ? `${action} x${streak}` : 'no streak';

  return (
    <span
      className={`pips${voided ? ' voided' : lit >= cap ? ' hot' : ''}`}
      title={`Repeat cap ${cap}: ${detail}`}
    >
      <span className="pip-label">REP</span>
      <span className="pip-row" aria-hidden>
        {Array.from({ length: cap }, (_, i) => (
          <i key={i} className={`pip${i < lit ? ' on' : ''}`} />
        ))}
      </span>
      <b className="pip-n">
        {voided ? 'VOID' : `${lit}/${cap}`}
        <span className="sr-only">{` repeat streak, ${detail}`}</span>
      </b>
    </span>
  );
}

/** The action chip under the fighters. A voided action is struck through: it was
 *  submitted, and it did nothing. */
function MoveChip({
  corner,
  move,
  events,
}: {
  corner: Corner;
  move: string;
  events: ArenaEvent[];
}) {
  const voided = events.some((e) => e.type === 'void' && e.who === corner);
  const side = corner === 'agent_a' ? 'a' : 'b';
  return (
    <span className={`move ${side} mono${voided ? ' voided' : ''}`}>
      {move}
      {voided && <em> void</em>}
    </span>
  );
}

interface Callout {
  key: string;
  corner: Corner;
  kind: string;
  head: string;
  sub?: string;
}

/** Per-tick flash text over the canvas, on the side of the fighter it happened to.
 *  Purely decorative — the event log under the stage carries the same lines as text,
 *  so this layer is hidden from assistive tech rather than duplicated into it. */
function Callouts({ hud }: { hud: HudSnapshot | null }) {
  if (!hud) return null;
  const rows: Callout[] = [];
  hud.events.forEach((event, i) => {
    const at = (corner: Corner, kind: string, head: string, sub?: string) =>
      rows.push({ key: `${hud.tick}-${i}-${kind}`, corner, kind, head, sub });
    switch (event.type) {
      case 'hit':
        at(event.defender, 'hit', `-${event.damage}`, event.action);
        break;
      case 'blocked':
        at(event.defender, 'blocked', 'BLOCK', event.defense);
        break;
      case 'dodged':
        at(event.defender, 'dodged', 'SLIP', event.riposte ? `riposte ${event.riposte}` : undefined);
        if (event.riposte) at(event.attacker, 'hit', `-${event.riposte}`, 'riposte');
        break;
      case 'void':
        at(event.who, 'void', 'VOID', `${event.action} x${event.streak} · repeat cap`);
        break;
      case 'exhausted':
        at(event.who, 'exhausted', 'EMPTY', event.attempted);
        break;
      case 'no_move':
        at(event.who, 'no_move', 'NO MOVE', event.cause);
        break;
      case 'ko':
        at(event.who, 'ko', 'DOWN');
        break;
    }
  });
  if (rows.length === 0) return null;

  return (
    <div className="callouts" aria-hidden>
      {(['agent_a', 'agent_b'] as Corner[]).map((corner) => (
        <div className={`call-col ${corner === 'agent_a' ? 'a' : 'b'}`} key={corner}>
          {rows
            .filter((row) => row.corner === corner)
            .map((row) => (
              <span className={`call ${row.kind}`} key={row.key}>
                <b>{row.head}</b>
                {row.sub && <i className="mono">{row.sub}</i>}
              </span>
            ))}
        </div>
      ))}
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
    case 'void':
      return (
        <span className="ev-void">
          {who(event.who)} repeated {event.action} {event.streak}× — voided by the repeat
          cap, no damage, no defense, stamina still spent
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
