#!/usr/bin/env python3
"""Shared rig maths for the clip tools — the browser-free half of the renderer.

`clipFactory.ts` turns `src/renderer/clips.json` into THREE.AnimationClips. This
module does the same arithmetic in pure Python (Euler XYZ -> quaternion, forward
kinematics down the Mixamo Xbot hierarchy, the grounding solve, and the slerp the
mixer runs between keyframes) so the library can be inspected as *data*.

Two tools sit on top of it and must never disagree about what a clip does:
  * `pose_check.py`  — validates the library and fails the build data.
  * `pose_render.py` — draws it, so a pose can be looked at without a browser.

Anything either of them needs about the rig belongs here; a second copy of the
grounding rule is exactly the drift docs/skeleton.md warns about.

Nothing here imports anything outside the standard library, deliberately: `npm run
poses` runs on whatever `python3` the machine has.
"""

from __future__ import annotations

import json
import math
import os
import struct

_HERE = os.path.dirname(os.path.abspath(__file__))
_FRONTEND = os.path.dirname(_HERE)
GLB_PATH = os.path.join(_FRONTEND, "public", "models", "xbot.glb")
MAP_PATH = os.path.join(_FRONTEND, "src", "renderer", "skeleton-map.json")

BONE_PREFIX = "mixamorig:"

#: Rig units (cm) per world metre — the Armature scale in xbot.glb.
UNIT_SCALE = 0.01

#: The joints the grounding solve keeps on the floor. Toe *joints*, not the sole:
#: see docs/skeleton.md, "The floor is not a keyframe property".
FOOT_PROBES = ("LeftToeBase", "RightToeBase", "LeftFoot", "RightFoot")

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

_COMPONENT = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2),
              5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def load_glb(path):
    """(json chunk, binary chunk). The binary is what the mesh passes skin from."""
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


# ---------------------------------------------------------------- pose merging


def mirror_name(joint, swaps):
    for a, b in swaps:
        if joint.startswith(a):
            return b + joint[len(a):]
        if joint.startswith(b):
            return a + joint[len(b):]
    return joint


