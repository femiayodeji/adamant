/** Shapes of clips.json / skeleton-map.json. See the `_comment` blocks in those files. */

export interface AxisRot {
  x?: number;
  y?: number;
  z?: number;
}

/** Joint name -> rotation offsets from T-pose in degrees, plus root motion keys. */
export interface Pose {
  [joint: string]: AxisRot | undefined;
  /** Hips translation offset in rig units (cm), fighter-local. */
  _root?: AxisRot;
  /** Whole-body rotation in degrees, pre-multiplied onto the Hips rotation. */
  _rootRot?: AxisRot;
}

/** How closed a hand is: 0 = the rig's open rest hand, 1 = the authored fist. */
export type Curl = number | { left?: number; right?: number };

/**
 * A pose as it is written in clips.json, before `_fist` becomes finger rotations.
 *
 * The distinction is only about typing: `_fist` is the one key whose value is not
 * an AxisRot, so it is expanded away by `expandFist` at load time and every other
 * consumer sees a plain `Pose`.
 */
export interface AuthoredPose {
  [joint: string]: AxisRot | Curl | undefined;
  _root?: AxisRot;
  _rootRot?: AxisRot;
  _fist?: Curl;
}

export interface ClipKey {
  f: number;
  pose: AuthoredPose;
}

export interface ClipDef {
  _note?: string;
  frames: number;
  keys: ClipKey[];
  loop?: boolean;
  terminal?: boolean;
  /** false = keep the authored root motion (the KO fall). Default true. */
  ground?: boolean;
}

export interface ClipLibrary {
  fps: number;
  stance: AuthoredPose;
  clips: Record<string, ClipDef>;
}

export interface JointSpec {
  axes: string[];
  range: Record<string, [number, number]>;
  note?: string;
}

export interface SkeletonMap {
  joints: Record<string, JointSpec>;
  mirror: { negate: string[]; swapPrefixes: [string, string][] };
  /** The left hand's fully closed fist; the right is derived by `mirror`. */
  hand: { curl: Record<string, AxisRot> };
}

/** The name of every clip the renderer can play. */
export const STANCE_CLIP = 'stance';
