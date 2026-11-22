"""
Premium Black: scheme layouts (flow, layers, cycle, matrix, tree, compare, funnel, loop).

Diagrams are built from the same tokens as the rest of the system: glass / ink / caramel nodes,
latte 1px connectors, one copper accent per slide, Inter labels. Shapes and connectors are SVG,
every label is an HTML element, so the PPTX export keeps the text editable.

Optional `anim: true` (flow, cycle; always on for loop) adds the living line: a procedural light
trail along the path (scripts/particle_path.py), a still PNG for PDF and HTML, an animated GIF for
PPTX (and for the HTML with --gif). Night slides only. Frames are cached under
<state>/cache/anim/<version>/ and embedded into the HTML as data URIs.

Imported by build_deck.py; helpers are injected through `init(ns)` to avoid a circular import.
"""
import base64, math, os, secrets, sys

import particle_path

B = {}            # helpers from build_deck (T, pill, vf, head, fit_lines, text_width, num_style, card_open ...)

def init(ns):
    B.update(ns)

MX, CW = 72, 1776
TOP, BOT = 400, 1000           # content band under the standard head

# ------------------------------------------------------------------ living line (particle_path.py)
ANIM_CACHE = None              # set by build_deck per build: <state>/cache/anim/<version>/
MAKE_GIF = False               # build_deck sets True for --pptx / --gif
EMBED_GIF = False              # build_deck sets True for --gif: the HTML then plays the GIF in a browser
USED = {}                      # key -> {"png": path, "gif": path or None} used by this build (for build_pptx.py)

def configure(cache_dir, make_gif, embed_gif):
    global ANIM_CACHE, MAKE_GIF, EMBED_GIF
    ANIM_CACHE, MAKE_GIF, EMBED_GIF = str(cache_dir), bool(make_gif), bool(embed_gif)
    USED.clear()
    BUILD_WARNINGS.clear()

def _data_uri(path, mime):
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode("ascii")

def anim(shape, w, h, left, top, seed=7, strength=1.0, still=None, z=2, make_gif=None):
    """Generates (cached) PNG + GIF and returns the <img class="anim"> tag with the frame embedded."""
    if make_gif is None:
        make_gif = MAKE_GIF
    key = f"{shape}_{w}x{h}_s{seed}_k{strength:g}" + (f"_p{still:g}" if still is not None else "")
    png = os.path.join(ANIM_CACHE, key + ".png")
    gif = os.path.join(ANIM_CACHE, key + ".gif")
    need_png = not os.path.exists(png)
    need_gif = make_gif and not os.path.exists(gif)
    if need_png or need_gif:
        tmp = os.path.join(ANIM_CACHE, f".{key}.{secrets.token_hex(4)}")
        tmp_png, tmp_gif = (tmp + ".png") if need_png else None, (tmp + ".gif") if need_gif else None
        try:
            particle_path.generate(shape, w, h, tmp_png, tmp_gif, seed=seed, still=still, strength=strength)
            for t, final in ((tmp_png, png), (tmp_gif, gif)):
                if t:
                    os.replace(t, final)
        finally:
            for t in (tmp_png, tmp_gif):          # only this call's own temporary files
                if t and os.path.exists(t):
                    os.remove(t)
    USED[key] = {"png": png, "gif": gif if make_gif else None}
    gif_attr = f' data-gif="{_data_uri(gif, "image/gif")}"' if (make_gif and EMBED_GIF) else ""
    return (f'<img class="anim" src="{_data_uri(png, "image/png")}" data-anim="{key}"{gif_attr} alt="" '
            f'style="position:absolute;left:{left}px;top:{top}px;width:{w}px;height:{h}px;z-index:{z};pointer-events:none">')

BUILD_WARNINGS = []       # per-build notes for the report (cleared by configure())

def build_warnings():
    """This build's scheme warnings, de-duplicated in first-seen order."""
    seen, out = set(), []
    for w in BUILD_WARNINGS:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out

def want_anim(sl, default=False):
    on = sl.get("anim", default)
    if on and sl.get("day"):
        w = (f"anim на дневном слайде ({sl.get('type')}) отключён: "
             "световая линия рассчитана на тёмную сцену")
        print("WARN " + w)
        BUILD_WARNINGS.append(w)
        return False
    return bool(on)

