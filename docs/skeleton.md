# Adamant — Rig Skeleton Map & Clip Conventions

The joint map for the **Mixamo Xbot** rig (`statics/models/xbot.glb`), plus the conventions for authoring the combat animation clips built on it.

**What consumes this:** the Phase 4 renderer ([`plan.md`](plan.md)). Each of the eight actions in [`adamant_spec.md`](adamant_spec.md) §2 gets one pre-baked keyframe clip per fighter, driven by these joints. Nothing in the rules engine or the protocol touches this file — agents submit intents, and the renderer alone decides what those look like.

**How to read the map:** all rotations are **offsets from the T-pose rest position, in degrees**. `0` on every axis is the rest pose. `range` is the usable envelope for each axis; going outside it produces visible mesh deformation. The `note` on each joint describes what its axes do in plain English, which is what makes the map usable directly in an LLM prompt for pose generation.

Every bone's rest rotation in `xbot.glb` is identity, so all bones share the model's axis frame — verified by `frontend/tools/pose_check.py`, which validates the clip library as data (joint names, axis ranges, stance anchoring, loop seams, FK sanity, and the skinned mesh against the floor) without a browser. Its sibling `pose_render.py` *draws* the library — CPU-skinned PNG contact sheets, one tile per sampled frame — because a clip can be entirely legal and still look wrong, and the alternative was building the frontend and watching a match go past at speed. Both sit on `tools/rig.py`, which is the one Python implementation of the renderer's maths. `frontend/src/renderer/skeleton-map.json` is the machine-readable copy this file describes; keep the two in sync.

**The fighters wear their corner colour.** `xbot.glb` carries two `MeshStandard` materials and no textures, so both corners come out of the file identically salmon. `Fighter.skin()` puts the corner colour on the albedo — the body tinted and darkened, the joint material mixed almost to black so knees, elbows and shoulders read as articulation — and keeps a low emissive tint of the same colour, which is also the channel the hit flash drives. The rim lights, deck band, post cap and HUD carry the colour too, but they cannot carry it alone: at the moment a punch lands, which is the moment that has to be readable, the two silhouettes overlap and a rim light is exactly what is occluded.

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
        "note": "Upper arm. T-pose=0. x: twist/roll along the bone. y: swing fwd(−)/back(+). z: raise overhead(+)/lower to side(−). −90z = arm hanging at side. Combine with shoulder, forearm, hand, spine, and hips for reaching and expressive human gestures."
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
        "note": "Upper arm (mirror). x: twist/roll along the bone. y: swing fwd(+)/back(−). z: raise overhead(−)/lower to side(+). +90z = arm hanging at side. Combine with shoulder, forearm, hand, spine, and hips for reaching and expressive human gestures."
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

### The fingers are not in the map, on purpose

The rig has all thirty finger bones and its rest hand is **flat and open**, which means a punch that never touches them lands as an open-palm slap. But no keyframe should ever name `LeftHandRing2`: forty-eight finger rotations per pose would bury the pose that matters, and the fingers have no reason to move independently — the hand is a fist when it throws and slack when the fighter stops fighting.

So a pose asks for a hand *shape* instead. `"_fist": 1` is the closed fist, `0` is the rig's own open rest hand, `{"left": 1}` drives one hand alone, and anything between is a hand on its way to one or the other (the guard sits at `0.85`, `idle` opens to `0.3`, `ko` goes slack at `0.1`). `skeleton-map.json`'s `hand.curl` section holds the fifteen rotations of a fully closed **left** fist; the right is derived by the mirror rule above, so there is one hand to tune. `clipFactory.ts` and `tools/rig.py` expand it, in that order of authority.

Two facts about the rest hand that the numbers depend on: palms face **down** in the T-pose, so `-z` curls a finger into the palm; and the thumb needs `+y` as well, or it hangs off the side of the fist instead of coming across the front of it.

---

## Clip-Authoring Conventions

All settled during Phase 4 and implemented in `frontend/src/renderer/`. Fourteen clips per fighter, and they must all agree.

### The neutral pose is a boxing stance, not the T-pose

T-pose is the rig's *rest* (the zero of this map). It is not the *neutral* the fighters return to. Define a named `STANCE` pose — guard up, elbows in, knees slightly bent, torso bladed — expressed as offsets from T-pose using the map above, and treat it as the visual origin for every clip.

This distinction is the most common source of broken clips: an author reads "0 = rest" and builds a clip that starts from a T-pose, which then pops when queued after a clip that ended in stance.

### Every clip starts and ends at `STANCE`

Non-negotiable, and the reason is the clip queue. Ticks can resolve faster than a clip plays ([`adamant_spec.md`](adamant_spec.md) §13), so the renderer queues clips back to back. If clips do not share a common start and end pose, every transition pops. Blending between arbitrary poses would be the alternative and is far more work than authoring to a fixed anchor.

### Format

| Convention | Value |
|---|---|
| Frame rate | 30 fps |
| Keyframes per clip | 3–6 — anticipation, contact/extreme, recovery. Interpolate between; do not hand-key every frame. Only `ko` needs the sixth, because a fall and its landing are two events, not one. |
| Attack clip length | ~0.5 s (15 frames) |
| Defense clip length | ~0.4 s (12 frames) |
| Idle / stance loop | ~1.5 s (45 frames), seamless loop |
| Hit-react, riposte, void | ~0.4 s (12–16 frames) |
| Exhausted | ~1.5 s (45 frames) |
| KO | ~1.5 s (45 frames), ends on the floor — the only clip exempt from returning to `STANCE`, since it is terminal |

### Root motion: `_root` and `_rootRot`

