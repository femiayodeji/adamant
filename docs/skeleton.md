# ADAM Arena — Rig Skeleton Map & Clip Conventions

The joint map for the **Mixamo Xbot** rig (`statics/models/xbot.glb`), plus the conventions for authoring the combat animation clips built on it.

**What consumes this:** the Phase 4 renderer ([`plan.md`](plan.md)). Each of the eight actions in [`arena_spec.md`](arena_spec.md) §2 gets one pre-baked keyframe clip per fighter, driven by these joints. Nothing in the rules engine or the protocol touches this file — agents submit intents, and the renderer alone decides what those look like.

**How to read the map:** all rotations are **offsets from the T-pose rest position, in degrees**. `0` on every axis is the rest pose. `range` is the usable envelope for each axis; going outside it produces visible mesh deformation. The `note` on each joint describes what its axes do in plain English, which is what makes the map usable directly in an LLM prompt for pose generation.

---

## Skeleton Map

```python
# ─── Skeleton map for Mixamo Xbot (T-pose rest) ───
# All rotations are OFFSETS from T-pose (degrees). 0 = rest/T-pose.
# "note" tells the LLM what each axis does in plain English.
# This rig represents a full-body humanoid. Motions should be planned as human
# movement using the available major joints, even when the request is complex.
SKELETON_MAP = {
    "Hips": {
        "axes": ["x", "y", "z"],
        "range": {"x": [-35, 35], "y": [-180, 180], "z": [-35, 35]},
        "note": "Root pelvis. x: tilt fwd(+)/back(−). y: rotate whole body left(+)/right(−). z: tilt sideways. Use for balance, crouching, turns, gait, and weight transfer."
    },
    "Spine": {
        "axes": ["x", "y", "z"],
        "range": {"x": [-40, 40], "y": [-30, 30], "z": [-20, 20]},
        "note": "Lower torso. x: bend fwd(+)/back(−). y: twist torso. z: side bend. Coordinate with Spine1 and Spine2 for realistic human bending."
    },
    "Spine1": {
        "axes": ["x", "y", "z"],
        "range": {"x": [-25, 25], "y": [-20, 20], "z": [-15, 15]},
        "note": "Mid torso. Same as Spine but smaller range."
    },
    "Spine2": {
        "axes": ["x", "y", "z"],
        "range": {"x": [-20, 20], "y": [-15, 15], "z": [-10, 10]},
        "note": "Upper chest. Small corrections. Arms attach here."
    },
    "Neck": {
        "axes": ["x", "y", "z"],
        "range": {"x": [-40, 40], "y": [-70, 70], "z": [-30, 30]},
        "note": "x: look down(+)/up(−). y: turn head left(+)/right(−). z: tilt head sideways."
    },
    "Head": {
        "axes": ["x", "y", "z"],
        "range": {"x": [-30, 30], "y": [-60, 60], "z": [-25, 25]},
        "note": "Same axes as Neck. Combine Neck + Head for full head range."
    },
    "LeftShoulder": {
        "axes": ["x", "y", "z"],
        "range": {"x": [-15, 15], "y": [-10, 10], "z": [-10, 30]},
        "note": "Clavicle. +z: shrug up. Keep small — this is a subtle bone."
    },
    "LeftArm": {
        "axes": ["x", "y", "z"],
        "range": {"x": [-80, 80], "y": [-90, 90], "z": [-90, 180]},
        "note": "Upper arm. T-pose=0. x: swing fwd(+)/back(−). y: twist/roll. z: raise overhead(+)/lower to side(−). −90z = arm hanging at side. Combine with shoulder, forearm, hand, spine, and hips for reaching and expressive human gestures."
    },
    "LeftForeArm": {
        "axes": ["x", "y"],
        "range": {"x": [-10, 10], "y": [-145, 0]},
        "note": "Elbow. y(−): bend elbow (curl toward bicep). x: minor wrist pronation."
    },
    "LeftHand": {
        "axes": ["x", "z"],
        "range": {"x": [-60, 60], "z": [-40, 40]},
        "note": "Wrist. x: flex down(+)/extend up(−). z: side bend."
    },
    "RightShoulder": {
        "axes": ["x", "y", "z"],
        "range": {"x": [-15, 15], "y": [-10, 10], "z": [-30, 10]},
        "note": "Clavicle (mirror). −z: shrug up."
    },
    "RightArm": {
        "axes": ["x", "y", "z"],
        "range": {"x": [-80, 80], "y": [-90, 90], "z": [-180, 90]},
        "note": "Upper arm (mirror). x: fwd(+)/back(−). y: twist. z: raise overhead(−)/lower to side(+). +90z = arm hanging at side. Combine with shoulder, forearm, hand, spine, and hips for reaching and expressive human gestures."
    },
    "RightForeArm": {
        "axes": ["x", "y"],
        "range": {"x": [-10, 10], "y": [0, 145]},
        "note": "Elbow (mirror). y(+): bend elbow."
    },
    "RightHand": {
        "axes": ["x", "z"],
        "range": {"x": [-60, 60], "z": [-40, 40]},
        "note": "Wrist. Same as LeftHand."
    },
    "LeftUpLeg": {
        "axes": ["x", "y", "z"],
        "range": {"x": [-120, 35], "y": [-45, 45], "z": [-30, 45]},
        "note": "Thigh. x: kick forward(−)/extend back(+). y: twist. z: spread outward(+)/inward(−). Primary driver for walking, running, crouching, stepping, and jumping."
    },
    "LeftLeg": {
        "axes": ["x"],
        "range": {"x": [0, 145]},
        "note": "Knee. x(+): bend knee. Cannot hyperextend."
    },
    "LeftFoot": {
        "axes": ["x", "z"],
        "range": {"x": [-45, 50], "z": [-20, 20]},
        "note": "Ankle. x: point toes down(−)/flex up(+). z: ankle roll."
    },
    "LeftToeBase": {
        "axes": ["x"],
        "range": {"x": [-30, 60]},
        "note": "Toe pivot. x(+): curl toes up."
    },
    "RightUpLeg": {
        "axes": ["x", "y", "z"],
        "range": {"x": [-120, 35], "y": [-45, 45], "z": [-45, 30]},
        "note": "Thigh (mirror). x: kick forward(−)/back(+). z: spread outward(−)/inward(+)."
    },
    "RightLeg": {
        "axes": ["x"],
        "range": {"x": [0, 145]},
        "note": "Knee (mirror). Same as LeftLeg. Critical for stepping, landing, crouching, and human gait shock absorption."
    },
    "RightFoot": {
        "axes": ["x", "z"],
        "range": {"x": [-45, 50], "z": [-20, 20]},
        "note": "Ankle. Same as LeftFoot."
    },
    "RightToeBase": {
        "axes": ["x"],
        "range": {"x": [-30, 60]},
        "note": "Toe pivot (mirror). Same as LeftToeBase."
    },
}
```

