'use client';

/**
 * The spectator connection (`/ws/watch/{code}`, spec 7) reduced into view state.
 *
 * `tick_result` deliberately does NOT go into React state — it goes straight to the
 * renderer through the sink callback, and the HUD is updated from the renderer when
 * the tick's animation actually starts. Otherwise the HUD would race ahead of the
 * fighters whenever ticks resolve faster than clips play.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { getMatch } from './api';
import { wsUrl } from './config';
import { DEMO_CODE, startDemo, type DemoHandle } from './demo';
import type {
  Corner,
  CornerInfo,
  MatchConfig,
  MatchEnd,
  MatchLog,
  MatchPaused,
  SpectatorMessage,
  TickResult,
} from './types';

export type Phase = 'connecting' | 'lobby' | 'live' | 'paused' | 'ended';
export type Connection = 'connecting' | 'open' | 'closed';

export interface MatchView {
  phase: Phase;
  connection: Connection;
  corners: Record<Corner, CornerInfo | null>;
  config: MatchConfig | null;
  paused: MatchPaused | null;
  end: MatchEnd | null;
  error: string | null;
}

const EMPTY: MatchView = {
  phase: 'connecting',
  connection: 'connecting',
  corners: { agent_a: null, agent_b: null },
  config: null,
  paused: null,
  end: null,
  error: null,
};

export interface FeedOptions {
  code: string | null;
  demo?: 'match' | 'lobby' | null;
  onTick: (tick: TickResult) => void;
  /** Called when the fight starts, so the arena can clear anything left over. */
  onMatchStart?: () => void;
}

export function useMatchFeed({ code, demo, onTick, onMatchStart }: FeedOptions) {
  const [view, setView] = useState<MatchView>(EMPTY);
  const tickRef = useRef(onTick);
  const startRef = useRef(onMatchStart);
  const demoRef = useRef<DemoHandle | null>(null);
  tickRef.current = onTick;
  startRef.current = onMatchStart;

  /**
   * The highest tick already forwarded to the renderer.
   *
   * Every `/ws/watch` connection replays the whole match from tick 1 (spec 7), which
   * is what makes a watch link work mid-fight — and what makes a reconnect a second
   * telling of a fight already on screen. Without this the HUD rewinds to tick 1, the
   * event log doubles, and a fighter already on the deck stays there through the
   * replay while the winner shadowboxes alone. Ticks are 1-indexed and monotonic, so
   * the high-water mark is the whole de-duplication rule.
   */
  const seenTick = useRef(0);

  const handle = useCallback((msg: SpectatorMessage) => {
    switch (msg.type) {
      case 'lobby_update':
        setView((v) => ({
          ...v,
          corners: msg.corners,
          phase: v.phase === 'connecting' ? 'lobby' : v.phase,
        }));
        break;
      case 'match_start':
        // On a reconnect this is the catch-up copy of a start we already handled.
        // Clearing the arena, the result banner and the pause overlay here would
        // discard the fight we are already showing.
        if (seenTick.current === 0) startRef.current?.();
        setView((v) => ({
          ...v,
          phase: seenTick.current === 0 ? 'live' : v.phase,
          corners: msg.agents,
          config: msg.config ?? v.config,
          end: seenTick.current === 0 ? null : v.end,
          paused: seenTick.current === 0 ? null : v.paused,
        }));
        break;
      case 'tick_result':
        if (msg.tick <= seenTick.current) break;
        seenTick.current = msg.tick;
        tickRef.current(msg);
        setView((v) => (v.phase === 'live' ? v : { ...v, phase: 'live', paused: null }));
        break;
      case 'match_paused':
        setView((v) => ({ ...v, phase: 'paused', paused: msg }));
        break;
      case 'match_resumed':
        setView((v) => ({ ...v, phase: 'live', paused: null }));
        break;
      case 'match_end':
        setView((v) => ({ ...v, phase: 'ended', end: msg, paused: null }));
        break;
      case 'error':
        // The server sends this and closes; the code will never resolve, so there is
        // nothing for the retry loop to come back to.
        setView((v) => ({ ...v, error: msg.reason, connection: 'closed' }));
        break;
    }
  }, []);

  /* ------------------------------------------------------------ demo mode */

  useEffect(() => {
    if (!demo) return;
    setView({ ...EMPTY, connection: 'open', phase: 'lobby' });
    const handle_ = startDemo({ lobbyOnly: demo === 'lobby', onMessage: handle });
    demoRef.current = handle_;
    return () => {
      handle_.stop();
      demoRef.current = null;
    };
  }, [demo, handle]);

  /* ------------------------------------------------------------ live mode */

  useEffect(() => {
    if (demo || !code) return;
    let closed = false;
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let fatal = false;
    // A different code is a different fight: the previous one's high-water mark
    // would suppress every tick of this one (spec 8.4 — a rematch is a new match).
    seenTick.current = 0;

    // Seed from REST so a spectator arriving mid-fight, or after it finished,
    // sees the right view before any socket traffic arrives (spec 8.1: {ref}
    // accepts the match code, which is all a spectator has).
    getMatch(code)
      .then((status) => {
        if (closed) return;
        setView((v) => ({
          ...v,
          corners: status.corners ?? v.corners,
          config: status.config ?? v.config,
          end: status.result ?? v.end,
          phase:
            v.phase !== 'connecting'
              ? v.phase
              : status.status === 'lobby'
                ? 'lobby'
                : status.status === 'paused'
                  ? 'paused'
                  : status.status === 'in_progress'
                    ? 'live'
                    : 'ended',
        }));
      })
      .catch((err: Error) => {
        if (!closed) setView((v) => ({ ...v, error: err.message }));
      });

    const connect = () => {
      if (closed) return;
      setView((v) => ({ ...v, connection: 'connecting' }));
      socket = new WebSocket(wsUrl(`/ws/watch/${encodeURIComponent(code)}`));
      socket.onopen = () => {
        attempt = 0;
        setView((v) => ({ ...v, connection: 'open', error: null }));
      };
      socket.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data as string) as SpectatorMessage;
          if (msg.type === 'error') fatal = true;
          handle(msg);
        } catch {
          /* a message we cannot parse is not worth tearing the page down over */
        }
      };
      socket.onclose = () => {
        if (closed || fatal) return;
        setView((v) => ({ ...v, connection: 'closed' }));
        attempt += 1;
        retry = setTimeout(connect, Math.min(1000 * attempt, 8000));
      };
      socket.onerror = () => socket?.close();
    };
    connect();

    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      socket?.close();
    };
  }, [code, demo, handle]);

  const demoLog = useCallback((): MatchLog | null => demoRef.current?.log() ?? null, []);
  const bothCornersFilled = useMemo(
    () => Boolean(view.corners.agent_a && view.corners.agent_b),
    [view.corners],
  );

  return { view, setView, demoLog, bothCornersFilled, demoCode: DEMO_CODE };
}
