/**
 * Wire types for ADAM Arena, transcribed from docs/arena_spec.md 6-8.
 *
 * Spectator-facing messages and the log are ABSOLUTE (`agent_a` / `agent_b`) —
 * the frontend never speaks the egocentric agent vocabulary (spec 1.1).
 */

export type Corner = 'agent_a' | 'agent_b';

export const CORNERS: readonly Corner[] = ['agent_a', 'agent_b'];

/** The eight action names of spec 2. Clip name IS the action name. */
export type ActionName =
  | 'jab'
  | 'cross'
  | 'hook'
  | 'block_high'
  | 'block_low'
  | 'dodge_left'
  | 'dodge_right'
  | 'idle';

export interface FighterState {
  hp: number;
  stamina: number;
  points: number;
  penalties: number;
  /** Last SUBMITTED action, or null at match start and after a no_move (spec 4.6).
   *  Optional here only because logs written before the repeat cap landed lack it —
   *  the live wire always carries it. Read it as `?? null`. */
  last_action?: string | null;
  /** How many times in a row `last_action` has been submitted. 0 with no last action.
   *  A submission at `MAX_CONSECUTIVE_REPEATS + 1` or beyond is void. */
  repeat_streak?: number;
}

/** Repeats allowed before a move voids (spec 4.6, `MAX_CONSECUTIVE_REPEATS`).
 *  Mirrors the server constant so the HUD can draw the cap; the server is the
 *  authority, and the pip row degrades gracefully if it ever moves. */
export const MAX_CONSECUTIVE_REPEATS = 3;

export interface AgentProfile {
  model?: string;
  provider?: string;
  prompt_version?: string;
  temperature?: number;
  agent_version?: string;
  [key: string]: unknown;
}

export interface CornerInfo {
  name: string;
  profile?: AgentProfile;
}

export interface MatchConfig {
  max_ticks: number;
  deadline_ms: number;
  resume_window_ms?: number;
}

/* ---------------------------------------------------------------- events (4.5) */

export interface HitEvent {
  type: 'hit';
  attacker: Corner;
  defender: Corner;
  action: string;
  damage: number;
}
export interface BlockedEvent {
  type: 'blocked';
  attacker: Corner;
  defender: Corner;
  action: string;
  defense: string;
}
export interface DodgedEvent {
  type: 'dodged';
  attacker: Corner;
  defender: Corner;
  action: string;
  defense: string;
  riposte?: number;
}
export interface ExhaustedEvent {
  type: 'exhausted';
  who: Corner;
  attempted: string;
  stamina: number;
}
export interface NoMoveEvent {
  type: 'no_move';
  who: Corner;
  cause: 'timeout' | 'malformed' | 'invalid_action' | 'wrong_tick' | 'duplicate' | string;
  penalty: number;
}
/** The repeat cap fired (spec 4.6): the action was submitted legally and on time but
 *  is stripped to nothing — no damage, no defense, no riposte, no penalty. Stamina is
 *  still paid. `tick_result.moves` still reports the submitted action, so the renderer
 *  must key off this event, not off `moves`. */
export interface VoidEvent {
  type: 'void';
  who: Corner;
  action: string;
  streak: number;
}
export interface KoEvent {
  type: 'ko';
  who: Corner;
}

export type ArenaEvent =
  | HitEvent
  | BlockedEvent
  | DodgedEvent
  | ExhaustedEvent
  | NoMoveEvent
  | VoidEvent
  | KoEvent;

/* ------------------------------------------------- spectator messages (7) */

export interface LobbyUpdate {
  type: 'lobby_update';
  corners: Record<Corner, CornerInfo | null>;
}

export interface SpectatorMatchStart {
  type: 'match_start';
  schema_version?: number;
  match_code?: string;
  agents: Record<Corner, CornerInfo>;
  config: MatchConfig;
  started_at?: string;
}

export interface TickTelemetry {
  latency_ms?: number;
  fallback?: boolean;
  [key: string]: unknown;
}

