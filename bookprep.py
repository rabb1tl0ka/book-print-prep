#!/usr/bin/env python3
"""Turn a book PDF into a print-ready PDF for loose-leaf (double fan) binding: A4 sheets, cut in half to A5 leaves.

Two input modes:
  spread (default): scanned PDF with two book pages per PDF page. Stage 1 renders each spread, drops red ink,
                    flattens the paper tone, boosts contrast and splits it into left/right pages.
  single:           born-digital PDF with one book page per PDF page. Rendered as-is (add --clean for scans).

Stage 2 crops every page to the shared text block (wipes margin notes and scan-edge junk), adds a spine margin,
and imposes two leaves per landscape A4 sheet in cut-and-stack order, so after printing duplex and cutting each
sheet in half you stack all left halves, then all right halves.
"""
import argparse
import json
import math
import subprocess
import sys
from multiprocessing import Pool
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

PAPER_MM = {"a4": (210, 297), "letter": (215.9, 279.4)}


def mm2px(mm, dpi):
    return round(mm / 25.4 * dpi)


def text_bbox(v, edge=0.04, min_dark=25):
    """Bounding box of real text: a column/row needs min_dark dark pixels, so stray pencil marks don't count."""
    h, w = v.shape
    x0, x1, y0, y1 = int(w * edge), int(w * (1 - edge)), int(h * 0.03), int(h * 0.97)
    dk = v[y0:y1, x0:x1] < 0.5
    cols = np.flatnonzero(dk.sum(0) >= min_dark)
    rows = np.flatnonzero(dk.sum(1) >= min_dark)
    if not len(cols) or not len(rows):
        return None
    return [int(x0 + cols[0]), int(y0 + rows[0]), int(x0 + cols[-1] + 1), int(y0 + rows[-1] + 1)]


def clean_spread(job):
    pdf, p, work, dpi, lo, hi, sigma, force, mode, clean, side = job
    meta = work / "halves" / f"s{p:03d}.json"
    key = [mode, clean, dpi, lo, hi, sigma, side if mode == "single" else None]  # everything that changes the pixels
    if meta.exists() and not force:
        cached = json.loads(meta.read_text())
        if cached.get("key") == key:
            return p, cached["boxes"]
    raw = work / "raw" / f"s{p:03d}"
    subprocess.run(["pdftoppm", "-f", str(p), "-l", str(p), "-r", str(dpi), "-png", "-singlefile", str(pdf), str(raw)], check=True)
    png = Path(f"{raw}.png")
    rgb = Image.open(png).convert("RGB")
    png.unlink()
    if mode == "single" and not clean:
        pages = {side: np.asarray(rgb.convert("L"), np.float32) / 255}  # born-digital: nothing to clean
    else:
        red = rgb.getchannel("R")  # red ink has a high red value, so it drops out
        a = np.asarray(red, np.float32)
        bg = np.asarray(red.filter(ImageFilter.GaussianBlur(sigma * dpi / 200)), np.float32)
        v = np.clip(a / np.maximum(bg, 1), 0, 1)  # divide by blurred background: yellowed paper becomes white
        v = np.clip((v - lo) / (hi - lo), 0, 1)  # levels: fades light pencil
        mid = v.shape[1] // 2
        pages = {side: v} if mode == "single" else {"L": v[:, :mid], "R": v[:, mid:]}
    boxes = {}
    for sd, arr in pages.items():
        boxes[sd] = text_bbox(arr, min_dark=round(25 * dpi / 200))
        img = Image.fromarray((arr * 255).astype(np.uint8))
        if mode == "spread" or clean:
            img = img.filter(ImageFilter.UnsharpMask(1, 60, 0))
        img.save(work / "halves" / f"s{p:03d}{sd}.png")
    meta.write_text(json.dumps({"key": key, "boxes": boxes}))
    return p, boxes


def global_box(boxes, slack, k=1.0, full=False):
    """Body pages share one text block size, so use robust percentiles of the per-page boxes.
    Each page is later cropped to this size around its OWN text, which absorbs scan offsets.
    full=True (born-digital, no scan noise) uses the maximum so nothing is ever clipped."""
    b = np.array([x for x in boxes if x])
    pw, ph = (100, 100) if full else (90, 95)
    return {"cx": float(np.median((b[:, 0] + b[:, 2]) / 2)), "y0": float(np.median(b[:, 1])), "slack": slack, "k": k,
            "w": int(np.percentile(b[:, 2] - b[:, 0], pw)) + 2 * slack,
            "h": int(np.percentile(b[:, 3] - b[:, 1], ph)) + 2 * slack}


