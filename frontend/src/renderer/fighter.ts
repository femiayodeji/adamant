/**
 * One corner: a rig instance, its mixer, its preloaded clip set, and its clip queue.
 *
 * The clip queue is the answer to arena_spec.md 13: ticks resolve as soon as both
 * moves arrive, which can be far faster than a 0.5 s clip plays. Clips are never
 * dropped mid-play and never blended from an arbitrary pose — every clip starts
 * and ends at STANCE (docs/skeleton.md), so back-to-back playback is continuous
 * and a short crossfade is enough to hide the seam.
 *
 * Falling behind is handled by time, not by truncation: `timeScale` rises with the
 * backlog so the queue drains faster without anything being skipped, and only when
 * the backlog is extreme are non-essential reaction clips dropped (see
 * ArenaRenderer.startTick).
 */

import * as THREE from 'three';

import { buildClipSet, Rig, type ClipSet } from './clipFactory';
import { STANCE_CLIP } from './clipTypes';

const FADE = 0.07;

export class Fighter {
  readonly group = new THREE.Group();
  readonly rig: Rig;
  readonly clips: ClipSet;
  private readonly mixer: THREE.AnimationMixer;
  private readonly actions = new Map<string, THREE.AnimationAction>();

  private queue: string[] = [];
  private current: string | null = null;
  private remaining = 0;
  private terminal = false;
  private timeScale = 1;

  constructor(model: THREE.Object3D, mirrored: boolean) {
    this.rig = new Rig(model);
    this.clips = buildClipSet(this.rig, mirrored);
    this.rig.restPose();

    this.group.add(model);
    this.mixer = new THREE.AnimationMixer(model);

    // Preload every action up front — thirteen clips is not a cache problem.
    for (const [name, clip] of this.clips) {
      const action = this.mixer.clipAction(clip);
      action.clampWhenFinished = true;
      this.actions.set(name, action);
    }
    this.playStance(0);
  }

  /** Number of clips waiting behind the one playing. */
  get pending(): number {
    return this.queue.length;
  }

  /** True while anything other than the idle stance loop is playing. */
  get busy(): boolean {
    return this.current !== null || this.queue.length > 0;
  }

  get knockedOut(): boolean {
    return this.terminal;
  }

  has(clip: string): boolean {
    return this.clips.has(clip);
  }

  setTimeScale(scale: number): void {
    this.timeScale = scale;
    this.mixer.timeScale = scale;
  }

  /** Queue a clip by name. Unknown names are ignored rather than throwing — the
   *  action table is the server's, and an unknown action should not blank the canvas. */
  enqueue(clip: string): void {
    if (this.terminal || !this.clips.has(clip)) return;
    if (clip === 'ko') {
      this.queue = [];
      this.play('ko');
      this.terminal = true;
      return;
    }
    this.queue.push(clip);
    if (this.current === null) this.advance();
  }

  clear(): void {
    this.queue = [];
    this.current = null;
    this.terminal = false;
    this.playStance(FADE);
  }

  /** Snap everything back to the stance loop — used by Replay and by reset. */
  reset(): void {
    this.queue = [];
    this.current = null;
    this.terminal = false;
    this.mixer.stopAllAction();
    this.playStance(0);
  }

  update(dt: number): void {
    if (this.current !== null) {
      this.remaining -= dt * this.timeScale;
      if (this.remaining <= 0) {
        // A terminal clip (ko) clamps on its last frame and the fighter stops
        // being busy — otherwise the end banner, which waits for the animation
        // to catch up with the result, would never appear.
        if (this.terminal) this.current = null;
        else this.advance();
      }
    }
    this.mixer.update(dt);
  }

  dispose(): void {
    this.mixer.stopAllAction();
    this.mixer.uncacheRoot(this.mixer.getRoot() as THREE.Object3D);
  }

  private advance(): void {
    const next = this.queue.shift();
    if (next === undefined) {
      this.current = null;
      this.playStance(FADE);
      return;
    }
    this.play(next);
  }

  private play(name: string): void {
    const action = this.actions.get(name);
    const clip = this.clips.get(name);
    if (!action || !clip) {
      this.current = null;
      return;
    }
    this.fadeOthers(name);
    action.reset();
    action.setLoop(THREE.LoopOnce, 1);
    action.clampWhenFinished = true;
    action.setEffectiveTimeScale(1);
    action.setEffectiveWeight(1);
    action.fadeIn(FADE);
    action.play();
    this.current = name;
    this.remaining = clip.duration;
  }

  private playStance(fade: number): void {
    const action = this.actions.get(STANCE_CLIP);
    if (!action) return;
    this.fadeOthers(STANCE_CLIP);
    action.reset();
    action.setLoop(THREE.LoopRepeat, Infinity);
    action.clampWhenFinished = false;
    action.setEffectiveTimeScale(1);
    action.setEffectiveWeight(1);
    if (fade > 0) action.fadeIn(fade);
    action.play();
  }

  private fadeOthers(keep: string): void {
    for (const [name, action] of this.actions) {
      if (name !== keep && action.isRunning()) action.fadeOut(FADE);
    }
  }
}
