/**
 * The scoreboard, drawn into the recorded video.
 *
 * The live HUD is React over the canvas, and none of it reaches a canvas capture
 * (see recorder.ts). Rather than screenshot the DOM, this repaints the same
 * numbers with the same rules in 2D: HP and stamina as notched gauges, points,
 * penalties, the repeat streak, the tick counter, and the current tick's events.
 *
 * It is deliberately a *reading* of the HUD, not a pixel copy — the video is
 * 1280 px wide whatever the browser window was, so everything is laid out from
 * the frame size rather than inherited from CSS.
 */

import { cornerHex } from './corners';
import {
  MAX_CONSECUTIVE_REPEATS,
  type ArenaEvent,
  type Corner,
  type FighterState,
  type MatchEnd,
} from '@/lib/types';

/** From `corners.ts`, so a recording can never label the corners in different
 *  colours from the canvas it is recording. */
const COLOR: Record<Corner, string> = {
  agent_a: cornerHex('agent_a'),
  agent_b: cornerHex('agent_b'),
};
const ALARM = '#ff3a2f';
const VOID = '#ffb020';
const INK = '#e8f2f8';
const DIM = 'rgba(232, 242, 248, 0.55)';
const PANEL = 'rgba(6, 10, 15, 0.72)';

export interface OverlayData {
  code: string | null;
  names: Record<Corner, string>;
  state: Record<Corner, FighterState>;
  tick: number;
  maxTicks: number;
  events: ArenaEvent[];
  /** Drawn as the closing card once the replay has run out of ticks. */
  end: MatchEnd | null;
}

const mono = (size: number, weight = '400') =>
  `${weight} ${size}px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`;

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, r);
}

/** One notched bar. `align` mirrors the whole gauge for the right-hand corner. */
function gauge(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  value: number,
  color: string,
  label: string,
  align: 'left' | 'right',
): void {
  const pct = Math.max(0, Math.min(100, value)) / 100;
  const shown = Math.max(0, Math.round(value));

  ctx.fillStyle = 'rgba(255,255,255,0.10)';
  roundRect(ctx, x, y, w, h, h / 2);
  ctx.fill();

  ctx.fillStyle = pct <= 0.25 ? '#ff6a4a' : color;
  const fw = w * pct;
  if (fw > 0) {
    roundRect(ctx, align === 'left' ? x : x + w - fw, y, fw, h, h / 2);
    ctx.fill();
  }

  // Notches every five points, so damage can be counted off the bar.
  ctx.strokeStyle = 'rgba(5, 8, 12, 0.55)';
  ctx.lineWidth = 1;
  for (let i = 1; i < 20; i++) {
    const nx = x + (w * i) / 20;
    ctx.beginPath();
    ctx.moveTo(nx, y);
    ctx.lineTo(nx, y + h);
    ctx.stroke();
  }

  ctx.font = mono(13, '600');
  ctx.fillStyle = DIM;
  ctx.textBaseline = 'middle';
  if (align === 'left') {
    ctx.textAlign = 'right';
    ctx.fillText(label, x - 8, y + h / 2);
    ctx.textAlign = 'left';
    ctx.fillStyle = INK;
    ctx.fillText(String(shown), x + w + 8, y + h / 2);
  } else {
    ctx.textAlign = 'left';
    ctx.fillText(label, x + w + 8, y + h / 2);
    ctx.textAlign = 'right';
    ctx.fillStyle = INK;
    ctx.fillText(String(shown), x - 8, y + h / 2);
  }
}

function fighterPanel(
  ctx: CanvasRenderingContext2D,
  corner: Corner,
  name: string,
  state: FighterState,
  x: number,
  y: number,
  w: number,
): void {
  const right = corner === 'agent_b';
  const color = COLOR[corner];
  const h = 96;

  ctx.fillStyle = PANEL;
  roundRect(ctx, x, y, w, h, 10);
  ctx.fill();
  ctx.strokeStyle = color;
  ctx.globalAlpha = 0.5;
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.globalAlpha = 1;

  const pad = 14;
  ctx.textBaseline = 'middle';
  ctx.textAlign = right ? 'right' : 'left';
  const nameX = right ? x + w - pad : x + pad;

  ctx.font = mono(12, '700');
  ctx.fillStyle = color;
  ctx.fillText(right ? 'B' : 'A', nameX, y + 18);

  ctx.font = `600 15px ui-sans-serif, system-ui, sans-serif`;
  ctx.fillStyle = INK;
  ctx.fillText(name.slice(0, 22), nameX + (right ? -18 : 18), y + 18);

  const barW = w - pad * 2 - 60;
  const barX = right ? x + pad + 34 : x + pad + 26;
  gauge(ctx, barX, y + 36, barW, 9, state.hp, color, 'HP', right ? 'right' : 'left');
  gauge(ctx, barX, y + 54, barW, 9, state.stamina, '#8affd0', 'ST', right ? 'right' : 'left');

  // Points / penalties / repeat streak, in the HUD's own order.
  const streak = state.repeat_streak ?? 0;
  const lit = Math.min(streak, MAX_CONSECUTIVE_REPEATS);
  ctx.font = mono(12, '600');
  const bits: [string, string][] = [[`PTS ${state.points}`, INK]];
  if (state.penalties > 0) bits.push([`PEN ${state.penalties}`, '#ff9a6c']);
  bits.push([
    streak > MAX_CONSECUTIVE_REPEATS ? 'REP VOID' : `REP ${lit}/${MAX_CONSECUTIVE_REPEATS}`,
    streak > MAX_CONSECUTIVE_REPEATS ? VOID : lit >= MAX_CONSECUTIVE_REPEATS ? VOID : DIM,
  ]);

  let cx = right ? x + w - pad : x + pad;
  for (const [text, tint] of right ? [...bits].reverse() : bits) {
    ctx.fillStyle = tint;
    ctx.fillText(text, cx, y + 78);
    const adv = ctx.measureText(text).width + 14;
    cx += right ? -adv : adv;
  }
}

