/**
 * The arena renderer: one Three.js scene, two mirrored fighters, one tick queue.
 *
 * It has exactly one input — `pushTick(tick_result)` — and it does not care where
 * that came from. The live spectator socket, the replay driver reading a stored
 * match log, and the offline demo fixture all feed this same method, which is how
 * arena_spec.md 7's "Replay = feeding the same events through the same renderer
 * code on a timer" is satisfied with no second implementation.
 */

import * as THREE from 'three';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';

import { Fighter } from './fighter';
import { Shake, Sparks } from './fx';
import { CORNERS, type ArenaEvent, type Corner, type TickResult } from '@/lib/types';

/** Everything the HUD needs, emitted when a tick's animation actually begins. */
export interface HudSnapshot {
  tick: number;
  state: TickResult['state'];
  moves: TickResult['moves'];
  events: ArenaEvent[];
  telemetry?: TickResult['telemetry'];
  backlog: number;
}

export interface RendererOptions {
  modelUrl?: string;
  onHud?: (snapshot: HudSnapshot) => void;
}

/** Corner identity, shared with globals.css (`--a` / `--b`). Cyan diagnostic vs.
 *  hazard red — legible side by side and distinguishable at phone size. Colour is
 *  never the only cue: the HUD labels every corner and the sides never swap. */
const CORNER_COLOR: Record<Corner, number> = {
  agent_a: 0x29e0ff,
  agent_b: 0xff5a3c,
};

/** Warning amber. The repeat cap's colour everywhere: clip flash, spark, HUD pip. */
const VOID_COLOR = 0xffb020;
const STEEL = 0x0a0e13;

/** Half the gap between the fighters, in metres. Tuned against tools/pose_check.py:
 *  a jab puts the fist 0.67 m in front of the model origin, and a torso is ~0.15 m
 *  deep, so 0.42 is the separation at which a straight punch arrives on the chin. */
const HALF_GAP = 0.42;
const MIN_TICK_SECONDS = 0.45;
const MAX_TIME_SCALE = 2.4;
/** Backlog past which reaction clips are dropped to catch up (KO is never dropped). */
const RUSH_BACKLOG = 6;

export class ArenaRenderer {
  private readonly scene = new THREE.Scene();
  private readonly camera: THREE.PerspectiveCamera;
  private readonly renderer: THREE.WebGLRenderer;
  private readonly fighters: Record<Corner, Fighter>;
  private readonly observer: ResizeObserver | null = null;
  private readonly sparks = new Sparks();
  private readonly shake = new Shake();
  private readonly cameraHome = new THREE.Vector3();
  private readonly lookAt = new THREE.Vector3(0, 1.02, 0);

  private queue: TickResult[] = [];
  private currentTick: TickResult | null = null;
  private tickElapsed = 0;
  private frame = 0;
  private lastFrameMs = 0;
  private paused = false;
  private disposed = false;
  /** Replay transport speed. Multiplies the backlog catch-up scale, so `0.25` is a
   *  quarter-speed review and `4` a fast skim; live play never touches it. */
  private speed = 1;
  private frameHook: (() => void) | null = null;

  private constructor(
    private readonly canvas: HTMLCanvasElement,
    fighterA: Fighter,
    fighterB: Fighter,
    private readonly onHud?: (s: HudSnapshot) => void,
  ) {
    this.fighters = { agent_a: fighterA, agent_b: fighterB };

    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    // ACES keeps the emissive rims and the spark bursts from clipping to white
    // flat, which is what makes hot accents read as hot rather than as blown out.
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;

    this.scene.background = new THREE.Color(0x05070a);
    this.scene.fog = new THREE.Fog(0x05070a, 5.5, 15);

    this.camera = new THREE.PerspectiveCamera(38, 1, 0.1, 60);

    // agent_a on the left facing +X, agent_b on the right facing -X: a literal
    // mirror across the X axis, which is what the mirrored clip set assumes.
    fighterA.group.position.set(-HALF_GAP, 0, 0);
    fighterA.group.rotation.y = Math.PI / 2;
    fighterB.group.position.set(HALF_GAP, 0, 0);
    fighterB.group.rotation.y = -Math.PI / 2;

    fighterA.skin(CORNER_COLOR.agent_a);
    fighterB.skin(CORNER_COLOR.agent_b);

    this.scene.add(fighterA.group, fighterB.group);
    this.scene.add(this.sparks.points);
    this.buildStage();

    if (typeof ResizeObserver !== 'undefined') {
      this.observer = new ResizeObserver(() => this.resize());
      this.observer.observe(canvas.parentElement ?? canvas);
    }
    this.resize();
    this.lastFrameMs = performance.now();
    this.loop();
  }