# ------------------------------------------------------------------ small building blocks
def node_open(kind, left, top, w, h, pad="32px 36px", radius=24):
    return B["card_open"](kind, left, top, w, h, radius=radius, pad=pad)

CLOSE = "</div></div>"

def label(text, size=15, color="var(--caramel)", extra=""):
    """Uppercase tracked label (eyebrow style)."""
    ws = "" if "width:" in extra else "white-space:nowrap;"
    return f'<div style="font-size:{size}px;letter-spacing:0.22em;text-transform:uppercase;font-weight:500;color:{color};{ws}{extra}">{B["T"](text)}</div>'

def chip(text, kind="", avail=None):
    """Small glass chip for leaf items (layers, tree). Uses pill geometry so it never overflows."""
    return B["pill"](text, kind, avail)

def svg_open(left, top, w, h, z=1):
    return (f'<svg style="position:absolute;left:{left}px;top:{top}px;z-index:{z};overflow:visible" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" fill="none">')

LINE = "rgba(220,200,168,0.45)"
LINE_SOFT = "rgba(220,200,168,0.22)"
COPPER = "#C98B66"

def chevron(x, y, direction="right", size=9, color=LINE):
    d = {"right": f"M{x - size} {y - size} L{x} {y} L{x - size} {y + size}",
         "left": f"M{x + size} {y - size} L{x} {y} L{x + size} {y + size}",
         "down": f"M{x - size} {y - size} L{x} {y} L{x + size} {y - size}",
         "up": f"M{x - size} {y + size} L{x} {y} L{x + size} {y + size}"}[direction]
    return f'<path d="{d}" stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'

def kind_of(it, default="glass"):
    k = it.get("kind", default)
    if it.get("accent"):
        k = "caramel"
    return k

def body_colors(kind):
    """title colour class, text colour for a node surface."""
    if kind == "caramel":
        return "", "color:#3A2A1E;"
    return " bronze", ""

# ------------------------------------------------------------------ flow
def L_flow(sl):
    """3-6 steps left to right with a rail on top. items: [{title, text, pill, kind, accent}], anim, result."""
    items = sl["items"]
    n = len(items)
    if not 3 <= n <= 6:
        sys.exit("flow: нужно от 3 до 6 шагов")
    body = [B["head"](sl)]
    gap = {3: 40, 4: 32, 5: 28, 6: 24}[n]
    w = (CW - gap * (n - 1)) / n
    rail_y = TOP + 40
    # rail with dots at step centres
    body.append(svg_open(MX, rail_y - 30, CW, 60))
    body.append(f'<line x1="0" y1="30" x2="{CW}" y2="30" stroke="{LINE_SOFT}" stroke-width="1"/>')
    for i in range(n):
        cx = i * (w + gap) + w / 2
        acc = items[i].get("accent") or (i == n - 1 and sl.get("accent_last", False))
        col = COPPER if acc else "#DCC8A8"
        body.append(f'<circle cx="{cx:.0f}" cy="30" r="5" fill="{col}"/><circle cx="{cx:.0f}" cy="30" r="12" stroke="{col}" stroke-opacity="0.35"/>')
        if i < n - 1:
            body.append(chevron(cx + (w + gap) / 2 + 6, 30, "right", 7, LINE))
    body.append("</svg>")
    if want_anim(sl):
        body.append(anim("line", CW + 120, 120, MX - 60, rail_y - 60, strength=0.9, z=2))
    card_top = rail_y + 70
    card_h = BOT - card_top
    title_fs = {3: 30, 4: 26, 5: 24, 6: 22}[n]
    text_fs = {3: 19, 4: 18, 5: 17, 6: 16}[n]
    for i, it in enumerate(items):
        left = round(MX + i * (w + gap))
        kind = kind_of(it)
        if kind == "caramel" and not it.get("accent") and i == n - 1 and sl.get("accent_last", False):
            pass
        pad = "28px 30px" if n <= 4 else "24px 24px"
        body.append(node_open(kind, left, card_top, round(w), card_h, pad=pad))
        inner_w = w - (60 if n <= 4 else 48)
        if it.get("pill"):
            body.append(B["pill"](it["pill"], B["pill_kind"](kind), inner_w))
        else:
            body.append(B["vf"](i + 1, "xs"))
        tcls, tcol = body_colors(kind)
        fs = B["fit_lines"](it.get("title", ""), title_fs, inner_w, 2, min_size=title_fs - 6)
        body.append(f'<div class="ctitle{tcls}" style="font-size:{fs}px;margin-top:auto;padding-top:18px">{B["T"](it.get("title"))}</div>')
        if it.get("text"):
            body.append(f'<div class="ctext" style="font-size:{text_fs}px;{tcol}">{B["T"](it["text"])}</div>')
        body.append(CLOSE)
    return "".join(body)

