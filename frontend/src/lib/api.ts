/** REST client for the platform API (docs/adamant_spec.md 8). */

import { apiUrl } from './config';
import type {
  AgentRoster,
  BotName,
  Corner,
  CreateMatchResponse,
  MatchLog,
  MatchStatusResponse,
  RematchResponse,
  Rules,
} from './types';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(apiUrl(path), {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, 'Could not reach the arena server.');
  }
  if (!res.ok) {
    throw new ApiError(res.status, await explain(res));
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

async function explain(res: Response): Promise<string> {
  const generic: Record<number, string> = {
    401: 'Not the host of this match.',
    404: 'No match with that code.',
    409: 'That is not allowed right now (both corners filled? already started?).',
  };
  try {
    const body = (await res.json()) as { detail?: unknown; reason?: unknown };
    const detail = body.detail ?? body.reason;
    if (typeof detail === 'string' && detail) return detail;
  } catch {
    /* not JSON */
  }
  return generic[res.status] ?? `Server returned ${res.status}.`;
}

export function createMatch(body?: {
  max_ticks?: number;
  deadline_ms?: number;
}): Promise<CreateMatchResponse> {
  return request<CreateMatchResponse>('/api/matches', {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  });
}

/** `ref` is a match_id OR a match_code (spec 8.1). */
export function getMatch(ref: string): Promise<MatchStatusResponse> {
  return request<MatchStatusResponse>(`/api/matches/${encodeURIComponent(ref)}`);
}

export function getLog(ref: string): Promise<MatchLog> {
  return request<MatchLog>(`/api/matches/${encodeURIComponent(ref)}/log`);
}

export function fillBot(
  ref: string,
  hostToken: string,
  body: { corner: Corner; bot: BotName; seed?: number },
): Promise<unknown> {
  return request(`/api/matches/${encodeURIComponent(ref)}/fill_bot`, {
    method: 'POST',
    headers: { 'X-Host-Token': hostToken },
    body: JSON.stringify(body),
  });
}

/** The LLM roster (spec 8.6). Public, and answers even when the server cannot run
 *  one — read `available` and `reason` before offering the button. */
export function getAgents(): Promise<AgentRoster> {
  return request<AgentRoster>('/api/agents');
}

/** Fill a corner with a roster fighter (spec 8.6).
 *
 *  This starts spending the *server's* OpenRouter credit: one model call per tick
 *  until the match ends. It is a host-only action for that reason. */
export function fillAgent(
  ref: string,
  hostToken: string,
  body: { corner: Corner; agent: string; model?: string },
): Promise<{ corner: Corner; agent: string; model: string }> {
  return request(`/api/matches/${encodeURIComponent(ref)}/fill_agent`, {
    method: 'POST',
    headers: { 'X-Host-Token': hostToken },
    body: JSON.stringify(body),
  });
}

/** Run it back (spec 8.4). A *new* match: the caller must remember the new host
 *  token and move to the new code — the old one keeps its result and its log. */
export function rematch(ref: string, hostToken: string): Promise<RematchResponse> {
  return request<RematchResponse>(`/api/matches/${encodeURIComponent(ref)}/rematch`, {
    method: 'POST',
    headers: { 'X-Host-Token': hostToken },
  });
}

/** The rules table (spec 8.5). Public — no match and no token needed. */
export function getRules(): Promise<Rules> {
  return request<Rules>('/api/rules');
}

export function startMatch(ref: string, hostToken: string): Promise<unknown> {
  return request(`/api/matches/${encodeURIComponent(ref)}/start`, {
    method: 'POST',
    headers: { 'X-Host-Token': hostToken },
  });
}

/* ------------------------------------------------------- host token storage */

interface HostSession {
  match_id: string;
  match_code: string;
  host_token: string;
}

const hostKey = (code: string) => `adamant:host:${code.toUpperCase()}`;

export function rememberHost(session: HostSession): void {
  try {
    window.sessionStorage.setItem(hostKey(session.match_code), JSON.stringify(session));
  } catch {
    /* sessionStorage unavailable; the in-memory copy in React state still works */
  }
}

export function recallHost(code: string): HostSession | null {
  try {
    const raw = window.sessionStorage.getItem(hostKey(code));
    return raw ? (JSON.parse(raw) as HostSession) : null;
  } catch {
    return null;
  }
}
