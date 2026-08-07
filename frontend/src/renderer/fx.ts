/**
 * Cheap impact feedback: a spark burst and a camera shake.
 *
 * Deliberately not a particle library and not a post-processing chain. One pooled
 * Points cloud of 64 additive quads covers every hit, block and jam in a match, and
 * the shake is a decaying offset applied to the camera position each frame. Both are
 * pure functions of dt with no allocation after construction, so a phone rendering a
 * shared watch link pays almost nothing for them.
 */

import * as THREE from 'three';

const SPARK_COUNT = 64;
const SPARK_LIFE = 0.42;
const GRAVITY = -5.2;

/** One pooled burst emitter. Bursts overwrite the oldest particles, so a flurry of
 *  ticks can never grow the buffer. */
export class Sparks {
  readonly points: THREE.Points;
  private readonly positions: Float32Array;
  private readonly velocities = new Float32Array(SPARK_COUNT * 3);
  private readonly life = new Float32Array(SPARK_COUNT);
  private readonly colors: Float32Array;
  private cursor = 0;

  constructor() {
    const geometry = new THREE.BufferGeometry();
    this.positions = new Float32Array(SPARK_COUNT * 3);
    this.colors = new Float32Array(SPARK_COUNT * 3);
    geometry.setAttribute('position', new THREE.BufferAttribute(this.positions, 3));
    geometry.setAttribute('color', new THREE.BufferAttribute(this.colors, 3));
    this.points = new THREE.Points(
      geometry,
      new THREE.PointsMaterial({
        size: 0.035,
        vertexColors: true,
        transparent: true,
        opacity: 0.95,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        sizeAttenuation: true,
      }),
    );
    this.points.frustumCulled = false;
    // Park every particle dead and far away rather than at the origin, where a
    // stray zero-life point would sit visibly on the canvas floor.
    for (let i = 0; i < SPARK_COUNT; i++) this.positions[i * 3 + 1] = -50;
  }

  burst(origin: THREE.Vector3, color: THREE.ColorRepresentation, count: number, speed = 2.1): void {
    const tint = new THREE.Color(color);
    for (let n = 0; n < count; n++) {
      const i = this.cursor;
      this.cursor = (this.cursor + 1) % SPARK_COUNT;
      this.positions.set([origin.x, origin.y, origin.z], i * 3);
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      const v = speed * (0.35 + Math.random() * 0.65);
      this.velocities.set(
        [
          Math.sin(phi) * Math.cos(theta) * v,
          Math.abs(Math.cos(phi)) * v * 0.9 + 0.4,
          Math.sin(phi) * Math.sin(theta) * v * 0.6,
        ],
        i * 3,
      );
      this.colors.set([tint.r, tint.g, tint.b], i * 3);
      this.life[i] = SPARK_LIFE;
    }
  }

  /** Kill every live particle — a replay jump must not carry the last hit's sparks
   *  into the tick it lands on. */
  clear(): void {
    for (let i = 0; i < SPARK_COUNT; i++) {
      this.life[i] = 0;
      this.positions[i * 3 + 1] = -50;
    }
    this.points.geometry.attributes.position.needsUpdate = true;
  }

  update(dt: number): void {
    let live = false;
    for (let i = 0; i < SPARK_COUNT; i++) {
      if (this.life[i] <= 0) continue;
      live = true;
      this.life[i] -= dt;
      const p = i * 3;
      if (this.life[i] <= 0) {
        this.positions[p + 1] = -50;
        continue;
      }
      this.velocities[p + 1] += GRAVITY * dt;
      this.positions[p] += this.velocities[p] * dt;
      this.positions[p + 1] += this.velocities[p + 1] * dt;
      this.positions[p + 2] += this.velocities[p + 2] * dt;
      // Fade by dimming the colour: one shared material, no per-particle alpha.
      const fade = 1 - dt / SPARK_LIFE;
      this.colors[p] *= fade;
      this.colors[p + 1] *= fade;
      this.colors[p + 2] *= fade;
    }
    if (live) {
      this.points.geometry.attributes.position.needsUpdate = true;
      this.points.geometry.attributes.color.needsUpdate = true;
    }
  }

  dispose(): void {
    this.points.geometry.dispose();
    (this.points.material as THREE.Material).dispose();
  }
}

/** A decaying positional jolt. `offset` is added to the camera's framed position. */
export class Shake {
  readonly offset = new THREE.Vector3();
  private amount = 0;
  private phase = Math.random() * 100;

  hit(amount: number): void {
    this.amount = Math.min(0.09, this.amount + amount);
  }

  update(dt: number): void {
    if (this.amount <= 0) {
      this.offset.set(0, 0, 0);
      return;
    }
    this.amount = Math.max(0, this.amount - dt * 0.42);
    this.phase += dt * 46;
    this.offset.set(
      Math.sin(this.phase) * this.amount,
      Math.sin(this.phase * 1.7 + 1.3) * this.amount * 0.8,
      0,
    );
  }
}
