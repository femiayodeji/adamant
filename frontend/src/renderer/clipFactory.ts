/**
 * Turns the pose data in clips.json into THREE.AnimationClips at load time.
 *
 * There is no baked binary animation anywhere in this project: the eleven combat
 * clips (plus the stance loop and the exhausted variant) are 3-5 keyframe pose
 * dicts in JSON, interpolated at 30 fps by three.js. That keeps them reviewable
 * and editable as text, per docs/skeleton.md.
 *
 * Three things this file owns beyond track building:
 *   - MIRRORING. agent_b is agent_a mirrored across X, so one authored set of
 *     poses yields both corners: swap the Left and Right joints, negate y and z.
 *   - GROUNDING. Bending the knees in pure FK lifts the feet instead of lowering
 *     the hips, so every keyframe's hips height is solved so the lowest toe stays
 *     on the canvas floor. Exact in one pass, because the Hips node is the root
 *     of the bone chain and translating it moves every descendant rigidly.
 *   - HANDS. A pose asks for a hand shape (`_fist`) rather than naming fifteen
 *     finger bones, and that is expanded here against the template in
 *     skeleton-map.json. The rig's rest hand is flat and open; without this every
 *     punch in the library lands as an open-palm slap.
 */

import * as THREE from 'three';

import rawClips from './clips.json';
import rawMap from './skeleton-map.json';
import type {
  AuthoredPose,
  AxisRot,
  ClipDef,
  ClipLibrary,
  Pose,
  SkeletonMap,
} from './clipTypes';

export const LIBRARY = rawClips as unknown as ClipLibrary;
export const SKELETON: SkeletonMap = rawMap as unknown as SkeletonMap;

const DEG = Math.PI / 180;
const FLOOR_PROBES = ['LeftToeBase', 'RightToeBase', 'LeftFoot', 'RightFoot'];

export type ClipSet = Map<string, THREE.AnimationClip>;

/** Bone lookup for one loaded rig instance, keyed by logical joint name. */
export class Rig {
  readonly bones = new Map<string, THREE.Bone | THREE.Object3D>();
  /** Rest (T-pose) rotation per joint. Offsets in clips.json are relative to these. */
  readonly restQuats = new Map<string, THREE.Quaternion>();
  readonly hips: THREE.Object3D;
  readonly hipsRest: THREE.Vector3;
  /** Rig units (cm) per world metre for the hips translation track. */
  readonly unitScale: number;
  /** World Y of the rest-pose feet — the canvas floor. */
  readonly groundY: number;

  constructor(readonly root: THREE.Object3D) {
    root.updateMatrixWorld(true);
    root.traverse((obj) => {
      const logical = obj.name.replace(/^mixamorig:?/, '');
      if (logical && logical !== obj.name) {
        this.bones.set(logical, obj);
        this.restQuats.set(logical, obj.quaternion.clone());
      }
    });
    const hips = this.bones.get('Hips');
    if (!hips) throw new Error('xbot.glb: no Hips bone — wrong rig?');
    this.hips = hips;
    this.hipsRest = hips.position.clone();

    const parentScale = new THREE.Vector3();
    (hips.parent ?? root).getWorldScale(parentScale);
    this.unitScale = parentScale.y || 1;

    this.groundY = this.lowestProbe();
  }

  bone(joint: string): THREE.Object3D {
    const b = this.bones.get(joint);
    if (!b) throw new Error(`xbot.glb: no bone for joint '${joint}'`);
    return b;
  }

  private lowestProbe(): number {
    this.root.updateMatrixWorld(true);
    const v = new THREE.Vector3();
    let low = Infinity;
    for (const name of FLOOR_PROBES) {
      const bone = this.bones.get(name);
      if (!bone) continue;
      bone.getWorldPosition(v);
      low = Math.min(low, v.y);
    }
    return low === Infinity ? 0 : low;
  }

  /** Apply a fully merged pose to the live skeleton (used for the grounding solve). */
  applyPose(pose: Pose, rootOffset: THREE.Vector3, rootRot: AxisRot | undefined): void {
    for (const joint of POSED_JOINTS) {
      const bone = this.bones.get(joint);
      if (!bone) continue;
      bone.quaternion.copy(this.restQuats.get(joint) ?? IDENTITY);
      const rot = pose[joint];
      if (rot) bone.quaternion.multiply(eulerQuat(rot));
    }
    if (rootRot) {
      this.hips.quaternion.premultiply(eulerQuat(rootRot));
    }
    this.hips.position.copy(this.hipsRest).add(rootOffset);
    this.root.updateMatrixWorld(true);
  }

