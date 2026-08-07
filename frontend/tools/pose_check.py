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
  7. THE FLOOR, on the skinned mesh, on every interpolated frame — see below.

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

Usage:  python3 tools/pose_check.py          (from frontend/)
        python3 tools/pose_check.py --no-mesh    (skip check 7; ~5s faster)
Exit code 1 on any error.
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import time

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
    return load_glb(path)[0]


def load_glb(path):
    """(json chunk, binary chunk). The binary is what checks 7 skins from."""
    with open(path, "rb") as fh:
        data = fh.read()
    magic, _version, length = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67:
        raise SystemExit(f"{path} is not a GLB")
    offset, gltf, binary = 12, None, b""
    while offset < length:
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        payload = data[offset + 8 : offset + 8 + chunk_len]
        if chunk_type == 0x4E4F534A:  # 'JSON'
            gltf = json.loads(payload.decode("utf-8"))
        elif chunk_type == 0x004E4942:  # 'BIN\0'
            binary = payload
        offset += 8 + chunk_len
    if gltf is None:
        raise SystemExit(f"{path} has no JSON chunk")
    return gltf, binary


# --------------------------------------------------- skinned-mesh floor probe

_COMPONENT = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2),
              5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}

# 6 axes + 8 diagonals. Enough directions that the extreme vertex of any bone in
# any pose is almost always already in the set; more of them only costs build time.
PROBE_DIRECTIONS = [
    (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1),
    (1, 1, 1), (1, 1, -1), (1, -1, 1), (1, -1, -1),
    (-1, 1, 1), (-1, 1, -1), (-1, -1, 1), (-1, -1, -1),
]


def read_accessor(gltf, binary, index):
    acc = gltf["accessors"][index]
    view = gltf["bufferViews"][acc["bufferView"]]
    fmt, size = _COMPONENT[acc["componentType"]]
    n = _NCOMP[acc["type"]]
    stride = view.get("byteStride") or size * n
    base = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    return [
        struct.unpack_from("<" + fmt * n, binary, base + i * stride)
        for i in range(acc["count"])
    ]


class FloorProbe:
    """The lowest point of the skinned mesh, for any pose."""

    def __init__(self, gltf, binary, nodes, parent):
        self.nodes, self.parent = nodes, parent
        skin = gltf["skins"][0]
        self.joints = skin["joints"]
        raw = read_accessor(gltf, binary, skin["inverseBindMatrices"])
        # glTF stores matrices column-major.
        self.ibm = [[[m[c * 4 + r] for c in range(4)] for r in range(4)] for m in raw]

        verts = []
        for mesh in gltf["meshes"]:
            for prim in mesh["primitives"]:
                attrs = prim["attributes"]
                pos = read_accessor(gltf, binary, attrs["POSITION"])
                jnt = read_accessor(gltf, binary, attrs["JOINTS_0"])
                wgt = read_accessor(gltf, binary, attrs["WEIGHTS_0"])
                for i in range(len(pos)):
                    infl = [(jnt[i][k], wgt[i][k]) for k in range(4) if wgt[i][k] > 0]
                    if infl:
                        verts.append((pos[i], infl))
        self.total = len(verts)

        by_joint = {}
        for vi, (_pos, infl) in enumerate(verts):
            for j, _w in infl:
                by_joint.setdefault(j, []).append(vi)

        keep = set()
        for indices in by_joint.values():
            for d in PROBE_DIRECTIONS:
                best, arg = None, None
                for vi in indices:
                    p = verts[vi][0]
                    s = p[0] * d[0] + p[1] * d[1] + p[2] * d[2]
                    if best is None or s > best:
                        best, arg = s, vi
                keep.add(arg)
        self.verts = [verts[vi] for vi in sorted(keep)]
        self.ground = self.lowest({})

    def lowest(self, overrides):
        world = [world_matrix(self.nodes, self.parent, j, overrides) for j in self.joints]
        skin = [mat_mul(world[i], self.ibm[i]) for i in range(len(self.joints))]
        low = None
        for pos, infl in self.verts:
            y = 0.0
            for j, w in infl:
                m = skin[j]
                y += w * (m[1][0] * pos[0] + m[1][1] * pos[1] + m[1][2] * pos[2] + m[1][3])
            if low is None or y < low:
                low = y
        return 0.0 if low is None else low