# ------------------------------------------------------------------ layers
def L_layers(sl):
    """3-5 horizontal bands. layers: [{title, text, items: [str], kind, accent}], side: {title, text}."""
    layers = sl["layers"]
    n = len(layers)
    if not 3 <= n <= 5:
        sys.exit("layers: нужно от 3 до 5 слоёв")
    body = [B["head"](sl)]
    side = sl.get("side")
    side_w = 300 if side else 0
    gap = 16
    band_w = CW - (side_w + 32 if side else 0)
    band_h = (BOT - TOP - gap * (n - 1)) / n
    title_col = 400 if band_w > 1400 else 340
    for i, ly in enumerate(layers):
        top = round(TOP + i * (band_h + gap))
        kind = kind_of(ly)
        body.append(node_open(kind, MX, top, band_w, round(band_h), pad="0", radius=20))
        body.append('<div style="display:flex;height:100%;align-items:center">')
        tcls, tcol = body_colors(kind)
        tfs = 26 if band_h >= 120 else 22
        body.append(f'<div style="width:{title_col}px;flex-shrink:0;padding:0 36px;border-right:1px solid rgba(220,200,168,0.14);display:flex;flex-direction:column;justify-content:center;height:100%">'
                    f'<div class="ctitle{tcls}" style="font-size:{tfs}px">{B["T"](ly.get("title"))}</div>'
                    + (f'<div class="ctext" style="font-size:16px;margin-top:8px;{tcol}">{B["T"](ly["text"])}</div>' if ly.get("text") else "") + "</div>")
        chips_w = band_w - title_col - 72
        body.append(f'<div style="flex:1;display:flex;flex-wrap:wrap;gap:12px;padding:0 36px;align-items:center;align-content:center">')
        for c in ly.get("items") or []:
            body.append(chip(c, "oncaramel" if kind == "caramel" else "", chips_w))
        body.append("</div></div>" + CLOSE)
    if side:
        left = MX + band_w + 32
        body.append(node_open("ink", left, TOP, side_w, BOT - TOP, pad="36px 32px"))
        body.append(label(side.get("label", "Сквозной слой"), 13))
        body.append(f'<div class="ctitle bronze" style="font-size:26px;margin-top:auto">{B["T"](side.get("title"))}</div>')
        if side.get("text"):
            body.append(f'<div class="ctext" style="font-size:16px">{B["T"](side["text"])}</div>')
        body.append(CLOSE)
    return "".join(body)