/** The same sentences the on-page event log prints, for the current tick only. */
function eventLine(event: ArenaEvent, names: Record<Corner, string>): [string, string] {
  switch (event.type) {
    case 'hit':
      return [`${names[event.attacker]} lands ${event.action} for ${event.damage}`, '#ffc9a8'];
    case 'blocked':
      return [`${names[event.defender]} blocks ${event.action} with ${event.defense}`, '#a9e6ff'];
    case 'dodged':
      return [
        `${names[event.defender]} slips ${event.action} with ${event.defense}` +
          (event.riposte ? ` and ripostes ${event.riposte}` : ''),
        '#8affd0',
      ];
    case 'exhausted':
      return [
        `${names[event.who]} could not afford ${event.attempted} (${event.stamina} stamina)`,
        VOID,
      ];
    case 'no_move':
      return [
        `${names[event.who]} made no move (${event.cause}) — penalty ${event.penalty}`,
        '#ff9a6c',
      ];
    case 'void':
      return [
        `${names[event.who]} repeated ${event.action} ${event.streak}× — voided by the repeat cap`,
        VOID,
      ];
    case 'ko':
      return [`${names[event.who]} is knocked out`, ALARM];
    default:
      return ['', INK];
  }
}

export function paintOverlay(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  data: OverlayData,
): void {
  const margin = Math.round(width * 0.022);
  const panelW = Math.round(width * 0.30);

  fighterPanel(ctx, 'agent_a', data.names.agent_a, data.state.agent_a, margin, margin, panelW);
  fighterPanel(
    ctx,
    'agent_b',
    data.names.agent_b,
    data.state.agent_b,
    width - margin - panelW,
    margin,
    panelW,
  );

  // Tick counter, centred between the panels.
  const cx = width / 2;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillStyle = PANEL;
  roundRect(ctx, cx - 58, margin, 116, 58, 10);
  ctx.fill();
  ctx.font = mono(28, '700');
  ctx.fillStyle = INK;
  ctx.fillText(
    `${String(data.tick).padStart(2, '0')}`,
    cx - 10,
    margin + 26,
  );
  ctx.font = mono(14, '400');
  ctx.fillStyle = DIM;
  ctx.fillText(`/${data.maxTicks}`, cx + 26, margin + 29);
  ctx.font = mono(10, '600');
  ctx.fillText('TICK', cx, margin + 47);

  // This tick's events, stacked up from the bottom edge.
  const lines = data.events.map((e) => eventLine(e, data.names)).filter(([t]) => t);
  ctx.textAlign = 'left';
  ctx.font = mono(15);
  let ly = height - margin - 8;
  for (const [text, tint] of [...lines].reverse()) {
    const w = ctx.measureText(text).width;
    ctx.fillStyle = PANEL;
    roundRect(ctx, margin, ly - 15, w + 24, 26, 6);
    ctx.fill();
    ctx.fillStyle = tint;
    ctx.fillText(text, margin + 12, ly);
    ly -= 32;
  }

  // Watermark, so a shared clip still says where it came from.
  ctx.textAlign = 'right';
  ctx.font = mono(12, '600');
  ctx.fillStyle = 'rgba(232, 242, 248, 0.38)';
  ctx.fillText(
    data.code ? `ADAM ARENA · ${data.code}` : 'ADAM ARENA',
    width - margin,
    height - margin,
  );

  if (data.end) {
    ctx.fillStyle = 'rgba(3, 5, 9, 0.82)';
    ctx.fillRect(0, 0, width, height);
    ctx.textAlign = 'center';
    ctx.fillStyle = INK;
    ctx.font = `800 46px ui-sans-serif, system-ui, sans-serif`;
    ctx.fillText(
      data.end.winner ? `${data.names[data.end.winner]} wins`.toUpperCase() : 'DRAW',
      width / 2,
      height / 2 - 16,
    );
    ctx.font = mono(18);
    ctx.fillStyle = DIM;
    const a = data.end.final_state.agent_a;
    const b = data.end.final_state.agent_b;
    ctx.fillText(
      `by ${data.end.reason} · tick ${data.end.final_tick} · ` +
        `${a.points - a.penalties} — ${b.points - b.penalties} decision score`,
      width / 2,
      height / 2 + 28,
    );
  }
}
