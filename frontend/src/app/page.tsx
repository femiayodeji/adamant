'use client';

/**
 * The whole SPA: three views, one document, hash routing.
 *
 * Hash routing (not the Next router) because this is a static export served by
 * FastAPI from a single index.html — there is no server that can answer
 * /m/RUMBLE-4F2 with the app. A share link therefore looks like
 * https://host/#/m/RUMBLE-4F2 and works on a cold load.
 */

import { useCallback, useEffect, useState } from 'react';

import { DEMO_CODE } from '@/lib/demo';
import { Build } from '@/components/Build';
import { Home } from '@/components/Home';
import { Mark } from '@/components/Mark';
import { MatchSession } from '@/components/MatchSession';

type Route = { view: 'home' } | { view: 'match'; code: string } | { view: 'build' };
type Demo = 'match' | 'lobby' | null;

function parseHash(): Route {
  const hash = window.location.hash.replace(/^#\/?/, '');
  const match = /^m\/([^/?#]+)/.exec(hash);
  if (match) return { view: 'match', code: decodeURIComponent(match[1]).toUpperCase() };
  if (/^build/.test(hash)) return { view: 'build' };
  return { view: 'home' };
}

function parseDemo(): Demo {
  const value = new URLSearchParams(window.location.search).get('demo');
  if (!value) return null;
  return value === 'lobby' ? 'lobby' : 'match';
}

export default function Page() {
  // Nothing renders until the client has read the URL: this page is prerendered
  // to static HTML at build time, where there is no location and no sessionStorage.
  const [route, setRoute] = useState<Route | null>(null);
  const [demo, setDemo] = useState<Demo>(null);

  useEffect(() => {
    const read = () => setRoute(parseHash());
    setDemo(parseDemo());
    read();
    window.addEventListener('hashchange', read);
    return () => window.removeEventListener('hashchange', read);
  }, []);

  const open = useCallback((code: string) => {
    window.location.hash = `#/m/${encodeURIComponent(code)}`;
  }, []);

  const exit = useCallback(() => {
    window.location.hash = '#/';
  }, []);

  if (!route) return <main className="app" />;

  const code = demo ? DEMO_CODE : route.view === 'match' ? route.code : null;

  if (code) {
    return (
      <main className="app">
        <MatchSession key={code} code={code} demo={demo} onExit={exit} onOpen={open} />
      </main>
    );
  }

  return (
    <main className="app">
      <div className="topbar">
        <span className="brand">
          <Mark />
          ADAM <b>ARENA</b>
        </span>
        <span className="spacer" />
        {route.view === 'build' ? (
          <button onClick={exit}>Back to the arena</button>
        ) : (
          <span className="chip">boxing rules · simultaneous ticks</span>
        )}
      </div>
      {route.view === 'build' ? <Build /> : <Home onOpen={open} />}
    </main>
  );
}