# ------------------------------------------------------------------ cycle
def L_cycle(sl):
    """3-6 nodes around a ring. items: [{title, text}], center: {num, title, text}, anim."""
    items = sl["items"]
    n = len(items)
    if not 3 <= n <= 6:
        sys.exit("cycle: нужно от 3 до 6 узлов")
    body = [B["head"](sl)]
    cx, cy = 960, 716
    rx, ry = 520, 224
    # ring
    body.append(svg_open(cx - rx - 20, cy - ry - 20, 2 * (rx + 20), 2 * (ry + 20)))
    body.append(f'<ellipse cx="{rx + 20}" cy="{ry + 20}" rx="{rx}" ry="{ry}" stroke="{LINE_SOFT}" stroke-width="1.2" stroke-dasharray="2 9"/>')
    pts = []
    for i in range(n):
        a = -math.pi / 2 + i * 2 * math.pi / n
        px, py = cx + rx * math.cos(a), cy + ry * math.sin(a)
        pts.append((px, py, a))
        col = COPPER if items[i].get("accent") else "#DCC8A8"
        body.append(f'<circle cx="{px - (cx - rx - 20):.0f}" cy="{py - (cy - ry - 20):.0f}" r="6" fill="{col}"/>'
                    f'<circle cx="{px - (cx - rx - 20):.0f}" cy="{py - (cy - ry - 20):.0f}" r="14" stroke="{col}" stroke-opacity="0.35"/>')
        # chevron halfway to the next node (clockwise)
        am = a + math.pi / n
        mx_, my_ = cx + rx * math.cos(am) - (cx - rx - 20), cy + ry * math.sin(am) - (cy - ry - 20)
        # tangent direction (clockwise on screen)
        tx, ty = -rx * math.sin(am), ry * math.cos(am)
        L = math.hypot(tx, ty); tx, ty = tx / L, ty / L
        nx, ny = -ty, tx
        s = 8
        body.append(f'<path d="M{mx_ - tx * s + nx * s:.1f} {my_ - ty * s + ny * s:.1f} L{mx_:.1f} {my_:.1f} L{mx_ - tx * s - nx * s:.1f} {my_ - ty * s - ny * s:.1f}" stroke="{LINE}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>')
    body.append("</svg>")
    if want_anim(sl):
        body.append(anim("ellipse", 2 * rx + 160, 2 * ry + 160, cx - rx - 80, cy - ry - 80, strength=0.8, z=2))
    # labels outside the ring
    lw = 360
    for i, (px, py, a) in enumerate(pts):
        it = items[i]
        cosv, sinv = math.cos(a), math.sin(a)
        offx, offy = 40, 30
        if abs(cosv) < 0.2:          # top or bottom
            left = px - lw / 2; align = "center"
            top = py - offy - 96 if sinv < 0 else py + offy
        elif cosv > 0:
            left = px + offx; align = "left"
            top = py - 50 if abs(sinv) < 0.95 else py
        else:
            left = px - offx - lw; align = "right"
            top = py - 50 if abs(sinv) < 0.95 else py
        tcls = " bronze" if not it.get("accent") else ""
        tcol = f"color:{COPPER};" if it.get("accent") else ""
        body.append(f'<div style="position:absolute;left:{left:.0f}px;top:{top:.0f}px;width:{lw}px;text-align:{align};z-index:4">'
                    f'<div class="ctitle{tcls}" style="font-size:24px;{tcol}">{B["T"](it.get("title"))}</div>'
                    + (f'<div class="ctext" style="font-size:16px;margin-top:8px">{B["T"](it["text"])}</div>' if it.get("text") else "") + "</div>")
    c = sl.get("center")
    if c:
        body.append(f'<div style="position:absolute;left:{cx - 300}px;top:{cy - 110}px;width:600px;text-align:center;z-index:4">')
        if c.get("num"):
            body.append(f'<div class="num bronze" style="font-size:{B["fit_size"](c["num"], 72, 560, min_size=40)}px">{B["T"](c["num"])}</div>')
        if c.get("title"):
            body.append(f'<div class="ctitle" style="font-size:28px;margin-top:14px">{B["T"](c["title"])}</div>')
        if c.get("text"):
            body.append(f'<div class="ctext" style="font-size:17px;margin-top:10px">{B["T"](c["text"])}</div>')
        body.append("</div>")
    return "".join(body)

