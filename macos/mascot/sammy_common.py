"""Shared Blender helpers for Sammy mascot builds: brand palette, materials,
keyframe channels, stage (camera/lights/world) and state FX.

Import from a build script running inside Blender:

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import sammy_common as mc
"""

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import bmesh
import bpy
from mathutils import Vector

# pydantic.dev CSS tokens (--color-*)
PALETTE = {
    'lithium': '#e520e9',
    'paraffin': '#1d0214',
    'darkpurple': '#36182d',
    'ivory': '#fbffea',
    'aqua': '#77ffd8',
    'persimmon': '#ff6550',
    'white': '#ffffff',
}

FPS = 24
Vec3 = tuple[float, float, float]


# ---------------------------------------------------------------- color / materials
def rgba(hex_color: str) -> tuple[float, float, float, float]:
    """sRGB hex -> linear RGBA (what Blender color sockets expect)."""

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    h = hex_color.lstrip('#')
    return tuple(lin(int(h[i : i + 2], 16) / 255) for i in (0, 2, 4)) + (1.0,)


def material(name: str, hex_color: str, rough: float = 0.5, glow: float = 0.0) -> bpy.types.Material:
    mat = bpy.data.materials.new(name)
    if mat.node_tree is None:
        mat.use_nodes = True
    bsdf = mat.node_tree.nodes['Principled BSDF']
    bsdf.inputs['Base Color'].default_value = rgba(hex_color)
    bsdf.inputs['Roughness'].default_value = rough
    if glow:
        bsdf.inputs['Emission Color'].default_value = rgba(hex_color)
        bsdf.inputs['Emission Strength'].default_value = glow
    mat.diffuse_color = rgba(hex_color)
    return mat


# ---------------------------------------------------------------- scene objects
def wipe_scene() -> None:
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    pools = (
        bpy.data.meshes,
        bpy.data.curves,
        bpy.data.materials,
        bpy.data.actions,
        bpy.data.lights,
        bpy.data.cameras,
        bpy.data.armatures,
    )
    for pool in pools:
        for block in list(pool):
            if block.users == 0:
                pool.remove(block)


def new_collection(name: str) -> bpy.types.Collection:
    coll = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(coll)
    return coll


def new_obj(coll, name, data, parent=None, loc=(0, 0, 0), rot=(0, 0, 0)) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, data)
    coll.objects.link(obj)
    obj.parent = parent
    obj.location = loc
    obj.rotation_euler = rot
    return obj


def to_mesh(name: str, bm: bmesh.types.BMesh) -> bpy.types.Mesh:
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    return mesh


def ellipsoid(name, radius=1.0, scale=(1, 1, 1)) -> bpy.types.Mesh:
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=24, radius=radius)
    bmesh.ops.scale(bm, vec=scale, verts=bm.verts)
    for face in bm.faces:
        face.smooth = True
    return to_mesh(name, bm)


def torus(name, major: float, minor: float) -> bpy.types.Mesh:
    bm = bmesh.new()
    ring, tube = 48, 12
    grid = [
        [
            bm.verts.new(
                (
                    (major + minor * math.cos(b)) * math.cos(a),
                    (major + minor * math.cos(b)) * math.sin(a),
                    minor * math.sin(b),
                )
            )
            for b in (2 * math.pi * j / tube for j in range(tube))
        ]
        for a in (2 * math.pi * i / ring for i in range(ring))
    ]
    for i in range(ring):
        for j in range(tube):
            quad = (grid[i][j], grid[(i + 1) % ring][j], grid[(i + 1) % ring][(j + 1) % tube], grid[i][(j + 1) % tube])
            bm.faces.new(quad).smooth = True
    return to_mesh(name, bm)


