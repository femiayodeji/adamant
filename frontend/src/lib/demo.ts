/**
 * Offline fixture driver — `?demo=1`.
 *
 * The server does not exist yet while this frontend is being built, and a renderer
 * that can only be exercised against a running backend cannot be developed at all.
 * This module fakes the spectator stream of arena_spec.md 7: it emits the same
 * `lobby_update` / `match_start` / `tick_result` / `match_paused` / `match_end`
 * messages, through the same reducer and the same renderer, and records them into
 * a match log so the Replay button works offline too.
 *
 * It contains a small copy of the resolution rules (spec 2-4) purely so the fake
 * events are internally consistent. That is a deliberate duplicate of server logic
 * confined to this file; nothing in the live path imports it. See SPEC_NOTES.md #7.
 */

import type {
  ActionName,
  ArenaEvent,
  Corner,
  FighterState,
  MatchEnd,
  MatchLog,
  SpectatorMessage,
  TickResult,
} from './types';

const ATTACKS: Record<string, { damage: number; cost: number }> = {
  jab: { damage: 5, cost: 5 },
  cross: { damage: 10, cost: 10 },
  hook: { damage: 15, cost: 15 },
};

const DEFENSES: Record<string, { cost: number; counters: string[]; riposte: number }> = {
  block_high: { cost: 3, counters: ['jab', 'cross'], riposte: 0 },
  block_low: { cost: 3, counters: ['hook'], riposte: 0 },
  dodge_left: { cost: 8, counters: ['cross', 'hook'], riposte: 5 },
  dodge_right: { cost: 8, counters: ['jab'], riposte: 5 },
};

const ALL: ActionName[] = [
  'jab',
  'cross',
  'hook',
  'block_high',
  'block_low',
  'dodge_left',
  'dodge_right',
  'idle',
];

const cost = (action: string): number =>
  ATTACKS[action]?.cost ?? DEFENSES[action]?.cost ?? 0;

function fresh(): FighterState {
  return { hp: 100, stamina: 100, points: 0, penalties: 0 };
}

/** Deterministic PRNG so the demo match is the same every time. */
function lcg(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 0x100000000;
  };
}

interface Resolution {
  events: ArenaEvent[];
  state: Record<Corner, FighterState>;
  moves: Record<Corner, string>;
  ko: boolean;
}

/** A compact stand-in for server/engine/resolver.py, following spec 4.1's order. */
function resolveTick(
  state: Record<Corner, FighterState>,
  intents: Record<Corner, string | null>,
): Resolution {
  const next: Record<Corner, FighterState> = {
    agent_a: { ...state.agent_a },
    agent_b: { ...state.agent_b },
  };
  const events: ArenaEvent[] = [];
  const moves: Record<Corner, string> = { agent_a: 'idle', agent_b: 'idle' };
  const noMove: Record<Corner, boolean> = { agent_a: false, agent_b: false };
  const corners: Corner[] = ['agent_a', 'agent_b'];

  for (const corner of corners) {
    const intent = intents[corner];
    if (intent === null) {
      noMove[corner] = true;
      moves[corner] = 'no_move';
      next[corner].penalties += 15;
      events.push({ type: 'no_move', who: corner, cause: 'timeout', penalty: 15 });
      continue;
    }
    if (cost(intent) > state[corner].stamina) {
      events.push({
        type: 'exhausted',
        who: corner,
        attempted: intent,
        stamina: state[corner].stamina,
      });
      moves[corner] = 'idle';
      continue;
    }
    moves[corner] = intent;
  }

  for (const corner of corners) next[corner].stamina -= cost(moves[corner]);

  for (const attacker of corners) {
    const defender: Corner = attacker === 'agent_a' ? 'agent_b' : 'agent_a';
    const action = moves[attacker];
    const attack = ATTACKS[action];
    if (!attack || noMove[attacker]) continue;

    const defense = noMove[defender] ? null : DEFENSES[moves[defender]];
    if (defense && defense.counters.includes(action)) {
      if (defense.riposte > 0) {
        events.push({
          type: 'dodged',
          attacker,
          defender,
          action,
          defense: moves[defender],
          riposte: defense.riposte,
        });
        next[attacker].hp -= defense.riposte;
        next[defender].points += defense.riposte;
      } else {
        events.push({
          type: 'blocked',
          attacker,
          defender,
          action,
          defense: moves[defender],
        });
      }
      continue;
    }
    events.push({ type: 'hit', attacker, defender, action, damage: attack.damage });
    next[defender].hp -= attack.damage;
    next[attacker].points += attack.damage;
  }

  for (const corner of corners) {
    const regen = noMove[corner] ? 5 : moves[corner] === 'idle' ? 20 : 5;
    next[corner].stamina = Math.min(100, Math.max(0, next[corner].stamina + regen));
  }

  let ko = false;
  for (const corner of corners) {
    if (next[corner].hp <= 0) {
      next[corner].hp = 0;
      events.push({ type: 'ko', who: corner });
      ko = true;
    }
  }
  return { events, state: next, moves, ko };
}