The map defines rotations only, and two things cannot be authored without translation. `ko` must end on the floor — an ~88° whole-body rotation plus an ~87 cm pelvis drop, which as a `Hips` rotation would blow through the documented ±35° even though nothing deforms. And **every crouch** (`block_low`, `block_high`, both dodges): bending the knees in pure FK *lifts the feet*, it does not lower the hips, so a crouch without translation walks the fighter into the air.

Two renderer-level keys, explicitly not joints, cover this: **`_root`** (Hips translation in rig units, fighter-local) and **`_rootRot`** (whole-body rotation pre-multiplied onto the Hips). `_root.y` is normally *solved*, not authored — `clipFactory` grounds each keyframe so the lowest toe stays on the canvas. `ko` opts out with `"ground": false` and authors its own fall.

### The floor is not a keyframe property

Opting out of grounding puts the whole fall on the author, and there are two ways to get it wrong that inspecting the keyframes will not reveal.

**Joints are not the body.** The grounding solve targets the toe *joint*, which sits inside the foot. In the rest pose the sole is already ~3 mm below it, and a bent-knee crouch flexes it a couple of centimetres further — so every clip in the library sits slightly *in* the deck, by design and harmlessly (0.4 cm for a jab, 2.4 cm at the worst, on `block_low`). What that tolerance also hides is a foot that is genuinely buried: an ankle can be a comfortable 8 cm above the deck while the foot around it is not.

**Frames between keyframes are not interpolations of their heights.** three.js slerps the rotations and lerps the hips path independently. Pair a large `_rootRot` with a large `_root.y` drop across one segment and the limbs sweep on an arc the two endpoints say nothing about. The `ko` fall did exactly this: every keyframe passed every check, and the right foot still ploughed **14 cm** through the deck for most of a second, because the legs were authored to trail the body while the hips rotated ~88° about a point the legs had to swing clear of.

The fix was in the **leg fold**: the hips flex harder through the middle of the fall so the trailing foot lifts, the way a real body's legs do on the way down.

So: when a clip authors its own root motion, the legs are load-bearing, and **`tools/pose_check.py` check 7 is what proves it** — it skins the real mesh (vertex weights, joint matrices, inverse bind matrices) and evaluates the true lowest vertex at every other frame of every clip, not at keyframes and not on joints.

### A fall is not a descent

The floor check has an opposite, and passing one while failing the other is what a body that *sinks* looks like.

The first `ko` that cleared the deck did so by tipping backwards about planted feet at a constant rate and stopping in mid air — visually, a fighter sinking to the ground standing on both feet. Every keyframe was legal. Nothing went through the floor. It just did not look like anything falling, because two things were missing and neither is a floor problem:

- **Acceleration.** The hips have to speed up. In the current clip they drop 0.9 cm per frame while the knees buckle, then 2.4, then 4.7, and then stop dead at the landing (f26) with a 7-frame settle behind it. A constant rate is an elevator; a rate that grows is gravity. `tools/pose_render.py --trace` prints exactly this column, which is how it is tuned.
- **Contact.** The body has to end up *on* the deck. Check 7 now measures the terminal clip's last frame against the floor and fails it if the skin settles more than a few centimetres above — the same measurement as the penetration check, read from the other side.

The legs are what buys both. Feet that stay planted give the body nothing to rotate about but the ankles, which is why the old fall could only descend; folding the knees hard through the middle of the fall lifts them clear, lets the hips travel back as well as down, and leaves the fighter landing on the back and shoulders with the legs following.

### Action-to-clip binding

Clip name **is** the action name from [`adamant_spec.md`](adamant_spec.md) §2 — `jab`, `cross`, `hook`, `block_high`, `block_low`, `dodge_left`, `dodge_right`, `idle`. The renderer looks clips up by the action string with no mapping table, so a rules-table rename is also a clip rename.

The other six are named after **events and states, not actions**, and the renderer reaches them from `tick.events` rather than `tick.moves`. This is what lets the eight action clips keep their 1:1 naming rule:

| Clip | Played when |
|---|---|
| `stance` | The clip queue is empty — a seamless loop, the visual origin every other clip returns to |
| `hit_react` | A `hit` event lands on this fighter |
| `riposte` | A `dodged` event where this fighter is the defender |
| `ko` | A `ko` event. Terminal, and the only clip exempt from returning to `STANCE` |
| `exhausted` | An `exhausted` event (spec §3) for this fighter — played *instead of* `idle` |
| `void` | A `void` event (spec §4.6) for this fighter — played *instead of* the submitted action's clip |

**Fourteen clips per fighter.** `agent_b` is `agent_a` mirrored across X, so that is fourteen to author, not twenty-eight.

Two of those exist because the renderer must never show what the agent *chose* when the platform did something else with it:

- **`idle` is distinct from `stance`**, at the cost of one clip. `idle` is a choice with a cost and a payoff — +20 stamina, zero defense. If it looked like standing still, the most legible piece of strategy in the game (a fighter deciding to recover rather than fight) would be invisible, and the stamina bar would move for no reason a spectator could see.
- **`exhausted` and `void` are the two outcomes where the chosen action and what happened differ.** Rendering either as its own action clip would lie: a voided jab that plays a jab reads as a miss or a block, not as the cap firing. `exhausted` slumps; `void` loads the lead hand, jams it a third of the way out, and drops back to guard — an aborted wind-up, visibly not a punch.

### Directional clips are named from the fighter's own perspective

`dodge_left` moves the fighter to *its* left, which is screen-right for the mirrored corner. Author once for `agent_a` and let the mirror handle `agent_b`; do not author a second set.