  /** Hips Y offset (rig units) that puts the lowest toe back on the floor. */
  solveGrounding(pose: Pose, rootOffset: THREE.Vector3, rootRot?: AxisRot): number {
    this.applyPose(pose, rootOffset, rootRot);
    return (this.groundY - this.lowestProbe()) / this.unitScale;
  }

  restPose(): void {
    for (const joint of POSED_JOINTS) {
      this.bones.get(joint)?.quaternion.copy(this.restQuats.get(joint) ?? IDENTITY);
    }
    this.hips.position.copy(this.hipsRest);
    this.root.updateMatrixWorld(true);
  }
}

const IDENTITY = new THREE.Quaternion();

function eulerQuat(rot: AxisRot): THREE.Quaternion {
  return new THREE.Quaternion().setFromEuler(
    new THREE.Euler((rot.x ?? 0) * DEG, (rot.y ?? 0) * DEG, (rot.z ?? 0) * DEG, 'XYZ'),
  );
}

/** stance + partial override, with joint values clamped to the documented ranges. */
function mergePose(stance: Pose, override: Pose): Pose {
  const out: Pose = {};
  for (const joint of POSED_JOINTS) {
    const rot = override[joint] ?? stance[joint];
    if (rot) out[joint] = clampJoint(joint, rot);
  }
  return out;
}

function clampJoint(joint: string, rot: AxisRot): AxisRot {
  const spec = SKELETON.joints[joint];
  if (!spec) return rot;
  const out: AxisRot = {};
  for (const axis of ['x', 'y', 'z'] as const) {
    const v = rot[axis];
    if (v === undefined) continue;
    const range = spec.range[axis];
    out[axis] = range ? Math.min(Math.max(v, range[0]), range[1]) : v;
  }
  return out;
}

/* -------------------------------------------------------------- mirroring */

const SWAPS = SKELETON.mirror.swapPrefixes;

function mirrorJointName(joint: string): string {
  for (const [a, b] of SWAPS) {
    if (joint.startsWith(a)) return b + joint.slice(a.length);
    if (joint.startsWith(b)) return a + joint.slice(b.length);
  }
  return joint;
}

function mirrorRot(rot: AxisRot): AxisRot {
  const out: AxisRot = {};
  for (const axis of ['x', 'y', 'z'] as const) {
    if (rot[axis] === undefined) continue;
    out[axis] = SKELETON.mirror.negate.includes(axis) ? -rot[axis]! : rot[axis]!;
  }
  return out;
}

/** Mirror a pose across the fighter's own X axis (skeleton.md, "Directional clips"). */
export function mirrorPose(pose: Pose): Pose {
  const out: Pose = {};
  for (const [key, val] of Object.entries(pose)) {
    if (!val) continue;
    if (key === '_root') {
      out._root = { ...val, x: val.x === undefined ? undefined : -val.x };
    } else if (key === '_rootRot') {
      out._rootRot = mirrorRot(val);
    } else {
      out[mirrorJointName(key)] = mirrorRot(val);
    }
  }
  return out;
}

/* -------------------------------------------------------------------- hands */

/**
 * The full-fist rotation of every finger bone, both hands.
 *
 * skeleton-map.json authors the LEFT hand only; the right is derived by the same
 * mirror rule agent_b's whole clip set is, so there is one hand to tune and no
 * way for the two to drift apart.
 */
const HAND_CURL: Pose = (() => {
  const out: Pose = {};
  for (const [joint, rot] of Object.entries(SKELETON.hand.curl)) {
    out[joint] = rot;
    out[mirrorJointName(joint)] = mirrorRot(rot);
  }
  return out;
})();

/** Joints a clip can drive: the authored map, plus the finger chains. */
const POSED_JOINTS = [...Object.keys(SKELETON.joints), ...Object.keys(HAND_CURL)];
const FINGER_JOINTS = new Set(Object.keys(HAND_CURL));

/**
 * `_fist` -> actual finger rotations.
 *
 * A pose says how closed each hand is and this scales the template: 1 is the
 * authored fist, 0 the rig's own open rest hand, and anything between is a hand
 * on its way to one or the other. A hand nobody mentions keeps whatever the
 * stance gave it, exactly like any other joint.
 */
export function expandFist(pose: AuthoredPose): Pose {
  const curl = pose._fist;
  const out: Pose = {};
  for (const [key, value] of Object.entries(pose)) {
    if (key !== '_fist') out[key] = value as AxisRot;
  }
  if (curl === undefined) return out;

  const amounts: { left?: number; right?: number } =
    typeof curl === 'number' ? { left: curl, right: curl } : curl;
  for (const [joint, rot] of Object.entries(HAND_CURL)) {
    const amount = joint.startsWith('Left') ? amounts.left : amounts.right;
    if (amount === undefined) continue;
    const scaled: AxisRot = {};
    for (const axis of ['x', 'y', 'z'] as const) {
      if (rot![axis] !== undefined) scaled[axis] = rot![axis]! * amount;
    }
    out[joint] = scaled;
  }
  return out;
}