# ------------------------------------------------------------------ matrix
def L_matrix(sl):
    """2x2. x_axis: {label, low, high}, y_axis: {label, low, high}, quadrants: [{title, text, accent}] (TL, TR, BL, BR), points: [{x, y, label}]."""
    q = sl.get("quadrants", [])
    if len(q) != 4:
        sys.exit("matrix: нужно ровно 4 квадранта (слева сверху, справа сверху, слева снизу, справа снизу)")
    body = [B["head"](sl)]
    ax_l, ax_b = 70, 56
    gx = MX + ax_l; gy = TOP
    gw = CW - ax_l; gh = BOT - TOP - ax_b
    gap = 14
    qw, qh = (gw - gap) / 2, (gh - gap) / 2
    for i, it in enumerate(q):
        col, row = i % 2, i // 2
        left = round(gx + col * (qw + gap)); top = round(gy + row * (qh + gap))
        kind = kind_of(it)
        body.append(node_open(kind, left, top, round(qw), round(qh), pad="30px 34px"))
        tcls, tcol = body_colors(kind)
        body.append(f'<div class="ctitle{tcls}" style="font-size:28px">{B["T"](it.get("title"))}</div>')
        if it.get("text"):
            body.append(f'<div class="ctext" style="font-size:18px;{tcol}">{B["T"](it["text"])}</div>')
        body.append(CLOSE)
    # points
    for p in sl.get("points") or []:
        px = gx + float(p["x"]) / 100 * gw; py = gy + (1 - float(p["y"]) / 100) * gh
        body.append(f'<div style="position:absolute;left:{px - 7:.0f}px;top:{py - 7:.0f}px;width:14px;height:14px;border-radius:50%;background:{COPPER};box-shadow:0 0 18px {COPPER};z-index:6"></div>')
        if p.get("label"):
            # the label sits right of the point; near the right edge (x close to 100) it moves to the left
            # side, right-aligned, so it never leaves the matrix or the slide
            lw = B["text_width"](str(p["label"]), "Inter", 15) * 1.08
            pos = f"left:{px + 16:.0f}px"
            room_right, room_left = gx + gw - 12 - (px + 16), px - 16 - (gx + 12)
            if lw > room_right and room_left > room_right:
                lw = min(lw, room_left)
                pos = f"left:{px - 16 - lw:.0f}px;width:{lw:.0f}px;text-align:right"
            body.append(f'<div style="position:absolute;{pos};top:{py - 11:.0f}px;font-size:15px;font-weight:500;color:var(--ivory);z-index:6;white-space:nowrap">{B["T"](p["label"])}</div>')
    xa, ya = sl.get("x_axis") or {}, sl.get("y_axis") or {}
    # x axis labels
    body.append(f'<div style="position:absolute;left:{gx}px;top:{gy + gh + 18}px;width:{gw}px;display:flex;justify-content:space-between;z-index:5">'
                + label(xa.get("low", ""), 13, "var(--cold)") + label(xa.get("label", ""), 13) + label(xa.get("high", ""), 13, "var(--cold)") + "</div>")
    # y axis labels (rotated)
    body.append(f'<div style="position:absolute;left:{MX - gh / 2 + 22}px;top:{gy + gh / 2 - 10}px;width:{gh}px;display:flex;justify-content:space-between;transform:rotate(-90deg);z-index:5">'
                + label(ya.get("low", ""), 13, "var(--cold)") + label(ya.get("label", ""), 13) + label(ya.get("high", ""), 13, "var(--cold)") + "</div>")
    return "".join(body)

