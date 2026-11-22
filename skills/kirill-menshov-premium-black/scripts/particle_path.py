#!/usr/bin/env python3
"""
Premium Black: procedural light trail along a path (the "living line" of the design system).

A faint dust ribbon marks the whole path; a bright head with a bronze tail travels along it.
Output: an animated GIF (plays in PowerPoint and browsers) and one still PNG frame (used in PDF and
in the HTML snapshot). Everything is seeded, so the same spec gives the same pixels.

A module, not a command: build_deck.py (through schemes.py) calls generate() and keeps the results
in its cache under <state>/cache/anim/. Parameters: frames 56, 10 fps, seed 7, GIF palette 96
colours; the still frame is the phase `still` (0..1 along the path); for the lemniscate 0.27 puts
the head near the crossing.

Shapes (all fit the w x h box with a small margin):
  lemniscate  - figure eight, two loops (cycle of two processes)
  ellipse     - closed ring (one cycle)
  line        - straight horizontal run left to right (a flow); head fades at both ends
  arc         - gentle upward arc left to right (growth)
"""
import math
import numpy as np
from PIL import Image, ImageFilter

BRONZE = np.array([198, 162, 116], dtype=np.float32) / 255     # #C6A274
LATTE = np.array([220, 200, 168], dtype=np.float32) / 255      # #DCC8A8
WHITE = np.array([255, 247, 232], dtype=np.float32) / 255      # warm white head

def path_points(shape, w, h, n=4000, margin=0.06):
    """Returns (x, y) arrays of n points along the path for t in [0,1), and `closed` flag."""
    t = np.linspace(0, 1, n, endpoint=False)
    mw, mh = w * margin, h * margin
    if shape == "lemniscate":
        a = 1.0
        th = t * 2 * math.pi
        d = 1 + np.sin(th) ** 2
        x = a * np.cos(th) / d
        y = a * np.sin(th) * np.cos(th) / d
        closed = True
    elif shape == "ellipse":
        th = -t * 2 * math.pi + math.pi / 2       # start at top, clockwise
        x, y = np.cos(th), -np.sin(th)
        closed = True
    elif shape == "line":
        x = t * 2 - 1
        y = np.zeros_like(t)
        closed = False
    elif shape == "arc":
        x = t * 2 - 1
        y = -(1 - (x ** 2)) * 0.8 + 0.4
        closed = False
    else:
        raise ValueError(f"unknown shape {shape}")
    # normalise into the box
    x = (x - x.min()) / max(x.max() - x.min(), 1e-9)
    yr = y.max() - y.min()
    y = (y - y.min()) / yr if yr > 1e-9 else np.full_like(y, 0.5)
    X = mw + x * (w - 2 * mw)
    Y = mh + y * (h - 2 * mh) if yr > 1e-9 else np.full_like(y, h / 2)
    return X.astype(np.float32), Y.astype(np.float32), closed

def normals(X, Y):
    dx = np.gradient(X); dy = np.gradient(Y)
    L = np.hypot(dx, dy) + 1e-9
    return (-dy / L).astype(np.float32), (dx / L).astype(np.float32)

def splat(w, h, xs, ys, ws):
    img, _, _ = np.histogram2d(ys, xs, bins=[h, w], range=[[0, h], [0, w]], weights=ws)
    return img.astype(np.float32)

def _box(arr, r, axis):
    """Box blur of radius r along axis via cumulative sums (edge-clamped)."""
    if r < 1:
        return arr
    n = arr.shape[axis]
    pad = [(0, 0), (0, 0)]
    pad[axis] = (r + 1, r)
    a = np.pad(arr, pad, mode="edge")
    c = np.cumsum(a, axis=axis, dtype=np.float64)
    hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return ((hi - lo) / (2 * r + 1)).astype(np.float32)

