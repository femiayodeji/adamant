/**
 * One corner: a rig instance, its mixer, its preloaded clip set, and its clip queue.
 *
 * The clip queue is the answer to adamant_spec.md 13: ticks resolve as soon as both
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

/** Resting emissive tint. Low on purpose — the corner colour is on the albedo now,
 *  and this is a rim of it, not a second coat. It is also the channel `pulse` drives. */
const BASE_EMISSIVE = 0.06;

/** How dark the corner colour goes on the body. Full saturation under the key
 *  light clips to a flat plate and takes the shading of the torso with it. */
const BODY_TINT = 0.55;

/** The dark end the joint material is mixed toward: near-black, so knees, elbows
 *  and shoulders read as articulation instead of as more torso. */
const JOINT_DARK = 0x05070a;
const JOINT_MIX = 0.82;

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
   * Put this corner's colours on the fighter — red corner, blue corner.
   *
   * Both fighters are the same rig loaded twice, and `xbot.glb` dresses it in one
   * salmon body and no textures at all, so out of the file the two corners are
   * literally the same colour. Rim lights and a deck band underneath are not
   * enough to fix that once the fighters are close together and moving: at the
   * moment a punch lands, which is the moment that has to be readable, the two
   * silhouettes overlap and a rim is exactly what is occluded.
   *
   * So the corner colour goes on the albedo. The rig carries two materials and
   * they are treated differently on purpose — the body takes the corner colour
   * darkened (full saturation clips flat under the key light), and the joint
   * material is mixed almost to black so knees, elbows and shoulders stay
   * legible as articulation. Nothing here needs a texture, because the file
   * has none to preserve.
   *
   * The materials are CLONED per corner, because the emissive is also the channel
   * `pulse` drives — two corners sharing one material would flash together.
   */
  skin(color: number): void {
    this.tone.set(color);
    const body = new THREE.Color(color).multiplyScalar(BODY_TINT);
    const joint = new THREE.Color(color).lerp(new THREE.Color(JOINT_DARK), JOINT_MIX);

    this.group.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      if (!mesh.isMesh) return;
      const source = Array.isArray(mesh.material) ? mesh.material[0] : mesh.material;
      const material = (source as THREE.MeshStandardMaterial).clone();
      // xbot.glb names them `Beta_Joints_MAT` and `…Beta_HighLimbsGeoSG2`.
      const isJoint = /joint/i.test(material.name);
      material.color.copy(isJoint ? joint : body);
      material.metalness = isJoint ? 0.62 : 0.1;
      material.roughness = isJoint ? 0.38 : 0.62;
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

  /**
   * Jump straight to the settled end of the KO clip, skipping the fall.
   *
   * Scrubbing a replay to a tick after the knockout must show the fighter already
   * on the floor: replaying the fall every time the slider moves would make the
   * timeline unreadable, and leaving it standing would contradict the HP the HUD
   * is printing for that same tick.
   */
  snapToKo(): void {
    const clip = this.clips.get('ko');
    const action = this.actions.get('ko');
    if (!clip || !action) return;
    this.queue = [];

    // Deliberately NOT `play('ko')` + `mixer.setTime(duration)`.
    //
    // `fadeIn`/`fadeOut` schedule their weight interpolants against the MIXER
    // clock, which by this point is however many seconds the tab has been open.
    // `setTime` rewinds that clock to zero, so every fade in flight then gets
    // evaluated far before its own start and returns its *starting* value — the
    // incoming ko clip pinned at weight 0, the outgoing stance at weight 1. The
    // fighter stays standing while the HUD prints 0 HP.
    //
    // A jump has no previous pose worth blending from anyway, so: no fades, set
    // the weight outright, set the clip time directly, and flush it to the bones
    // with a zero-length update.
    for (const [name, other] of this.actions) {
      other.stopFading();
      if (name !== 'ko') other.stop();
    }
    action.reset();
    action.setLoop(THREE.LoopOnce, 1);
    action.clampWhenFinished = true;
    action.setEffectiveTimeScale(1);
    action.setEffectiveWeight(1);
    action.play();
    action.time = clip.duration;
    // Paused before the flush, so this update and every later one leave it here.
    action.paused = true;
    this.mixer.update(0);

    this.terminal = true;
    this.current = null;
    this.remaining = 0;
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
        material.emissiveIntensity = BASE_EMISSIVE + this.flash * 1.5;
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

  /**
   * Fade out everything the mixer is still evaluating except `keep`.
   *
   * The test is deliberately not `isRunning()`. A LoopOnce action that has
   * FINISHED with `clampWhenFinished` is paused, not stopped: three.js leaves it
   * enabled, active in the mixer, and contributing its last frame at full weight
   * — and `isRunning()` reports false for exactly that state, so a check on it
   * skips the one case that needs clearing.
   *
   * That is invisible for thirteen of the fourteen clips, because every
   * non-terminal clip ends at STANCE (docs/skeleton.md) and a stale one is
   * therefore blending stance into stance. `ko` is the exception — it ends on
   * the deck — so a knockout thrown over a pile of clamped clips resolved to an
   * AVERAGE of lying down and standing up: a fighter stopped half-fallen,
   * hovering above the floor, which reads as a body sinking rather than landing.
   */
  private fadeOthers(keep: string): void {
    for (const [name, action] of this.actions) {
      if (name === keep) continue;
      if (action.isRunning() || (action.enabled && action.paused)) action.fadeOut(FADE);
    }
  }
}