# ------------------------------------------------------------------ tree
def L_tree(sl):
    """root: {title, text}, children: [{title, text, items: [str], accent}] (2-5)."""
    root = sl["root"]; ch = sl.get("children", [])
    n = len(ch)
    if not 2 <= n <= 5:
        sys.exit("tree: нужно от 2 до 5 ветвей")
    body = [B["head"](sl)]
    rw, rh = 560, 110
    rl, rt = 960 - rw / 2, TOP
    body.append(node_open("ink", round(rl), rt, rw, rh, pad="0 36px", radius=22))
    body.append('<div style="display:flex;align-items:center;gap:24px;height:100%">'
                f'<div class="ctitle bronze" style="font-size:28px">{B["T"](root.get("title"))}</div>'
                + (f'<div class="ctext" style="font-size:16px;margin-top:0;opacity:0.9">{B["T"](root["text"])}</div>' if root.get("text") else "") + "</div>" + CLOSE)
    gap = {2: 48, 3: 36, 4: 28, 5: 24}[n]
    w = (CW - gap * (n - 1)) / n
    ct = rt + rh + 90
    ch_h = 150
    # connectors
    body.append(svg_open(MX, rt + rh, CW, ct - rt - rh))
    midy = (ct - rt - rh) / 2
    body.append(f'<line x1="{960 - MX}" y1="0" x2="{960 - MX}" y2="{midy}" stroke="{LINE}"/>')
    x0 = w / 2; x1 = (n - 1) * (w + gap) + w / 2
    body.append(f'<line x1="{x0:.0f}" y1="{midy}" x2="{x1:.0f}" y2="{midy}" stroke="{LINE}"/>')
    for i in range(n):
        cx = i * (w + gap) + w / 2
        body.append(f'<line x1="{cx:.0f}" y1="{midy}" x2="{cx:.0f}" y2="{ct - rt - rh}" stroke="{LINE}"/>')
        body.append(chevron(cx, ct - rt - rh - 2, "down", 6))
    body.append("</svg>")
    tfs = {2: 28, 3: 26, 4: 23, 5: 21}[n]
    for i, c in enumerate(ch):
        left = round(MX + i * (w + gap))
        kind = kind_of(c)
        body.append(node_open(kind, left, ct, round(w), ch_h, pad="24px 28px", radius=22))
        tcls, tcol = body_colors(kind)
        body.append(f'<div class="ctitle{tcls}" style="font-size:{B["fit_lines"](c.get("title", ""), tfs, w - 56, 2, min_size=tfs - 6)}px">{B["T"](c.get("title"))}</div>')
        if c.get("text"):
            body.append(f'<div class="ctext" style="font-size:16px;margin-top:8px;{tcol}">{B["T"](c["text"])}</div>')
        body.append(CLOSE)
        leaves = c.get("items") or []
        if leaves:
            body.append(f'<div style="position:absolute;left:{left}px;top:{ct + ch_h + 24}px;width:{round(w)}px;display:flex;flex-direction:column;gap:10px;align-items:flex-start;z-index:4">')
            for lf in leaves[:6]:
                body.append(chip(lf, "", w))
            body.append("</div>")
    return "".join(body)

# ------------------------------------------------------------------ compare
def L_compare(sl):
    """left: {pill, title}, right: {pill, title}, rows: [{left, right}] (3-6), right_kind: caramel|ink|glass."""
    rows = sl["rows"]
    n = len(rows)
    if not 2 <= n <= 6:
        sys.exit("compare: нужно от 2 до 6 строк")
    body = [B["head"](sl)]
    gapc = 120
    cw = (CW - gapc) / 2
    L, R = sl.get("left") or {}, sl.get("right") or {}
    rk = sl.get("right_kind", "caramel")
    for side, meta, kind, left in (("left", L, sl.get("left_kind", "ink"), MX), ("right", R, rk, MX + cw + gapc)):
        body.append(node_open(kind, round(left), TOP, round(cw), BOT - TOP, pad="0", radius=24))
        tcls, tcol = body_colors(kind)
        body.append('<div style="display:flex;flex-direction:column;height:100%">')
        body.append(f'<div style="padding:30px 40px 22px;border-bottom:1px solid {"rgba(21,17,14,0.18)" if kind == "caramel" else "rgba(220,200,168,0.14)"};display:flex;align-items:center;gap:20px">')
        if meta.get("pill"):
            body.append(B["pill"](meta["pill"], "acc" if side == "right" and kind != "caramel" else B["pill_kind"](kind), 300))
        body.append(f'<div class="ctitle{tcls}" style="font-size:26px">{B["T"](meta.get("title"))}</div></div>')
        rfs = 20 if n <= 4 else 17
        body.append('<div style="flex:1;display:flex;flex-direction:column;justify-content:space-around;padding:10px 40px">')
        for r in rows:
            body.append(f'<div class="ctext" style="font-size:{rfs}px;margin-top:0;{tcol}">{B["T"](r.get(side))}</div>')
        body.append("</div></div>" + CLOSE)
    # arrows between rows (in the gap)
    body.append(svg_open(round(MX + cw), TOP, gapc, BOT - TOP))
    hdr = 96
    for i in range(n):
        y = hdr + 10 + (BOT - TOP - hdr - 20) * (i + 0.5) / n
        body.append(f'<line x1="30" y1="{y:.0f}" x2="{gapc - 30}" y2="{y:.0f}" stroke="{LINE_SOFT}"/>' + chevron(gapc - 30, y, "right", 6))
    body.append("</svg>")
    return "".join(body)

