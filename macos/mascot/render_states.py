"""Render every bot state from a sammy .blend.

Default: one MP4 + poster PNG per state (dark stage, for previews/README).
``--frames``: transparent RGBA PNG sequences per state for app loops; the
ground disc is hidden and the duplicated closing neutral frame is dropped so
the loop is seamless. Feed the result to ``assets/make_loops.py``.

Usage:
    blender -b assets/<bot>/<bot>.blend --python assets/render_states.py
    blender -b assets/<bot>/<bot>.blend --python assets/render_states.py -- --frames
"""

import json
import shutil
import sys
from pathlib import Path

import bpy

scene = bpy.context.scene
render = scene.render
blend = Path(bpy.data.filepath)
states = json.loads(scene['sammy_states'])
script_args = sys.argv[sys.argv.index('--') + 1 :] if '--' in sys.argv else []
transparent_frames = '--frames' in script_args


def render_videos(out_dir: Path) -> None:
    for state, (start, length) in states.items():
        name = f'{blend.stem}_{state}'
        scene.frame_start, scene.frame_end = start, start + length - 1

        render.image_settings.media_type = 'VIDEO'
        render.image_settings.file_format = 'FFMPEG'
        render.ffmpeg.format, render.ffmpeg.codec = 'MPEG4', 'H264'
        render.ffmpeg.constant_rate_factor = 'HIGH'
        render.filepath = str(out_dir / f'{name}_')
        bpy.ops.render.render(animation=True)
        for produced in out_dir.glob(f'{name}_*.mp4'):
            produced.replace(out_dir / f'{name}.mp4')

        render.image_settings.media_type = 'IMAGE'
        render.image_settings.file_format = 'PNG'
        scene.frame_set(start + length // 3)
        render.filepath = str(out_dir / f'{name}.png')
        bpy.ops.render.render(write_still=True)


def render_transparent_frames(out_dir: Path) -> None:
    render.film_transparent = True
    render.resolution_x = render.resolution_y = 512
    render.image_settings.media_type = 'IMAGE'
    render.image_settings.file_format = 'PNG'
    render.image_settings.color_mode = 'RGBA'
    ground = bpy.data.objects.get('ground')
    if ground:
        ground.hide_render = True
    for state, (start, length) in states.items():
        state_dir = out_dir / state
        shutil.rmtree(state_dir, ignore_errors=True)
        # last frame == first frame (both neutral): drop it so the loop doesn't stutter
        scene.frame_start, scene.frame_end = start, start + length - 2
        render.filepath = str(state_dir / '####')
        bpy.ops.render.render(animation=True)


if transparent_frames:
    render_transparent_frames(blend.parent / 'frames')
else:
    out = blend.parent / 'renders'
    out.mkdir(exist_ok=True)
    render_videos(out)
