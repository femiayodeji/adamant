#!/usr/bin/env python3
"""Headless validator for the ADAM Arena clip library.

Runs the same maths the renderer runs (Euler XYZ -> quaternion, forward kinematics
down the Mixamo Xbot hierarchy), without a browser or a GPU, so the eleven-plus
clips in src/renderer/clips.json can be checked as *data* before anyone looks at
a canvas.

What it checks
  1. Every joint named in a pose exists in the rig and in docs/skeleton.md's map.
  2. Every authored axis is inside that joint's documented range.
  3. Every non-terminal clip starts and ends at STANCE (skeleton.md, non-negotiable).
  4. Keyframe counts and clip lengths match the skeleton.md format table.
  5. Loop clips are seamless (first key == last key).
  6. FK sanity per keyframe: feet on the floor after grounding, hips at a plausible
     height, no fist below the hips in a guard pose, punches actually reach forward.

Usage:  python3 tools/pose_check.py          (from frontend/)
Exit code 1 on any error.
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.dirname(HERE)
GLB = os.path.join(FRONTEND, "public", "models", "xbot.glb")
CLIPS = os.path.join(FRONTEND, "src", "renderer", "clips.json")
MAP = os.path.join(FRONTEND, "src", "renderer", "skeleton-map.json")

BONE_PREFIX = "mixamorig:"

# ---------------------------------------------------------------- math helpers


def quat_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quat_from_euler_xyz(x_deg, y_deg, z_deg):
    """three.js Euler order 'XYZ': q = qx * qy * qz."""
    hx, hy, hz = (math.radians(v) / 2 for v in (x_deg, y_deg, z_deg))
    qx = (math.sin(hx), 0.0, 0.0, math.cos(hx))
    qy = (0.0, math.sin(hy), 0.0, math.cos(hy))
    qz = (0.0, 0.0, math.sin(hz), math.cos(hz))
    return quat_mul(quat_mul(qx, qy), qz)


def mat_from_trs(t, q, s):
    x, y, z, w = q
    x2, y2, z2 = x + x, y + y, z + z
    xx, xy, xz = x * x2, x * y2, x * z2
    yy, yz, zz = y * y2, y * z2, z * z2
    wx, wy, wz = w * x2, w * y2, w * z2
    sx, sy, sz = s
    return [
        [(1 - (yy + zz)) * sx, (xy - wz) * sy, (xz + wy) * sz, t[0]],
        [(xy + wz) * sx, (1 - (xx + zz)) * sy, (yz - wx) * sz, t[1]],
        [(xz - wy) * sx, (yz + wx) * sy, (1 - (xx + yy)) * sz, t[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]


def mat_mul(a, b):
    return [
        [sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)
    ]


def mat_pos(m):
    return (m[0][3], m[1][3], m[2][3])


def dist(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


# ---------------------------------------------------------------- glb parsing


def load_gltf(path):
    with open(path, "rb") as fh:
        data = fh.read()
    magic, _version, _length = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67:
        raise SystemExit(f"{path} is not a GLB")
    chunk_len, _chunk_type = struct.unpack_from("<II", data, 12)
    return json.loads(data[20 : 20 + chunk_len].decode("utf-8"))


def build_hierarchy(gltf):
    nodes = gltf["nodes"]
    parent = {}
    for i, n in enumerate(nodes):
        for c in n.get("children", []):
            parent[c] = i
    by_name = {}
    for i, n in enumerate(nodes):
        if n.get("name"):
            by_name[n["name"]] = i
    return nodes, parent, by_name


def world_matrix(nodes, parent, index, overrides):
    """overrides: node index -> (translation, quaternion)."""
    chain = []
    i = index
    while i is not None:
        chain.append(i)
        i = parent.get(i)
    m = [[1 if r == c else 0 for c in range(4)] for r in range(4)]
    for i in reversed(chain):
        n = nodes[i]
        t = list(n.get("translation", [0, 0, 0]))
        q = tuple(n.get("rotation", [0, 0, 0, 1]))
        s = list(n.get("scale", [1, 1, 1]))
        if i in overrides:
            ot, oq = overrides[i]
            if ot is not None:
                t = [t[0] + ot[0], t[1] + ot[1], t[2] + ot[2]]
            if oq is not None:
                q = oq  # already includes the rest rotation
        m = mat_mul(m, mat_from_trs(t, q, s))
    return m


# ---------------------------------------------------------------- pose -> nodes


def merged_pose(stance, override):
    pose = {k: dict(v) for k, v in stance.items() if not k.startswith("_")}
    root = {"x": 0.0, "y": 0.0, "z": 0.0}
    root_rot = {"x": 0.0, "y": 0.0, "z": 0.0}
    for key, val in override.items():
        if key == "_root":
            root.update(val)
        elif key == "_rootRot":
            root_rot.update(val)
        elif key.startswith("_"):
            continue
        else:
            pose[key] = dict(val)
    return pose, root, root_rot


def pose_overrides(pose, root, root_rot, by_name, rest_rot):
    """Build the node-index -> (translation offset, quaternion) override table."""
    out = {}
    for joint, rot in pose.items():
        name = BONE_PREFIX + joint
        idx = by_name[name]
        q = quat_mul(
            rest_rot[idx],
            quat_from_euler_xyz(rot.get("x", 0), rot.get("y", 0), rot.get("z", 0)),
        )
        out[idx] = (None, q)
    hips = by_name[BONE_PREFIX + "Hips"]
    hq = out.get(hips, (None, rest_rot[hips]))[1]
    if any(root_rot.values()):
        hq = quat_mul(
            quat_from_euler_xyz(root_rot["x"], root_rot["y"], root_rot["z"]), hq
        )
    out[hips] = ([root["x"], root["y"], root["z"]], hq)
    return out


# ---------------------------------------------------------------- main


def main() -> int:
    gltf = load_gltf(GLB)
    nodes, parent, by_name = build_hierarchy(gltf)
    rest_rot = {
        i: tuple(n.get("rotation", [0, 0, 0, 1])) for i, n in enumerate(nodes)
    }

    with open(CLIPS) as fh:
        lib = json.load(fh)
    with open(MAP) as fh:
        smap = json.load(fh)["joints"]

    errors: list[str] = []
    warnings: list[str] = []

    # --- rest pose assumption -------------------------------------------------
    for joint in smap:
        name = BONE_PREFIX + joint
        if name not in by_name:
            errors.append(f"rig is missing joint {name}")
            continue
        q = rest_rot[by_name[name]]
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

    # --- ground reference from the rest pose ---------------------------------
    toes = [by_name[BONE_PREFIX + n] for n in ("LeftToe_End", "RightToe_End")]
    ground = min(mat_pos(world_matrix(nodes, parent, t, {}))[1] for t in toes)

    def fk(pose, root, root_rot):
        ov = pose_overrides(pose, root, root_rot, by_name, rest_rot)
        return lambda joint: mat_pos(
            world_matrix(nodes, parent, by_name[BONE_PREFIX + joint], ov)
        )

    # --- per clip -------------------------------------------------------------
    print(f"rig ground plane (rest toe height): {ground:+.4f} m")
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

        if not (3 <= len(keys) <= 5):
            errors.append(f"{name}: {len(keys)} keyframes, skeleton.md says 3-5")
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

        def solve(key_pose, extra_dy=0.0):
            """FK for one keyframe, with the grounding solve applied."""
            pose, root, root_rot = merged_pose(stance, key_pose)
            root["y"] += extra_dy
            probe = fk(pose, root, root_rot)
            low = min(
                probe(j)[1]
                for j in ("LeftToeBase", "RightToeBase", "LeftFoot", "RightFoot")
            )
            dy = (ground - low) / 0.01  # 0.01 = Armature scale, rig cm -> metres
            return probe, root["y"], dy

        # "ground": false (the KO fall) keeps its authored root motion, but is
        # offset once so its first frame sits at exactly the stance height —
        # otherwise the fall starts with a visible pop out of the queue.
        clip_dy = 0.0
        if not do_ground:
            clip_dy = solve(keys[0]["pose"])[2]

        traces = {}
        for k in keys:
            where = f"{name}@f{k['f']}"
            check_pose(where, k["pose"])
            probe, root_y, dy = solve(k["pose"], clip_dy)
            if do_ground:
                probe, root_y, dy = solve(k["pose"], dy)
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

            if lowest < ground - 0.02:
                errors.append(
                    f"{where}: geometry {abs(lowest - ground) * 100:.0f} cm through the floor"
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
