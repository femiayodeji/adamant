#!/usr/bin/env python3
"""Headless validator for the ADAM Arena clip library.

Runs the same maths the renderer runs (Euler XYZ -> quaternion, forward kinematics
down the Mixamo Xbot hierarchy), without a browser or a GPU, so the fourteen clips
in src/renderer/clips.json can be checked as *data* before anyone looks at a
canvas. The maths itself lives in `rig.py`, shared with `pose_render.py`.

What it checks
  1. Every joint named in a pose exists in the rig and in docs/skeleton.md's map.
  2. Every authored axis is inside that joint's documented range.
  3. Every non-terminal clip starts and ends at STANCE (skeleton.md, non-negotiable).
  4. Keyframe counts and clip lengths match the skeleton.md format table.
  5. Loop clips are seamless (first key == last key).
  6. FK sanity per keyframe: feet on the floor after grounding, hips at a plausible
     height, no fist below the hips in a guard pose, punches actually reach forward.
  7. THE FLOOR, on the skinned mesh, on every interpolated frame — see below —
     and, for the KO, that the body comes to rest ON the deck rather than above it.

Why check 7 is not check 6 again
  Checks 1-6 look at JOINT positions at KEYFRAMES, and that misses the floor twice
  over. Joints sit inside the body, so an ankle can be comfortably above the deck
  while the foot around it is buried; and three.js plays the frames *between* the
  keyframes, where a slerped rotation paired with a linear hips path can dive far
  below anything the keyframes reveal. The `ko` fall did both at once: every
  keyframe passed, and the right foot still ploughed 14 cm under the deck for most
  of a second.

  So check 7 skins the actual mesh — glTF vertex weights, joint matrices, inverse
  bind matrices — and evaluates the real lowest vertex at every other frame of
  every clip, after applying the same grounding solve clipFactory.ts applies. To
  keep that affordable in pure Python it runs against a precomputed probe set
  (per bone, the extreme vertices along 14 directions) rather than all 28k
  vertices; the probe tracks the true minimum to within ~4 mm, well inside the
  tolerances below.

  It also watches the other side of the same coin. Penetrating the deck and
  hovering above it are both "not lying on the floor", and only the first one has
  ever had a check: a KO that settles 20 cm up is a body sinking to a stop in mid
  air, which is what the fall used to look like.

Usage:  python3 tools/pose_check.py          (from frontend/)
        python3 tools/pose_check.py --no-mesh    (skip check 7; ~5s faster)
Exit code 1 on any error.
"""

from __future__ import annotations

import json
import os
import sys
import time

from rig import BONE_PREFIX, FloorProbe, Rig, dist

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.dirname(HERE)
GLB = os.path.join(FRONTEND, "public", "models", "xbot.glb")
CLIPS = os.path.join(FRONTEND, "src", "renderer", "clips.json")