export interface DemoOptions {
  /** Stop in the lobby instead of starting the fight (`?demo=lobby`). */
  lobbyOnly?: boolean;
  onMessage: (msg: SpectatorMessage) => void;
}

export interface DemoHandle {
  stop(): void;
  log(): MatchLog | null;
}

export const DEMO_CODE = 'DEMO-000';

const AGENTS = {
  agent_a: {
    name: 'FemiBot-v1',
    profile: {
      model: 'claude-opus-5',
      provider: 'anthropic',
      prompt_version: 'ref-v1',
      temperature: 0,
    },
  },
  agent_b: {
    name: 'Jab-bot',
    profile: { model: 'none', agent_version: 'jabbot@1.0' },
  },
};

/**
 * Runs a scripted match on timers. The tick pacing is deliberately uneven so the
 * clip queue has to absorb bursts, which is the behaviour spec 13 describes.
 */
export function startDemo(options: DemoOptions): DemoHandle {
  const timers: ReturnType<typeof setTimeout>[] = [];
  const after = (ms: number, fn: () => void) => timers.push(setTimeout(fn, ms));

  const config = { max_ticks: 60, deadline_ms: 5000, resume_window_ms: 120000 };
  const ticks: TickResult[] = [];
  let end: MatchEnd | null = null;
  let stopped = false;

  const send = (msg: SpectatorMessage) => {
    if (!stopped) options.onMessage(msg);
  };

  after(300, () =>
    send({ type: 'lobby_update', corners: { agent_a: AGENTS.agent_a, agent_b: null } }),
  );
  after(1400, () =>
    send({
      type: 'lobby_update',
      corners: { agent_a: AGENTS.agent_a, agent_b: AGENTS.agent_b },
    }),
  );

  if (options.lobbyOnly) {
    return { stop: () => timers.forEach(clearTimeout), log: () => null };
  }

  after(2600, () => {
    send({
      type: 'match_start',
      schema_version: 1,
      match_code: DEMO_CODE,
      agents: AGENTS,
      config,
      started_at: new Date().toISOString(),
    });
    run();
  });

  const random = lcg(20260806);
  let state: Record<Corner, FighterState> = { agent_a: fresh(), agent_b: fresh() };
  let tick = 0;
  let historyB: string[] = [];

  /** agent_a: a competent mixer that finds the hook-into-jab trade (spec 11). */
  function decideA(): string | null {
    const recentJabs = historyB.slice(-4).filter((m) => m === 'jab').length;
    if (state.agent_a.stamina < 20) return 'idle';
    if (recentJabs >= 2 && tick % 4 === 0) return 'hook';
    if (recentJabs >= 2) return 'block_high';
    return ALL[Math.floor(random() * ALL.length)];
  }

  /** agent_b: Jab-bot, with one scripted timeout so the no-move path is exercised. */
  function decideB(): string | null {
    if (tick === 5) return null;
    return 'jab';
  }

  function run(): void {
    if (stopped) return;
    tick += 1;
    const intents = { agent_a: decideA(), agent_b: decideB() };
    const result = resolveTick(state, intents);
    state = result.state;
    historyB.push(result.moves.agent_b);

    const tickResult: TickResult = {
      type: 'tick_result',
      tick,
      moves: result.moves,
      events: result.events,
      state,
      telemetry: {
        agent_a: { latency_ms: Math.round(400 + random() * 1800), fallback: false },
        agent_b: { latency_ms: Math.round(15 + random() * 30), fallback: false },
      },
    };
    ticks.push(tickResult);
    send(tickResult);

    if (result.ko || tick >= 24) {
      const winner: Corner | null = result.ko
        ? state.agent_a.hp <= 0
          ? state.agent_b.hp <= 0
            ? null
            : 'agent_b'
          : 'agent_a'
        : score(state.agent_a) === score(state.agent_b)
          ? null
          : score(state.agent_a) > score(state.agent_b)
            ? 'agent_a'
            : 'agent_b';
      end = {
        type: 'match_end',
        winner,
        reason: result.ko ? 'ko' : 'points',
        final_tick: tick,
        final_state: state,
      };
      after(1600, () => send(end!));
      return;
    }

    // One scripted infrastructure failure, so the paused overlay is reachable
    // without unplugging anything (spec 6.4).
    if (tick === 8) {
      after(700, () => {
        send({
          type: 'match_paused',
          tick: tick + 1,
          missing: 'agent_b',
          resume_window_ms: 120000,
        });
        after(6000, () => {
          send({ type: 'match_resumed', tick: tick + 1 });
          run();
        });
      });
      return;
    }

    // Uneven pacing: some ticks resolve instantly, some take most of the deadline.
    after(random() < 0.3 ? 180 : 900 + random() * 700, run);
  }

  return {
    stop: () => {
      stopped = true;
      timers.forEach(clearTimeout);
    },
    log: () =>
      ticks.length === 0
        ? null
        : {
            schema_version: 1,
            match_start: {
              type: 'match_start',
              schema_version: 1,
              match_code: DEMO_CODE,
              agents: AGENTS,
              config,
            },
            ticks,
            match_end: end,
          },
  };
}

const score = (s: FighterState) => s.points - s.penalties;
