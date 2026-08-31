#!/usr/bin/env python3
"""Turn demo_render.py frames into an animated GIF, with no screen recording.

    python3 tests/ansi_to_gif.py --frames 40 --out ~/Desktop/machine-monitor.gif

A recording of the real dashboard publishes whatever the operator is running, so
the README media is generated from the fictional machine in demo_render.py
instead. Rendering the ANSI directly also avoids a capture: no window chrome, no
cursor, no compression noise on the text, and it is reproducible.

Only the escape sequences this dashboard actually emits are handled -- SGR 0
(reset), 1 (bold), 2 (dim) and 38;5;N (256-colour foreground). Anything else is
skipped rather than guessed at, and `--strict` turns an unhandled code into a
failure so a renderer change cannot silently degrade the output.
"""

import argparse
import os
import re
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SGR = re.compile(r"\x1b\[([0-9;]*)m")
BG = (13, 13, 18)
FG_DEFAULT = (200, 200, 200)


def xterm256(n: int) -> tuple[int, int, int]:
    """The standard xterm 256-colour palette."""
    if n < 16:
        base = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16),
                (36, 114, 200), (188, 63, 188), (17, 168, 205), (229, 229, 229),
                (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67),
                (59, 142, 234), (214, 112, 214), (41, 184, 219), (255, 255, 255)]
        return base[n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return (steps[n // 36], steps[(n // 6) % 6], steps[n % 6])
    v = 8 + (n - 232) * 10
    return (v, v, v)


def parse(line: str, strict: bool) -> list[tuple[str, tuple[int, int, int], bool]]:
    """Split one line into (text, colour, bold) runs."""
    runs, pos = [], 0
    colour, bold, dim = FG_DEFAULT, False, False
    for m in SGR.finditer(line):
        if m.start() > pos:
            runs.append((line[pos:m.start()], colour, bold))
        codes = [c for c in m.group(1).split(";") if c != ""] or ["0"]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == "0":
                colour, bold, dim = FG_DEFAULT, False, False
            elif c == "1":
                bold = True
            elif c == "2":
                dim = True
            elif c == "38" and codes[i + 1:i + 2] == ["5"]:
                colour = xterm256(int(codes[i + 2]))
                i += 2
            elif strict:
                raise SystemExit(f"unhandled SGR code {c!r} in: {line[:60]!r}")
            i += 1
        if dim:
            colour = tuple(int(v * 0.55) for v in colour)
        pos = m.end()
    if pos < len(line):
        runs.append((line[pos:], colour, bold))
    return runs


def render(text: str, font, bold_font, cw: int, ch: int, pad: int, strict: bool) -> Image.Image:
    lines = text.split("\n")
    width = max((len(SGR.sub("", ln)) for ln in lines), default=80)
    img = Image.new("RGB", (width * cw + pad * 2, len(lines) * ch + pad * 2), BG)
    d = ImageDraw.Draw(img)
    for row, line in enumerate(lines):
        x, y = pad, pad + row * ch
        for run, colour, bold in parse(line, strict):
            if run.strip():
                d.text((x, y), run, font=bold_font if bold else font, fill=colour)
            x += len(run) * cw
    return img


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--frames", type=int, default=40)
    p.add_argument("--fps", type=int, default=5)
    p.add_argument("--size", type=int, default=13, help="font pixel size")
    p.add_argument("--out", default=os.path.expanduser("~/Desktop/machine-monitor.gif"))
    p.add_argument("--strict", action="store_true",
                   help="fail on an escape code this renderer does not implement")
    args = p.parse_args()

    font_path = "/System/Library/Fonts/Menlo.ttc"
    font = ImageFont.truetype(font_path, args.size, index=0)
    bold_font = ImageFont.truetype(font_path, args.size, index=1)
    cw = round(font.getlength("M"))
    ch = args.size + 5

    frames = []
    for tick in range(args.frames):
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "demo_render.py"), "--tick", str(tick)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(r.stderr[:600], file=sys.stderr)
            return 1
        frames.append(render(r.stdout.rstrip("\n"), font, bold_font, cw, ch, 16, args.strict))
        print(f"\rframe {tick + 1}/{args.frames}", end="", flush=True)
    print()

    pal = [f.quantize(colors=200, method=Image.MEDIANCUT) for f in frames]
    pal[0].save(args.out, save_all=True, append_images=pal[1:],
                duration=round(1000 / args.fps), loop=0, optimize=True, disposal=2)
    size = os.path.getsize(args.out)
    print(f"{args.out}  {frames[0].size[0]}x{frames[0].size[1]}  "
          f"{len(frames)} frames  {size / 1024 / 1024:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