def main() -> int:
    rig = Rig(GLB)
    check_mesh = "--no-mesh" not in sys.argv and bool(rig.binary)

    with open(CLIPS) as fh:
        lib = json.load(fh)
    smap = rig.joints

    errors: list[str] = []
    warnings: list[str] = []

    # --- rest pose assumption -------------------------------------------------
    for joint in smap:
        name = BONE_PREFIX + joint
        if name not in rig.by_name:
            errors.append(f"rig is missing joint {name}")
            continue
        q = rig.rest_rot[rig.by_name[name]]
        if abs(q[3] - 1.0) > 1e-4:
            warnings.append(f"{joint} rest rotation is not identity: {q}")

    if errors:
        for e in errors:
            print("ERROR:", e)
        return 1

    stance = lib["stance"]
    fps = lib["fps"]

    # --- range + membership ---------------------------------------------------
    def check_pose(where, pose):
        for joint, rot in pose.items():
            if joint.startswith("_"):
                continue
            if joint not in smap:
                errors.append(f"{where}: joint '{joint}' is not in the skeleton map")
                continue
            spec = smap[joint]
            for axis, val in rot.items():
                if axis not in spec["axes"]:
                    errors.append(
                        f"{where}: {joint}.{axis} is not an authored axis for this joint"
                    )
                    continue
                lo, hi = spec["range"][axis]
                if val < lo or val > hi:
                    errors.append(
                        f"{where}: {joint}.{axis}={val} outside documented range [{lo}, {hi}]"
                    )

    check_pose("stance", stance)

    # --- per clip -------------------------------------------------------------
    print(f"rig ground plane (rest toe height): {rig.ground:+.4f} m")
    print(f"{'clip':<12} {'f':>4} {'hips.y':>7} {'rootY*':>7} {'lowest':>7}  "
          f"{'L fist x/y/z':>22}  {'R fist x/y/z':>22}")
    print("-" * 96)
    probe_joints = (
        "LeftToeBase", "RightToeBase", "LeftFoot", "RightFoot",
        "LeftHand", "RightHand", "Head", "Hips",
    )

    for name, clip in lib["clips"].items():
        keys = clip["keys"]
        frames = clip["frames"]
        terminal = clip.get("terminal", False)
        loop = clip.get("loop", False)
        do_ground = clip.get("ground", True)

        if not (3 <= len(keys) <= 6):
            errors.append(f"{name}: {len(keys)} keyframes, skeleton.md says 3-6")
        fs = [k["f"] for k in keys]
        if fs != sorted(fs) or len(set(fs)) != len(fs):
            errors.append(f"{name}: keyframe indices are not strictly increasing: {fs}")
        if fs[0] != 0:
            errors.append(f"{name}: first keyframe is at f={fs[0]}, must be 0")
        if fs[-1] != frames:
            errors.append(f"{name}: last keyframe f={fs[-1]} != frames={frames}")
        if not terminal:
            if keys[0]["pose"] != {}:
                errors.append(f"{name}: does not START at STANCE")
            if keys[-1]["pose"] != {}:
                errors.append(f"{name}: does not END at STANCE")
        if loop and keys[0]["pose"] != keys[-1]["pose"]:
            errors.append(f"{name}: loop clip is not seamless")

        # "ground": false (the KO fall) keeps its authored root motion, but is
        # offset once so its first frame sits at exactly the stance height —
        # otherwise the fall starts with a visible pop out of the queue.
        clip_dy = 0.0 if do_ground else rig.solve(stance, keys[0]["pose"])[2]

        traces = {}
        for k in keys:
            where = f"{name}@f{k['f']}"
            check_pose(where, k["pose"])
            probe, root_y, dy = rig.solve(stance, k["pose"], clip_dy)
            if do_ground:
                probe, root_y, dy = rig.solve(stance, k["pose"], dy)
                if abs(dy) > 1e-6:
                    errors.append(f"{where}: grounding solve did not converge")

            hips, head = probe("Hips"), probe("Head")
            lf, rf = probe("LeftHand"), probe("RightHand")
            traces[k["f"]] = {"L": lf, "R": rf}
            lowest = min(probe(j)[1] for j in probe_joints)
            print(
                f"{name:<12} {k['f']:>4} {hips[1]:>7.3f} {root_y:>7.1f} {lowest:>7.3f}  "
                f"{lf[0]:>7.3f}/{lf[1]:>6.3f}/{lf[2]:>6.3f}  "
                f"{rf[0]:>7.3f}/{rf[1]:>6.3f}/{rf[2]:>6.3f}"
            )

            if lowest < rig.ground - 0.02:
                errors.append(
                    f"{where}: geometry {abs(lowest - rig.ground) * 100:.0f} cm through the floor"
                )
            if not terminal:
                if hips[1] < 0.60 or hips[1] > 1.15:
                    warnings.append(
                        f"{where}: hips at {hips[1]:.2f} m — outside a plausible standing range"
                    )
                if head[1] < 1.20:
                    warnings.append(
                        f"{where}: head at {head[1]:.2f} m — fighter is collapsing"
                    )
                if name != "exhausted":
                    for label, fist in (("left", lf), ("right", rf)):
                        if fist[1] < hips[1] - 0.02:
                            warnings.append(
                                f"{where}: {label} fist at {fist[1]:.2f} m is below the hips"
                            )

        # Punches have to actually go somewhere. Straights extend toward the
        # opponent (+z); a hook does not — it travels laterally across the guard.
        if name in ("jab", "cross", "riposte", "hook"):
            side = "L" if name in ("jab", "hook") else "R"
            rest = traces[0][side]
            far = max(traces.values(), key=lambda t: dist(t[side], rest))[side]
            d = dist(far, rest)
            print(
                f"    {name}: {side} fist travels {d:.3f} m "
                f"(dx {far[0] - rest[0]:+.3f}, dz {far[2] - rest[2]:+.3f})"
            )
            if name == "hook":
                if d < 0.25 or abs(far[0] - rest[0]) < 0.15:
                    errors.append(
                        f"{name}: fist travels {d:.3f} m with {far[0] - rest[0]:+.3f} m "
                        "of lateral sweep — that is not a hook"
                    )
            elif far[2] - rest[2] < 0.30:
                errors.append(
                    f"{name}: fist extends only {far[2] - rest[2]:.3f} m toward the "
                    "opponent — that is not a straight punch"
                )

    # --- 7. the floor, on the skinned mesh, between the keyframes -------------
    #
    # Tolerances are not zero because the grounding solve targets the TOE JOINT
    # while this measures the MESH: the rig's rest pose already has ~3 mm of sole
    # below the toe joint, and every crouch (block_low, the dodges, exhausted)
    # flexes the sole a little further in — 2.4 cm at the worst, on block_low.
    # That baseline is inherent to joint-based grounding, not a clip bug, so the
    # thresholds sit above it. The failure they exist to catch is the 14 cm the
    # `ko` fall used to plough, and the table below prints every clip's real
    # figure regardless of whether it trips one.
    FLOOR_WARN_CM = 3.0
    FLOOR_ERROR_CM = 4.0
    # And the opposite failure, for a terminal clip only: a body that stops short
    # of the deck is hanging in the air. A shoulder or a hip has to be touching.
    SETTLE_ERROR_CM = 6.0
    FRAME_STEP = 2

    if check_mesh:
        started = time.time()
        floor = FloorProbe(rig)
        print()
        print(
            f"skinned floor probe: {len(floor.verts)} of {floor.total} vertices, "
            f"deck at y={floor.ground:+.4f} m"
        )
        print(f"{'clip':<12} {'worst frame':>12} {'lowest':>9} {'vs deck':>9} {'settles':>9}")
        print("-" * 56)

        for name, clip in lib["clips"].items():
            roots = rig.clip_roots(stance, clip)
            tracks = rig.clip_tracks(stance, clip)
            times = [k["f"] for k in clip["keys"]]
            sampled = sorted(set(range(0, clip["frames"] + 1, FRAME_STEP)) | set(times))

            worst = (None, None)
            settle = None
            for f in sampled:
                low = floor.lowest(rig.sample(clip, f, roots, tracks))
                if worst[1] is None or low < worst[1]:
                    worst = (f, low)
                settle = low

            frame, low = worst
            depth_cm = (floor.ground - low) * 100
            gap_cm = (settle - floor.ground) * 100
            print(
                f"{name:<12} {frame:>12} {low:>9.4f} {-depth_cm:>+8.1f}cm "
                f"{gap_cm:>+8.1f}cm"
            )
            if depth_cm > FLOOR_ERROR_CM:
                errors.append(
                    f"{name}@f{frame}: mesh {depth_cm:.1f} cm through the floor "
                    "(joint-space keyframes can pass while the skin does not)"
                )
            elif depth_cm > FLOOR_WARN_CM:
                warnings.append(f"{name}@f{frame}: mesh {depth_cm:.1f} cm through the floor")
            if clip.get("terminal") and gap_cm > SETTLE_ERROR_CM:
                errors.append(
                    f"{name}: settles {gap_cm:.1f} cm ABOVE the deck — a terminal clip "
                    "must come to rest on the floor, not sink to a stop in the air"
                )

        print(f"(mesh pass: {time.time() - started:.1f}s, every {FRAME_STEP} frames)")

    print()
    print(f"clips: {len(lib['clips'])}   fps: {fps}")
    for w in warnings:
        print("WARN :", w)
    for e in errors:
        print("ERROR:", e)
    print(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