  static async create(
    canvas: HTMLCanvasElement,
    options: RendererOptions = {},
  ): Promise<ArenaRenderer> {
    const url = options.modelUrl ?? 'models/xbot.glb';
    const buffer = await fetch(url).then((r) => {
      if (!r.ok) throw new Error(`could not load ${url} (${r.status})`);
      return r.arrayBuffer();
    });
    const loader = new GLTFLoader();
    const parse = (): Promise<THREE.Object3D> =>
      new Promise((resolve, reject) =>
        loader.parse(buffer.slice(0), '', (gltf) => resolve(gltf.scene), reject),
      );

    // Two independent parses instead of SkeletonUtils.clone: the file is fetched
    // once, and each corner gets a skeleton it can pose without touching the other.
    const [modelA, modelB] = await Promise.all([parse(), parse()]);
    return new ArenaRenderer(
      canvas,
      new Fighter(modelA, false),
      new Fighter(modelB, true),
      options.onHud,
    );
  }

  /* ------------------------------------------------------------- input */

  pushTick(tick: TickResult): void {
    this.queue.push(tick);
  }

  /** Clips still waiting. Replay uses this for backpressure. */
  get backlog(): number {
    return this.queue.length;
  }

  get idle(): boolean {
    return this.queue.length === 0 && this.currentTick === null;
  }

  setPaused(paused: boolean): void {
    this.paused = paused;
  }

  /** Replay transport speed (1 = as fought). Clamped to a range that still animates. */
  setSpeed(speed: number): void {
    this.speed = Math.min(Math.max(speed, 0.1), 8);
  }

  /**
   * Run `hook` immediately after every `render()`, while the drawing buffer is
   * still valid.
   *
   * This is the only safe moment to `drawImage` the WebGL canvas into a 2D one:
   * outside the frame that drew it the buffer is already cleared, which is why the
   * video recorder composites from here rather than from its own rAF loop.
   */
  setFrameHook(hook: (() => void) | null): void {
    this.frameHook = hook;
  }

  /** Back to two fighters standing in their corners — used before a replay. */
  reset(): void {
    this.queue = [];
    this.currentTick = null;
    this.tickElapsed = 0;
    for (const corner of CORNERS) this.fighters[corner].reset();
  }

  /**
   * Put the scene where it would be at `tick` without playing anything in between —
   * the slider drag. `state` on a tick_result is absolute (spec §7), so one tick is
   * the whole picture and there is nothing to accumulate; the only thing a jump has
   * to reconstruct is whether a fighter is already on the floor.
   */
  scrubTo(tick: TickResult, knockedOut: readonly Corner[]): void {
    this.queue = [];
    this.currentTick = null;
    this.tickElapsed = 0;
    for (const corner of CORNERS) {
      this.fighters[corner].reset();
      if (knockedOut.includes(corner)) this.fighters[corner].snapToKo();
    }
    this.sparks.clear();
    this.onHud?.({
      tick: tick.tick,
      state: tick.state,
      moves: tick.moves ?? {},
      events: tick.events ?? [],
      telemetry: tick.telemetry,
      backlog: 0,
    });
  }