def star(name, outer=1.0, inner=0.28, depth=0.12) -> bpy.types.Mesh:
    """Four-point sparkle in the XZ plane (faces the camera)."""
    bm = bmesh.new()
    ring = [
        bm.verts.new(
            (
                (outer if i % 2 == 0 else inner) * math.cos(math.pi / 4 * i),
                0,
                (outer if i % 2 == 0 else inner) * math.sin(math.pi / 4 * i),
            )
        )
        for i in range(8)
    ]
    ext = bmesh.ops.extrude_face_region(bm, geom=[bm.faces.new(ring)])
    moved = [v for v in ext['geom'] if isinstance(v, bmesh.types.BMVert)]
    bmesh.ops.translate(bm, vec=(0, depth, 0), verts=moved)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return to_mesh(name, bm)


def poly_curve(coll, name, points: Sequence[Vec3], mat, parent=None, loc=(0, 0, 0), bevel=0.03) -> bpy.types.Object:
    curve = bpy.data.curves.new(name, 'CURVE')
    curve.dimensions, curve.bevel_depth, curve.use_fill_caps = '3D', bevel, True
    curve.bevel_resolution = 4
    spline = curve.splines.new('POLY')
    spline.points.add(len(points) - 1)
    for pt, co in zip(spline.points, points, strict=True):
        pt.co = (*co, 1)
    curve.materials.append(mat)
    return new_obj(coll, name, curve, parent, loc)


def mesh_obj(coll, name, mesh, mat, parent=None, loc=(0, 0, 0)) -> bpy.types.Object:
    mesh.materials.append(mat)
    return new_obj(coll, name, mesh, parent, loc)


# ---------------------------------------------------------------- keyframing
@dataclass
class Channels:
    """Registry of animated properties and their neutral values.

    Every state clip keys all channels to neutral at its first and last frame,
    so clips loop cleanly and chain in any order.
    """

    items: list[tuple[bpy.types.bpy_struct, str, object]] = field(default_factory=list)

    def add(self, owner, path: str, neutral) -> None:
        self.items.append((owner, path, neutral))

    def neutral(self, frame: int) -> None:
        for owner, path, value in self.items:
            key(owner, path, frame, value)


def key(owner, path: str, frame: int, value) -> None:
    setattr(owner, path, value)
    owner.keyframe_insert(data_path=path, frame=frame)


def animate_states(
    scene, channels: Channels, states: dict[str, tuple[int, int]], animators: dict[str, Callable[[int, int], None]]
) -> None:
    scene.timeline_markers.clear()
    for name, (start, length) in states.items():
        channels.neutral(start)
        channels.neutral(start + length - 1)
        animators[name](start, length)
        scene.timeline_markers.new(name, frame=start)
    scene['sammy_states'] = json.dumps(states)
    scene.frame_start = 1
    scene.frame_end = max(s + n - 1 for s, n in states.values())
    scene.frame_set(1)


# ---------------------------------------------------------------- stage
def aim(obj, target: Vector) -> None:
    obj.rotation_euler = (target - obj.location).to_track_quat('-Z', 'Y').to_euler()


def build_stage(coll, cam_loc: Vec3, target: Vec3, lens: float, ground_radius: float) -> None:
    """Camera, three-point brand lighting, paraffin world, Standard view transform."""
    scene = bpy.context.scene
    target_v = Vector(target)
    cam = new_obj(coll, 'Camera', bpy.data.cameras.new('Camera'), loc=cam_loc)
    cam.data.lens = lens
    aim(cam, target_v)
    scene.camera = cam

    sun = new_obj(coll, 'key_sun', bpy.data.lights.new('key_sun', 'SUN'), rot=(math.radians(48), 0, math.radians(-28)))
    sun.data.energy = 3.2
    rim = new_obj(coll, 'rim_aqua', bpy.data.lights.new('rim_aqua', 'AREA'), loc=(-1.6, 2.4, 4.6))
    rim.data.energy, rim.data.size, rim.data.color = 350, 2.5, rgba(PALETTE['aqua'])[:3]
    aim(rim, target_v)
    fill = new_obj(coll, 'fill', bpy.data.lights.new('fill', 'AREA'), loc=(3.2, -3.2, 1.6))
    fill.data.energy, fill.data.size = 160, 3
    aim(fill, target_v)

    ground = mesh_obj(
        coll,
        'ground',
        ellipsoid('ground', 1.0, (ground_radius, ground_radius, 0.02)),
        material('ground', PALETTE['darkpurple'], rough=0.9),
    )
    ground.location = (0, 0, -0.01)

    world = scene.world or bpy.data.worlds.new('World')
    scene.world = world
    if world.node_tree is None:
        world.use_nodes = True
    world.node_tree.nodes['Background'].inputs['Color'].default_value = rgba(PALETTE['paraffin'])

    scene.render.engine = 'BLENDER_EEVEE'
    scene.render.resolution_x = scene.render.resolution_y = 640
    scene.render.fps = FPS
    scene.view_settings.view_transform = 'Standard'  # keep brand hex values honest
    scene.eevee.taa_render_samples = 32