def crop_aligned(im, box, bb):
    """Crop a w x h window around the page's own text; fall back to the median position for odd pages."""
    cx, y0 = box["cx"], box["y0"]
    if bb:
        if bb[2] - bb[0] <= box["w"] - 2 * box["slack"] or abs((bb[0] + bb[2]) / 2 - cx) <= 60 * box["k"]:
            cx = (bb[0] + bb[2]) / 2  # narrow pages (half-title, headings) keep their own centre
        if abs(bb[1] - y0) <= 40 * box["k"]:
            y0 = bb[1]
    left, top = round(cx - box["w"] / 2), round(y0 - box["slack"])
    out = Image.new("L", (box["w"], box["h"]), 255)  # white fill where the window leaves the scan
    l, t = max(left, 0), max(top, 0)
    out.paste(im.crop((l, t, min(left + box["w"], im.width), min(top + box["h"], im.height))), (l - left, t - top))
    return out


def render_page(cfg, page):
    sw, sh = cfg["slot"]
    canvas = Image.new("L", (sw, sh), 255)
    if page is None:
        return canvas
    p, side = page
    im = Image.open(Path(cfg["work"]) / "halves" / f"s{p:03d}{side}.png")
    im = crop_aligned(im, cfg["box"][side], cfg["pbox"].get(f"{p}{side}"))
    s = cfg["scale"]
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    m, g = cfg["margin"], cfg["gutter"]
    recto = side == "R"  # right-hand page: spine on the left
    x_lo, x_hi = (m + g, sw - m) if recto else (m, sw - m - g)
    canvas.paste(im, (x_lo + (x_hi - x_lo - im.width) // 2, (sh - im.height) // 2))
    return canvas


def build_sheet(job):
    cfg, j, left, right = job
    sw, sh = cfg["slot"]

    def side_img(l_page, r_page, rotate=False):
        sheet = Image.new("L", (2 * sw, sh), 255)
        sheet.paste(render_page(cfg, l_page), (0, 0))
        sheet.paste(render_page(cfg, r_page), (sw, 0))
        d = ImageDraw.Draw(sheet)
        tick = mm2px(4, cfg["dpi"])
        for y in (mm2px(3, cfg["dpi"]), sh - mm2px(3, cfg["dpi"]) - tick):
            d.line([(sw, y), (sw, y + tick)], fill=0, width=2)
        return sheet.rotate(180) if rotate else sheet

    (lf, lb), (rf, rb) = left, right
    front = side_img(lf, rf)
    back = side_img(rb, lb) if cfg["flip"] == "short" else side_img(lb, rb, rotate=True)
    out = Path(cfg["work"]) / "sheets"
    for tag, img in (("a", front), ("b", back)):
        img.save(out / f"{j:04d}{tag}.pdf", "PDF", resolution=cfg["dpi"], quality=88)
    return j


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--out", required=True, help="output PDF path")
    ap.add_argument("--work", required=True, help="cache/work dir (cleaned pages live here)")
    ap.add_argument("--mode", choices=("spread", "single"), default="spread",
                    help="spread: scan with 2 book pages per PDF page; single: 1 book page per PDF page (born-digital)")
    ap.add_argument("--clean", action="store_true", help="single mode only: apply the scan cleanup too")
    ap.add_argument("--first-side", choices=("recto", "verso"), default="recto",
                    help="single mode: is the first used page right-hand (recto) or left-hand (verso)?")
    ap.add_argument("--first", type=int, default=1, help="first PDF page to use (1-based)")
    ap.add_argument("--last", type=int, default=0, help="last PDF page to use (0 = to the end)")
    ap.add_argument("--dpi", type=int, help="render/print resolution (default 200 for spread, 300 for single)")
    ap.add_argument("--levels", type=float, nargs=2, default=(0.55, 0.92), metavar=("LOW", "HIGH"),
                    help="contrast levels; raise LOW to fade pencil harder")
    ap.add_argument("--bg-sigma", type=float, default=25)
    ap.add_argument("--paper", choices=PAPER_MM, default="a4")
    ap.add_argument("--margin", type=float, default=10, help="outer/top/bottom margin per leaf, mm")
    ap.add_argument("--gutter", type=float, default=8, help="extra spine margin per leaf, mm")
    ap.add_argument("--box-slack", type=int, default=20, help="px around the median text block kept at 200 dpi")
    ap.add_argument("--max-scale", type=float, default=1.25)
    ap.add_argument("--flip", choices=("short", "long"), default="short",
                    help="duplex flip edge as set in the print dialog for a landscape page")
    ap.add_argument("--no-blank-front", action="store_true",
                    help="don't insert a blank recto before the first left-hand page")
    ap.add_argument("--blank", default="", help="comma list of pages to blank, e.g. 290L,290R")
    ap.add_argument("--test-sheet", type=int, help="only build this sheet number (0-based) to test the printer")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--force", action="store_true", help="redo stage 1 even if cached")
    a = ap.parse_args()
    a.dpi = a.dpi or (300 if a.mode == "single" else 200)
    k = a.dpi / 200

    pdf, work = Path(a.pdf).resolve(), Path(a.work)
    for d in ("raw", "halves", "sheets"):
        (work / d).mkdir(parents=True, exist_ok=True)
    n = int(next(l for l in subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True).stdout.splitlines()
                 if l.startswith("Pages:")).split()[1])
    last = a.last or n
    spreads = list(range(a.first, last + 1))

    lo, hi = a.levels
    # single mode: pages alternate recto (R, spine left) / verso (L, spine right) from the first used page
    side_of = {p: "R" if ((p - a.first) % 2 == 0) == (a.first_side == "recto") else "L" for p in spreads}
    jobs = [(pdf, p, work, a.dpi, lo, hi, a.bg_sigma, a.force, a.mode, a.clean, side_of[p]) for p in spreads]
    with Pool(a.jobs) as pool:
        res = dict(pool.imap_unordered(clean_spread, jobs))
    slack = round(a.box_slack * k)
    box = {}
    for s in "LR":
        found = [res[p].get(s) for p in spreads]
        if any(found):
            box[s] = global_box(found, slack, k, full=a.mode == "single")
    pbox = {f"{p}{s}": res[p].get(s) for p in spreads for s in "LR"}
    maxw = max(b["w"] for b in box.values())
    maxh = max(b["h"] for b in box.values())

    long_mm, short_mm = max(PAPER_MM[a.paper]), min(PAPER_MM[a.paper])
    slot = (mm2px(long_mm / 2, a.dpi), mm2px(short_mm, a.dpi))
    margin, gutter = mm2px(a.margin, a.dpi), mm2px(a.gutter, a.dpi)
    scale = min((slot[0] - 2 * margin - gutter) / maxw, (slot[1] - 2 * margin) / maxh, a.max_scale)

    blank = {t.strip().upper() for t in a.blank.split(",") if t.strip()}
    if a.mode == "single":
        seq = [None] if a.first_side == "verso" else []  # a leading verso needs a blank recto in front of it
        for p in spreads:
            seq.append(None if str(p) in blank or f"{p}{side_of[p]}" in blank else (p, side_of[p]))
    else:
        seq = [None] if not a.no_blank_front else []
        for p in spreads:
            for side in "LR":
                seq.append(None if f"{p}{side}" in blank else (p, side))
    if len(seq) % 2:
        seq.append(None)
    leaves = [(seq[i], seq[i + 1]) for i in range(0, len(seq), 2)]
    half = math.ceil(len(leaves) / 2)
    blank_leaf = (None, None)

    cfg = {"work": str(work), "dpi": a.dpi, "slot": slot, "box": box, "pbox": pbox, "scale": scale,
           "margin": margin, "gutter": gutter, "flip": a.flip}
    sheets = range(half) if a.test_sheet is None else [a.test_sheet]
    jobs = [(cfg, j, leaves[j], leaves[j + half] if j + half < len(leaves) else blank_leaf) for j in sheets]
    with Pool(a.jobs) as pool:
        pool.map(build_sheet, jobs)

    files = [str(work / "sheets" / f"{j:04d}{t}.pdf") for j in sheets for t in "ab"]
    subprocess.run(["pdfunite", *files, a.out], check=True)
    print(f"{a.mode} pages {a.first}-{last} -> {len(seq)} pages, {len(leaves)} leaves, {half} {a.paper.upper()} sheets "
          f"(cut each in half), scale {scale:.3f}, text boxes {box}")
    print(f"wrote {a.out} ({len(files)} PDF pages, print duplex, flip on {a.flip} edge)")


if __name__ == "__main__":
    sys.exit(main())
