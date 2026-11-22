"""
Premium Black: PNG + geometry -> editable PPTX (called by build_deck.py --pptx).

The renderer writes s_NN.png (full slide) into the job's png/ folder and, for an editable PPTX,
bg_NN.png (slide without text) and geometry.json (text blocks and living-line media boxes) into the
job's scratch folder. Each slide becomes a 13.333 x 7.5 in page: bg_NN.png as the full-bleed picture
(cards, photos, glow, grain, pills, lines) and one text box per text block with the exact position,
font, size, weight, colour and letter spacing. Living-line GIFs lie between picture and text.

Fonts: Inter Light / Inter / Inter Medium / Inter SemiBold, Unbounded Medium / Unbounded SemiBold.
python-pptx cannot embed fonts, so build_deck.py writes "<name> fonts.zip" next to the PPTX: the six
static TTFs with their SIL OFL 1.1 licences, to install on the machine that opens the file.

flat=True puts the full s_NN.png on each slide instead (nothing editable, pixel-exact).
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import zipfile

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import premium_common as pc
import render_deck            # slide ordering: numeric, never lexicographic (100+ slide decks)

SLIDE_W_IN, SLIDE_H_IN = 13.333, 7.5
PX = 914400 * SLIDE_W_IN / 1920      # EMU per scene pixel (1920 px = 13.333 in)
PT_PER_PX = 72 * SLIDE_W_IN / 1920   # 0.5 pt per px


def emu(px):
    return Emu(int(round(px * PX)))


def font_name(family, weight):
    fam = (family or "Inter").lower()
    if "unbounded" in fam:
        return "Unbounded SemiBold" if weight >= 600 else "Unbounded Medium"
    if weight <= 300:
        return "Inter Light"
    if weight < 500:
        return "Inter"
    if weight < 600:
        return "Inter Medium"
    return "Inter SemiBold"


def parse_color(css):
    """'rgb(1, 2, 3)' | 'rgba(1, 2, 3, 0.5)' -> (RGBColor, alpha 0..1)."""
    s = css.strip()
    if s.startswith("rgb"):
        parts = [p.strip() for p in s[s.find("(") + 1:s.rfind(")")].split(",")]
        r, g, b = (int(float(p)) for p in parts[:3])
        a = float(parts[3]) if len(parts) > 3 else 1.0
        return RGBColor(r, g, b), a
    if s.startswith("#") and len(s) == 7:
        return RGBColor.from_string(s[1:].upper()), 1.0
    return RGBColor(0xF3, 0xEB, 0xDD), 1.0


def set_alpha(run, alpha):
    if alpha >= 0.999:
        return
    rPr = run._r.get_or_add_rPr()
    solid = rPr.find(qn("a:solidFill"))
    if solid is None:
        return
    clr = solid[0]
    a = clr.makeelement(qn("a:alpha"), {"val": str(int(alpha * 100000))})
    clr.append(a)


def set_spacing(run, spacing_px):
    if abs(spacing_px) < 0.05:
        return
    rPr = run._r.get_or_add_rPr()
    rPr.set("spc", str(int(round(spacing_px * PT_PER_PX * 100))))


def add_text_block(slide, b):
    runs = b["runs"]
    if not runs:
        return
    base_size = max(r["size"] for r in runs)
    align = b.get("align", "left")
    slack = b["w"] * 0.04 + 6           # PowerPoint and Chromium break lines slightly differently
    x, w = b["x"], b["w"] + slack
    if align == "right":
        x = b["x"] - slack
    elif align == "center":
        x = b["x"] - slack / 2
    box = slide.shapes.add_textbox(emu(x), emu(b["y"] - 1), emu(w), emu(b["h"] + 2))
    if b.get("rot"):
        box.rotation = b["rot"]
    tf = box.text_frame
    tf.word_wrap = not b.get("nowrap", False)   # nowrap blocks (pills, labels) stay on one line even if a renderer measures wider
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE if b.get("vcenter") else MSO_ANCHOR.TOP
    # paragraphs split by '\n' runs
    paras = [[]]
    for r in runs:
        if r["text"] == "\n":
            paras.append([])
        else:
            paras[-1].append(r)
    first = True
    for pr in paras:
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.alignment = {"right": PP_ALIGN.RIGHT, "center": PP_ALIGN.CENTER}.get(align, PP_ALIGN.LEFT)
        lh = b.get("lineHeight") or base_size * 1.2
        p.line_spacing = Pt(lh * PT_PER_PX)
        for r in pr:
            text = r["text"]
            if r.get("transform") == "uppercase":
                text = text.upper()
            if not text:
                continue
            run = p.add_run()
            run.text = text
            f = run.font
            f.name = font_name(r.get("font"), r.get("weight", 400))
            f.size = Pt(r["size"] * PT_PER_PX)
            f.bold = False
            rgb, a = parse_color(r.get("color", "rgb(243,235,221)"))
            alpha = a * float(r.get("opacity", 1.0))
            if alpha < 0.999 and abs(r.get("spacing", 0)) >= 0.05:
                # LibreOffice clips semi-transparent tracked text to its unspaced width; bake the alpha into the colour instead
                bg = (0x15, 0x11, 0x0E)
                rgb = RGBColor(*(int(round(c * alpha + g * (1 - alpha))) for c, g in zip((rgb[0], rgb[1], rgb[2]), bg)))
                alpha = 1.0
            f.color.rgb = rgb
            set_alpha(run, alpha)
            set_spacing(run, r.get("spacing", 0))


def build(png_dir, out_path, *, work_dir=None, flat=False, media=None, title=""):
    """png_dir: s_NN.png; work_dir: bg_NN.png + geometry.json (editable mode); media: living-line key -> files."""
    png_dir = pathlib.Path(png_dir)
    out_path = pathlib.Path(out_path)
    prs = Presentation()
    prs.slide_width = Emu(int(914400 * SLIDE_W_IN))
    prs.slide_height = Emu(int(914400 * SLIDE_H_IN))
    blank = prs.slide_layouts[6]
    geo = None
    if not flat:
        geo_path = pathlib.Path(work_dir or "") / "geometry.json"
        if work_dir is None or not geo_path.is_file():
            raise RuntimeError("geometry.json не найден: редактируемый PPTX собирается только из рендера build_deck.py --pptx")
        geo = json.loads(geo_path.read_text(encoding="utf-8"))
    fulls = render_deck.slide_pngs(png_dir)      # numeric order: slide 100 after 99, not after 10
    placed_media = 0
    for full in fulls:
        n = int(render_deck.SLIDE_PNG.match(full.name).group(1))
        slide = prs.slides.add_slide(blank)
        bg = full if flat else pathlib.Path(work_dir) / ("bg_" + full.name[2:])
        if not bg.is_file():
            bg = full
        slide.shapes.add_picture(str(bg), 0, 0, prs.slide_width, prs.slide_height)
        if geo:
            entry = next((s for s in geo["slides"] if s["n"] == n), {})
            for m in entry.get("media", []):          # animated GIFs: above the background, below the text
                gif = (media or {}).get(m.get("anim"), {}).get("gif")
                if gif and os.path.isfile(gif):
                    slide.shapes.add_picture(gif, emu(m["x"]), emu(m["y"]), emu(m["w"]), emu(m["h"]))
                    placed_media += 1
            for b in entry.get("blocks", []):
                add_text_block(slide, b)
    props = prs.core_properties                     # no machine or person names in the file
    props.title = str(title or "")[:250]
    props.author = ""
    props.last_modified_by = ""
    props.comments = f"kirill-menshov-premium-black v{pc.VERSION}"
    props.subject = props.keywords = props.category = ""
    props.revision = 1
    with open(out_path, "xb") as stream:            # the job folder is new: never overwrite anything
        prs.save(stream)
    blocks = sum(len(s["blocks"]) for s in geo["slides"]) if geo else 0
    kind = "плоский, без редактируемого текста" if flat else f"редактируемый текст, {blocks} блоков"
    print(f"PPTX: {out_path} ({len(fulls)} слайдов, {kind})")
    return {"slides": len(fulls), "editable_text_blocks": blocks, "animated_media": placed_media, "flat": bool(flat)}


FONT_FILES = ("Inter-Light.ttf", "Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf",
              "Unbounded-Medium.ttf", "Unbounded-SemiBold.ttf")


def write_fonts_zip(path):
    """The six static TTFs, both OFL licences and install notes; fixed timestamps, so the archive is reproducible."""
    fonts = pc.ASSETS / "fonts"
    members = [(f"Premium Black fonts/{name}", fonts / "ttf" / name) for name in FONT_FILES]
    members += [("Premium Black fonts/LICENSE-Inter.txt", fonts / "LICENSE-Inter.txt"),
                ("Premium Black fonts/LICENSE-Unbounded.txt", fonts / "LICENSE-Unbounded.txt"),
                ("Premium Black fonts/README.md", fonts / "ttf" / "README.md")]
    with open(path, "xb") as stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname, source in members:
            info = zipfile.ZipInfo(arcname, date_time=(2026, 11, 20, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, source.read_bytes())
    return path