class Rig:
    """One loaded copy of xbot.glb, posable by clips.json data."""

    def __init__(self, glb_path=GLB_PATH, map_path=MAP_PATH):
        with open(map_path) as fh:
            self.map = json.load(fh)
        self.joints = self.map["joints"]
        self.curl = self._hand_curl()
        #: Every joint a clip can end up driving — the authored map plus the
        #: finger chains `_fist` expands into. Tracks are built over this.
        self.posed_joints = list(self.joints) + list(self.curl)

        self.gltf, self.binary = load_glb(glb_path)
        nodes = self.gltf["nodes"]
        self.nodes = nodes
        self.parent = {}
        for i, n in enumerate(nodes):
            for c in n.get("children", []):
                self.parent[c] = i
        self.by_name = {n["name"]: i for i, n in enumerate(nodes) if n.get("name")}
        self.rest_rot = {
            i: tuple(n.get("rotation", [0, 0, 0, 1])) for i, n in enumerate(nodes)
        }
        # The canvas floor: where the rest pose's toe tips sit.
        self.ground = min(
            mat_pos(self.world(self.by_name[BONE_PREFIX + n], {}))[1]
            for n in ("LeftToe_End", "RightToe_End")
        )

    # ------------------------------------------------------------------ hands

    def _hand_curl(self):
        """The full-fist rotation of every finger bone, both hands.

        skeleton-map.json authors the LEFT hand only and the right is derived by
        the same mirror rule agent_b's whole clip set is (negate y and z, swap the
        Left/Right prefix) — one hand to tune, and no way for the two to drift.
        """
        hand = self.map.get("hand", {})
        swaps = [tuple(p) for p in self.map["mirror"]["swapPrefixes"]]
        negate = self.map["mirror"]["negate"]
        out = {}
        for joint, rot in hand.get("curl", {}).items():
            out[joint] = dict(rot)
            out[mirror_name(joint, swaps)] = {
                axis: (-v if axis in negate else v) for axis, v in rot.items()
            }
        return out

    def expand_fist(self, pose):
        """`_fist` -> actual finger rotations. Mirrors `expandFist` in clipFactory.ts.

        A pose says how closed each hand is (0 = the rig's open rest hand, 1 = the
        authored fist) and this scales the template. Authoring 48 finger bones per
        keyframe by hand would bury the pose that matters, and the fingers never
        want to move independently anyway — the whole point is that the hand is a
        fist when it throws and slack when the fighter stops fighting.
        """
        amounts = pose.get("_fist")
        if amounts is None:
            return pose
        if not isinstance(amounts, dict):
            amounts = {"left": amounts, "right": amounts}
        out = {k: v for k, v in pose.items() if k != "_fist"}
        for joint, rot in self.curl.items():
            side = "left" if joint.startswith("Left") else "right"
            amount = amounts.get(side)
            if amount is None:
                continue
            out[joint] = {axis: v * amount for axis, v in rot.items()}
        return out

    # ---------------------------------------------------------------- posing

    def merge(self, stance, override):
        """stance + a keyframe's partial override -> (rotations, _root, _rootRot).

        Mirrors `mergePose` in clipFactory.ts: a joint the keyframe does not
        mention holds its stance value, which is what makes an empty pose `{}`
        *be* stance — including its hands.
        """
        stance = self.expand_fist(stance)
        override = self.expand_fist(override)
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

    # ------------------------------------------------------------ kinematics

    def node(self, joint):
        return self.by_name[BONE_PREFIX + joint]

    def world(self, index, overrides):
        """World matrix of a node. `overrides`: node index -> (translation, quat)."""
        chain = []
        i = index
        while i is not None:
            chain.append(i)
            i = self.parent.get(i)
        m = [[1 if r == c else 0 for c in range(4)] for r in range(4)]
        for i in reversed(chain):
            n = self.nodes[i]
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

    def overrides(self, pose, root, root_rot):
        """Node-index -> (translation offset, quaternion) for one merged pose."""
        out = {}
        for joint, rot in pose.items():
            idx = self.node(joint)
            out[idx] = (
                None,
                quat_mul(
                    self.rest_rot[idx],
                    quat_from_euler_xyz(rot.get("x", 0), rot.get("y", 0), rot.get("z", 0)),
                ),
            )
        hips = self.node("Hips")
        hq = out.get(hips, (None, self.rest_rot[hips]))[1]
        if any(root_rot.values()):
            hq = quat_mul(
                quat_from_euler_xyz(root_rot["x"], root_rot["y"], root_rot["z"]), hq
            )
        out[hips] = ([root["x"], root["y"], root["z"]], hq)
        return out

    def fk(self, pose, root, root_rot):
        """-> probe(joint) giving that joint's world position under this pose."""
        ov = self.overrides(pose, root, root_rot)
        return lambda joint: mat_pos(self.world(self.node(joint), ov))

    # -------------------------------------------------------------- grounding

    def solve(self, stance, key_pose, extra_dy=0.0):
        """FK for one keyframe -> (probe, root.y used, hips dy that would ground it).

        `dy` is in rig units, exactly as `Rig.solveGrounding` returns it in
        clipFactory.ts: add it to `_root.y` and the lowest toe lands on the floor.
        """
        pose, root, root_rot = self.merge(stance, key_pose)
        root["y"] += extra_dy
        probe = self.fk(pose, root, root_rot)
        low = min(probe(j)[1] for j in FOOT_PROBES)
        return probe, root["y"], (self.ground - low) / UNIT_SCALE

    def clip_roots(self, stance, clip):
        """The solved `_root` of every keyframe, as the renderer will play them.

        The rule is clipFactory.ts's, once: frame 0's solve offsets the whole clip
        so it leaves the stance at the height every other clip ends at, and a
        grounded clip then re-solves each later keyframe on top. `"ground": false`
        (the KO fall) keeps its authored arc after that first offset.
        """
        keys = clip["keys"]
        do_ground = clip.get("ground", True)
        clip_dy = 0.0 if do_ground else self.solve(stance, keys[0]["pose"])[2]
        roots = []
        for k in keys:
            _probe, root_y, dy = self.solve(stance, k["pose"], clip_dy)
            if do_ground:
                _probe, root_y, dy = self.solve(stance, k["pose"], dy)
            _pose, root, _rr = self.merge(stance, k["pose"])
            root["y"] = root_y
            roots.append(root)
        return roots

    # --------------------------------------------------------- clip sampling

    def clip_tracks(self, stance, clip, joints=None):
        """Per-joint quaternion per keyframe — the KeyframeTracks, as authored."""
        joints = self.posed_joints if joints is None else joints
        tracks = {}
        for joint in joints:
            idx = self.node(joint)
            column = []
            for k in clip["keys"]:
                pose, _root, root_rot = self.merge(stance, k["pose"])
                rot = pose.get(joint, {})
                q = quat_mul(
                    self.rest_rot[idx],
                    quat_from_euler_xyz(rot.get("x", 0), rot.get("y", 0), rot.get("z", 0)),
                )
                if joint == "Hips" and any(root_rot.values()):
                    q = quat_mul(
                        quat_from_euler_xyz(root_rot["x"], root_rot["y"], root_rot["z"]), q
                    )
                column.append(q)
            tracks[joint] = column
        return tracks

    def sample(self, clip, frame, roots, tracks, joints=None):
        """Overrides for an arbitrary frame — slerped rotations, lerped hips path.

        This is the pair the keyframes cannot describe on their own, and the reason
        the mesh pass exists: three.js interpolates the two independently, so the
        limbs sweep an arc neither endpoint reveals.
        """
        joints = self.posed_joints if joints is None else joints
        times = [k["f"] for k in clip["keys"]]
        seg = 0
        while seg < len(times) - 2 and frame > times[seg + 1]:
            seg += 1
        span = times[seg + 1] - times[seg]
        t = 0.0 if span == 0 else min(max((frame - times[seg]) / span, 0.0), 1.0)

        out = {}
        for joint in joints:
            out[self.node(joint)] = (
                None,
                quat_slerp(tracks[joint][seg], tracks[joint][seg + 1], t),
            )
        hips = self.node("Hips")
        out[hips] = (
            [
                roots[seg][axis] + t * (roots[seg + 1][axis] - roots[seg][axis])
                for axis in ("x", "y", "z")
            ],
            out[hips][1],
        )
        return out


