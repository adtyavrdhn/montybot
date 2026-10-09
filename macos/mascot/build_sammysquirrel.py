"""Build SammySquirrel: the Meshy flying-squirrel mascot, painted, rigged and animated.

Run inside Blender (MCP exec or ``blender -b --python``). Idempotent.

The Meshy FBX is a single fused, untextured, unrigged mesh, so this script:
  1. paints vertex colors by region (matching the concept art),
  2. builds a small armature and assigns weights from spatial rules
     (automatic weights bleed badly on fused AI meshes),
  3. keys the four bot states using the shared FX/stage helpers.

Landmarks are in the FBX's native coordinates (z up, facing -Y, centered at
the origin) and were read off orthographic renders.
"""

import importlib
import math
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sammy_common as mc

importlib.reload(mc)  # pick up edits when re-run inside a live Blender session

FBX = HERE / 'meshy_squirrel.fbx'
STATES = {
    'idle': (1, 48),
    'ack': (61, 36),
    'thinking': (111, 72),
    'question': (196, 72),
    'success': (281, 60),
    'failed': (351, 60),
}
Z_OFF = 0.952  # lifts the feet onto z=0

COLORS = {
    'fawn': '#d9a46c',
    'cream': mc.PALETTE['ivory'],
    'eye': mc.PALETTE['paraffin'],
    'shine': mc.PALETTE['white'],
    'mouth': '#5b1626',
    'tongue': '#ff7a85',
    'blush': mc.PALETTE['persimmon'],
    'ear': '#f4b4ae',
    'accent': mc.PALETTE['lithium'],
}

# --- landmarks (x, z) in front view ---
EYES = [(-0.50, 0.23), (0.035, 0.17)]
NOSE = (-0.24, 0.17)
MOUTH = (-0.25, 0.04)
CHEEKS = [(-0.71, 0.06), (0.13, 0.02)]
EARS = [(-0.63, 0.65), (0.19, 0.57)]
PAWS = [(-0.51, -0.37), (-0.12, -0.39)]


