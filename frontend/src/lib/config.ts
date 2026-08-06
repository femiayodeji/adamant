/**
 * Where the platform lives.
 *
 * Default is SAME ORIGIN, because FastAPI serves this static export itself
 * (plan.md Phase 6: one process for API, WS, and static files). Two overrides,
 * in priority order:
 *
 *   1. `?api=http://localhost:8000` in the URL — sticky for the tab, for running
 *      `next dev` on :3000 against a server on :8000.
 *   2. NEXT_PUBLIC_API_BASE at build time.
 */

const STORAGE_KEY = 'adam-arena:api-base';

let cached: string | null = null;

function readOverride(): string {
  if (typeof window === 'undefined') return '';
  const fromQuery = new URLSearchParams(window.location.search).get('api');
  if (fromQuery !== null) {
    try {
      if (fromQuery) window.sessionStorage.setItem(STORAGE_KEY, fromQuery);
      else window.sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      /* private mode — fall through, the query param still wins this load */
    }
    return fromQuery;
  }
  try {
    return window.sessionStorage.getItem(STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

/** '' means same-origin. Never ends with a slash. */
export function apiBase(): string {
  if (cached === null) {
    const base = readOverride() || process.env.NEXT_PUBLIC_API_BASE || '';
    cached = base.replace(/\/+$/, '');
  }
  return cached;
}

export function apiUrl(path: string): string {
  return `${apiBase()}${path}`;
}

/** ws:// or wss:// URL for a platform WebSocket path. */
export function wsUrl(path: string): string {
  const base = apiBase();
  const origin =
    base ||
    (typeof window !== 'undefined' ? window.location.origin : 'http://localhost:8000');
  return `${origin.replace(/^http/, 'ws')}${path}`;
}

/** Absolute link a spectator can be sent (spec 9: "Share link for spectators"). */
export function watchLink(code: string): string {
  if (typeof window === 'undefined') return `#/m/${code}`;
  const { origin, pathname } = window.location;
  return `${origin}${pathname}#/m/${code}`;
}

/** The command an agent builder pastes to join (spec 9 / 10). */
export function joinCommand(code: string): string {
  const base = apiBase() || (typeof window !== 'undefined' ? window.location.origin : '');
  const host = base.replace(/^http/, 'ws');
  return `python agents/reference_agent.py --code ${code} --url ${host}`;
}
