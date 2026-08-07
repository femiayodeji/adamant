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

/** Resting emissive on the chassis: enough to read as powered, not enough to glow. */
const BASE_EMISSIVE = 0.055;

/**
 * Panel lines and machining noise, as a roughness map. One canvas shared by both
 * corners: bare metal with a uniform roughness reads as liquid, and this is the
 * cheapest way to break up the highlight into plate segments without touching
 * geometry or shipping a texture file.
 */
let PANELS: THREE.Texture | null = null;

function panelTexture(): THREE.Texture {
  if (PANELS) return PANELS;
  const size = 256;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d')!;

  ctx.fillStyle = '#4a4a4a';
  ctx.fillRect(0, 0, size, size);

  // Brushed grain.
  for (let i = 0; i < 2600; i++) {
    const v = 40 + Math.floor(Math.random() * 90);
    ctx.fillStyle = `rgb(${v},${v},${v})`;
    ctx.fillRect(Math.random() * size, Math.random() * size, 1 + Math.random() * 9, 1);
  }
  // Plate seams: rough grooves, so the specular breaks along them.
  ctx.strokeStyle = '#d8d8d8';
  ctx.lineWidth = 2;
  for (const y of [0.14, 0.38, 0.52, 0.79]) {
    ctx.beginPath();
    ctx.moveTo(0, y * size);
    ctx.lineTo(size, y * size);
    ctx.stroke();
  }
  for (const x of [0.22, 0.61, 0.88]) {
    ctx.beginPath();
    ctx.moveTo(x * size, 0);
    ctx.lineTo(x * size, size);
    ctx.stroke();
  }

  PANELS = new THREE.CanvasTexture(canvas);
  PANELS.wrapS = PANELS.wrapT = THREE.RepeatWrapping;
  PANELS.repeat.set(4, 4);
  return PANELS;
}
/** Flash decay, in units of `flash` per second. ~0.3 s from a full hit back to rest. */
const FLASH_DECAY = 3.4;

export class Fighter {
  readonly group = new THREE.Group();
  readonly rig: Rig;
  readonly clips: ClipSet;
  private readonly mixer: THREE.AnimationMixer;
  private readonly actions = new Map<string, THREE.AnimationAction>();

  /** Chassis materials, cloned per corner so a flash on one never tints the other. */
  private readonly materials: THREE.MeshStandardMaterial[] = [];
  private readonly tone = new THREE.Color(0xffffff);
  private readonly flashTone = new THREE.Color(0xffffff);
  private flash = 0;

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

    // Preload every action up front — fourteen clips is not a cache problem.
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

  /**
   * Turn the rig into a machine in this corner's colour: dark anodised plate, high
   * metalness so the environment does the shaping, flat shading for panel facets,
   * and a low emissive so the fighter reads as powered-on against a dark arena.
   * The accent lives in the emissive, which is also the channel `pulse` drives.
   */
  skin(color: number): void {
    this.tone.set(color);
    const panels = panelTexture();
    this.group.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      if (!mesh.isMesh) return;
      const source = Array.isArray(mesh.material) ? mesh.material[0] : mesh.material;
      const material = (source as THREE.MeshStandardMaterial).clone();
      // Drop the rig's own skin texture: with it in place this is a person in a
      // coloured suit. Without it, and at metalness 1, the shape is described
      // entirely by what it reflects — which is what makes it read as a machine.
      material.map = null;
      material.color = new THREE.Color(color).multiplyScalar(0.075);
      material.metalness = 1;
      material.roughness = 0.45;
      material.roughnessMap = panels;
      material.flatShading = true;
      material.emissive = this.tone.clone();
      material.emissiveIntensity = BASE_EMISSIVE;
      material.needsUpdate = true;
      mesh.material = material;
      this.materials.push(material);
    });
  }

  /** Drive the emissive channel toward `color` and let it decay — the hit flash, the
   *  block spark's counterpart on the body, and the amber jolt on a voided move. */
  pulse(color: number, strength = 1): void {
    this.flashTone.set(color);
    this.flash = Math.max(this.flash, strength);
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
    this.flash = 0;
    for (const material of this.materials) {
      material.emissive.copy(this.tone);
      material.emissiveIntensity = BASE_EMISSIVE;
    }
    this.mixer.stopAllAction();
    this.playStance(0);
  }

  update(dt: number): void {
    if (this.flash > 0) {
      // Runs one more frame after reaching zero, which is what restores the resting
      // emissive — no separate "clear the flash" branch.
      this.flash = Math.max(0, this.flash - dt * FLASH_DECAY);
      for (const material of this.materials) {
        material.emissive.copy(this.tone).lerp(this.flashTone, Math.min(1, this.flash));
        material.emissiveIntensity = BASE_EMISSIVE + this.flash * 1.9;
      }
    }
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
