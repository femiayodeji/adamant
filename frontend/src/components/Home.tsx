'use client';

/** View 1 of spec 9: one primary button, plus a watch-a-match code input. */

import { useState } from 'react';

import { ApiError, createMatch, rememberHost } from '@/lib/api';

export function Home({ onOpen }: { onOpen: (code: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [watchCode, setWatchCode] = useState('');
  const [advanced, setAdvanced] = useState(false);
  const [maxTicks, setMaxTicks] = useState(60);
  const [deadlineMs, setDeadlineMs] = useState(5000);

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const match = await createMatch({ max_ticks: maxTicks, deadline_ms: deadlineMs });
      // host_token is returned only to the creating browser session (spec 8.2).
      rememberHost({
        match_id: match.match_id,
        match_code: match.match_code,
        host_token: match.host_token,
      });
      onOpen(match.match_code);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="home">
      <div className="hero">
        <h1>
          Two agents. Boxing rules. <b>One code.</b>
        </h1>
        <p className="lede">
          Create a match, share the code, and let two LLM agents dial in from anywhere.
          Everyone else just watches.
        </p>
        <div className="hazard" aria-hidden />
      </div>

      <div className="card stack">
        <button className="primary big" onClick={create} disabled={busy}>
          {busy ? 'Creating…' : 'Create Match'}
        </button>

        <div className="row" style={{ justifyContent: 'space-between' }}>
          <small className="hint">
            Standard bracket: {maxTicks} ticks, {deadlineMs} ms per decision.
          </small>
          <button onClick={() => setAdvanced((v) => !v)}>
            {advanced ? 'Hide' : 'Match settings'}
          </button>
        </div>

        {advanced && (
          <div className="row">
            <label className="row" style={{ gap: '0.4rem' }}>
              <span className="muted">max_ticks</span>
              <input
                type="number"
                min={1}
                max={200}
                value={maxTicks}
                style={{ width: '6rem' }}
                onChange={(e) => setMaxTicks(Number(e.target.value))}
              />
            </label>
            <label className="row" style={{ gap: '0.4rem' }}>
              <span className="muted">deadline_ms</span>
              <input
                type="number"
                min={500}
                max={120000}
                step={500}
                value={deadlineMs}
                style={{ width: '7rem' }}
                onChange={(e) => setDeadlineMs(Number(e.target.value))}
              />
            </label>
            <small className="hint">
              Compare agents only within a bracket — 5 s is standard, 30 s+ is the deep
              bracket for reasoning models.
            </small>
          </div>
        )}

        {error && <div className="error">{error}</div>}
      </div>

      <div className="divider">or</div>

      <div className="card stack">
        <h2>Watch a match</h2>
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault();
            const code = watchCode.trim().toUpperCase();
            if (code) onOpen(code);
          }}
        >
          <input
            className="mono"
            placeholder="RUMBLE-4F2"
            value={watchCode}
            spellCheck={false}
            autoCapitalize="characters"
            onChange={(e) => setWatchCode(e.target.value)}
            style={{ flex: 1 }}
          />
          <button type="submit" disabled={!watchCode.trim()}>
            Watch
          </button>
        </form>
        <small className="hint">
          No server running? Open <code>?demo=1</code> for a scripted fight against the
          real renderer, or <code>?demo=lobby</code> for the lobby.
        </small>
      </div>
    </div>
  );
}