def quat_slerp(a, b, t):
    """three.js Quaternion.slerp — how the mixer gets between two keyframes."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    cos = ax * bx + ay * by + az * bz + aw * bw
    if cos < 0:
        bx, by, bz, bw, cos = -bx, -by, -bz, -bw, -cos
    if cos > 0.9995:
        out = (ax + t * (bx - ax), ay + t * (by - ay),
               az + t * (bz - az), aw + t * (bw - aw))
        norm = math.sqrt(sum(c * c for c in out)) or 1.0
        return tuple(c / norm for c in out)
    theta = math.acos(cos)
    sin = math.sin(theta)
    w1, w2 = math.sin((1 - t) * theta) / sin, math.sin(t * theta) / sin
    return (ax * w1 + bx * w2, ay * w1 + by * w2,
            az * w1 + bz * w2, aw * w1 + bw * w2)


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
    gltf, binary = load_glb(GLB)
    check_mesh = "--no-mesh" not in sys.argv and bool(binary)
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
    FRAME_STEP = 2

    if check_mesh:
        started = time.time()
        floor = FloorProbe(gltf, binary, nodes, parent)
        print()
        print(
            f"skinned floor probe: {len(floor.verts)} of {floor.total} vertices, "
            f"deck at y={floor.ground:+.4f} m"
        )
        print(f"{'clip':<12} {'worst frame':>12} {'lowest':>9} {'vs deck':>9}")
        print("-" * 46)

        for name, clip in lib["clips"].items():
            keys = clip["keys"]
            do_ground = clip.get("ground", True)

            # Reproduce clipFactory.ts: the frame-0 solve offsets the whole clip,
            # and a grounded clip then re-solves every later keyframe on top.
            clip_dy = 0.0 if do_ground else solve(keys[0]["pose"])[2]
            roots = []
            for k in keys:
                _probe, root_y, dy = solve(k["pose"], clip_dy)
                if do_ground:
                    _probe, root_y, dy = solve(k["pose"], dy)
                _pose, root, _rr = merged_pose(stance, k["pose"])
                root["y"] = root_y
                roots.append(root)

            # Per-key quaternion per joint, exactly as the KeyframeTracks carry them.
            tracks = {}
            for joint in smap:
                idx = by_name[BONE_PREFIX + joint]
                column = []
                for k in keys:
                    pose, _root, root_rot = merged_pose(stance, k["pose"])
                    rot = pose.get(joint, {})
                    q = quat_mul(
                        rest_rot[idx],
                        quat_from_euler_xyz(rot.get("x", 0), rot.get("y", 0), rot.get("z", 0)),
                    )
                    if joint == "Hips" and any(root_rot.values()):
                        q = quat_mul(
                            quat_from_euler_xyz(root_rot["x"], root_rot["y"], root_rot["z"]), q
                        )
                    column.append(q)
                tracks[joint] = column

            times = [k["f"] for k in keys]
            hips_idx = by_name[BONE_PREFIX + "Hips"]
            sampled = sorted(set(range(0, clip["frames"] + 1, FRAME_STEP)) | set(times))

            worst = (None, None)
            for f in sampled:
                seg = 0
                while seg < len(times) - 2 and f > times[seg + 1]:
                    seg += 1
                span = times[seg + 1] - times[seg]
                t = 0.0 if span == 0 else min(max((f - times[seg]) / span, 0.0), 1.0)

                overrides = {}
                for joint in smap:
                    idx = by_name[BONE_PREFIX + joint]
                    overrides[idx] = (
                        None,
                        quat_slerp(tracks[joint][seg], tracks[joint][seg + 1], t),
                    )
                translation = [
                    roots[seg][axis] + t * (roots[seg + 1][axis] - roots[seg][axis])
                    for axis in ("x", "y", "z")
                ]
                overrides[hips_idx] = (translation, overrides[hips_idx][1])

                low = floor.lowest(overrides)
                if worst[1] is None or low < worst[1]:
                    worst = (f, low)

            frame, low = worst
            depth_cm = (floor.ground - low) * 100
            print(f"{name:<12} {frame:>12} {low:>9.4f} {-depth_cm:>+8.1f}cm")
            if depth_cm > FLOOR_ERROR_CM:
                errors.append(
                    f"{name}@f{frame}: mesh {depth_cm:.1f} cm through the floor "
                    "(joint-space keyframes can pass while the skin does not)"
                )
            elif depth_cm > FLOOR_WARN_CM:
                warnings.append(f"{name}@f{frame}: mesh {depth_cm:.1f} cm through the floor")

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
