'use client';

/**
 * View 2 of spec 9. Corner cards are driven by `lobby_update` over the spectator
 * socket, so the host and every spectator see the same thing at the same time.
 */

import { useEffect, useState } from 'react';

import { ApiError, fillAgent, fillBot, getAgents, startMatch } from '@/lib/api';
import { joinCommand, watchLink } from '@/lib/config';
import {
  BOTS,
  BOT_BLURBS,
  type AgentRoster,
  type BotName,
  type Corner,
  type CornerInfo,
} from '@/lib/types';
import type { MatchView } from '@/lib/useMatchFeed';
import { CopyButton, ProfileChips } from './bits';

interface LobbyProps {
  code: string;
  view: MatchView;
  hostToken: string | null;
  demo: boolean;
}

export function Lobby({ code, view, hostToken, demo }: LobbyProps) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [roster, setRoster] = useState<AgentRoster | null>(null);
  const isHost = Boolean(hostToken);
  const bothFilled = Boolean(view.corners.agent_a && view.corners.agent_b);

  // Only the host can act on it, but it is fetched for everyone: it is public, it
  // is one request, and a spectator seeing which fighters are on offer costs
  // nothing. A server without the roster simply leaves this null.
  useEffect(() => {
    let live = true;
    getAgents()
      .then((data) => live && setRoster(data))
      .catch(() => live && setRoster(null));
    return () => {
      live = false;
    };
  }, []);

  async function fill(corner: Corner, action: () => Promise<unknown>) {
    if (!hostToken) return;
    setBusy(corner);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  const addBot = (corner: Corner, bot: BotName) =>
    fill(corner, () => fillBot(code, hostToken!, { corner, bot }));

  const addAgent = (corner: Corner, agent: string) =>
    fill(corner, () => fillAgent(code, hostToken!, { corner, agent }));

  async function start() {
    if (!hostToken) return;
    setBusy('start');
    setError(null);
    try {
      await startMatch(code, hostToken);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="stack">
      <div className="card code-plate">
        <div>
          <div className="label">Match code</div>
          <div className="code mono">{code}</div>
        </div>
        <div className="row">
          <CopyButton value={code} label="Copy code" />
          <CopyButton value={watchLink(code)} label="Copy watch link" />
        </div>
      </div>

      <div className="card stack">
        <h2>Join from anywhere</h2>
        <p className="lede">
          Agents are WebSocket clients — no hosting, no public URL. Send a friend this
          line.
        </p>
        <div className="copyblock">
          <pre className="mono">{joinCommand(code)}</pre>
          <CopyButton value={joinCommand(code)} />
        </div>
      </div>

      <div className="corners">
        {(['agent_a', 'agent_b'] as Corner[]).map((corner) => (
          <CornerCard
            key={corner}
            corner={corner}
            info={view.corners[corner]}
            isHost={isHost}
            disabled={demo || busy !== null}
            roster={roster}
            onAddBot={(bot) => addBot(corner, bot)}
            onAddAgent={(agent) => addAgent(corner, agent)}
          />
        ))}
      </div>

      {error && <div className="error">{error}</div>}

      <div className="card row" style={{ justifyContent: 'space-between' }}>
        <div className="stack" style={{ gap: '0.25rem' }}>
          <b>{bothFilled ? 'Both corners filled.' : 'Waiting for both corners…'}</b>
          <small className="hint">
            {isHost
              ? 'Only you can start this match — the host token stays in this browser tab.'
              : 'The host starts the fight. This page will switch to the arena on its own.'}
          </small>
        </div>
        <button
          className="primary big"
          disabled={!isHost || !bothFilled || busy !== null || demo}
          onClick={start}
        >
          {busy === 'start' ? 'Starting…' : 'Start Fight'}
        </button>
      </div>
    </div>
  );
}

function CornerCard({
  corner,
  info,
  isHost,
  disabled,
  roster,
  onAddBot,
  onAddAgent,
}: {
  corner: Corner;
  info: CornerInfo | null;
  isHost: boolean;
  disabled: boolean;
  roster: AgentRoster | null;
  onAddBot: (bot: BotName) => void;
  onAddAgent: (agent: string) => void;
}) {
  const [bot, setBot] = useState<BotName>('jab');
  const [agent, setAgent] = useState<string>('');
  const side = corner === 'agent_a' ? 'a' : 'b';
  const fighters = roster?.agents ?? [];
  const chosen = fighters.find((entry) => entry.key === agent) ?? fighters[0];

  return (
    <div className={`corner ${side}`}>
      <div className="row" style={{ gap: '0.45rem' }}>
        <span className={`badge ${side}`} aria-hidden>
          {side.toUpperCase()}
        </span>
        <span className="label">{corner === 'agent_a' ? 'Corner A' : 'Corner B'}</span>
      </div>
      {info ? (
        <>
          <div className="name">{info.name}</div>
          <ProfileChips profile={info.profile} />
        </>
      ) : (
        <>
          <div className="waiting">
            <span className="dot connecting" /> Waiting…
          </div>
          {isHost && (
            <div className="stack" style={{ gap: '0.5rem', marginTop: 'auto' }}>
              <div className="row">
                <select
                  value={bot}
                  onChange={(e) => setBot(e.target.value as BotName)}
                  style={{ flex: 1 }}
                >
                  {BOTS.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
                <button disabled={disabled} onClick={() => onAddBot(bot)}>
                  Add built-in bot
                </button>
              </div>
              <small className="hint">{BOT_BLURBS[bot]}</small>

              {chosen && (
                <>
                  <div className="row">
                    <select
                      value={chosen.key}
                      onChange={(e) => setAgent(e.target.value)}
                      style={{ flex: 1 }}
                    >
                      {fighters.map((entry) => (
                        <option key={entry.key} value={entry.key}>
                          {entry.name} — {entry.model}
                        </option>
                      ))}
                    </select>
                    <button
                      disabled={disabled || !roster?.available}
                      onClick={() => onAddAgent(chosen.key)}
                      title={
                        roster?.available
                          ? 'Spends the server’s OpenRouter credit: one model call per tick'
                          : (roster?.reason ?? 'Unavailable')
                      }
                    >
                      Add LLM agent
                    </button>
                  </div>
                  <small className="hint">
                    {roster?.available
                      ? `${chosen.blurb} Costs API credit — one call per tick.`
                      : (roster?.reason ?? 'LLM agents are unavailable on this server.')}
                  </small>
                </>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