# ---------------------------------------------------------------- state FX
@dataclass
class Fx:
    """Effect props: ping ring (ack), typing dots (thinking), bouncing "?" (question), check + sparkles (success)."""

    ring: bpy.types.Object
    ring_alpha: bpy.types.NodeSocket
    qmark: bpy.types.Object
    check: bpy.types.Object
    sparkles: list[bpy.types.Object]
    sparkle_size: float = 0.24
    dots: list[bpy.types.Object] = field(default_factory=list)

    def register(self, channels: Channels) -> None:
        for obj in (self.qmark, self.check, self.ring, *self.sparkles, *self.dots):
            channels.add(obj, 'scale', (0, 0, 0))
        for dot in self.dots:
            channels.add(dot, 'location', tuple(dot.location))
        channels.add(self.qmark, 'location', tuple(self.qmark.location))
        channels.add(self.ring_alpha, 'default_value', 1.0)
        for sp in self.sparkles:
            channels.add(sp, 'location', tuple(sp.location))
            channels.add(sp, 'rotation_euler', (0, 0, 0))

    def ping(self, s: int, radius: float = 2.3) -> None:
        key(self.ring, 'scale', s + 2, (0, 0, 0))
        key(self.ring, 'scale', s + 3, (0.5, 0.5, 1))
        key(self.ring, 'scale', s + 22, (radius, radius, 1))
        key(self.ring, 'scale', s + 23, (0, 0, 0))
        key(self.ring_alpha, 'default_value', s + 3, 1.0)
        key(self.ring_alpha, 'default_value', s + 22, 0.0)

    def thinking(self, s: int, n: int, period: int = 24, hop: float = 0.12) -> None:
        """Chat-style typing indicator: dots pop in, bounce in sequence, pop out."""
        for i, dot in enumerate(self.dots):
            base = Vector(dot.location)
            key(dot, 'scale', s + 3 + 2 * i, (0, 0, 0))
            key(dot, 'scale', s + 8 + 2 * i, (1, 1, 1))
            key(dot, 'scale', s + n - 8, (1, 1, 1))
            key(dot, 'scale', s + n - 3, (0, 0, 0))
            lag = i * period // 6
            for f in range(s + 8 + lag, s + n - 12, period):
                key(dot, 'location', f, tuple(base))
                key(dot, 'location', f + period // 4, tuple(base + Vector((0, 0, hop))))
                key(dot, 'location', f + period // 2, tuple(base))

    def question(self, s: int, n: int) -> None:
        base = Vector(self.qmark.location)
        key(self.qmark, 'scale', s + 6, (0, 0, 0))
        key(self.qmark, 'scale', s + 12, (1.25, 1.25, 1.25))
        key(self.qmark, 'scale', s + 15, (1, 1, 1))
        for f, dz in ((s + 15, 0.0), (s + 25, 0.14), (s + 35, 0.0), (s + 45, 0.14), (s + 55, 0.0)):
            key(self.qmark, 'location', f, tuple(base + Vector((0, 0, dz))))
        key(self.qmark, 'scale', s + n - 12, (1, 1, 1))
        key(self.qmark, 'scale', s + n - 6, (0, 0, 0))

    def celebrate(self, s: int) -> None:
        key(self.check, 'scale', s + 13, (0, 0, 0))
        key(self.check, 'scale', s + 19, (1.35, 1.35, 1.35))
        key(self.check, 'scale', s + 23, (1, 1, 1))
        key(self.check, 'scale', s + 48, (1, 1, 1))
        key(self.check, 'scale', s + 54, (0, 0, 0))
        for i, sp in enumerate(self.sparkles):
            t0 = s + 15 + 2 * i
            start = Vector(sp.location)
            key(sp, 'scale', t0, (0, 0, 0))
            key(sp, 'location', t0, tuple(start))
            key(sp, 'rotation_euler', t0, (0, 0, 0))
            key(sp, 'scale', t0 + 6, (self.sparkle_size,) * 3)
            key(sp, 'scale', t0 + 18, (0, 0, 0))
            key(sp, 'location', t0 + 18, (start.x * 1.35, start.y, start.z + 0.25))
            key(sp, 'rotation_euler', t0 + 18, (0, math.radians(90), 0))


def build_fx(
    coll,
    parent,
    qmark_loc: Vec3,
    check_loc: Vec3,
    sparkle_spots: Sequence[tuple[float, float]],
    sparkle_y: float = -0.3,
    sparkle_size: float = 0.24,
    dots_loc: Vec3 | None = None,
) -> Fx:
    mats = {
        'pink': material('fx_pink', PALETTE['lithium'], glow=1.0),
        'aqua': material('fx_aqua', PALETTE['aqua'], glow=1.0),
        'orange': material('fx_orange', PALETTE['persimmon'], glow=1.0),
        'ring': material('fx_ring', PALETTE['aqua'], glow=1.0),
    }
    mats['ring'].surface_render_method = 'BLENDED'

    qcurve = bpy.data.curves.new('fx_question', 'FONT')
    qcurve.body = '?'
    qcurve.align_x, qcurve.align_y = 'CENTER', 'CENTER'
    qcurve.size, qcurve.extrude, qcurve.bevel_depth = 0.9, 0.06, 0.012
    qcurve.materials.append(mats['aqua'])
    qmark = new_obj(coll, 'fx_question', qcurve, parent, qmark_loc, (math.radians(90), 0, 0))

    check = poly_curve(
        coll,
        'fx_check',
        [(-0.32, 0, 0.02), (-0.1, 0, -0.22), (0.34, 0, 0.3)],
        mats['aqua'],
        parent,
        check_loc,
        bevel=0.075,
    )
    ring = mesh_obj(coll, 'fx_ring', torus('fx_ring', 1.0, 0.03), mats['ring'], parent, (0, 0, 0.03))
    sparkle_mats = (mats['pink'], mats['aqua'], mats['orange'])
    sparkles = [
        mesh_obj(coll, f'fx_sparkle_{i}', star(f'sparkle_{i}'), sparkle_mats[i % 3], parent, (x, sparkle_y, z))
        for i, (x, z) in enumerate(sparkle_spots)
    ]
    dots = []
    if dots_loc is not None:
        dot_mats = (mats['aqua'], mats['pink'], mats['aqua'])
        dots = [
            mesh_obj(
                coll,
                f'fx_dot_{i}',
                ellipsoid(f'dot_{i}', 0.075),
                dot_mats[i],
                parent,
                (dots_loc[0] + 0.24 * i, dots_loc[1], dots_loc[2]),
            )
            for i in range(3)
        ]
    ring_alpha = mats['ring'].node_tree.nodes['Principled BSDF'].inputs['Alpha']
    return Fx(ring, ring_alpha, qmark, check, sparkles, sparkle_size, dots)