/* ---------------------------------------------------------- clip building */

function vec(rot: AxisRot | undefined): THREE.Vector3 {
  return new THREE.Vector3(rot?.x ?? 0, rot?.y ?? 0, rot?.z ?? 0);
}

/** A clip whose poses have been through `expandFist` and the mirror. */
interface ShapedClip extends Omit<ClipDef, 'keys'> {
  keys: { f: number; pose: Pose }[];
}

function buildClip(name: string, def: ShapedClip, stance: Pose, rig: Rig, fps: number) {
  const times = def.keys.map((k) => k.f / fps);
  const duration = def.frames / fps;

  // Which joints this clip touches at all: the union over keyframes. A joint
  // nobody mentions simply holds its stance value and needs no track.
  const touched = new Set<string>();
  for (const key of def.keys) {
    for (const joint of Object.keys(key.pose)) {
      if (!joint.startsWith('_')) touched.add(joint);
    }
  }
  for (const joint of Object.keys(stance)) {
    if (joint.startsWith('_')) continue;
    if (SKELETON.joints[joint] || FINGER_JOINTS.has(joint)) touched.add(joint);
  }

  const merged = def.keys.map((k) => mergePose(stance, k.pose));
  const rootOffsets = def.keys.map((k) => vec(k.pose._root));
  const rootRots = def.keys.map((k) => k.pose._rootRot);

  // Grounding. Clips opt out with "ground": false (the KO fall keeps its authored
  // arc) but are still offset by the solve at frame 0, so they leave the stance
  // at exactly the height every other clip ends at — no pop out of the queue.
  const grounded = def.ground !== false;
  const baseDy = rig.solveGrounding(merged[0], rootOffsets[0], rootRots[0]);
  for (let i = 0; i < merged.length; i++) {
    rootOffsets[i].y += baseDy;
    if (grounded && i > 0) {
      rootOffsets[i].y += rig.solveGrounding(merged[i], rootOffsets[i], rootRots[i]);
    }
  }
  rig.restPose();

  const tracks: THREE.KeyframeTrack[] = [];
  const q = new THREE.Quaternion();

  for (const joint of touched) {
    const bone = rig.bones.get(joint);
    if (!bone) continue;
    const values = new Float32Array(times.length * 4);
    for (let i = 0; i < merged.length; i++) {
      q.copy(rig.restQuats.get(joint) ?? new THREE.Quaternion());
      const rot = merged[i][joint];
      if (rot) q.multiply(eulerQuat(rot));
      if (joint === 'Hips' && rootRots[i]) q.premultiply(eulerQuat(rootRots[i]!));
      values.set([q.x, q.y, q.z, q.w], i * 4);
    }
    tracks.push(new THREE.QuaternionKeyframeTrack(`${bone.name}.quaternion`, times, values));
  }

  const positions = new Float32Array(times.length * 3);
  for (let i = 0; i < rootOffsets.length; i++) {
    positions.set(
      [
        rig.hipsRest.x + rootOffsets[i].x,
        rig.hipsRest.y + rootOffsets[i].y,
        rig.hipsRest.z + rootOffsets[i].z,
      ],
      i * 3,
    );
  }
  tracks.push(
    new THREE.VectorKeyframeTrack(`${rig.hips.name}.position`, times, positions),
  );

  return new THREE.AnimationClip(name, duration, tracks);
}

/**
 * Build every clip for one corner. `mirrored` produces agent_b's set from the
 * same authored data (skeleton.md: eleven clips total, not twenty-two).
 */
export function buildClipSet(rig: Rig, mirrored: boolean): ClipSet {
  const fps = LIBRARY.fps;
  // Hands first, mirror second: expanding `_fist` up front means the finger
  // rotations go through the same joint mirroring as every other joint, so the
  // two corners cannot end up with differently shaped fists.
  const shape = (pose: AuthoredPose): Pose => {
    const expanded = expandFist(pose);
    return mirrored ? mirrorPose(expanded) : expanded;
  };
  const stance = shape(LIBRARY.stance);
  const set: ClipSet = new Map();

  for (const [name, def] of Object.entries(LIBRARY.clips)) {
    const keys = def.keys.map((k) => ({ f: k.f, pose: shape(k.pose) }));
    set.set(name, buildClip(name, { ...def, keys }, stance, rig, fps));
  }
  return set;
}