# ------------------------------------------------------------------ funnel
def L_funnel(sl):
    """stages: [{label, num, text, accent}] (3-5), top to bottom, widths shrink."""
    st = sl["stages"]
    n = len(st)
    if not 3 <= n <= 5:
        sys.exit("funnel: нужно от 3 до 5 ступеней")
    body = [B["head"](sl)]
    fw = 1000; fl = MX
    gap = 12
    h = (BOT - TOP - gap * (n - 1)) / n
    body.append(svg_open(fl, TOP, fw, BOT - TOP))
    for i, s in enumerate(st):
        top_w = fw * (1 - i * 0.14); bot_w = fw * (1 - (i + 1) * 0.14)
        y0 = i * (h + gap); y1 = y0 + h
        x0 = (fw - top_w) / 2; x1 = (fw - bot_w) / 2
        fill = "url(#fun_acc)" if s.get("accent") else "rgba(243,235,221,0.06)"
        stroke = COPPER if s.get("accent") else "rgba(198,162,116,0.28)"
        body.append(f'<path d="M{x0:.0f} {y0:.0f} L{x0 + top_w:.0f} {y0:.0f} L{x1 + bot_w:.0f} {y1:.0f} L{x1:.0f} {y1:.0f} Z" fill="{fill}" stroke="{stroke}" stroke-width="1"/>')
    body.append('<defs><linearGradient id="fun_acc" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#DCC8A8"/><stop offset="1" stop-color="#C6A274"/></linearGradient></defs></svg>')
    for i, s in enumerate(st):
        y0 = TOP + i * (h + gap)
        acc = s.get("accent")
        col = "color:#3A2A1E;" if acc else ""
        body.append(f'<div style="position:absolute;left:{fl}px;top:{y0:.0f}px;width:{fw}px;height:{h:.0f}px;display:flex;align-items:center;justify-content:center;z-index:4">'
                    f'<div class="ctitle{"" if acc else " bronze"}" style="font-size:{24 if n <= 4 else 21}px;text-align:center;{col}">{B["T"](s.get("label"))}</div></div>')
        # right column: number + text
        rl = fl + fw + 80; rw = CW - fw - 80
        body.append(f'<div style="position:absolute;left:{rl}px;top:{y0:.0f}px;width:{rw}px;height:{h:.0f}px;display:flex;align-items:center;gap:28px;z-index:4;border-top:1px solid rgba(220,200,168,0.12)">')
        if s.get("num"):
            body.append(f'<div class="num{"" if acc else " bronze"}" style="font-size:{B["fit_size"](s["num"], 44 if n <= 4 else 36, 260, min_size=26)}px;width:260px;flex-shrink:0;{"color:" + COPPER + ";" if acc else ""}">{B["T"](s["num"])}</div>')
        if s.get("text"):
            body.append(f'<div class="ctext" style="font-size:17px;margin-top:0">{B["T"](s["text"])}</div>')
        body.append("</div>")
    return "".join(body)