export interface TickResult {
  type: 'tick_result';
  tick: number;
  /** Action submitted per corner, `null` on a no-move. A voided or exhausted action is
   *  still reported as submitted, so a renderer must check events before animating
   *  (spec §7). */
  moves: Partial<Record<Corner, string>>;
  events: ArenaEvent[];
  state: Record<Corner, FighterState>;
  telemetry?: Partial<Record<Corner, TickTelemetry>>;
}

export interface MatchPaused {
  type: 'match_paused';
  tick: number;
  missing: Corner;
  resume_window_ms: number;
}

export interface MatchResumed {
  type: 'match_resumed';
  tick?: number;
}

export type EndReason = 'ko' | 'points' | 'draw' | 'abandoned';

export interface MatchEnd {
  type: 'match_end';
  /** null on a draw — the key is always present (spec 5.1). */
  winner: Corner | null;
  reason: EndReason;
  final_tick: number;
  final_state: Record<Corner, FighterState>;
}

export type SpectatorMessage =
  | LobbyUpdate
  | SpectatorMatchStart
  | TickResult
  | MatchPaused
  | MatchResumed
  | MatchEnd;

/* ------------------------------------------------------------- REST (8) */

export interface CreateMatchResponse {
  match_id: string;
  match_code: string;
  host_token: string;
  watch_url?: string;
}

/** POST /api/matches/{ref}/rematch (spec 8.4) — a *new* match, so the host gets a
 *  new code and a new token and the fought match keeps its log. */
export interface RematchResponse extends CreateMatchResponse {
  config: MatchConfig;
  rematch_of: string;
  bots: { corner: Corner; bot: string; seed?: number | null; period?: number }[];
}

/** GET /api/rules (spec 8.5) — the BOXING table, served from the same module the
 *  resolver reads. The agent-builder page renders from this rather than restating
 *  numbers that would drift the first time the table is retuned. */
export interface Rules {
  combat_type: string;
  actions: string[];
  attacks: Record<string, { damage: number; stamina: number }>;
  defenses: Record<string, { stamina: number; counters: string[]; riposte: number }>;
  idle: string;
  economy: {
    start_hp: number;
    start_stamina: number;
    stamina_cap: number;
    passive_regen: number;
    idle_regen_bonus: number;
  };
  repeat_cap: number;
  no_move: { penalty: number; causes: string[] };
  defaults: {
    max_ticks: number;
    deadline_ms: number;
    resume_window_ms: number;
    max_ticks_range: [number, number];
    deadline_ms_range: [number, number];
  };
  bots: string[];
}

export type MatchStatus = 'lobby' | 'in_progress' | 'paused' | 'finished' | 'abandoned';

/** Shape of GET /api/matches/{ref} — field names beyond `status` are not pinned
 *  down by the spec, so everything optional is read defensively. SPEC_NOTES.md #1. */
export interface MatchStatusResponse {
  status: MatchStatus;
  match_id?: string;
  match_code?: string;
  corners?: Record<Corner, CornerInfo | null>;
  config?: MatchConfig;
  result?: MatchEnd | null;
}

/** GET /api/matches/{ref}/log (spec 7, "Log format"). */
export interface MatchLog {
  schema_version: number;
  match_start: SpectatorMatchStart;
  ticks: TickResult[];
  match_end: MatchEnd | null;
}

export type BotName = 'random' | 'jab' | 'cycle' | 'frequency' | 'mixer';

export const BOTS: readonly BotName[] = ['random', 'jab', 'cycle', 'frequency', 'mixer'];

export const BOT_BLURBS: Record<BotName, string> = {
  random: 'Uniform over the eight actions — the floor.',
  jab: 'Always jab. A designed trap: blocking it draws.',
  cycle: 'Fixed period-3/5 pattern. Tests pattern detection.',
  frequency: 'Counters your most-used action so far.',
  mixer: 'Near-equilibrium mix — the ceiling.',
};
