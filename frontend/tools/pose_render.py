#!/usr/bin/env python3
"""Draw the clip library. No browser, no GPU — a PNG contact sheet per clip.

`pose_check.py` proves a clip is legal. It cannot tell you the guard is hanging
open, that a hook looks like a shove, or that a fall reads as an elevator rather
than as a body losing its balance. Those are all things you have to *see*, and
until this existed the only way to see them was to build the frontend, start a
match and watch it go past at speed.

So: same rig maths (`rig.py`, shared with the validator, so this can never draw
a pose the renderer would not play), skinned on the CPU, splatted as depth-sorted
points into a contact sheet of evenly spaced frames.

    python3 tools/pose_render.py ko
    python3 tools/pose_render.py jab hook --view front
    python3 tools/pose_render.py ko --trace          # heights per frame, no PNG
    python3 tools/pose_render.py --all --stride 6    # everything, coarse and fast

Views are named for what the SPECTATOR sees. The arena camera sits on +Z world
and the fighters face each other along X, so `side` — the default — is the view
from the seats: the fighter's own +Z (the direction it punches) runs to the right
of frame. `front` looks the fighter in the face.

Output lands in tools/preview/<clip>.<view>.png, which is gitignored: these are
something you look at while working, not an artefact anything depends on.
"""

from __future__ import annotations

import json
import os
import sys
import time
import zlib

from rig import Rig, SkinnedMesh

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.dirname(HERE)
GLB = os.path.join(FRONTEND, "public", "models", "xbot.glb")
CLIPS = os.path.join(FRONTEND, "src", "renderer", "clips.json")
OUT_DIR = os.path.join(HERE, "preview")

TILE_W, TILE_H = 190, 300
#: World-space box each tile frames, in metres: (horizontal half-width, y range).
HALF_W = 1.05
Y_LO, Y_HI = -0.25, 2.05
POINT_R = 2

BG = (12, 16, 22)
DECK = (46, 58, 72)
GRID = (26, 33, 43)
NEAR = (232, 238, 245)
FAR = (58, 74, 94)
LABEL = (150, 170, 190)

# 5x7 bitmap digits and the few letters the tile labels need.
GLYPHS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "f": ("011", "100", "110", "100", "100"),
    " ": ("000", "000", "000", "000", "000"),
}


class Canvas:
    """An RGB byte buffer with a z-buffer, and just enough drawing to read a pose."""

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.px = bytearray()
        for _ in range(width * height):
            self.px += bytes(BG)
        self.z = [None] * (width * height)

    def put(self, x, y, color, depth=None):
        if not (0 <= x < self.w and 0 <= y < self.h):
            return
        i = y * self.w + x
        if depth is not None:
            if self.z[i] is not None and depth <= self.z[i]:
                return
            self.z[i] = depth
        self.px[i * 3 : i * 3 + 3] = bytes(color)

    def hline(self, y, x0, x1, color):
        for x in range(x0, x1):
            self.put(x, y, color)

    def vline(self, x, y0, y1, color):
        for y in range(y0, y1):
            self.put(x, y, color)

    def text(self, x, y, s, color):
        for ch in s:
            rows = GLYPHS.get(ch, GLYPHS[" "])
            for ry, row in enumerate(rows):
                for rx, bit in enumerate(row):
                    if bit == "1":
                        self.put(x + rx, y + ry, color)
            x += 4

    def png(self, path):
        raw = bytearray()
        for y in range(self.h):
            raw.append(0)  # filter type 0
            raw += self.px[y * self.w * 3 : (y + 1) * self.w * 3]

        def chunk(tag, data):
            body = tag + data
            return (
                len(data).to_bytes(4, "big")
                + body
                + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")
            )

        header = (
            self.w.to_bytes(4, "big")
            + self.h.to_bytes(4, "big")
            + bytes([8, 2, 0, 0, 0])  # 8-bit, truecolour
        )
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")
            fh.write(chunk(b"IHDR", header))
            fh.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
            fh.write(chunk(b"IEND", b""))


def project(point, view):
    """World (x, y, z) -> (horizontal, vertical, depth) for a named view."""
    x, y, z = point
    if view == "front":
        # Looking the fighter in the face, from the direction it punches.
        return -x, y, z
    # From the seats: the punching direction runs to the right of frame.
    return z, y, -x


