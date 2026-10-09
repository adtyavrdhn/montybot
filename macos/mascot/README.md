# Sammy's squirrel

This folder holds the source for `Resources/Squirrel/sammysquirrel_<clip>.png`. Those files are looping 192px APNGs with
full alpha, which `SammySquirrel.swift` plays. To regenerate them, run `make mascot` from `macos/`. It needs Blender
(set `BLENDER=` if it isn't in `/Applications`) and uv.

| Clip       | When (SammyMark.Mood)                 | Motion                                          |
|------------|---------------------------------------|-------------------------------------------------|
| `idle`     | idle, and after done/failed settle    | breathing, head sway, lazy tail swish           |
| `ack`      | once, on starting work                | bounce, double nod, aqua ping ring              |
| `thinking` | working                               | gazes up, typing dots bounce                    |
| `question` | waiting (a question or an approval)   | head tilt and lean, bouncing `?`                |
| `success`  | once, on done                         | double hop with a spin, check mark, sparkles    |
| `failed`   | once, on failed                       | slumps, shakes its head, tail and mohawk wilt   |

Every clip starts and ends on the same pose, which is why the app can cut between them at any loop boundary.

- `meshy_squirrel.fbx`: the source mesh, generated in Meshy from the concept art. It's untextured, unrigged and fused
  into a single piece.
- `build_sammysquirrel.py`: builds the scene. It paints vertex colors by region, adds a 5-bone rig whose weights come
  from spatial rules (automatic weights bleed badly on a fused AI mesh), and keys every clip. The landmarks at the top
  of the file are in the FBX's own coordinates. If you swap the mesh, measure them again.
- `sammy_common.py`: brand palette (from pydantic.dev's CSS), stage, keyframe channels, and the effects.
- `render_states.py`: renders each clip. With `-- --frames` it writes transparent PNG frames and drops each clip's
  repeated last frame, so the loops don't stutter.
- `make_loops.py`: turns the frames into APNGs, and GIFs too unless you pass `--no-gif`.

`frames/`, `renders/` and the `.blend` are build output, and they're gitignored.