def blur(arr, sigma):
    """Gaussian approximation by three box blurs (no scipy needed, float precision kept)."""
    if sigma <= 0:
        return arr
    try:
        from scipy.ndimage import gaussian_filter
        return gaussian_filter(arr, sigma, mode="nearest").astype(np.float32)
    except ImportError:
        pass
    r = max(1, int(round(math.sqrt(sigma * sigma * 12 / 3 + 1) / 2)))
    out = arr
    for _ in range(3):
        out = _box(_box(out, r, 0), r, 1)
    return out

def render_frame(w, h, X, Y, NX, NY, closed, phase, rng_fixed, strength=1.0, dust=None, ribbon=1.0):
    """Returns RGBA float image (h, w, 4) in 0..1 for the head at `phase`."""
    n = len(X)
    t = np.arange(n, dtype=np.float32) / n
    # ---- dust ribbon (static positions, slight twinkle)
    if dust is None:
        dust = {}
        rng = rng_fixed
        m = 9000
        idx = rng.integers(0, n, m)
        strands = rng.integers(0, 4, m)
        amp = np.array([6, 11, 16, 24], dtype=np.float32)[strands] * ribbon
        ph = np.array([0.0, 1.9, 3.7, 5.1], dtype=np.float32)[strands]
        off = amp * np.sin(t[idx] * 2 * math.pi * 3 + ph) + rng.normal(0, 2.2, m).astype(np.float32)
        # sparse wide scatter
        wide = rng.random(m) < 0.14
        off[wide] += rng.normal(0, 26 * ribbon, wide.sum()).astype(np.float32)
        dust["x"] = X[idx] + NX[idx] * off
        dust["y"] = Y[idx] + NY[idx] * off
        dust["t"] = t[idx]
        dust["w"] = (rng.random(m) ** 2 * 0.9 + 0.1).astype(np.float32) * np.where(wide, 0.5, 1.0)
        dust["tw"] = rng.random(m).astype(np.float32)
        # three thin continuous strands (the braided look of the line)
        sx, sy, st = [], [], []
        for amp, ph in ((5, 0.4), (10, 2.3), (15, 4.4)):
            off = amp * ribbon * np.sin(t * 2 * math.pi * 3 + ph)
            sx.append(X + NX * off); sy.append(Y + NY * off); st.append(t)
        dust["sx"] = np.concatenate(sx); dust["sy"] = np.concatenate(sy); dust["st"] = np.concatenate(st)
    twinkle = 0.85 + 0.15 * np.sin(dust["tw"] * 6.283 + phase * 6.283)
    base_w = dust["w"] * twinkle * 8.0 * strength
    # ---- trail behind head: brightness by circular distance behind the head
    d = (phase - dust["t"]) % 1.0 if closed else (phase - dust["t"])
    tail = 0.3
    trail = np.where((d >= 0) & (d < tail), np.exp(-d / (tail * 0.3)), 0).astype(np.float32)
    ds = (phase - dust["st"]) % 1.0 if closed else (phase - dust["st"])
    strail = np.where((ds >= 0) & (ds < tail), np.exp(-ds / (tail * 0.3)), 0).astype(np.float32)
    if not closed:
        # fade head in/out near the ends of an open path
        fade = min(1.0, phase / 0.08, (1 - phase) / 0.08) if 0 <= phase <= 1 else 0.0
        trail *= fade
    else:
        fade = 1.0
    ws_core = base_w * 1.6 + trail * dust["w"] * 70 * strength
    ws_glow = base_w * 0.6 + trail * dust["w"] * 90 * strength
    core = splat(w, h, dust["x"], dust["y"], ws_core)
    glow = splat(w, h, dust["x"], dust["y"], ws_glow)
    if not closed:
        strail *= fade
    sw = (0.9 * strength + strail * 14 * strength).astype(np.float32)
    core += splat(w, h, dust["sx"], dust["sy"], sw)
    glow += splat(w, h, dust["sx"], dust["sy"], sw * 0.5)
    # ---- head: dense sparks right at the head point
    hi = int(phase * n) % n
    hx, hy = X[hi], Y[hi]
    rng = np.random.default_rng(int(phase * 10000) + 11)
    k = 500
    sx = hx + rng.normal(0, 7, k); sy = hy + rng.normal(0, 7, k)
    head = splat(w, h, np.clip(sx, 0, w - 1), np.clip(sy, 0, h - 1), np.full(k, 1.2 * fade * strength, np.float32))
    L_core = blur(core * 0.05 + head * 0.012, 0.7)
    L_mid = blur(glow * 0.05 + head * 0.03, 3.5)
    L_glow = blur(glow * 0.30 + head * 1.8, 34)
    L = L_core + L_mid + L_glow
    floor = 0.02                      # no faint lift over the whole box: the edges must vanish into the slide
    A = np.clip((L - floor) / (1 - floor), 0, 1)
    # colour: bronze in the dark, latte in the middle, warm white in the hottest core
    k1 = np.clip(L / 0.9, 0, 1)[..., None]
    k2 = np.clip((L - 0.9) / 1.4, 0, 1)[..., None]
    rgb = BRONZE * (1 - k1) + LATTE * k1
    rgb = rgb * (1 - k2) + WHITE * k2
    out = np.concatenate([rgb, A[..., None]], axis=2)
    return out, dust