def draw_frame(canvas, ox, points, ground, view, label):
    """One tile: floor line, waist rule, the skinned point cloud, a frame number."""
    def to_px(p):
        hx, hy, depth = project(p, view)
        px = ox + int((hx + HALF_W) / (2 * HALF_W) * TILE_W)
        py = int((Y_HI - hy) / (Y_HI - Y_LO) * TILE_H)
        return px, py, depth

    floor_y = int((Y_HI - ground) / (Y_HI - Y_LO) * TILE_H)
    for y in range(0, TILE_H, 24):
        canvas.hline(y, ox, ox + TILE_W, GRID)
    canvas.hline(floor_y, ox, ox + TILE_W, DECK)
    canvas.hline(floor_y + 1, ox, ox + TILE_W, DECK)
    canvas.vline(ox, 0, TILE_H, GRID)

    depths = [project(p, view)[2] for p in points]
    lo, hi = min(depths), max(depths)
    span = (hi - lo) or 1.0
    for p in points:
        px, py, depth = to_px(p)
        t = (depth - lo) / span
        color = tuple(int(FAR[i] + (NEAR[i] - FAR[i]) * t) for i in range(3))
        for dx in range(-POINT_R, POINT_R + 1):
            for dy in range(-POINT_R, POINT_R + 1):
                if dx * dx + dy * dy <= POINT_R * POINT_R:
                    canvas.put(px + dx, py + dy, color, depth)

    canvas.text(ox + 4, 6, label, LABEL)


def frames_for(clip, columns):
    total = clip["frames"]
    if columns >= total + 1:
        return list(range(total + 1))
    step = total / (columns - 1)
    return sorted({int(round(i * step)) for i in range(columns)})


def render(rig, mesh, subset, lib, name, view, columns, out_dir):
    clip = lib["clips"][name]
    stance = lib["stance"]
    roots = rig.clip_roots(stance, clip)
    tracks = rig.clip_tracks(stance, clip)
    frames = frames_for(clip, columns)

    canvas = Canvas(TILE_W * len(frames), TILE_H)
    for col, f in enumerate(frames):
        skin = mesh.matrices(rig.sample(clip, f, roots, tracks))
        points = SkinnedMesh.skin_points(skin, subset)
        draw_frame(canvas, col * TILE_W, points, rig.ground, view, f"f{f}")

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.{view}.png")
    canvas.png(path)
    return path, frames


def trace(rig, mesh, subset, lib, name, step):
    """Heights per frame — hips, head, and the lowest point of the skin.

    This is the numeric half of "does the fall accelerate": `drop` is how far the
    hips fell since the previous sample, so a body under gravity has it growing
    until the landing and then going flat.
    """
    clip = lib["clips"][name]
    stance = lib["stance"]
    roots = rig.clip_roots(stance, clip)
    tracks = rig.clip_tracks(stance, clip)
    hips_idx, head_idx = rig.node("Hips"), rig.node("Head")

    print(f"\n{name}: {clip['frames']} frames @ {lib['fps']} fps")
    print(f"{'f':>4} {'hips.y':>8} {'drop':>7} {'head.y':>8} {'skin low':>9}")
    print("-" * 40)
    previous = None
    for f in sorted(set(range(0, clip["frames"] + 1, step)) | {clip["frames"]}):
        ov = rig.sample(clip, f, roots, tracks)
        hips = rig.world(hips_idx, ov)[1][3]
        head = rig.world(head_idx, ov)[1][3]
        low = SkinnedMesh.skin_y(mesh.matrices(ov), subset)
        drop = "" if previous is None else f"{hips - previous:+7.3f}"
        previous = hips
        print(f"{f:>4} {hips:>8.3f} {drop:>7} {head:>8.3f} {low:>9.3f}")


def main() -> int:
    args = [a for a in sys.argv[1:]]
    view = "side"
    columns = 8
    stride = 3
    trace_step = 0
    names = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--view":
            i += 1
            view = args[i]
        elif a == "--cols":
            i += 1
            columns = int(args[i])
        elif a == "--stride":
            i += 1
            stride = int(args[i])
        elif a == "--trace":
            trace_step = 1
        elif a == "--all":
            names.append("--all")
        else:
            names.append(a)
        i += 1

    with open(CLIPS) as fh:
        lib = json.load(fh)

    if "--all" in names or not names:
        names = list(lib["clips"])
    unknown = [n for n in names if n not in lib["clips"]]
    if unknown:
        print(f"no such clip: {', '.join(unknown)}")
        print(f"have: {', '.join(lib['clips'])}")
        return 1

    started = time.time()
    rig = Rig(GLB)
    mesh = SkinnedMesh(rig)
    # Every nth vertex. The cloud only has to read as a body, and pure-Python
    # skinning of all 28k of them per frame is a minute a clip.
    subset = mesh.verts[::stride]
    print(f"{len(subset)} of {len(mesh.verts)} vertices, view '{view}'")

    for name in names:
        if trace_step:
            trace(rig, mesh, subset, lib, name, trace_step)
            continue
        path, frames = render(rig, mesh, subset, lib, name, view, columns, OUT_DIR)
        print(f"{os.path.relpath(path, FRONTEND):<34} frames {frames}")

    print(f"({time.time() - started:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