# ------------------------------------------------------------------ loop (two linked cycles, the living line)
def L_loop(sl):
    """left: {title, top, bottom, caption}, right: {title, top, bottom, caption}, center_top: {title, text}, center_bottom: {title, text}, anim (default true)."""
    L, R = sl.get("left") or {}, sl.get("right") or {}
    body = [B["head"](sl)]
    fw, fh = 1520, 540
    fl, ft = 960 - fw / 2, 376
    on = want_anim(sl, True)
    if on:
        body.append(anim("lemniscate", fw, fh, round(fl), ft, strength=1.0, z=2))
    else:
        # quiet static figure
        body.append(svg_open(round(fl), ft, fw, fh))
        pts = []
        for k in range(400):
            th = k / 400 * 2 * math.pi
            d = 1 + math.sin(th) ** 2
            x = math.cos(th) / d; y = math.sin(th) * math.cos(th) / d
            pts.append(f"{fl + fw / 2 + x * (fw / 2 - 40) - fl:.1f},{ft + fh / 2 + y * (fh - 80) - ft:.1f}")
        body.append(f'<polygon points="{" ".join(pts)}" stroke="{LINE}" stroke-width="1.5"/></svg>')
    # loop centres
    lcx, rcx = fl + fw * 0.26, fl + fw * 0.74
    cy = ft + fh / 2
    for cx, side in ((lcx, L), (rcx, R)):
        body.append(f'<div style="position:absolute;left:{cx - 210:.0f}px;top:{cy - 160:.0f}px;width:420px;height:320px;display:flex;flex-direction:column;align-items:center;justify-content:space-between;text-align:center;z-index:4">')
        body.append(label(side.get("top", ""), 14, "var(--latte)", "width:400px;line-height:1.4;opacity:0.9"))
        body.append(f'<div class="ctitle bronze" style="font-size:{B["fit_size"](side.get("title", ""), 44, 380, min_size=28)}px">{B["T"](side.get("title"))}</div>')
        body.append(label(side.get("bottom", ""), 14, "var(--latte)", "width:400px;line-height:1.4;opacity:0.9"))
        body.append("</div>")
        if side.get("caption"):
            body.append(f'<div style="position:absolute;left:{cx - 260:.0f}px;top:{ft + fh + 8}px;width:520px;text-align:center;z-index:4">'
                        f'<div class="ctitle" style="font-size:22px;letter-spacing:0.06em;text-transform:uppercase;color:var(--ivory)">{B["T"](side["caption"])}</div></div>')
    for key, top in (("center_top", ft - 26), ("center_bottom", ft + fh - 124)):
        c = sl.get(key)
        if not c:
            continue
        body.append(f'<div style="position:absolute;left:{960 - 230}px;top:{top}px;width:460px;text-align:center;z-index:4">'
                    f'<div style="font-size:19px;font-weight:600;color:var(--ivory)">{B["T"](c.get("title"))}</div>'
                    + (f'<div class="ctext" style="font-size:16px;margin-top:6px">{B["T"](c["text"])}</div>' if c.get("text") else "") + "</div>")
    return "".join(body)

# Nested structure of the scheme layouts, merged into build_deck's schema checks:
# required keys inside list elements and the single-object fields that must be dicts.
ELEM_REQUIRED = {"matrix": {"points": {"x", "y"}}}
DICT_FIELDS = {"cycle": {"center"}, "tree": {"root"}, "layers": {"side"},
               "compare": {"left", "right"}, "loop": {"left", "right", "center_top", "center_bottom"},
               "matrix": {"x_axis", "y_axis"}}

LAYOUTS = {"flow": L_flow, "layers": L_layers, "cycle": L_cycle, "matrix": L_matrix,
           "tree": L_tree, "compare": L_compare, "funnel": L_funnel, "loop": L_loop}

NODE = {"title", "text", "pill", "kind", "accent"}
SCHEMA = {
    "flow":    ({"items", "anim", "accent_last"},                         {"items"},            {"items": NODE}),
    "layers":  ({"layers", "side"},                                       {"layers"},           {"layers": {"title", "text", "items", "kind", "accent"}, "side": {"label", "title", "text"}}),
    "cycle":   ({"items", "center", "anim"},                              {"items"},            {"items": {"title", "text", "accent"}, "center": {"num", "title", "text"}}),
    "matrix":  ({"x_axis", "y_axis", "quadrants", "points"},              {"quadrants"},        {"quadrants": {"title", "text", "kind", "accent"}, "points": {"x", "y", "label"},
                                                                                                 "x_axis": {"label", "low", "high"}, "y_axis": {"label", "low", "high"}}),
    "tree":    ({"root", "children"},                                     {"root", "children"}, {"root": {"title", "text"}, "children": {"title", "text", "items", "kind", "accent"}}),
    "compare": ({"left", "right", "rows", "left_kind", "right_kind"},     {"rows"},             {"left": {"pill", "title"}, "right": {"pill", "title"}, "rows": {"left", "right"}}),
    "funnel":  ({"stages"},                                               {"stages"},           {"stages": {"label", "num", "text", "accent"}}),
    "loop":    ({"left", "right", "center_top", "center_bottom", "anim"}, {"left", "right"},    {"left": {"title", "top", "bottom", "caption"}, "right": {"title", "top", "bottom", "caption"},
                                                                                                 "center_top": {"title", "text"}, "center_bottom": {"title", "text"}}),
}
