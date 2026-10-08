# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10"]
# ///
"""Turn transparent per-state PNG sequences into looping app assets.

For every ``<frames>/<state>/*.png`` this writes:
  * ``<bot>_<state>.gif``  - 1-bit alpha GIF (universal, slightly crunchy edges)
  * ``<bot>_<state>.png``  - APNG with full 8-bit alpha (smooth edges on any
    background; ImageIO / SwiftUI play it natively on Apple platforms)

Usage:
    uv run make_loops.py frames ../Resources/Squirrel --name sammysquirrel --apng-size 192 --no-gif
"""

import argparse
from pathlib import Path

from PIL import Image, PngImagePlugin

FPS = 24
TRANSPARENT_INDEX = 255  # palette slot reserved for "clear"; colors use 0..254


def load_frames(state_dir: Path, size: int) -> list[Image.Image]:
    frames = [Image.open(p).convert('RGBA') for p in sorted(state_dir.glob('*.png'))]
    if not frames:
        raise SystemExit(f'no frames in {state_dir}')
    if frames[0].width != size:
        frames = [f.resize((size, size), Image.Resampling.LANCZOS) for f in frames]
    return frames


def shared_palette(frames: list[Image.Image]) -> Image.Image:
    """One palette for the whole loop, so colors don't shimmer frame to frame."""
    sample = frames[:: max(1, len(frames) // 8)]
    w, h = sample[0].size
    strip = Image.new('RGB', (w * len(sample), h))
    for i, frame in enumerate(sample):
        strip.paste(frame.convert('RGB'), (i * w, 0))
    return strip.quantize(colors=TRANSPARENT_INDEX, method=Image.Quantize.MEDIANCUT)


def gif_frame(frame: Image.Image, palette: Image.Image) -> Image.Image:
    indexed = frame.convert('RGB').quantize(palette=palette, dither=Image.Dither.NONE)
    clear = frame.getchannel('A').point(lambda a: 255 if a < 128 else 0)
    indexed.paste(TRANSPARENT_INDEX, mask=clear)
    return indexed


def write_gif(frames: list[Image.Image], path: Path) -> None:
    palette = shared_palette(frames)
    indexed = [gif_frame(f, palette) for f in frames]
    indexed[0].save(
        path,
        save_all=True,
        append_images=indexed[1:],
        duration=round(1000 / FPS),
        loop=0,
        transparency=TRANSPARENT_INDEX,
        disposal=2,
        optimize=False,
    )


def write_apng(frames: list[Image.Image], path: Path) -> None:
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=round(1000 / FPS),
        loop=0,
        disposal=PngImagePlugin.Disposal.OP_BACKGROUND,
        blend=PngImagePlugin.Blend.OP_SOURCE,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('frames', type=Path, help='directory containing one subdirectory of PNGs per state')
    parser.add_argument('out', type=Path)
    parser.add_argument('--gif-size', type=int, default=320)
    parser.add_argument('--apng-size', type=int, default=512)
    parser.add_argument('--no-gif', action='store_true', help='only write the APNGs')
    parser.add_argument('--name', help='file prefix (default: the bot folder name)')
    args = parser.parse_args()

    prefix = args.name or args.frames.resolve().parent.name
    args.out.mkdir(parents=True, exist_ok=True)
    for state_dir in sorted(p for p in args.frames.iterdir() if p.is_dir()):
        stem = args.out / f'{prefix}_{state_dir.name}'
        if not args.no_gif:
            write_gif(load_frames(state_dir, args.gif_size), stem.with_suffix('.gif'))
        write_apng(load_frames(state_dir, args.apng_size), stem.with_suffix('.png'))
        print(f'{state_dir.name}: {stem}')


if __name__ == '__main__':
    main()