def smoothstep(a: float, b: float, x: float) -> float:
    t = min(1.0, max(0.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


def near(co: Vector, center: tuple[float, float], rx: float, rz: float | None = None) -> bool:
    rz = rx if rz is None else rz
    return ((co.x - center[0]) / rx) ** 2 + ((co.z - center[1]) / rz) ** 2 <= 1.0


def tailness(co: Vector) -> float:
    return smoothstep(0.05, 0.25, co.y)


def headness(co: Vector) -> float:
    return smoothstep(-0.32, -0.14, co.z) * (1 - tailness(co))


def mohawkness(co: Vector) -> float:
    in_x = smoothstep(-0.39, -0.35, co.x) * (1 - smoothstep(-0.06, -0.02, co.x))
    return headness(co) * smoothstep(0.5, 0.6, co.z) * in_x


# ---------------------------------------------------------------- paint
def region(co: Vector, n: Vector) -> str:
    """Concept-art color for one vertex. First matching rule wins."""
    front = n.y < -0.2
    tail, head = tailness(co) > 0.5, headness(co) > 0.5
    if head and front:
        for ex, ez in EYES:
            if near(co, (ex - 0.03, ez + 0.035), 0.028):
                return 'shine'
            if near(co, (ex, ez), 0.085, 0.1):
                return 'eye'
        if near(co, NOSE, 0.05, 0.035):
            return 'eye'
        if near(co, MOUTH, 0.085, 0.06):
            return 'tongue' if co.z < MOUTH[1] - 0.005 else 'mouth'
        if any(near(co, c, 0.075, 0.055) for c in CHEEKS):
            return 'blush'
        if any(near(co, c, 0.075, 0.09) for c in EARS):
            return 'ear'
    if mohawkness(co) > 0.5 and co.z > 0.6:  # stricter than the weights: keep pink off the scalp
        return 'accent'
    if (
        not tail
        and front
        and -0.42 < co.x < -0.18
        and -0.50 < co.z < -0.30
        and not any(near(co, p, 0.07) for p in PAWS)
    ):
        return 'accent'  # chest pendant (logomark nod)
    if not tail and -0.30 < co.z < -0.20 and -0.8 < co.x < 0.2 and co.y < 0.0:
        return 'accent'  # collar
    forehead_stripe = abs(co.x - NOSE[0]) < 0.07 and co.z > NOSE[1] - 0.02
    if head and n.y < -0.35 and co.z < 0.36 and co.x < 0.28 and not forehead_stripe:
        return 'cream'
    if not tail and not head and n.y < -0.4 and -0.62 < co.x < 0.12 and -0.82 < co.z < -0.3:
        return 'cream'  # belly
    return 'fawn'


def tail_tip(co: Vector) -> float:
    """0..1 fawn->cream gradient toward the top of the tail curl."""
    return tailness(co) * smoothstep(0.45, 0.8, co.z)


def paint(mesh: bpy.types.Mesh) -> None:
    lut = {name: mc.rgba(hex_color) for name, hex_color in COLORS.items()}
    fawn, cream = Vector(lut['fawn']), Vector(lut['cream'])
    attr = mesh.color_attributes.new('Col', 'FLOAT_COLOR', 'POINT')
    flat: list[float] = []
    for v in mesh.vertices:
        name = region(v.co, v.normal)
        flat.extend(fawn.lerp(cream, tail_tip(v.co)) if name == 'fawn' else lut[name])
    attr.data.foreach_set('color', flat)
    mesh.color_attributes.active_color = attr


def vertex_color_material() -> bpy.types.Material:
    mat = mc.material('squirrel', COLORS['fawn'], rough=0.6)
    nodes = mat.node_tree.nodes
    bsdf = nodes['Principled BSDF']
    col = nodes.new('ShaderNodeVertexColor')
    col.layer_name = 'Col'
    mat.node_tree.links.new(col.outputs['Color'], bsdf.inputs['Base Color'])
    bsdf.inputs['Sheen Weight'].default_value = 0.35  # soft flocked-vinyl look from the concept
    return mat


# ---------------------------------------------------------------- rig
BONES = {  # name: (head, tail, parent), native coords
    'body': ((-0.2, -0.25, -0.9), (-0.25, -0.35, -0.22), None),
    'head': ((-0.25, -0.38, -0.22), (-0.25, -0.38, 0.55), 'body'),
    'mohawk': ((-0.2, -0.3, 0.55), (-0.2, -0.3, 0.92), 'head'),
    'tail1': ((0.15, 0.15, -0.65), (0.35, 0.6, -0.1), 'body'),
    'tail2': ((0.35, 0.6, -0.1), (0.3, 0.55, 0.8), 'tail1'),
}


def lifted(co) -> Vector:
    return Vector(co) + Vector((0, 0, Z_OFF))


def build_armature(coll) -> bpy.types.Object:
    arm_obj = mc.new_obj(coll, 'squirrel_rig', bpy.data.armatures.new('squirrel_rig'))
    bpy.context.view_layer.objects.active = arm_obj
    with bpy.context.temp_override(active_object=arm_obj, object=arm_obj):
        bpy.ops.object.mode_set(mode='EDIT')
        edit = arm_obj.data.edit_bones
        for name, (head, tail, parent) in BONES.items():
            bone = edit.new(name)
            bone.head, bone.tail, bone.roll = lifted(head), lifted(tail), 0.0
            if parent:
                bone.parent = edit[parent]
        bpy.ops.object.mode_set(mode='OBJECT')
    for pb in arm_obj.pose.bones:
        pb.rotation_mode = 'XYZ'
    return arm_obj


def weights(co: Vector) -> dict[str, float]:
    t, h = tailness(co), headness(co)
    upper = smoothstep(-0.25, 0.25, co.z)
    m = mohawkness(co)
    return {'tail1': t * (1 - upper), 'tail2': t * upper, 'mohawk': m, 'head': h - m, 'body': 1 - t - h}


def skin(mesh_obj: bpy.types.Object, arm_obj: bpy.types.Object) -> None:
    groups = {name: mesh_obj.vertex_groups.new(name=name) for name in BONES}
    buckets: dict[tuple[str, float], list[int]] = {}
    for v in mesh_obj.data.vertices:
        for name, w in weights(v.co).items():
            if w > 0.005:
                buckets.setdefault((name, round(w, 2)), []).append(v.index)
    for (name, w), ids in buckets.items():
        groups[name].add(ids, w, 'REPLACE')
    mod = mesh_obj.modifiers.new('rig', 'ARMATURE')
    mod.object = arm_obj
    mesh_obj.parent = arm_obj


# ---------------------------------------------------------------- build
def import_squirrel(coll) -> bpy.types.Object:
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=str(FBX))
    obj = next(o for o in bpy.data.objects if o not in before and o.type == 'MESH')
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    coll.objects.link(obj)
    obj.name = 'squirrel'
    obj.data.shade_smooth()
    paint(obj.data)  # paint + weight in native coords, then lift onto the floor
    return obj


mc.wipe_scene()
SCENE = bpy.context.scene
COLL = mc.new_collection('SammySquirrel')

squirrel = import_squirrel(COLL)
squirrel.data.materials.clear()
squirrel.data.materials.append(vertex_color_material())
rig = build_armature(COLL)
skin(squirrel, rig)
squirrel.data.transform(Matrix.Translation((0, 0, Z_OFF)))

mc.build_stage(COLL, cam_loc=(0.0, -5.8, 1.6), target=(-0.05, 0, 1.5), lens=56, ground_radius=1.3)
fx = mc.build_fx(
    COLL,
    None,
    qmark_loc=(0.8, -0.5, 2.0),
    check_loc=(-0.2, -0.5, 2.6),
    sparkle_spots=[(-0.95, 2.1), (0.55, 2.2), (-0.7, 2.55), (0.4, 2.65), (-1.05, 1.5), (0.85, 1.4)],
    sparkle_y=-0.5,
    sparkle_size=0.14,
    dots_loc=(0.45, -0.5, 2.05),
)

# ---------------------------------------------------------------- animation
POSE = rig.pose.bones
CH = mc.Channels()
CH.add(rig, 'location', (0, 0, 0))
CH.add(rig, 'scale', (1, 1, 1))
CH.add(rig, 'rotation_euler', (0, 0, 0))
for bone in BONES:
    CH.add(POSE[bone], 'rotation_euler', (0, 0, 0))
fx.register(CH)


def squish(frame: int, xy: float, z: float, lift: float = 0.0) -> None:
    mc.key(rig, 'scale', frame, (xy, xy, z))
    mc.key(rig, 'location', frame, (0, 0, lift))


def head_to(frame: int, nod: float = 0.0, turn: float = 0.0, tilt: float = 0.0) -> None:
    """nod > 0 looks down, turn twists left/right, tilt rolls the head sideways."""
    mc.key(POSE['head'], 'rotation_euler', frame, (nod, turn, tilt))


def lean(frame: int, fwd: float = 0.0, side: float = 0.0) -> None:
    mc.key(POSE['body'], 'rotation_euler', frame, (fwd, 0, side))


def wag(start: int, end: int, period: int, amp: float) -> None:
    """Tail swish with tail2 lagging half a beat behind tail1 (follow-through)."""
    half = period // 2
    for i, f in enumerate(range(start, end, half)):
        sign = 1 if i % 2 == 0 else -1
        mc.key(POSE['tail1'], 'rotation_euler', f, (0, 0, sign * amp * 0.6))
        mc.key(POSE['tail2'], 'rotation_euler', f + half // 2, (0, 0, sign * amp))


def mohawk(start: int, swings: list[tuple[int, float, float]]) -> None:
    for off, fwd, side in swings:
        mc.key(POSE['mohawk'], 'rotation_euler', start + off, (fwd, 0, side))


def anim_idle(s: int, n: int) -> None:
    squish(s + n // 2, 1.015, 0.985)
    head_to(s + 12, turn=0.06, tilt=0.03)
    head_to(s + 36, turn=-0.06, tilt=-0.03)
    wag(s + 4, s + n - 8, 24, 0.12)
    mohawk(s, [(14, 0.06, 0.05), (38, -0.04, -0.05)])


def anim_ack(s: int, n: int) -> None:
    squish(s + 3, 1.06, 0.93)
    squish(s + 8, 0.97, 1.05, lift=0.08)
    squish(s + 13, 1.02, 0.98)
    squish(s + 18, 1.0, 1.0)
    head_to(s + 7, nod=-0.12)
    head_to(s + 13, nod=0.22)
    head_to(s + 19, nod=-0.04)
    head_to(s + 24, nod=0.15)
    head_to(s + 30, nod=0.0)
    mohawk(s, [(7, 0.25, 0), (13, -0.35, 0), (19, 0.25, 0), (24, -0.15, 0), (30, 0.05, 0)])
    wag(s + 4, s + n - 6, 8, 0.3)
    fx.ping(s, radius=1.5)


def anim_thinking(s: int, n: int) -> None:
    """Pondering: gaze up and off to the side while the typing dots bounce."""
    hold = n - 10
    for f, nod, turn in ((s + 10, -0.16, 0.2), (s + 30, -0.12, 0.16), (s + 50, -0.17, 0.22), (s + hold, -0.16, 0.2)):
        head_to(f, nod=nod, turn=turn, tilt=-0.08)
    squish(s + 24, 1.012, 0.99)
    squish(s + 48, 1.0, 1.0)
    mohawk(s, [(10, -0.1, 0.06), (30, 0.05, -0.04), (50, -0.08, 0.05), (hold, -0.06, 0.0)])
    wag(s + 8, s + hold, 24, 0.12)
    fx.thinking(s, n)


def anim_question(s: int, n: int) -> None:
    hold = n - 10
    for f, tilt in ((s + 10, 0.3), (s + 32, 0.24), (s + 50, 0.32), (s + hold, 0.3)):
        head_to(f, nod=-0.05, turn=0.12, tilt=tilt)
    lean(s + 10, fwd=0.06, side=-0.05)
    lean(s + hold, fwd=0.06, side=-0.05)
    mohawk(s, [(10, 0, -0.3), (16, 0, 0.12), (22, 0, -0.08), (hold, 0, -0.05)])
    wag(s + 12, s + hold, 24, 0.1)
    fx.question(s, n)


def anim_success(s: int, n: int) -> None:
    hop = [  # (frame offset, scale xy, scale z, lift)
        (5, 1.08, 0.9, 0.0),
        (11, 0.95, 1.07, 0.6),
        (16, 1.0, 1.0, 0.75),
        (22, 1.1, 0.9, 0.0),
        (27, 1.0, 1.0, 0.0),
        (33, 0.97, 1.04, 0.28),
        (38, 1.05, 0.94, 0.0),
        (43, 1.0, 1.0, 0.0),
    ]
    for off, xy, z, lift in hop:
        squish(s + off, xy, z, lift)
    # full spin on the big hop to show off the tail; 2*pi reads as neutral, so hold it to the end
    mc.key(rig, 'rotation_euler', s + 9, (0, 0, 0))
    mc.key(rig, 'rotation_euler', s + 21, (0, 0, 2 * math.pi))
    mc.key(rig, 'rotation_euler', s + n - 1, (0, 0, 2 * math.pi))
    head_to(s + 5, nod=0.1)
    head_to(s + 12, nod=-0.2)
    head_to(s + 22, nod=0.08)
    head_to(s + 30, nod=-0.1)
    head_to(s + 46, nod=0.0)
    mohawk(
        s,
        [
            (5, -0.2, 0),
            (11, 0.4, 0),
            (16, -0.1, 0.1),
            (22, -0.35, 0),
            (27, 0.25, -0.1),
            (33, -0.1, 0),
            (38, 0.12, 0),
            (44, -0.05, 0),
        ],
    )
    wag(s + 4, s + n - 6, 6, 0.4)
    fx.celebrate(s)


def anim_failed(s: int, n: int) -> None:
    """Aw, shucks: slumps, shakes its head no, tail and mohawk wilt, then perks back up."""
    slump = n - 14
    squish(s + 10, 1.05, 0.93)
    squish(s + slump, 1.05, 0.93)
    for f, turn in ((s + 8, 0.0), (s + 13, 0.28), (s + 19, -0.28), (s + 25, 0.2), (s + 31, -0.12), (s + 37, 0.0)):
        head_to(f, nod=0.3, turn=turn, tilt=0.06)
    head_to(s + slump, nod=0.3, tilt=0.06)
    for bone, droop in (('tail1', -0.18), ('tail2', -0.3)):  # negative x lowers the tail back, away from the body
        mc.key(POSE[bone], 'rotation_euler', s + 12, (droop, 0, 0))
        mc.key(POSE[bone], 'rotation_euler', s + slump, (droop, 0, 0))
    mohawk(s, [(10, 0.55, 0.15), (16, 0.45, 0.1), (slump, 0.5, 0.12)])


mc.animate_states(
    SCENE,
    CH,
    STATES,
    {
        'idle': anim_idle,
        'ack': anim_ack,
        'thinking': anim_thinking,
        'question': anim_question,
        'success': anim_success,
        'failed': anim_failed,
    },
)
result = {'verts': len(squirrel.data.vertices), 'states': STATES}
