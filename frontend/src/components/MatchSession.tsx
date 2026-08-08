'use client';

/**
 * Owns one match code: the spectator socket, the host token if this browser
 * created the match, and the switch between the lobby and the arena.
 *
 * There is no navigation between "pages" — the lobby becomes the arena when
 * `match_start` arrives, for the host and every spectator alike (spec 9).
 */

import { useMemo, useRef, useState } from 'react';

import { recallHost } from '@/lib/api';
import { DEMO_CODE } from '@/lib/demo';
import { useMatchFeed } from '@/lib/useMatchFeed';
import { TickSink } from '@/renderer/tickSink';
import { Arena } from './Arena';
import { ConnectionChip, CopyButton } from './bits';
import { Lobby } from './Lobby';
import { Mark } from './Mark';

interface Props {
  code: string;
  demo: 'match' | 'lobby' | null;
  onExit: () => void;
  /** Rematch creates a new match and moves this browser to it (spec 8.4). */
  onOpen: (code: string) => void;
}

export function MatchSession({ code, demo, onExit, onOpen }: Props) {
  const sinkRef = useRef<TickSink | null>(null);
  if (!sinkRef.current) sinkRef.current = new TickSink();
  const sink = sinkRef.current;

  const [hostToken] = useState<string | null>(
    () => (demo ? 'demo-host' : recallHost(code)?.host_token) ?? null,
  );

  const { view, demoLog } = useMatchFeed({
    code: demo ? null : code,
    demo,
    onTick: sink.push,
    onMatchStart: () => sink.clear(),
  });

  const inArena = useMemo(
    () => view.phase === 'live' || view.phase === 'paused' || view.phase === 'ended',
    [view.phase],
  );

  return (
    <>
      <div className="topbar">
        <span className="brand">
          <Mark />
          Adamant
        </span>
        <span className="chip mono">{code}</span>
        <ConnectionChip connection={view.connection} />
        {demo && <span className="chip">demo fixture — no server</span>}
        <span className="spacer" />
        <CopyButton value={code} label="Copy code" />
        <button onClick={onExit}>Leave</button>
      </div>

      {view.error && !demo && (
        <div className="error">
          {view.error} — the arena server may not be running. Add <code>?api=</code>
          &nbsp;if it is on another origin.
        </div>
      )}

      {inArena ? (
        <Arena
          code={demo ? DEMO_CODE : code}
          view={view}
          sink={sink}
          demo={Boolean(demo)}
          demoLog={demoLog}
          hostToken={hostToken}
          onOpen={onOpen}
        />
      ) : (
        <Lobby code={code} view={view} hostToken={hostToken} demo={Boolean(demo)} />
      )}
    </>
  );
}
