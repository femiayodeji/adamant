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
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';

import { Fighter } from './fighter';
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

const CORNER_COLOR: Record<Corner, number> = {
  agent_a: 0x4da3ff,
  agent_b: 0xff6161,
};

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

  private queue: TickResult[] = [];
  private currentTick: TickResult | null = null;
  private tickElapsed = 0;
  private frame = 0;
  private lastFrameMs = 0;
  private paused = false;
  private disposed = false;

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

    this.scene.background = new THREE.Color(0x0a0d12);
    this.scene.fog = new THREE.Fog(0x0a0d12, 6, 16);

    this.camera = new THREE.PerspectiveCamera(38, 1, 0.1, 60);

    // agent_a on the left facing +X, agent_b on the right facing -X: a literal
    // mirror across the X axis, which is what the mirrored clip set assumes.
    fighterA.group.position.set(-HALF_GAP, 0, 0);
    fighterA.group.rotation.y = Math.PI / 2;
    fighterB.group.position.set(HALF_GAP, 0, 0);
    fighterB.group.rotation.y = -Math.PI / 2;

    tint(fighterA.group, CORNER_COLOR.agent_a);
    tint(fighterB.group, CORNER_COLOR.agent_b);

    this.scene.add(fighterA.group, fighterB.group);
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

  /** Back to two fighters standing in their corners — used before a replay. */
  reset(): void {
    this.queue = [];
    this.currentTick = null;
    this.tickElapsed = 0;
    for (const corner of CORNERS) this.fighters[corner].reset();
  }

  dispose(): void {
    this.disposed = true;
    cancelAnimationFrame(this.frame);
    this.observer?.disconnect();
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
  };

  private step(dt: number): void {
    // Catch-up: ticks can resolve much faster than a 0.5 s clip plays
    // (arena_spec.md 13). Speed the queue up rather than dropping frames.
    const scale = Math.min(1 + 0.3 * this.queue.length, MAX_TIME_SCALE);
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
  }

  private startTick(tick: TickResult): void {
    this.currentTick = tick;
    this.tickElapsed = 0;
    const rushing = this.queue.length > RUSH_BACKLOG;

    for (const corner of CORNERS) {
      const fighter = this.fighters[corner];
      const events = tick.events ?? [];

      // Clip name IS the action name (skeleton.md) — no mapping table. The two
      // exceptions are both event-driven, not action-driven:
      //   exhausted: the action was unaffordable and became idle (spec 3), so the
      //              tell belongs to the event, not to whatever `moves` reports.
      //   no_move  : nothing was submitted (spec 4.4) — the fighter just stands.
      const exhausted = events.some((e) => e.type === 'exhausted' && e.who === corner);
      const noMove = events.some((e) => e.type === 'no_move' && e.who === corner);
      const move = tick.moves?.[corner];

      if (exhausted) fighter.enqueue('exhausted');
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

    this.onHud?.({
      tick: tick.tick,
      state: tick.state,
      moves: tick.moves ?? {},
      events: tick.events ?? [],
      telemetry: tick.telemetry,
      backlog: this.queue.length,
    });
  }

  /* ------------------------------------------------------------- stage */

  private buildStage(): void {
    const hemi = new THREE.HemisphereLight(0x9dc4ff, 0x14181f, 1.1);
    this.scene.add(hemi);

    const key = new THREE.DirectionalLight(0xffffff, 2.2);
    key.position.set(2.5, 5, 3.5);
    this.scene.add(key);

    const rim = new THREE.DirectionalLight(0x7fb0ff, 1.1);
    rim.position.set(-3, 2.5, -3);
    this.scene.add(rim);

    const canvasMat = new THREE.MeshStandardMaterial({
      color: 0x1b2431,
      roughness: 0.95,
      metalness: 0,
    });
    const floor = new THREE.Mesh(new THREE.BoxGeometry(4.4, 0.14, 4.4), canvasMat);
    floor.position.y = -0.07;
    this.scene.add(floor);

    const apron = new THREE.Mesh(
      new THREE.BoxGeometry(4.8, 0.1, 4.8),
      new THREE.MeshStandardMaterial({ color: 0x0f151d, roughness: 1 }),
    );
    apron.position.y = -0.16;
    this.scene.add(apron);

    // Ring posts and ropes on three sides — the camera side is left open so the
    // fight is never watched through a rope.
    const postMat = new THREE.MeshStandardMaterial({ color: 0x2a3646, roughness: 0.6 });
    const ropeMat = new THREE.MeshStandardMaterial({ color: 0xd8dee9, roughness: 0.5 });
    const half = 2.1;
    for (const sx of [-1, 1]) {
      for (const sz of [-1, 1]) {
        const post = new THREE.Mesh(
          new THREE.CylinderGeometry(0.05, 0.05, 1.5, 12),
          postMat,
        );
        post.position.set(sx * half, 0.75, sz * half);
        this.scene.add(post);
      }
    }
    for (const y of [0.45, 0.85, 1.25]) {
      const back = new THREE.Mesh(
        new THREE.BoxGeometry(half * 2, 0.035, 0.035),
        ropeMat,
      );
      back.position.set(0, y, -half);
      this.scene.add(back);
      for (const sx of [-1, 1]) {
        const side = new THREE.Mesh(
          new THREE.BoxGeometry(0.035, 0.035, half * 2),
          ropeMat,
        );
        side.position.set(sx * half, y, 0);
        this.scene.add(side);
      }
    }

    const shadowTexture = contactShadowTexture();
    for (const corner of CORNERS) {
      const blob = new THREE.Mesh(
        new THREE.PlaneGeometry(1.1, 1.1),
        new THREE.MeshBasicMaterial({
          map: shadowTexture,
          transparent: true,
          opacity: 0.55,
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

    this.camera.position.set(0, 1.35, distance);
    this.camera.lookAt(0, 1.02, 0);
    this.camera.updateProjectionMatrix();
  }
}

function tint(root: THREE.Object3D, color: number): void {
  root.traverse((obj) => {
    const mesh = obj as THREE.Mesh;
    if (!mesh.isMesh) return;
    const source = Array.isArray(mesh.material) ? mesh.material[0] : mesh.material;
    const material = (source as THREE.MeshStandardMaterial).clone();
    material.color = new THREE.Color(color);
    material.roughness = 0.62;
    material.metalness = 0.05;
    mesh.material = material;
  });
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