# --------------------------------------------------- skinned-mesh evaluation

# 6 axes + 8 diagonals. Enough directions that the extreme vertex of any bone in
# any pose is almost always already in the set; more of them only costs build time.
PROBE_DIRECTIONS = [
    (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1),
    (1, 1, 1), (1, 1, -1), (1, -1, 1), (1, -1, -1),
    (-1, 1, 1), (-1, 1, -1), (-1, -1, 1), (-1, -1, -1),
]


class SkinnedMesh:
    """The rig's actual geometry: vertex weights, joints, inverse bind matrices.

    Joints are not the body (docs/skeleton.md), so anything that has to answer a
    question about where the *surface* is — is a foot through the deck, what does
    this pose look like — has to come through here.
    """

    def __init__(self, rig: Rig):
        self.rig = rig
        skin = rig.gltf["skins"][0]
        self.joints = skin["joints"]
        raw = read_accessor(rig.gltf, rig.binary, skin["inverseBindMatrices"])
        # glTF stores matrices column-major.
        self.ibm = [[[m[c * 4 + r] for c in range(4)] for r in range(4)] for m in raw]

        verts = []
        for mesh in rig.gltf["meshes"]:
            for prim in mesh["primitives"]:
                attrs = prim["attributes"]
                pos = read_accessor(rig.gltf, rig.binary, attrs["POSITION"])
                jnt = read_accessor(rig.gltf, rig.binary, attrs["JOINTS_0"])
                wgt = read_accessor(rig.gltf, rig.binary, attrs["WEIGHTS_0"])
                for i in range(len(pos)):
                    infl = [(jnt[i][k], wgt[i][k]) for k in range(4) if wgt[i][k] > 0]
                    if infl:
                        verts.append((pos[i], infl))
        self.verts = verts

    def matrices(self, overrides):
        """Skinning matrix per joint for one pose."""
        world = [self.rig.world(j, overrides) for j in self.joints]
        return [mat_mul(world[i], self.ibm[i]) for i in range(len(self.joints))]

    def extremes(self):
        """A small subset of vertices containing every bone's extreme in 14
        directions — enough to track the true lowest point to within ~4 mm, and
        cheap enough to evaluate on every other frame of every clip."""
        by_joint = {}
        for vi, (_pos, infl) in enumerate(self.verts):
            for j, _w in infl:
                by_joint.setdefault(j, []).append(vi)
        keep = set()
        for indices in by_joint.values():
            for d in PROBE_DIRECTIONS:
                best, arg = None, None
                for vi in indices:
                    p = self.verts[vi][0]
                    s = p[0] * d[0] + p[1] * d[1] + p[2] * d[2]
                    if best is None or s > best:
                        best, arg = s, vi
                keep.add(arg)
        return [self.verts[vi] for vi in sorted(keep)]

    @staticmethod
    def skin_y(skin, subset):
        """Lowest world Y over `subset` — one matrix row, so no full transform."""
        low = None
        for pos, infl in subset:
            y = 0.0
            for j, w in infl:
                m = skin[j]
                y += w * (m[1][0] * pos[0] + m[1][1] * pos[1] + m[1][2] * pos[2] + m[1][3])
            if low is None or y < low:
                low = y
        return 0.0 if low is None else low

    @staticmethod
    def skin_points(skin, subset):
        """Full world positions over `subset` — what the renderer draws."""
        out = []
        for pos, infl in subset:
            px = py = pz = 0.0
            for j, w in infl:
                m = skin[j]
                px += w * (m[0][0] * pos[0] + m[0][1] * pos[1] + m[0][2] * pos[2] + m[0][3])
                py += w * (m[1][0] * pos[0] + m[1][1] * pos[1] + m[1][2] * pos[2] + m[1][3])
                pz += w * (m[2][0] * pos[0] + m[2][1] * pos[1] + m[2][2] * pos[2] + m[2][3])
            out.append((px, py, pz))
        return out


class FloorProbe:
    """The lowest point of the skinned mesh, for any pose."""

    def __init__(self, rig: Rig):
        self.mesh = SkinnedMesh(rig)
        self.total = len(self.mesh.verts)
        self.verts = self.mesh.extremes()
        self.ground = self.lowest({})

    def lowest(self, overrides):
        return SkinnedMesh.skin_y(self.mesh.matrices(overrides), self.verts)
