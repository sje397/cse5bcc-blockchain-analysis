#!/usr/bin/env python3
"""Render console-output panels as PNGs for the slide deck.

The text is copied VERBATIM from the capture logs; nothing is retyped, reworded or
recomputed.  These panels are the deck's "embedded output screenshots" requirement:
the pixels are the real recorded output, presented on a dark panel the way a terminal
screenshot would be.

Selection is by CONTENT ANCHOR, never by line number, and every panel declares what it
expects to find.  If an anchor or an expected string is absent the script raises rather
than emitting a panel that quietly shows something else - a screenshot that cannot
fail to render is not evidence of anything.

Regenerate:  .venv/bin/python3 make_screenshots.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
LOG = HERE / "logs" / "phase_demo_capture.log"
ROOTCAUSE = HERE / "rootcause_report.txt"
OUT = HERE / "screenshots"

# One font size for every panel, so the panels look like one terminal session.
FONT_PATH = "/System/Library/Fonts/Menlo.ttc"
FONT_PX = 19
PAD = 24
LINE_H = 27
BG = (22, 26, 33)
FG = (208, 214, 226)
DIM = (128, 138, 158)
GREEN = (126, 216, 152)   # shell prompts / commands
CYAN = (110, 205, 230)    # step banners and section headers
YELLOW = (232, 200, 110)  # result lines that carry the claim
RED = (232, 128, 122)     # the failing / diverging case
RULE = (60, 68, 84)       # the heavy '===' rules


def load_font(bold: bool) -> ImageFont.FreeTypeFont:
    for index in ((1, 0) if bold else (0, 1)):
        try:
            return ImageFont.truetype(FONT_PATH, FONT_PX, index=index)
        except OSError:
            continue
    raise RuntimeError(f"no usable face in {FONT_PATH}")


FONT = load_font(False)
FONT_B = load_font(True)


def lines_of(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"capture source missing: {path}")
    return path.read_text().splitlines()


def is_rule(line: str) -> bool:
    return line.startswith("====")


def section(lines: list[str], heading: str) -> tuple[int, int]:
    """Line range [start, end) of the block whose heading line contains `heading`.

    Sections are framed rule / heading / rule, and the NEXT section's opening rule
    terminates this one - so the heading's own closing rule must be stepped over
    before the search for the terminator begins.
    """
    hits = [i for i, ln in enumerate(lines) if heading in ln]
    if len(hits) != 1:
        raise LookupError(f"anchor {heading!r} matched {len(hits)} lines, expected exactly 1")
    start = hits[0] - 1                      # the rule line above the heading
    if not is_rule(lines[start]):
        raise LookupError(f"anchor {heading!r} is not preceded by a '====' rule")

    end = hits[0] + 1
    if end < len(lines) and is_rule(lines[end]):
        end += 1                             # step over the heading's own closing rule
    while end < len(lines) and not is_rule(lines[end]):
        end += 1

    body = lines[start:end]
    if len(body) < 3 or sum(1 for ln in body if heading in ln) != 1:
        raise AssertionError(
            f"section {heading!r} captured {len(body)} lines with "
            f"{sum(1 for ln in body if heading in ln)} headings"
        )
    return start, end


def find(lines: list[str], needle: str) -> int:
    hits = [i for i, ln in enumerate(lines) if needle in ln]
    if not hits:
        raise LookupError(f"expected text not present: {needle!r}")
    return hits[0]


def wide(lines: list[str]) -> int:
    return max(len(ln) for ln in lines)


def render(lines: list[str], out: Path, must_contain: list[str]) -> None:
    for needle in must_contain:
        if not any(needle in ln for ln in lines):
            raise AssertionError(f"{out.name}: panel would not contain {needle!r}")

    cols = wide(lines)
    w = int(PAD * 2 + FONT.getlength("M" * cols))
    h = PAD * 2 + LINE_H * len(lines)
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)

    for row, text in enumerate(lines):
        x = PAD
        y = PAD + row * LINE_H
        stripped = text.strip()

        if stripped.startswith("====") or text.startswith("===="):
            colour, font = RULE, FONT
        elif stripped.startswith("$ "):
            colour, font = GREEN, FONT_B
        elif stripped.startswith("--") and stripped.endswith("--"):
            colour, font = CYAN, FONT_B
        elif "identical=False" in text or "still True  and hash_block changed" in text:
            colour, font = RED, FONT
        elif "identical=True" in text or "distinct block hashes" in text or "now equals A" in text:
            colour, font = YELLOW, FONT
        elif text.startswith("PHASE") or "FINDING" in text:
            colour, font = CYAN, FONT_B
        else:
            colour, font = FG, FONT

        d.text((x, y), text, font=font, fill=colour)

    img.save(out)
    print(f"  {out.name:34s} {len(lines):3d} lines  {w}x{h}px  ({cols} cols)")


def main() -> int:
    OUT.mkdir(exist_ok=True)
    log = lines_of(LOG)
    rc = lines_of(ROOTCAUSE)

    # ---- Panel 1: Phase 1 at the supplied target '0000' -------------------
    s, e = section(log, "PHASE 1 — single node, target '0000'  (provided program")
    p1 = log[s:e]
    render(
        p1,
        OUT / "p1_phase1_0000.png",
        ["PHASE 1", "median of 3 blocks: 0.0514 s", "transaction time = 0.0001 s",
         "chain at p1 (port 5201): 5 block(s)"],
    )

    # ---- Panel 2: Phase 2 at '0000' (the sync story) ---------------------
    s, e = section(log, "PHASE 2 — three nodes, target '0000'  (provided program")
    p2 = log[s:e]
    keep: list[str] = []
    for ln in p2:
        if ln.strip().startswith("-- add a transaction on A, mine it"):
            break                            # break BEFORE the banner, not after it
        keep.append(ln)
    keep += [
        "  …   (A then mined a block carrying the transaction and B re-synced to it)",
        "",
    ]
    tail_from = find(p2, "-- bring up a third node")
    keep += p2[tail_from:]
    render(
        keep,
        OUT / "p2_phase2_sync.png",
        ["identical=False", "identical=True", "all three chains identical: True",
         "node C"],
    )

    # ---- Panel 3: Phase 3, the two difficulty targets stacked -------------
    s, e = section(log, "PHASE 1 — single node, target '000'  (phase 3 repeat)")
    cheap = log[s:e]
    cheap = cheap[: find(cheap, "-- view the chain --")]
    s, e = section(log, "PHASE 1 — single node, target '00000'  (phase 3 repeat)")
    dear = log[s:e]
    dear = dear[: find(dear, "-- view the chain --")]
    render(
        cheap
        + ["", "  ...   same node, same three blocks, one more zero in the target", ""]
        + dear,
        OUT / "p3_difficulty.png",
        # Anchored on the port+target pair, so '000' cannot be satisfied by '0000'.
        ["5206   # DIFFICULTY=000", "5210   # DIFFICULTY=00000",
         "median of 3 blocks: 0.0011 s", "median of 3 blocks: 0.2403 s"],
    )

    # ---- Panel 4: the scaffold finding, live against the provided class ---
    s = find(rc, "FINDING 2b  live reproduction against the provided class") - 1
    e = find(rc, "CONTROL - valid_proof must fail when a field it DOES cover")
    render(
        [ln for ln in rc[s:e] if ln.strip()],
        OUT / "p4_same_nonce.png",
        ["distinct nonces        : 1", "distinct block hashes  : 3",
         "B now equals A            : True", "timestamp :=   2000000000.0"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
