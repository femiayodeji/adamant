/**
 * A buffer between the socket and the renderer.
 *
 * The glb takes a moment to load, and a spectator can open a watch link mid-fight,
 * so ticks routinely arrive before there is a renderer to hand them to. The sink
 * holds them and flushes on attach, which keeps the feed layer ignorant of whether
 * the canvas is ready.
 */

import type { TickResult } from '@/lib/types';
import type { ArenaRenderer } from './arenaRenderer';

export class TickSink {
  private renderer: ArenaRenderer | null = null;
  private pending: TickResult[] = [];

  attach(renderer: ArenaRenderer): void {
    this.renderer = renderer;
    for (const tick of this.pending) renderer.pushTick(tick);
    this.pending = [];
  }

  detach(): void {
    this.renderer = null;
  }

  push = (tick: TickResult): void => {
    if (this.renderer) this.renderer.pushTick(tick);
    else this.pending.push(tick);
  };

  clear(): void {
    this.pending = [];
  }
}