  dispose(): void {
    this.disposed = true;
    cancelAnimationFrame(this.frame);
    this.observer?.disconnect();
    this.sparks.dispose();
    this.scene.environment?.dispose();
    for (const corner of CORNERS) this.fighters[corner].dispose();
    this.scene.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      if (mesh.geometry) mesh.geometry.dispose();
      const material = mesh.material;
      if (Array.isArray(material)) material.forEach((m) => m.dispose());
      else if (material) material.dispose();
    });
    this.renderer.dispose();
  }

  /* -------------------------------------------------------------- loop */

  private loop = (): void => {
    if (this.disposed) return;
    this.frame = requestAnimationFrame(this.loop);
    const now = performance.now();
    const dt = Math.min((now - this.lastFrameMs) / 1000, 0.1);
    this.lastFrameMs = now;

    if (!this.paused) {
      this.step(dt);
    }
    this.renderer.render(this.scene, this.camera);
    this.frameHook?.();
  };

  private step(dt: number): void {
    // Catch-up: ticks can resolve much faster than a 0.5 s clip plays
    // (arena_spec.md 13). Speed the queue up rather than dropping frames.
    const scale = Math.min(1 + 0.3 * this.queue.length, MAX_TIME_SCALE) * this.speed;
    for (const corner of CORNERS) this.fighters[corner].setTimeScale(scale);

    if (this.currentTick) {
      this.tickElapsed += dt * scale;
      const stillPlaying =
        this.fighters.agent_a.busy || this.fighters.agent_b.busy;
      if (!stillPlaying && this.tickElapsed >= MIN_TICK_SECONDS) {
        this.currentTick = null;
      }
    }
    if (!this.currentTick && this.queue.length > 0) {
      this.startTick(this.queue.shift()!);
    }

    for (const corner of CORNERS) this.fighters[corner].update(dt);

    this.sparks.update(dt);
    this.shake.update(dt);
    this.camera.position.copy(this.cameraHome).add(this.shake.offset);
    this.camera.lookAt(this.lookAt);
  }

  private startTick(tick: TickResult): void {
    this.currentTick = tick;
    this.tickElapsed = 0;
    const rushing = this.queue.length > RUSH_BACKLOG;

    for (const corner of CORNERS) {
      const fighter = this.fighters[corner];
      const events = tick.events ?? [];

      // Clip name IS the action name (skeleton.md) — no mapping table. The three
      // exceptions are all event-driven, not action-driven:
      //   void     : the repeat cap stripped the action (spec 4.6). `moves` still
      //              reports the submitted action, but the punch never happened, so
      //              the strike clip must NOT play — the jam clip plays instead.
      //   exhausted: the action was unaffordable and became idle (spec 3), so the
      //              tell belongs to the event, not to whatever `moves` reports.
      //   no_move  : nothing was submitted (spec 4.4) — the fighter just stands.
      const voided = events.some((e) => e.type === 'void' && e.who === corner);
      const exhausted = events.some((e) => e.type === 'exhausted' && e.who === corner);
      const noMove = events.some((e) => e.type === 'no_move' && e.who === corner);
      const move = tick.moves?.[corner];

      // Spec 4.6: at most one of no_move / void / exhausted fires per fighter per
      // tick. Ordered anyway so a server that ever emitted two could not stack clips.
      if (voided) fighter.enqueue('void');
      else if (exhausted) fighter.enqueue('exhausted');
      else if (!noMove && move) fighter.enqueue(move);

      if (!rushing) {
        for (const event of events) {
          if (event.type === 'hit' && event.defender === corner) {
            fighter.enqueue('hit_react');
          } else if (event.type === 'dodged') {
            // The dodger counters; the attacker eats the riposte chip damage.
            if (event.defender === corner) fighter.enqueue('riposte');
            else if (event.attacker === corner && (event.riposte ?? 0) > 0) {
              fighter.enqueue('hit_react');
            }
          }
        }
      }
      for (const event of events) {
        if (event.type === 'ko' && event.who === corner) fighter.enqueue('ko');
      }
    }

    this.playImpacts(tick.events ?? []);

    this.onHud?.({
      tick: tick.tick,
      state: tick.state,
      moves: tick.moves ?? {},
      events: tick.events ?? [],
      telemetry: tick.telemetry,
      backlog: this.queue.length,
    });
  }

  /**
   * Impact feedback, once per tick over the whole event list — not per corner, or a
   * two-corner loop would fire each burst twice. Every branch is a spark burst, an
   * emissive pulse on a body, a camera jolt, or some combination; none of them
   * allocate geometry or touch the clip queue.
   */
  private playImpacts(events: ArenaEvent[]): void {
    for (const event of events) {
      switch (event.type) {
        case 'hit': {
          this.sparks.burst(
            this.contact(event.defender, 1.3),
            0xffe6b0,
            10 + Math.round(event.damage * 0.7),
            1.5 + event.damage * 0.07,
          );
          // Capped below 1 so a hard hook still leaves the corner's own colour
          // readable through the flash — the flash must not erase the identity.
          this.fighters[event.defender].pulse(0xffffff, Math.min(0.72, 0.3 + event.damage / 26));
          this.shake.hit(0.006 + event.damage * 0.0022);
          break;
        }
        case 'blocked':
          // The guard holds: a short cold spark off the blocking arm, no shake worth
          // the name. Visibly different from taking the punch.
          this.sparks.burst(this.contact(event.defender, 1.36), CORNER_COLOR[event.defender], 8, 1.4);
          this.fighters[event.defender].pulse(CORNER_COLOR[event.defender], 0.6);
          this.shake.hit(0.004);
          break;
        case 'dodged':
          this.fighters[event.defender].pulse(0x8affd0, 0.45);
          if ((event.riposte ?? 0) > 0) {
            this.sparks.burst(this.contact(event.attacker, 1.24), 0x8affd0, 8, 1.5);
            this.shake.hit(0.006);
          }
          break;
        case 'void':
          // Amber, low and close to the body: the machine jamming, not a landed blow.
          this.sparks.burst(this.contact(event.who, 1.14), VOID_COLOR, 7, 1);
          // Same cap as the hit flash: a warning wash over the chassis, not a repaint.
          // At full strength the fighter turns solid amber and stops being its corner.
          this.fighters[event.who].pulse(VOID_COLOR, 0.45);
          break;
        case 'ko':
          this.sparks.burst(this.contact(event.who, 1.22), 0xffffff, 26, 3);
          this.shake.hit(0.09);
          break;
      }
    }
  }

  /** The point where a punch aimed at `corner` arrives — between the two fighters,
   *  biased to the receiving side, at roughly chin height. */
  private contact(corner: Corner, height: number): THREE.Vector3 {
    return new THREE.Vector3((corner === 'agent_a' ? -1 : 1) * HALF_GAP * 0.55, height, 0);
  }

  /* ------------------------------------------------------------- stage */

  private buildStage(): void {
    // A metal fighter with nothing to reflect renders black. A one-off PMREM of the
    // built-in room gives the chassis something to catch without shipping an HDR.
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environmentIntensity = 0.5;
    pmrem.dispose();

    this.scene.add(new THREE.HemisphereLight(0x8fb4d8, 0x05070a, 0.5));

    // Hard overhead key: this is a machine under a work light, so the falloff is
    // steep and the shadows on the chassis are allowed to go properly dark.
    const key = new THREE.SpotLight(0xffffff, 90, 12, 0.72, 0.45, 1.6);
    key.position.set(0.6, 5.2, 2.2);
    key.target.position.set(0, 1, 0);
    this.scene.add(key, key.target);

    // One rim per corner, in that corner's colour, raking from behind. This is what
    // separates the two silhouettes from the dark and from each other.
    for (const corner of CORNERS) {
      const side = corner === 'agent_a' ? -1 : 1;
      const rim = new THREE.DirectionalLight(CORNER_COLOR[corner], 1.7);
      rim.position.set(side * 4, 2.2, -3.2);
      this.scene.add(rim);
    }

    // Deck: dark plate, machined grid, hazard chevrons on the two corner ends.
    const deck = new THREE.Mesh(
      new THREE.BoxGeometry(4.4, 0.14, 4.4),
      new THREE.MeshStandardMaterial({ color: 0x11161d, roughness: 0.55, metalness: 0.75 }),
    );
    deck.position.y = -0.07;
    this.scene.add(deck);

    const markings = new THREE.Mesh(
      new THREE.PlaneGeometry(4.4, 4.4),
      new THREE.MeshBasicMaterial({
        map: deckTexture(),
        transparent: true,
        depthWrite: false,
      }),
    );
    markings.rotation.x = -Math.PI / 2;
    markings.position.y = 0.002;
    this.scene.add(markings);

    const apron = new THREE.Mesh(
      new THREE.BoxGeometry(5.2, 0.1, 5.2),
      new THREE.MeshStandardMaterial({ color: 0x070a0e, roughness: 0.9, metalness: 0.4 }),
    );
    apron.position.y = -0.16;
    this.scene.add(apron);

    // Cage frame on three sides — the camera side is left open so the fight is never
    // watched through a cable. Steel uprights capped with the corner's own colour.
    const frameMat = new THREE.MeshStandardMaterial({
      color: STEEL,
      roughness: 0.42,
      metalness: 0.95,
    });
    const cableMat = new THREE.MeshStandardMaterial({
      color: 0x39434f,
      roughness: 0.35,
      metalness: 1,
    });
    const half = 2.1;
    for (const sx of [-1, 1]) {
      for (const sz of [-1, 1]) {
        const post = new THREE.Mesh(new THREE.BoxGeometry(0.11, 1.6, 0.11), frameMat);
        post.position.set(sx * half, 0.8, sz * half);
        this.scene.add(post);

        const cap = new THREE.Mesh(
          new THREE.BoxGeometry(0.15, 0.06, 0.15),
          new THREE.MeshStandardMaterial({
            color: 0x000000,
            emissive: CORNER_COLOR[sx < 0 ? 'agent_a' : 'agent_b'],
            emissiveIntensity: 2.4,
            roughness: 0.4,
          }),
        );
        cap.position.set(sx * half, 1.63, sz * half);
        this.scene.add(cap);
      }
    }
    for (const y of [0.45, 0.9, 1.35]) {
      const back = new THREE.Mesh(new THREE.BoxGeometry(half * 2, 0.03, 0.03), cableMat);
      back.position.set(0, y, -half);
      this.scene.add(back);
      for (const sx of [-1, 1]) {
        const side = new THREE.Mesh(new THREE.BoxGeometry(0.03, 0.03, half * 2), cableMat);
        side.position.set(sx * half, y, 0);
        this.scene.add(side);
      }
    }

    // Backdrop: a dim service grid that the fog eats before it competes with the
    // fighters, so the stage has depth without anything to read in it.
    const backdrop = new THREE.Mesh(
      new THREE.PlaneGeometry(16, 7),
      new THREE.MeshBasicMaterial({
        map: gridTexture(),
        transparent: true,
        opacity: 0.5,
        depthWrite: false,
        fog: true,
      }),
    );
    backdrop.position.set(0, 2.6, -5.4);
    this.scene.add(backdrop);

    const shadowTexture = contactShadowTexture();
    for (const corner of CORNERS) {
      const blob = new THREE.Mesh(
        new THREE.PlaneGeometry(1.1, 1.1),
        new THREE.MeshBasicMaterial({
          map: shadowTexture,
          transparent: true,
          opacity: 0.7,
          depthWrite: false,
        }),
      );
      blob.rotation.x = -Math.PI / 2;
      blob.position.set(corner === 'agent_a' ? -HALF_GAP : HALF_GAP, 0.005, 0);
      this.scene.add(blob);
    }
  }

  private resize(): void {
    const parent = this.canvas.parentElement;
    const width = parent?.clientWidth || this.canvas.clientWidth || 640;
    const height = parent?.clientHeight || this.canvas.clientHeight || 360;
    this.renderer.setSize(width, height, false);

    const aspect = width / Math.max(height, 1);
    this.camera.aspect = aspect;

    // Frame a fixed box around the two fighters, whatever the viewport shape —
    // a watch link opened on a phone in portrait must still show both corners.
    const vFov = (this.camera.fov * Math.PI) / 180;
    const needHeight = 2.2 / 2 / Math.tan(vFov / 2);
    const needWidth = 2.5 / 2 / Math.tan(vFov / 2) / Math.max(aspect, 0.35);
    const distance = Math.max(needHeight, needWidth) * 1.04;

    // The framed position is the home the shake displaces from; `step` writes the
    // camera every frame, so nothing here may set position directly.
    this.cameraHome.set(0, 1.35, distance);
    this.camera.position.copy(this.cameraHome).add(this.shake.offset);
    this.camera.lookAt(this.lookAt);
    this.camera.updateProjectionMatrix();
  }
}