The map is internally mirror-consistent: `LeftForeArm.y` `[-145, 0]` against `RightForeArm.y` `[0, 145]`, the shoulder `z` signs, and the thigh spread signs all invert correctly. A left-side pose can be mirrored to the right by negating `y` and `z` and leaving `x` alone.

---

## Clip-Authoring Conventions

Settle these **before** authoring clips, not after — there are eleven of them and they must all agree.

### The neutral pose is a boxing stance, not the T-pose

T-pose is the rig's *rest* (the zero of this map). It is not the *neutral* the fighters return to. Define a named `STANCE` pose — guard up, elbows in, knees slightly bent, torso bladed — expressed as offsets from T-pose using the map above, and treat it as the visual origin for every clip.

This distinction is the most common source of broken clips: an author reads "0 = rest" and builds a clip that starts from a T-pose, which then pops when queued after a clip that ended in stance.

### Every clip starts and ends at `STANCE`

Non-negotiable, and the reason is the clip queue. Ticks can resolve faster than a clip plays ([`arena_spec.md`](arena_spec.md) §13), so the renderer queues clips back to back. If clips do not share a common start and end pose, every transition pops. Blending between arbitrary poses would be the alternative and is far more work than authoring to a fixed anchor.

### Format

| Convention | Value |
|---|---|
| Frame rate | 30 fps |
| Keyframes per clip | 3–5 — anticipation, contact/extreme, recovery. Interpolate between; do not hand-key every frame. |
| Attack clip length | ~0.5 s (15 frames) |
| Defense clip length | ~0.4 s (12 frames) |
| Idle / stance loop | ~1.5 s (45 frames), seamless loop |
| Hit-react, riposte | ~0.4 s (12 frames) |
| KO | ~1.5 s (45 frames), ends on the floor — the only clip exempt from returning to `STANCE`, since it is terminal |

### Action-to-clip binding

Clip name **is** the action name from [`arena_spec.md`](arena_spec.md) §2 — `jab`, `cross`, `hook`, `block_high`, `block_low`, `dodge_left`, `dodge_right`, `idle`. The renderer looks clips up by the action string in `tick_result.moves` with no mapping table, so a rules-table rename is also a clip rename.

Three additional clips are event-driven rather than action-driven, played in response to [`arena_spec.md`](arena_spec.md) §4.5 events: `hit_react` (on a `hit` event against this fighter), `riposte` (on a `dodged` event where this fighter is the defender), and `ko` (on a `ko` event).

**Eleven clips per fighter.** `agent_b` is `agent_a` mirrored across X, so authoring is eleven clips total, not twenty-two.

### Directional clips are named from the fighter's own perspective

`dodge_left` moves the fighter to *its* left, which is screen-right for the mirrored corner. Author once for `agent_a` and let the mirror handle `agent_b`; do not author a second set.

### Open

- Whether `idle` (the stamina-regen action) reuses the stance loop or gets a distinct "catch your breath" clip. Distinct reads better and costs one clip — decide at authoring time.
- Whether `exhausted` (spec §3) warrants a visual tell. It is currently rendered as `idle`; a slumped variant would make the stamina economy legible to spectators, which is worth something for a platform meant to be watched.