def composite(frame, bg_rgb):
    a = frame[..., 3:4]
    rgb = frame[..., :3] * a + bg_rgb * (1 - a)
    return (np.clip(rgb, 0, 1) * 255).astype(np.uint8), a[..., 0]

def to_gif_frame(rgb8, alpha, bg8, colors=255):
    im = Image.fromarray(rgb8, "RGB").quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    arr = np.array(im, dtype=np.uint8)
    arr[alpha < 0.035] = 255
    out = Image.fromarray(arr, "P")
    pal = im.getpalette()[:colors * 3] + [0] * ((255 - colors) * 3) + list(bg8)
    out.putpalette(pal)
    out.info["transparency"] = 255
    return out

def generate(shape, w, h, png_path=None, gif_path=None, *, frames=56, fps=10, seed=7, still=None,
             strength=1.0, bg="15110E", colors=96, ribbon=None):
    """Writes the still PNG and/or the looping GIF of one path. Same seed, same pixels.

    The dust ribbon is built while rendering the still frame, so the GIF is always made after it:
    a GIF generated later on its own matches a PNG generated earlier."""
    bg8 = tuple(int(bg[i:i + 2], 16) for i in (0, 2, 4))
    bgf = np.array(bg8, dtype=np.float32) / 255
    X, Y, closed = path_points(shape, w, h)
    NX, NY = normals(X, Y)
    rng = np.random.default_rng(seed)
    still = still if still is not None else {"lemniscate": 0.27, "ellipse": 0.18, "line": 0.62, "arc": 0.66}.get(shape, 0.5)
    ribbon = ribbon if ribbon is not None else (0.55 if shape in ("line", "arc") else 1.0)
    frame, dust = render_frame(w, h, X, Y, NX, NY, closed, still, rng, strength, ribbon=ribbon)
    if png_path:
        rgba = np.concatenate([(np.clip(frame[..., :3], 0, 1) * 255).astype(np.uint8), (frame[..., 3] * 255).astype(np.uint8)[..., None]], axis=2)
        Image.fromarray(rgba, "RGBA").save(png_path, format="PNG")
    if gif_path:
        images = []
        for i in range(frames):
            phase = i / frames
            fr, _ = render_frame(w, h, X, Y, NX, NY, closed, phase, rng, strength, dust, ribbon)
            rgb8, alpha = composite(fr, bgf)
            images.append(to_gif_frame(rgb8, alpha, bg8, colors))
        images[0].save(gif_path, format="GIF", save_all=True, append_images=images[1:], duration=int(1000 / fps),
                       loop=0, disposal=2, transparency=255, optimize=False)