/** Deck markings: machined grid, centre ring, and a hazard band under each corner. */
function deckTexture(): THREE.Texture {
  const size = 512;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d')!;

  ctx.clearRect(0, 0, size, size);
  ctx.strokeStyle = 'rgba(120, 155, 185, 0.14)';
  ctx.lineWidth = 1;
  for (let i = 1; i < 16; i++) {
    const p = (i / 16) * size;
    ctx.beginPath();
    ctx.moveTo(p, 0);
    ctx.lineTo(p, size);
    ctx.moveTo(0, p);
    ctx.lineTo(size, p);
    ctx.stroke();
  }

  ctx.strokeStyle = 'rgba(160, 195, 225, 0.3)';
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size * 0.2, 0, Math.PI * 2);
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size * 0.055, 0, Math.PI * 2);
  ctx.stroke();

  // The two corner bands. The texture's X axis runs along the fighters' axis, so
  // the left band sits under agent_a and the right one under agent_b.
  const bands: [number, string][] = [
    [0, '#29e0ff'],
    [size - size * 0.11, '#ff5a3c'],
  ];
  for (const [x, color] of bands) {
    ctx.fillStyle = color;
    ctx.globalAlpha = 0.5;
    ctx.fillRect(x, size * 0.18, size * 0.11, size * 0.64);
    ctx.globalAlpha = 1;
    ctx.fillStyle = 'rgba(5, 7, 10, 0.75)';
    for (let y = size * 0.18; y < size * 0.82; y += 26) {
      ctx.fillRect(x, y, size * 0.11, 13);
    }
  }

  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  return texture;
}

/** A dim service grid for the backdrop wall. */
function gridTexture(): THREE.Texture {
  const w = 256;
  const h = 128;
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d')!;
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = 'rgba(110, 145, 180, 0.34)';
  ctx.lineWidth = 1;
  for (let x = 0; x <= w; x += 16) {
    ctx.beginPath();
    ctx.moveTo(x + 0.5, 0);
    ctx.lineTo(x + 0.5, h);
    ctx.stroke();
  }
  for (let y = 0; y <= h; y += 16) {
    ctx.beginPath();
    ctx.moveTo(0, y + 0.5);
    ctx.lineTo(w, y + 0.5);
    ctx.stroke();
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function contactShadowTexture(): THREE.Texture {
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d')!;
  const gradient = ctx.createRadialGradient(
    size / 2,
    size / 2,
    0,
    size / 2,
    size / 2,
    size / 2,
  );
  gradient.addColorStop(0, 'rgba(0,0,0,0.85)');
  gradient.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, size, size);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}
