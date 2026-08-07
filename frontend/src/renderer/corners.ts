/**
 * The two corners' colours, in one place.
 *
 * Boxing's own: blue corner and red corner. Four things wear them and all four
 * have to agree, or a spectator glancing between the canvas, the HUD and a
 * downloaded clip has to re-learn which side is which every time —
 *
 *   - the fighters themselves (`Fighter.skin`, on the albedo),
 *   - the rim lights, deck bands and post caps (`ArenaRenderer.buildStage`),
 *   - the recording overlay (`videoOverlay.ts`, a 2D canvas — hence the hex form
 *     and hence this module not importing three),
 *   - `--a` / `--b` in globals.css, the one copy that cannot import anything.
 *
 * Colour is never the only cue: every corner is labelled A/B in the HUD and the
 * sides never swap.
 */

import type { Corner } from '@/lib/types';

export const CORNER_COLOR: Record<Corner, number> = {
  agent_a: 0x2f6dff,
  agent_b: 0xff3a2f,
};

/** `#rrggbb`, for the 2D canvas contexts. */
export function cornerHex(corner: Corner): string {
  return `#${CORNER_COLOR[corner].toString(16).padStart(6, '0')}`;
}
