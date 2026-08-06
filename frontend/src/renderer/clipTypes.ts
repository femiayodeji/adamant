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

export interface ClipKey {
  f: number;
  pose: Pose;
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
  stance: Pose;
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
}

/** The name of every clip the renderer can play. */
export const STANCE_CLIP = 'stance';
