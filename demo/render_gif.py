#!/usr/bin/env python3
# Copyright 2026 HY
# SPDX-License-Identifier: Apache-2.0
"""Render a captured terminal session (TSV: epoch<TAB>line) into a GIF.

An alternative to vhs for producing the README GIF. vhs drives a headless
Chromium that go-rod downloads on first use, which fails on networks that
cannot reach its download host -- and it fails silently, leaving no output.
This script needs only Pillow: it replays the captured lines at their real
timing (compressed by SPEED) and draws them with a terminal look, colouring
the rows that carry the story (a limit jump, the watermark crossing, the
restart counter staying at zero).

Capture, then render:

    ./demo.sh | while IFS= read -r l; do \
        printf '%s\t%s\n' "$(python3 -c 'import time;print(time.time())')" "$l"; \
      done > frames.tsv
    python3 render_gif.py frames.tsv demo.gif 2.0

Requires: pip install pillow
"""
import os
import sys
from PIL import Image, ImageDraw, ImageFont

TSV, OUT = sys.argv[1], sys.argv[2]
SPEED = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0

BG = (26, 27, 38)          # tokyo-night background
FG = (192, 202, 245)       # default foreground
DIM = (86, 95, 137)        # header / chrome
MARK = (224, 175, 104)     # rows where the limit moved
GOOD = (158, 206, 106)     # restarts: 0
WARN = (247, 118, 142)     # USED% at or above the watermark

# Any monospaced TTF works; override with FONT=/path/to/font.ttf.
FONT_CANDIDATES = [
    os.environ.get("FONT", ""),
    "/System/Library/Fonts/Menlo.ttc",                                # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",            # Debian
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",                     # Fedora
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",                        # Arch
]
FONT_PATH = next((p for p in FONT_CANDIDATES if p and os.path.exists(p)), "")
if not FONT_PATH:
    sys.exit("No monospaced font found; set FONT=/path/to/font.ttf")
SIZE = int(os.environ.get("FONT_SIZE", "17"))
PAD = 22
LINE_H = 25

rows = []
with open(TSV, encoding="utf-8") as fh:
    for raw in fh:
        ts, _, line = raw.rstrip("\n").partition("\t")
        try:
            rows.append((float(ts), line))
        except ValueError:
            continue

font = ImageFont.truetype(FONT_PATH, SIZE, index=0)
probe = Image.new("RGB", (10, 10))
cw = ImageDraw.Draw(probe).textlength("M" * 10, font=font) / 10

cols = max(len(line) for _, line in rows) + 2
W = int(cols * cw) + PAD * 2
H = LINE_H * len(rows) + PAD * 2


CMD = (125, 207, 255)      # the verification command the viewer can re-run

def colour_for(line: str) -> tuple:
    st = line.strip()
    if st.startswith("$ ") or st.startswith("-o jsonpath"):
        return CMD
    if st.endswith("<- restarts") or "Rescue done" in st:
        return GOOD
    if "<<" in line:
        return MARK
    if line.startswith("TIME ") or line.startswith("Pod:") or line.startswith("Growing"):
        return DIM
    if st.startswith("restarted --") or st.startswith("Scale-down follows"):
        return DIM
    return FG


def draw(upto: int) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for i in range(upto):
        line = rows[i][1]
        y = PAD + i * LINE_H
        base = colour_for(line)
        d.text((PAD, y), line, font=font, fill=base)
        # Re-draw two spots over the base row: the USED% once it crosses the
        # watermark (the "about to OOM" moment) and the restart counter while
        # it stays at zero (the whole point of an in-place resize).
        if "%" in line and not line.startswith("TIME"):
            pct_end = line.find("%")
            pct_start = line.rfind(" ", 0, pct_end) + 1
            try:
                if int(line[pct_start:pct_end]) >= 80:
                    d.text((PAD + cw * pct_start, y),
                           line[pct_start:pct_end + 1], font=font, fill=WARN)
            except ValueError:
                pass
            # RESTARTS is the field right after USED%; colour a zero green.
            tail = line[pct_end + 1:]
            stripped = tail.lstrip()
            rs_start = pct_end + 1 + (len(tail) - len(stripped))
            rs = stripped.split(" ", 1)[0] if stripped else ""
            if rs == "0":
                d.text((PAD + cw * rs_start, y), rs, font=font, fill=GOOD)
    return img


frames, delays = [], []
for i in range(1, len(rows) + 1):
    frames.append(draw(i))
    if i < len(rows):
        gap = (rows[i][0] - rows[i - 1][0]) / SPEED
        delays.append(int(max(0.05, min(1.2, gap)) * 1000))
    else:
        delays.append(2600)   # hold the final frame

# MEDIANCUT at 32 colours drops the few red USED% pixels entirely;
# FASTOCTREE with a wider palette keeps small coloured runs.
pal = [f.quantize(colors=128, method=Image.Quantize.FASTOCTREE) for f in frames]
pal[0].save(OUT, save_all=True, append_images=pal[1:], duration=delays,
            loop=0, optimize=True, disposal=1)
print(f"{OUT}  {W}x{H}  {len(frames)} frames  {sum(delays)/1000:.1f}s")
