#!/usr/bin/env python3
"""
Kirill Menshov Premium Black presentation builder (OuroborosHub adaptation of the original 2.3.2).

Usage:
  build_deck.py spec.json [--photo NAME=PATH].. [--out "Name v1.html"] [--render] [--scale 1|2]
                      [--pdf "Name v1.pdf"] [--pptx "Name v1.pptx" [--flat]] [--gif]

Every build writes into a NEW folder <state>/outputs/<job>/: the self-contained HTML (CSS, fonts,
photos and living-line frames embedded, a Content-Security-Policy forbidding any network request),
and with --render/--pdf/--pptx the PNG slides in png/, the PDF, the PPTX and a fonts archive for the
machine that opens the PPTX. --render always writes PNG slides and a PDF (with --pptx too); --pptx
alone writes only the PPTX. Names are bare file names.

Reads (nothing else): the spec file the caller names, the photos of the bundled library, and the
photo files the operator passes for THIS build with --photo NAME=PATH (repeatable; the name is
what the spec's photo field refers to). A path inside the spec grants no permission: spec photo
values must be library keys or names declared with --photo. Every photo path and name is checked
against network addresses before anything is opened (premium_common.refused_if_url). Own photos
are decoded and re-encoded, so only pixels reach the deck: no EXIF, ICC, XMP or JPEG comment.
Nothing outside the new job folder and the skill's caches is written or deleted. The build itself
never uses the network.

Scheme layouts (flow, layers, cycle, matrix, tree, compare, funnel, loop) live in scripts/schemes.py.
The living line (anim) is rendered by scripts/particle_path.py and cached under <state>/cache/anim/.

Exit codes: 2 text hygiene (em/en dashes, exclamation marks, Latin "AI"), 3 the spec does not match
the layout catalogue or has an invalid value, 4 renderer missing (scripts/setup_renderer.py),
5 refused input or output name. `--version` prints the version.
"""
import argparse, base64, io, json, os, re, shutil, sys, time, html as htmlmod
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import premium_common as pc
import schemes

VERSION = pc.VERSION   # keep in sync with SKILL.md `version` and CHANGELOG.md

SKILL_DIR = str(pc.SKILL_DIR)
ASSETS = os.path.join(SKILL_DIR, "assets")
PHOTOS = os.path.join(ASSETS, "photos")
CTX = {"state": None, "photos": {}, "declared": {}}  # set by main() for each build

W, H = 1920, 1080
MX = 72                       # side margin
CW = W - 2 * MX               # 1776 content width

# ------------------------------------------------------------------ hygiene
BAD = {"—": "длинное тире (замени на дефис -)", "–": "среднее тире (замени на дефис -)",
       "!": "восклицательный знак"}
LATIN_AI = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])")

def walk_strings(obj, path="spec"):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("photo", "focus", "type", "kind", "fill", "width", "cols", "align", "photos"):
                continue
            yield from walk_strings(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_strings(v, f"{path}[{i}]")

def hygiene(spec, strict=True):
    errors, warns = [], []
    for path, s in walk_strings(spec):
        for ch, why in BAD.items():
            if ch in s:
                errors.append(f"{path}: {why}: «{s[:60]}»")
        if LATIN_AI.search(s):
            errors.append(f"{path}: латинское AI, пиши ИИ: «{s[:60]}»")
        if re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", s):
            errors.append(f"{path}: эмодзи запрещены: «{s[:60]}»")
    for i, sl in enumerate(spec.get("slides", [])):
        t = strip_tags(sl.get("title", ""))
        if len(t) > 90:
            warns.append(f"slide {i+1}: заголовок длиннее 90 знаков ({len(t)}), сократи до одного тезиса")
        for it in sl.get("items", []) or []:
            if isinstance(it, dict) and len(strip_tags(it.get("text", ""))) > 120:
                warns.append(f"slide {i+1}: текст карточки длиннее 120 знаков, сократи")
        for path, val in walk_nums(sl, f"slide {i+1}"):
            if len(strip_tags(val)) > NUM_MAX:
                warns.append(f"{path}: число длиннее {NUM_MAX} знаков, «{val}». Будет уменьшено, лучше округли: 1,24 трлн вместо 1 240 000 млн")
        for path, val in walk_pills(sl, f"slide {i+1}"):
            n = len(strip_tags(val))
            if n > PILL_MAX:
                warns.append(f"{path}: плашка длиннее {PILL_MAX} знаков ({n}), «{val}». Она будет уменьшена или перенесена, лучше сократи до 2-3 слов")
    return errors, warns

PILL_KEYS = ("pill", "card_pill", "author", "contacts")
NUM_MAX = 12    # знаков в крупном числе: «15,5 млрд ₽» ещё помещается, «1 240 000 млн» уже нет

def walk_nums(obj, path):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "num" and isinstance(v, str):
                yield f"{path}.num", v
            else:
                yield from walk_nums(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for j, v in enumerate(obj):
            yield from walk_nums(v, f"{path}[{j}]")

PILL_MAX = 28   # знаков: плашка 15px помещается в самую узкую карточку (4 в ряд) без подгонки

def walk_pills(obj, path):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in PILL_KEYS:
                vals = v if isinstance(v, list) else [v]
                for j, x in enumerate(vals):
                    if isinstance(x, str) and x:
                        yield f"{path}.{k}" + (f"[{j}]" if isinstance(v, list) else ""), x
            else:
                yield from walk_pills(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for j, v in enumerate(obj):
            yield from walk_pills(v, f"{path}[{j}]")

def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "")

# ------------------------------------------------------------------ text helpers

def fit_size(text, base, width, ratio=0.82, min_size=None):
    """Shrink a display font so the longest explicit line fits the width (Unbounded is wide: ~0.82em per char)."""
    lines = [strip_tags(l) for l in str(text or "").replace("[[", "").replace("]]", "").split("\n")]
    longest = max((len(l) for l in lines), default=1) or 1
    size = min(base, width / (ratio * longest))
    return int(max(min_size or base * 0.6, size))

def fit_lines(text, base, width, max_lines, ratio=0.82, min_size=None):
    """Shrink a wrapping headline so it needs at most max_lines at the chosen size."""
    plain = strip_tags(str(text or "").replace("[[", "").replace("]]", ""))
    size = base
    floor = min_size or base * 0.7
    while size > floor:
        per_line = max(1, int(width / (ratio * size)))
        words, lines, cur = plain.split(), 1, 0
        for w in words:
            if cur and cur + 1 + len(w) > per_line:
                lines += 1; cur = len(w)
            else:
                cur += (1 if cur else 0) + len(w)
        if lines <= max_lines:
            break
        size -= 2
    return int(max(floor, size))

def T(s):
    """Spec text -> HTML. Escapes, then applies [[bronze]] and newline -> <br>."""
    if s is None:
        return ""
    s = htmlmod.escape(str(s), quote=True)
    s = re.sub(r"\[\[(.+?)\]\]", r'<span class="bronze">\1</span>', s, flags=re.S)
    return s.replace("\n", "<br>")

USER_PHOTO_MAX_BYTES = 30 * 1024 * 1024
USER_PHOTO_MAX_PIXELS = 60_000_000
USER_PHOTO_MAX_SIDE = 2400

def parse_photos(items):
    """--photo NAME=PATH (repeatable): the only way an own photo enters a build.

    NAME is what the spec's photo field refers to; without "=" it is the file's base name.
    NAME and PATH are both checked against network addresses before anything is opened;
    the file must exist and be a regular file. A name that collides with the bundled
    library is refused, so a spec name always means the same photo in every build.
    """
    declared = {}
    for item in items or []:
        text = str(item).strip()
        if not text:
            raise pc.Refused("--photo: пустое значение; формат --photo имя=путь")
        name, eq, raw = text.partition("=")
        name = name.strip()
        raw = raw.strip() if eq else text
        if not eq:
            name = os.path.basename(raw)
        pc.refused_if_url(raw)
        pc.refused_if_url(name)
        if not pc.photo_name_is_valid(name):
            raise pc.Refused(f"--photo: имя «{name}» не подходит; это простое имя без папок и служебных "
                             f"знаков, например --photo дача=~/снимки/дача.jpg")
        if pc.photo_name_is_library(name):
            raise pc.Refused(f"--photo: имя «{name}» занято фотографией библиотеки, выбери другое")
        if name in declared:
            raise pc.Refused(f"--photo: имя «{name}» передано дважды")
        path = pc.pathlib.Path(raw).expanduser()
        try:
            path = path.resolve(strict=True)
        except OSError:
            raise pc.Refused(f"--photo {name}: файл не найден: {raw}") from None
        if not path.is_file():
            raise pc.Refused(f"--photo {name}: нужен обычный файл, а не папка: {raw}")
        declared[name] = path
    return declared

def _operator_photo(path, key):
    """A photo the operator passed with --photo, decoded and re-encoded without any metadata
    (no EXIF, ICC, XMP or JPEG comment): only pixels reach the deck."""
    import warnings
    from PIL import Image, ImageOps
    if not path.is_file() or path.stat().st_size > USER_PHOTO_MAX_BYTES:
        raise pc.Refused(f"фото «{key}»: нужен обычный файл до 30 МБ")
    too_big = f"фото «{key}»: снимок больше {USER_PHOTO_MAX_PIXELS} пикселей; уменьши его или пропусти через tone_photo.py"
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = USER_PHOTO_MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            opened = Image.open(path)                # reads the header only
        with opened as im:
            if im.format not in ("JPEG", "PNG", "WEBP"):
                raise pc.Refused(f"фото «{key}»: поддерживаются JPEG, PNG и WebP, а не {im.format}")
            width, height = im.size
            if width * height > USER_PHOTO_MAX_PIXELS:   # checked BEFORE any pixel is decoded
                raise pc.Refused(too_big)
            im.info.pop("comment", None)   # Pillow copies the JPEG comment through convert()
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.info.pop("comment", None)   # and writes it back as a COM marker on save
            im.thumbnail((USER_PHOTO_MAX_SIDE, USER_PHOTO_MAX_SIDE), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=92)
    except pc.Refused:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise pc.Refused(too_big) from None
    except Exception as exc:  # noqa: BLE001 - any decoder failure is a refused input
        raise pc.Refused(f"фото «{key}» не читается как изображение: {exc}") from None
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

def photo_src(key):
    """Library key or a name declared with --photo -> data URI. A path in the spec is NOT
    permission to read: only the bundled library and the operator's --photo files enter a
    build. Network addresses are refused before any path logic."""
    if not key:
        return ""
    if not isinstance(key, str):
        raise pc.Refused(f"photo должно быть строкой: {key!r}")
    cached = CTX["photos"].get(key)
    if cached:
        return cached
    pc.refused_if_url(key)
    norm = key[2:] if key.startswith("./") else key          # old spec spellings keep working
    declared = CTX.get("declared") or {}
    if norm in declared:
        uri = _operator_photo(declared[norm], key)
    elif "/" not in norm and "\\" not in norm:
        library = pc.library_photos()             # the same exact table as the --photo collision check
        lib_key = pc.library_photo_key(norm, library)
        if lib_key is None:
            raise pc.Refused(f"Фотография не найдена: {key}. Доступные из библиотеки (точное написание): "
                             f"{', '.join(sorted(library))}. "
                             f"Свой снимок передай в эту сборку: --photo {norm}=<полный путь к выбранному файлу>")
        with open(library[lib_key], "rb") as f:
            uri = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode("ascii")
    else:
        raise pc.Refused(f"фото «{key}»: путь в спецификации - не разрешение на чтение файла. Передай снимок "
                         "оператором: build_deck.py spec.json --photo имя=<полный путь к выбранному файлу> "
                         "и укажи это имя в поле photo")
    CTX["photos"][key] = uri
    return uri

def focus_style(sl):
    f = sl.get("focus")
    return f"--focus:{f};" if f else ""

VF = '<div class="vf{cls}"{style}><i></i><i></i><i></i><i></i><b></b><span{sstyle}>{n}</span></div>'
def vf(n, size="m", extra_style=""):
    n = f"{int(n):02d}" if str(n).isdigit() else htmlmod.escape(str(n))
    if size == "big":
        return VF.format(cls=" big", style=f' style="{extra_style}"' if extra_style else "", sstyle="", n=n)
    if size == "s":
        return VF.format(cls="", style=f' style="width:90px;height:60px;{extra_style}"', sstyle=' style="font-size:26px"', n=n)
    if size == "xs":
        return VF.format(cls="", style=f' style="width:80px;height:52px;{extra_style}"', sstyle=' style="font-size:22px"', n=n)
    return VF.format(cls="", style=f' style="{extra_style}"' if extra_style else "", sstyle="", n=n)

ARROW_UP = '<svg viewBox="0 0 16 16"><path d="M8 13V3M3 8l5-5 5 5" stroke="currentColor" stroke-width="1.8" fill="none"/></svg>'
ARROW_DN = '<svg viewBox="0 0 16 16"><path d="M8 3v10M3 8l5 5 5-5" stroke="currentColor" stroke-width="1.8" fill="none"/></svg>'
FLAT = '<svg viewBox="0 0 16 16"><path d="M3 8h10" stroke="currentColor" stroke-width="1.8" fill="none"/></svg>'

def delta(d, mt=16):
    """d: string like '+4,1 млн за год' or dict {text, dir: up|down|flat}."""
    if not d:
        return ""
    if isinstance(d, str):
        d = {"text": d, "dir": "down" if d.strip().startswith(("-", "−")) else ("flat" if d.strip().lower().startswith("без") else "up")}
    elif not isinstance(d, dict):
        sys.exit(f"delta: строка или объект {{text, dir: up|down|flat}}, а не {type(d).__name__}")
    icon = {"up": ARROW_UP, "down": ARROW_DN, "flat": FLAT}[d.get("dir", "up")]
    col = ";color:var(--cold)" if d.get("dir") == "flat" else ""
    return f'<div class="delta" style="margin-top:{mt}px{col}">{icon}<span>{T(d["text"])}</span></div>'

# ------------------------------------------------------------------ font metrics (engine-independent fitting)
# assets/font_metrics.json holds advance widths (per 1000 em) of Inter 500/300 and Unbounded 600, extracted
# from the bundled woff2 files. The builder measures text itself and writes explicit font-size and width
# into the HTML, so pills and numbers fit even if the HTML is consumed by a renderer other than Chromium.
METRICS = json.load(open(os.path.join(ASSETS, "font_metrics.json"), encoding="utf-8"))
SAFETY = 1.08   # 8% reserve for other rasterisers (PowerPoint, LibreOffice) and rounding

def text_width(text, fam="Inter", size=15, tracking=0.0):
    """Width in px of a single line of plain text (no markup)."""
    t = strip_tags(text)
    w = METRICS.get(fam, METRICS["Inter"])
    avg = 620 if fam == "Inter" else 800
    units = sum(w.get(ch, avg) for ch in t)
    return units / 1000 * size + tracking * size * len(t)

PILL_PAD = 18          # horizontal padding each side (deck.css)
PILL_BORDER = 1
PILL_FS = 15
PILL_MIN_FS = 12

def pill(text, kind="", avail=None, style=""):
    """Capsule with explicit width. `avail` = horizontal room in px (card inner width, column width).
    Fit order: 15px, shrink to 12px, then two lines. Width is written explicitly so the box never
    depends on the layout engine's shrink-to-fit."""
    if not text:
        return ""
    label = str(text)
    fs = PILL_FS
    chrome_w = 2 * (PILL_PAD + PILL_BORDER)
    def need(f, s_=label):
        return text_width(s_, "Inter", f, 0.02) * SAFETY + chrome_w
    if avail:
        while need(fs) > avail and fs > PILL_MIN_FS:
            fs -= 0.5
    lines = [label]
    if avail and need(fs) > avail:
        # two lines: split at the space closest to the middle, prefer break after the separator ' · '
        words = label.split(" ")
        best = None
        for k in range(1, len(words)):
            a, b = " ".join(words[:k]), " ".join(words[k:])
            score = max(need(fs, a), need(fs, b)) + (0 if a.endswith("·") else 4)
            if best is None or score < best[0]:
                best = (score, a, b)
        if best:
            lines = [best[1], best[2]]
            fs = max(fs, PILL_FS - 1)
            while max(need(fs, ln) for ln in lines) > avail and fs > PILL_MIN_FS:
                fs -= 0.5
    width = max(need(fs, ln) for ln in lines)
    if avail:
        width = min(width, avail)
    cls = "pill" + (f" {kind}" if kind else "") + (" wrap" if len(lines) > 1 else "")
    st = f"font-size:{fs:g}px;width:{round(width)}px;" + style
    inner = "<br>".join(T(ln) for ln in lines)
    return f'<span class="{cls}" style="{st}">{inner}</span>'

def pill_kind(card_kind):
    return {"photo": "onphoto", "caramel": "oncaramel"}.get(card_kind, "")

def num_style(text, size, avail, min_ratio=0.5):
    """Explicit font-size for an Unbounded number so it fits `avail` px; wraps as a last resort."""
    base = float(size)         # num_size may arrive as a valid numeric string ("96")
    fs = base
    while text_width(text, "Unbounded", fs) * SAFETY > avail and fs > base * min_ratio:
        fs -= 2
    st = f"font-size:{fs:g}px;"     # float(96) renders as 96, not 96.0
    if text_width(text, "Unbounded", fs) * SAFETY > avail:
        st += "white-space:normal;line-height:1.05;"
    return st

def eyebrow(sl, key="eyebrow", align="left"):
    e = sl.get(key)
    if not e:
        return ""
    e2 = sl.get("eyebrow2")
    inner = f'<span class="dot"></span><span>{T(e)}</span>'
    if e2:
        inner += f'<span class="rule"></span><span style="color:var(--latte);opacity:0.85;letter-spacing:0.2em">{T(e2)}</span>'
    if align == "right":
        inner = f'<span>{T(e)}</span><span class="dot"></span>'
        return f'<div class="eyebrow" style="justify-content:flex-end">{inner}</div>'
    return f'<div class="eyebrow">{inner}</div>'

def head(sl, h1_style=""):
    """Standard header block: eyebrow, h1, sub."""
    out = ['<div class="head">', eyebrow(sl)]
    if sl.get("title"):
        fs = fit_lines(sl["title"], 74, 1560, 2, min_size=56)
        out.append(f'<h1 style="font-size:{fs}px;{h1_style}">{T(sl["title"])}</h1>')
    if sl.get("sub"):
        out.append(f'<div class="sub">{T(sl["sub"])}</div>')
    out.append("</div>")
    return "".join(out)

def bg_photo(sl, scrim=None):
    src = photo_src(sl.get("photo"))
    if not src:
        return ""
    sc = f' style="background:{scrim}"' if scrim else ""
    return f'<img class="bg" src="{src}" style="{focus_style(sl)}"><div class="scrim"{sc}></div><div class="tintall"></div>'

def photo_layers(key, focus=None, grade="default"):
    src = photo_src(key)
    f = f' style="--focus:{focus}"' if focus else ""
    g = {"default": '<div class="grade"></div>',
         "soft": '<div class="grade" style="background:linear-gradient(180deg,rgba(21,17,14,0.05),rgba(21,17,14,0.35))"></div>',
         "half": '<div class="grade" style="background:linear-gradient(180deg,transparent 50%,rgba(21,17,14,0.6))"></div>',
         "none": ""}[grade]
    return f'<img src="{src}"{f}><div class="tint"></div>{g}'

def row_geometry(n, gap=None):
    gap = gap or {2: 40, 3: 36, 4: 32, 5: 34}.get(n, 32)
    w = (CW - gap * (n - 1)) / n
    return [(round(MX + i * (w + gap)), round(w)) for i in range(n)], round(w)

# ------------------------------------------------------------------ layouts
def L_title(sl):
    body = [bg_photo(sl)]
    body.append('<div style="position:absolute;left:72px;top:300px;width:1776px;z-index:5">')
    body.append(eyebrow(sl))
    fs = fit_size(sl.get("title"), 132, 1700, min_size=88)
    body.append(f'<h1 class="bronze" style="font-size:{fs}px;line-height:0.98;margin-top:34px">{T(sl.get("title"))}</h1>')
    if sl.get("sub"):
        body.append(f'<div class="sub" style="font-size:30px;margin-top:34px;max-width:1000px">{T(sl["sub"])}</div>')
    body.append("</div>")
    return "".join(body)

def L_title_short(sl):
    body = [bg_photo(sl, "linear-gradient(180deg,rgba(21,17,14,0.05),rgba(21,17,14,0.2) 50%,rgba(21,17,14,0.85))")]
    body.append('<div style="position:absolute;left:72px;bottom:170px;width:1700px;z-index:5">')
    body.append(eyebrow(sl))
    fs = fit_size(sl.get("title"), 150, 1700, min_size=96)
    body.append(f'<h1 class="bronze" style="font-size:{fs}px;line-height:0.96;margin-top:30px">{T(sl.get("title"))}</h1>')
    body.append("</div>")
    return "".join(body)

def L_section(sl):
    right = sl.get("align") == "right"
    body = [bg_photo(sl)]
    if right:
        body.append('<div style="position:absolute;right:72px;top:300px;width:1100px;text-align:right;z-index:5">')
        body.append(eyebrow(sl, align="right"))
    else:
        body.append('<div style="position:absolute;left:72px;top:330px;z-index:5">')
        if sl.get("number") is not None:
            body.append(vf(sl["number"], "big"))
    size = fit_size(sl.get("title"), 124 if right else 132, 1100 if right else 1700, min_size=84)
    body.append(f'<h1 class="bronze" style="font-size:{size}px;line-height:0.98;margin-top:40px">{T(sl.get("title"))}</h1>')
    if sl.get("sub"):
        body.append(f'<div class="sub" style="font-size:28px;margin-top:30px">{T(sl["sub"])}</div>')
    body.append("</div>")
    return "".join(body)

def card_open(kind, left, top, w, h, photo=None, focus=None, grade="default", radius=None, pad=None):
    r = f"border-radius:{radius}px;" if radius else ""
    s = f'<div class="card {kind}" style="left:{left}px;top:{top}px;width:{w}px;height:{h}px;{r}">'
    if kind == "photo":
        s += photo_layers(photo, focus, grade)
    p = f' style="padding:{pad}"' if pad else ""
    return s + f'<div class="inner"{p}>'

CARD_CLOSE = "</div></div>"

def item_kind(it, default="ink"):
    k = it.get("kind", default)
    if k == "photo" and not it.get("photo"):
        sys.exit("Карточка kind=photo требует поле photo")
    return k

def card_body_text(it, kind, title_size=None, text_size=None, day=False, mt=None):
    """Title + text for cards, colours depend on surface."""
    ts = (f"font-size:{title_size}px;" if title_size else "") + (f"margin-top:{mt}px;" if mt else "")
    xs = f"font-size:{text_size}px;" if text_size else ""
    bronze = " bronze" if kind in ("ink", "glass") else ""
    if day and kind == "ink":
        bronze = ""; ts += "color:var(--latte);"
    tcol = "color:var(--ivory);opacity:0.9;" if kind == "photo" else ""
    out = f'<div class="ctitle{bronze}" style="{ts}">{T(it.get("title"))}</div>'
    if it.get("text"):
        out += f'<div class="ctext" style="{xs}{tcol}">{T(it["text"])}</div>'
    return out

def L_cards(sl):
    """2-4 cards in a row. items: [{title, text, kind: ink|glass|caramel|photo, photo, pill}]"""
    items = sl["items"]
    n = len(items)
    if n not in (2, 3, 4):
        sys.exit("cards: нужно 2, 3 или 4 элемента")
    geo, w = row_geometry(n)
    top, h = (400, 600) if sl.get("sub") or n == 3 else (440, 560)
    if n == 4:
        top, h = 440, 560
    body = [head(sl)]
    numbered = sl.get("numbered", True)
    for i, (it, (left, w)) in enumerate(zip(items, geo)):
        kind = item_kind(it, "ink" if not sl.get("day") else "glass")
        pad = "36px" if n == 4 else None
        body.append(card_open(kind, left, top, w, h, it.get("photo"), it.get("focus"), pad=pad))
        if it.get("pill"):
            body.append(pill(it["pill"], pill_kind(kind), w - 2 * (36 if n == 4 else 48), "align-self:flex-start;"))
        elif numbered:
            body.append(vf(i + 1, "s" if n == 4 else "m"))
        if n == 4:
            body.append(card_body_text(it, kind, 30, 20, sl.get("day"), mt=40))
        else:
            body.append('<div class="cbottom">' + card_body_text(it, kind, day=sl.get("day")) + "</div>")
        body.append(CARD_CLOSE)
    return "".join(body)

def L_stairs(sl):
    """Four stepped cards: ink, glass, caramel, photo (slide 09). items: [{pill, title, text, kind, photo}]"""
    items = sl["items"]
    if len(items) != 4:
        sys.exit("stairs: ровно 4 элемента")
    kinds_default = ["ink", "glass", "caramel", "photo"]
    body = [head(sl)]
    for i, it in enumerate(items):
        kind = it.get("kind", kinds_default[i])
        left = 72 + i * 452
        top = 360 if i % 2 == 0 else 530
        body.append(card_open(kind, left, top, 420, 470, it.get("photo"), it.get("focus"), pad="36px 40px"))
        if it.get("pill"):
            body.append(pill(it["pill"], pill_kind(kind), 420 - 80, "align-self:flex-start;"))
        body.append('<div class="cbottom" style="min-height:0">' + vf(i + 1, "s") +
                    card_body_text(it, kind, 30, 19, sl.get("day"), mt=18) + "</div>")
        body.append(CARD_CLOSE)
    return "".join(body)

def L_kpi(sl):
    """Three equal KPI glass cards (slide 08). items: [{pill, meta, num, label, delta}]"""
    items = sl["items"]
    geo, w = row_geometry(len(items))
    body = [head(sl)]
    for it, (left, w) in zip(items, geo):
        body.append(card_open(it.get("kind", "glass"), left, 400, w, 600))
        meta_w = text_width(it.get("meta", ""), "Inter", 14, 0.24) + 24 if it.get("meta") else 0
        body.append('<div style="display:flex;justify-content:space-between;align-items:center">' + pill(it.get("pill"), "", w - 96 - meta_w) +
                    (f'<span class="meta">{T(it["meta"])}</span>' if it.get("meta") else "") + "</div>")
        num_size = it.get("num_size", 104 if len(strip_tags(it["num"])) <= 7 else 84)
        body.append(f'<div style="margin-top:auto"><div class="num bronze" style="{num_style(it["num"], num_size, w - 96)}">{T(it["num"])}</div>'
                    f'<div class="label">{T(it.get("label"))}</div>{delta(it.get("delta"), 22)}</div>')
        body.append(CARD_CLOSE)
    return "".join(body)

def L_year_kpi(sl):
    """Photo year card + three KPI glass cards with bars (slide 06).
    lead: {photo, pill, title, text}; items: [{meta, num, label, bar (0-100), delta}]"""
    lead = sl["lead"]
    items = sl["items"]
    body = [head(sl)]
    body.append(card_open("photo", 72, 400, 420, 600, lead["photo"], lead.get("focus")))
    body.append(pill(lead.get("pill"), "onphoto", 420 - 96, "align-self:flex-start;"))
    body.append(f'<div class="cbottom"><div class="ctitle bronze" style="font-size:52px">{T(lead.get("title"))}</div>'
                f'<div class="ctext" style="color:var(--ivory);opacity:0.9">{T(lead.get("text"))}</div></div>')
    body.append(CARD_CLOSE)
    n = len(items)
    gap = 20
    w = round((CW - 420 - 32 - gap * (n - 1)) / n)
    for i, it in enumerate(items):
        left = 72 + 420 + 32 + i * (w + gap)
        body.append(card_open("glass", left, 400, w, 600))
        body.append(f'<div class="meta">{T(it.get("meta"))}</div><div class="num bronze" style="{num_style(it["num"], it.get("num_size", 96), w - 96)}margin-top:40px">{T(it["num"])}</div>')
        body.append(f'<div class="label">{T(it.get("label"))}</div>')
        bar = it.get("bar")
        tail = ""
        if bar is not None:
            tail += f'<div class="bar"><i style="width:{bar}%"></i><em style="left:{bar}%"></em></div>'
        tail += delta(it.get("delta"))
        body.append(f'<div style="margin-top:auto">{tail}</div>')
        body.append(CARD_CLOSE)
    return "".join(body)

def L_timeline(sl):
    """Four columns with number, label and photo (slide 10). items: [{pill, num, label, photo, accent}]"""
    items = sl["items"]
    n = len(items)
    geo, w = row_geometry(n, 44)
    body = [head(sl)]
    for i in range(1, n):
        body.append(f'<div class="vline" style="left:{geo[i][0] - 22}px"></div>')
    for i, (it, (left, w)) in enumerate(zip(items, geo)):
        acc = " acc" if it.get("accent") or (i == n - 1 and sl.get("accent_last", True)) else ""
        body.append(f'<div class="col" style="left:{left}px;width:{w}px">' + pill(it.get("pill"), acc.strip(), w) +
                    f'<div class="num bronze" style="{num_style(it["num"], 54, w)}margin-top:40px">{T(it["num"])}</div><div class="label">{T(it.get("label"))}</div>')
        if it.get("photo"):
            body.append(f'<div class="card photo" style="left:0;top:380px;width:{w}px;height:220px;border-radius:20px">{photo_layers(it["photo"], it.get("focus"), "none")}</div>')
        body.append("</div>")
    return "".join(body)

def L_forecast(sl):
    """Photo background, staircase of year columns (slide 23).
    items: [{pill, label, metrics: [{num, label, copper}]}] - columns rise left to right."""
    items = sl["items"]
    n = len(items)
    body = [bg_photo(sl, "linear-gradient(180deg,rgba(21,17,14,0.35),rgba(21,17,14,0.2) 40%,rgba(21,17,14,0.85))"), head(sl)]
    geo, w = row_geometry(n, 44)
    for i in range(1, n):
        body.append(f'<div class="vline" style="left:{geo[i][0] - 22}px;top:380px;height:620px;background:linear-gradient(180deg,rgba(220,200,168,0.5),rgba(220,200,168,0.05))"></div>')
    tops = [640, 560, 480, 400][-n:] if n <= 4 else [400 + (n - 1 - i) * 60 for i in range(n)]
    for i, (it, (left, w)) in enumerate(zip(items, geo)):
        top = tops[i]
        acc = "acc" if i == n - 1 else "onphoto"
        body.append(f'<div class="col" style="left:{left}px;width:{w}px;top:{top}px;height:{1000 - top}px">' + pill(it.get("pill"), acc, w) +
                    f'<div class="label" style="margin-top:28px;color:var(--ivory)">{T(it.get("label"))}</div>')
        for j, m in enumerate(it.get("metrics") or []):
            col = "color:var(--acc-soft)" if m.get("copper") else ""
            cls = "num" if m.get("copper") else "num bronze"
            body.append(f'<div class="{cls}" style="{num_style(m["num"], 46, w)}margin-top:{10 if j == 0 else 26}px;{col}">{T(m["num"])}</div><div class="meta" style="margin-top:6px">{T(m.get("label"))}</div>')
        body.append("</div>")
    return "".join(body)

def L_statement(sl):
    """Thesis headline (left), optional vertical photo (centre), two or three points (slide 11).
    Fields: title (use [[...]] for bronze part), photo, items: [{title, text}]"""
    items = sl.get("items") or []
    has_photo = bool(sl.get("photo"))
    hw = 620 if has_photo else 1400
    fs = fit_lines(sl.get("title"), 60 if has_photo else 72, hw, 5 if has_photo else 3, min_size=44 if has_photo else 56)
    body = [f'<div style="position:absolute;left:72px;top:150px;width:{hw}px;z-index:5">', eyebrow(sl),
            f'<h1 style="font-size:{fs}px">{T(sl.get("title"))}</h1></div>']
    if has_photo:
        body.append(f'<div class="card photo" style="left:760px;top:120px;width:400px;height:880px">{photo_layers(sl["photo"], sl.get("focus"), "soft")}</div>')
        slots = [(72, 600), (1240, 608)]
    else:
        n = max(len(items), 1)
        geo, w = row_geometry(n, 60)
        slots = [(l, w) for l, w in geo]
    top = 560 if has_photo else 600
    for i, (it, (left, w)) in enumerate(zip(items, slots)):
        body.append(f'<div class="point" style="left:{left}px;top:{top}px;width:{w}px"><div style="height:1px;background:linear-gradient(90deg,rgba(198,162,116,0.6),transparent)"></div>'
                    f'{vf(i + 1, "m", "margin-top:40px")}<div class="ptitle">{T(it.get("title"))}</div><div class="ptext">{T(it.get("text"))}</div></div>')
    return "".join(body)

def L_five(sl):
    """Five vertical photo frames with captions (slide 13). items: [{photo, title, text}]"""
    items = sl["items"]
    n = len(items)
    geo, w = row_geometry(n, 34)
    body = [head(sl)]
    for i, (it, (left, w)) in enumerate(zip(items, geo)):
        body.append(f'<div class="col" style="left:{left}px;width:{w}px;top:360px;height:640px">'
                    f'<div class="card photo" style="left:0;top:0;width:{w}px;height:460px;border-radius:22px">{photo_layers(it["photo"], it.get("focus"), "half")}'
                    f'<div class="inner" style="padding:28px">{vf(i + 1, "xs", "margin-top:auto")}</div></div>'
                    f'<div class="ctitle" style="font-size:26px;margin-top:0;position:absolute;top:500px;left:0">{T(it.get("title"))}</div>'
                    f'<div class="ctext" style="font-size:19px;position:absolute;top:548px;left:0">{T(it.get("text"))}</div></div>')
    return "".join(body)

def L_curve(sl):
    """Qualitative two-curve chart (slide 14). Fields: x_label, y_label, notes: [{pill, text, pos: left|peak|right, accent}]"""
    body = [head(sl)]
    body.append('<div style="position:absolute;left:72px;top:400px;width:1776px;height:600px">'
                '<svg viewBox="0 0 1776 600" width="1776" height="600"><defs>'
                '<linearGradient id="gA" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#DCC8A8"/><stop offset="1" stop-color="#C6A274"/></linearGradient>'
                '<linearGradient id="gB" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#5A4636"/><stop offset="1" stop-color="#8A95A5"/></linearGradient>'
                '<linearGradient id="gF" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#C6A274" stop-opacity="0.22"/><stop offset="1" stop-color="#C6A274" stop-opacity="0"/></linearGradient></defs>'
                '<g stroke="#8A95A5" stroke-opacity="0.25"><line x1="0" y1="0" x2="0" y2="540"/><line x1="0" y1="540" x2="1776" y2="540"/>'
                '<line x1="0" y1="405" x2="1776" y2="405" stroke-dasharray="2 8"/><line x1="0" y1="270" x2="1776" y2="270" stroke-dasharray="2 8"/><line x1="0" y1="135" x2="1776" y2="135" stroke-dasharray="2 8"/></g>'
                f'<text x="24" y="22" font-family="Inter" font-size="18" fill="#8A95A5" letter-spacing="2">{T(sl.get("y_label", "")).upper()}</text>'
                f'<text x="1776" y="580" font-family="Inter" font-size="18" fill="#8A95A5" letter-spacing="2" text-anchor="end">{T(sl.get("x_label", "")).upper()}</text>'
                '<path d="M0 540 C 500 520, 900 420, 1240 250 S 1650 60, 1776 30 L 1776 540 Z" fill="url(#gF)"/>'
                '<path d="M0 540 C 500 520, 900 420, 1240 250 S 1650 60, 1776 30" fill="none" stroke="url(#gA)" stroke-width="5" stroke-linecap="round"/>'
                '<path d="M0 540 C 600 500, 1000 440, 1300 380 S 1700 300, 1776 290" fill="none" stroke="url(#gB)" stroke-width="3" stroke-dasharray="10 10" stroke-linecap="round"/>'
                '<circle cx="1240" cy="250" r="9" fill="#C98B66"/><circle cx="1240" cy="250" r="20" fill="none" stroke="#C98B66" stroke-opacity="0.4"/>'
                '<line x1="1240" y1="270" x2="1240" y2="540" stroke="#C98B66" stroke-opacity="0.4" stroke-dasharray="4 6"/></svg>')
    pos = {"left": (120, 70, 460), "peak": (880, 100, 420), "right": (1340, 420, 436)}
    for nt in sl.get("notes") or []:
        l, t, w = pos[nt.get("pos", "left")]
        acc = " acc" if nt.get("accent") or nt.get("pos") == "peak" else ""
        body.append(f'<div style="position:absolute;left:{l}px;top:{t}px;width:{w}px">' + pill(nt.get("pill"), acc.strip(), w) +
                    f'<div class="ptext" style="font-size:20px;font-weight:300;color:var(--latte);line-height:1.5;margin-top:16px">{T(nt.get("text"))}</div></div>')
    body.append("</div>")
    return "".join(body)

def L_team(sl):
    """Statement plus five photo frames along the bottom (slide 15). photos: [5 keys]"""
    photos = sl.get("photos") or []
    if len(photos) != 5:
        sys.exit("team: нужно ровно 5 фотографий")
    body = [f'<div style="position:absolute;left:72px;top:170px;width:1500px;z-index:5">{eyebrow(sl)}'
            f'<h1 style="font-size:{fit_lines(sl.get("title"), 66, 1500, 4, min_size=52)}px;margin-top:30px">{T(sl.get("title"))}</h1></div>']
    geo = [(-40, 720, 420), (384, 660, 480), (808, 600, 540), (1232, 660, 480), (1656, 720, 420)]
    for i, (p, (l, t, h)) in enumerate(zip(photos, geo)):
        inner = f'<div class="inner" style="padding:28px"><div class="logo" style="position:static;font-size:20px">{T(sl.get("_brand"))}</div></div>' if i == 2 else ""
        body.append(f'<div class="card photo" style="left:{l}px;top:{t}px;width:400px;height:{h}px;border-radius:22px">{photo_layers(p, None, "none")}{inner}</div>')
    return "".join(body)

def L_banner(sl):
    """Wide photo banner with mission text, big bronze greeting below (slide 16).
    Fields: photo, banner_eyebrow, banner_text, eyebrow, title"""
    body = [f'<div class="card photo" style="left:72px;top:60px;width:1776px;height:500px;border-radius:28px"><img src="{photo_src(sl["photo"])}" style="{focus_style(sl)}"><div class="tint"></div>'
            '<div class="grade" style="background:linear-gradient(90deg,rgba(21,17,14,0.0) 40%,rgba(21,17,14,0.55))"></div>'
            '<div class="inner" style="padding:60px;align-items:flex-end;justify-content:center"><div style="width:620px;text-align:left">'
            f'<div class="eyebrow"><span class="dot"></span><span>{T(sl.get("banner_eyebrow"))}</span></div>'
            f'<div class="ctitle" style="font-size:34px;margin-top:20px;line-height:1.2">{T(sl.get("banner_text"))}</div></div></div></div>']
    body.append(f'<div style="position:absolute;left:72px;top:620px;width:1776px;z-index:5">{eyebrow(sl)}'
                f'<h1 class="bronze" style="font-size:{fit_size(sl.get("title"), 112, 1700, min_size=80)}px;line-height:0.98;margin-top:24px">{T(sl.get("title"))}</h1></div>')
    return "".join(body)

def L_pictogram(sl):
    """Two stat cards left, headline stat with people grid right (slide 18).
    Fields: cards: [{pill, num, label, photo?}], eyebrow, num, sub, percent (0-100), total (default 50)"""
    cards = sl.get("cards") or []
    body = []
    geo = [(140, 400), (570, 430)]
    for (c, (t, h)) in zip(cards, geo):
        kind = "photo" if c.get("photo") else "ink"
        body.append(card_open(kind, 72, t, 760, h, c.get("photo"), c.get("focus")))
        body.append(pill(c.get("pill"), pill_kind(kind), 760 - 96, "align-self:flex-start;"))
        lab = ' style="color:var(--ivory)"' if kind == "photo" else ""
        body.append(f'<div style="margin-top:auto"><div class="num bronze" style="{num_style(c["num"], 112, 760 - 96)}">{T(c["num"])}</div><div class="label"{lab}>{T(c.get("label"))}</div></div>')
        body.append(CARD_CLOSE)
    body.append(f'<div style="position:absolute;left:900px;top:150px;width:948px;z-index:5">{eyebrow(sl)}'
                f'<div class="num bronze" style="{num_style(sl.get("num", ""), 104, 948)}margin-top:24px">{T(sl.get("num"))}</div><div class="sub" style="font-size:30px">{T(sl.get("sub"))}</div></div>')
    total = round(float(sl.get("total", 50)))
    lit = round(total * float(sl.get("percent", 0)) / 100)
    cols = 10
    rows = -(-total // cols)
    scale = min(1.0, (520 / rows) / 69)     # every row fits the 520px band: total=100 keeps ten rows in place
    pw, ph = 46 * scale, 69 * scale
    body.append(f'<div style="position:absolute;left:900px;top:480px;width:948px;height:520px;display:grid;grid-template-columns:repeat({cols},1fr);grid-template-rows:repeat({rows},1fr);gap:0">')
    for i in range(total):
        fill = "url(#pg)" if i < lit else "#3A2C22"
        body.append(f'<div style="display:flex;align-items:center;justify-content:center"><svg viewBox="0 0 40 60" width="{pw:g}" height="{ph:g}"><circle cx="20" cy="13" r="11" fill="{fill}"/><path d="M4 60 V42 a16 16 0 0 1 32 0 V60 Z" fill="{fill}"/></svg></div>')
    body.append("</div>")
    return "".join(body)

def L_ring_bars(sl):
    """Ring percentage left, bar rows right (slide 19).
    Fields: ring: {value (0-100), label}, bars: [{label, value, tone: bronze|latte|cold}]"""
    body = [head(sl)]
    ring = sl.get("ring")
    left = 72
    if ring:
        v = float(ring["value"])
        dash = 1005.3 * v / 100
        body.append('<div class="card glass" style="left:72px;top:400px;width:600px;height:600px"><div class="inner" style="align-items:center;justify-content:center">'
                    '<svg viewBox="0 0 400 400" width="420" height="420"><defs><linearGradient id="ring" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#DCC8A8"/><stop offset="1" stop-color="#C6A274"/></linearGradient></defs>'
                    '<circle cx="200" cy="200" r="160" fill="none" stroke="#5A4636" stroke-opacity="0.6" stroke-width="22"/>'
                    f'<circle cx="200" cy="200" r="160" fill="none" stroke="url(#ring)" stroke-width="22" stroke-linecap="round" stroke-dasharray="{dash:.1f} 1005.3" transform="rotate(-90 200 200)"/>'
                    '<circle cx="200" cy="40" r="11" fill="#C98B66"/>'
                    f'<text x="200" y="190" text-anchor="middle" font-family="Unbounded" font-weight="600" font-size="86" fill="#DCC8A8">{T(ring.get("text", f"{v:g}%"))}</text>'
                    f'<text x="200" y="240" text-anchor="middle" font-family="Inter" font-size="20" fill="#8A95A5" letter-spacing="3">{T(ring.get("label", "")).upper()}</text></svg></div></div>')
        left = 704
    w = CW - (left - 72)
    body.append(f'<div class="card ink" style="left:{left}px;top:400px;width:{w}px;height:600px"><div class="inner" style="padding:56px 60px;justify-content:center;gap:44px">')
    tones = {"bronze": ("", "num bronze", "", ""), "latte": ("color:var(--latte)", "num", "color:var(--latte)", "background:var(--latte)"),
             "cold": ("color:var(--cold)", "num", "color:var(--cold)", "background:var(--cold)")}
    for i, b in enumerate(sl.get("bars") or []):
        tone = b.get("tone", "bronze" if i == 0 else ("latte" if i == 1 else "cold"))
        lc, nc, ns, fill = tones[tone]
        v = float(b["value"])
        em = f'<em style="left:{v}%"></em>' if tone == "bronze" else ""
        body.append(f'<div><div style="display:flex;justify-content:space-between;align-items:baseline"><span style="font-size:24px;font-weight:500;{lc}">{T(b["label"])}</span>'
                    f'<span class="{nc}" style="font-size:44px;{ns}">{T(b.get("text", f"{v:g}%"))}</span></div>'
                    f'<div class="bar" style="height:10px;margin-top:18px;border-radius:5px"><i style="width:{v}%;{fill}"></i>{em}</div></div>')
    body.append("</div></div>")
    return "".join(body)

def L_market(sl):
    """Nested TAM / SAM / SOM on a photo card (slide 20). Fields: photo, levels: [{meta, num, label}] (3 levels, outer first)"""
    lv = sl["levels"]
    body = [head(sl)]
    body.append(f'<div class="card photo" style="left:72px;top:360px;width:1776px;height:640px">{photo_layers(sl["photo"], sl.get("focus"), "none")}'
                '<div class="grade" style="background:linear-gradient(180deg,rgba(21,17,14,0.25),rgba(21,17,14,0.7))"></div>'
                f'<div class="inner" style="padding:52px 56px"><div class="meta" style="color:var(--latte)">{T(lv[0].get("meta"))}</div><div class="num bronze" style="{num_style(lv[0]["num"], 88, 800)}margin-top:20px">{T(lv[0]["num"])}</div><div class="label" style="color:var(--ivory)">{T(lv[0].get("label"))}</div></div>')
    if len(lv) > 1:
        body.append(f'<div class="card glass" style="left:1000px;top:180px;width:720px;height:410px"><div class="inner" style="padding:40px 44px"><div class="meta">{T(lv[1].get("meta"))}</div><div class="num bronze" style="{num_style(lv[1]["num"], 64, 632)}margin-top:14px">{T(lv[1]["num"])}</div><div class="label">{T(lv[1].get("label"))}</div></div>')
        if len(lv) > 2:
            som_label = lv[2].get("label")
            som_h, som_text = 170, ""
            if som_label:
                # the SOM card grows 24px downwards (still inside the SAM card) for one or two label lines
                one_line = text_width(som_label, "Inter", 17) * SAFETY <= 316
                som_h = 194 if one_line else 212
                som_text = (f'<div class="label" style="font-size:{17 if one_line else 15}px;line-height:1.25;margin-top:6px;'
                            f'color:#3A2A1E;opacity:0.9">{T(som_label)}</div>')
            body.append(f'<div class="card caramel" style="left:300px;top:{200 if som_h <= 194 else 182}px;width:380px;height:{som_h}px;border-radius:22px"><div class="inner" style="padding:28px 32px"><div class="meta" style="color:#5A4636">{T(lv[2].get("meta"))}</div><div class="num" style="{num_style(lv[2]["num"], 48, 316)}margin-top:auto;color:var(--espresso)">{T(lv[2]["num"])}</div>{som_text}</div></div>')
        body.append("</div>")
    body.append("</div>")
    return "".join(body)

def L_model(sl):
    """Vertical photo card left, numbered metric rows right (slide 21).
    Fields: photo, card_pill, card_title, eyebrow, rows: [{num, label}]"""
    body = [f'<div class="card photo" style="left:72px;top:140px;width:560px;height:860px">{photo_layers(sl["photo"], sl.get("focus"), "none")}'
            '<div class="grade" style="background:linear-gradient(180deg,rgba(21,17,14,0.1),rgba(21,17,14,0.75))"></div>'
            f'<div class="inner">' + pill(sl.get("card_pill"), "onphoto", 560 - 96, "align-self:flex-start;") + f'<div class="cbottom"><div class="ctitle bronze" style="font-size:64px">{T(sl.get("card_title"))}</div></div></div></div>']
    body.append(f'<div style="position:absolute;left:700px;top:150px;z-index:5">{eyebrow(sl)}</div>')
    rows = sl.get("rows") or []
    pad = {1: 80, 2: 60, 3: 44, 4: 30}.get(len(rows), 24)
    body.append('<div style="position:absolute;left:700px;top:260px;width:1148px">')
    for i, r in enumerate(rows):
        bb = "border-bottom:1px solid rgba(138,149,165,0.25);" if i == len(rows) - 1 else ""
        body.append(f'<div style="display:grid;grid-template-columns:70px 640px 1fr;align-items:center;padding:{pad}px 0;border-top:1px solid rgba(138,149,165,0.25);{bb}">'
                    f'<span class="meta">{i + 1:02d}</span><span class="num bronze" style="{num_style(r["num"], r.get("num_size", 84), 520)}">{T(r["num"])}</span><span class="label" style="margin:0">{T(r.get("label"))}</span></div>')
    body.append("</div>")
    return "".join(body)

def L_table(sl):
    """Light data table without heavy chrome. Fields: columns: [..], rows: [[..]], widths: [fr..], highlight_col: index"""
    cols = sl["columns"]
    widths = sl.get("widths") or ["2fr"] + ["1fr"] * (len(cols) - 1)
    grid = " ".join(widths)
    body = [head(sl), '<div class="row-table">']
    body.append(f'<div class="tr th" style="grid-template-columns:{grid}">' + "".join(f"<span>{T(c)}</span>" for c in cols) + "</div>")
    hl = sl.get("highlight_col")
    hl = round(float(hl)) if hl is not None else None   # numeric strings ("1") must highlight too
    nrows = len(sl["rows"])
    pad = max(18, min(48, int((520 - 60 - nrows * 30) / (2 * nrows))))
    for r in sl["rows"]:
        cells = []
        for j, c in enumerate(r):
            cls = "first" if j == 0 else ("hl" if hl is not None and j == hl else "")
            cells.append(f'<span class="{cls}">{T(c)}</span>')
        body.append(f'<div class="tr" style="grid-template-columns:{grid};padding:{pad}px 0">' + "".join(cells) + "</div>")
    body.append("</div>")
    return "".join(body)

def L_quote(sl):
    """Large quotation or single thesis, optional author pill. Fields: text, author, photo (optional bg)"""
    body = [bg_photo(sl, "linear-gradient(180deg,rgba(21,17,14,0.55),rgba(21,17,14,0.75))")] if sl.get("photo") else []
    body.append(f'<div style="position:absolute;left:72px;top:260px;width:1500px;z-index:5">{eyebrow(sl)}'
                f'<div class="quote" style="margin-top:40px">{T(sl.get("text"))}</div>'
                + (f'<div style="margin-top:48px">{pill(sl.get("author"), "onphoto" if sl.get("photo") else "", 1400)}</div>' if sl.get("author") else "") + "</div>")
    return "".join(body)

def L_thanks(sl):
    """Closing slide (slide 24). Fields: photo, eyebrow, title, contacts: [..]"""
    body = [bg_photo(sl, "linear-gradient(180deg,rgba(21,17,14,0.1),rgba(21,17,14,0.2) 45%,rgba(21,17,14,0.88))")]
    body.append(f'<div style="position:absolute;left:72px;bottom:230px;z-index:5">{eyebrow(sl)}<h1 class="bronze" style="font-size:{fit_size(sl.get("title", "Спасибо"), 150, 1700, min_size=96)}px;line-height:0.96;margin-top:30px">{T(sl.get("title", "Спасибо"))}</h1></div>')
    body.append('<div style="position:absolute;left:72px;right:72px;bottom:72px;z-index:5;display:flex;justify-content:space-between;align-items:center;padding-top:28px;border-top:1px solid rgba(220,200,168,0.35)">'
                '<div style="display:flex;gap:40px;align-items:center">' + "".join(pill(c, "onphoto", 1000) for c in sl.get("contacts") or []) + "</div>"
                f'<span style="font-size:16px;letter-spacing:0.24em;color:var(--latte)">{{PAGE}} / {{TOTAL}}</span></div>')
    return "".join(body)

schemes.init({"T": T, "pill": pill, "pill_kind": pill_kind, "vf": vf, "head": head, "fit_lines": fit_lines,
              "fit_size": fit_size, "text_width": text_width, "num_style": num_style, "card_open": card_open})
LAYOUTS = {
    "title": L_title, "title-short": L_title_short, "section": L_section,
    "cards": L_cards, "stairs": L_stairs, "kpi": L_kpi, "year-kpi": L_year_kpi,
    "timeline": L_timeline, "forecast": L_forecast, "statement": L_statement,
    "five": L_five, "curve": L_curve, "team": L_team, "banner": L_banner,
    "pictogram": L_pictogram, "ring-bars": L_ring_bars, "market": L_market,
    "model": L_model, "table": L_table, "quote": L_quote, "thanks": L_thanks,
}
LAYOUTS.update(schemes.LAYOUTS)
PHOTO_BG = {"title", "title-short", "section", "forecast", "thanks"}

# ------------------------------------------------------------------ schema (field names per layout)
# Unknown or missing fields stop the build: a mistyped key would otherwise render an empty slide.
COMMON_FIELDS = {"type", "eyebrow", "eyebrow2", "title", "sub", "foot", "pill", "meta", "day", "photo", "focus",
                 "nofooter", "nofootline", "notes_", "comment"}
ITEM = {"kind", "title", "text", "photo", "focus", "pill"}
SCHEMA = {
    #  layout:        (own fields,                                   required,            list field -> allowed item keys)
    "title":        (set(),                                          {"title"},            {}),
    "title-short":  (set(),                                          {"title"},            {}),
    "section":      ({"number", "align"},                            {"title"},            {}),
    "thanks":       ({"contacts"},                                   {"title"},            {}),
    "cards":        ({"items", "numbered"},                          {"items"},            {"items": ITEM}),
    "stairs":       ({"items"},                                      {"items"},            {"items": ITEM}),
    "statement":    ({"items"},                                      {"title"},            {"items": {"title", "text"}}),
    "five":         ({"items"},                                      {"items"},            {"items": {"photo", "focus", "title", "text"}}),
    "quote":        ({"text", "author"},                             {"text"},             {}),
    "kpi":          ({"items"},                                      {"items"},            {"items": {"pill", "meta", "num", "label", "delta", "kind", "num_size"}}),
    "year-kpi":     ({"lead", "items"},                              {"lead", "items"},    {"items": {"meta", "num", "label", "bar", "delta", "num_size"},
                                                                                            "lead": {"photo", "focus", "pill", "title", "text"}}),
    "timeline":     ({"items", "accent_last"},                       {"items"},            {"items": {"pill", "num", "label", "photo", "focus", "accent"}}),
    "forecast":     ({"items"},                                      {"items"},            {"items": {"pill", "label", "metrics"}}),
    "model":        ({"card_pill", "card_title", "rows"},            {"rows", "photo"},    {"rows": {"num", "label", "num_size"}}),
    "market":       ({"levels"},                                     {"levels", "photo"},  {"levels": {"meta", "num", "label"}}),
    "pictogram":    ({"cards", "num", "percent", "total"},           {"percent"},          {"cards": {"pill", "num", "label", "photo", "focus", "kind"}}),
    "ring-bars":    ({"ring", "bars"},                               {"bars"},             {"bars": {"label", "value", "text", "tone"}, "ring": {"value", "label", "text"}}),
    "curve":        ({"x_label", "y_label", "notes"},                set(),                {"notes": {"pos", "pill", "text", "accent"}}),
    "table":        ({"columns", "rows", "widths", "highlight_col"}, {"columns", "rows"},  {}),
    "team":         ({"photos"},                                     {"photos", "title"},  {}),
    "banner":       ({"banner_eyebrow", "banner_text"},              {"title", "photo"},   {}),
}

SCHEMA.update(schemes.SCHEMA)

# Nested structure the layouts really read: required keys inside list elements and
# single-object fields that must be dicts (schemes.py adds its own). Missing or wrongly
# typed nested fields stop the build as schema errors instead of KeyError crashes.
ELEM_REQUIRED = {
    "kpi":        {"items": {"num"}},
    "year-kpi":   {"items": {"num"}, "lead": {"photo"}},
    "timeline":   {"items": {"num"}},
    "five":       {"items": {"photo"}},
    "forecast":   {"items": set()},           # items[].metrics checked separately below
    "pictogram":  {"cards": {"num"}},
    "ring-bars":  {"bars": {"label", "value"}, "ring": {"value"}},
    "market":     {"levels": {"num"}},
    "model":      {"rows": {"num"}},
}
DICT_FIELDS = {"year-kpi": {"lead"}, "ring-bars": {"ring"}}
STR_LISTS = {"team": {"photos"}, "thanks": {"contacts"}}
ELEM_REQUIRED.update(schemes.ELEM_REQUIRED)
for _t, _f in schemes.DICT_FIELDS.items():
    DICT_FIELDS.setdefault(_t, set()).update(_f)

def check_schema(spec):
    errors = []
    if not spec.get("brand") and not spec.get("doc_title"):
        errors.append("корень: нужно поле brand - короткое имя презентации или программы для левого верхнего угла (не имя автора)")
    for i, sl in enumerate(spec.get("slides", [])):
        if not isinstance(sl, dict):
            continue
        t = sl.get("type")
        if not isinstance(t, str) or not t:
            errors.append(f"slide {i+1}: type должен быть названием раскладки из каталога, а не «{t!r}»")
            continue
        if t not in SCHEMA:
            errors.append(f"slide {i+1}: неизвестный тип «{t}». Доступные: {', '.join(SCHEMA)}")
            continue
        own, required, lists = SCHEMA[t]
        allowed = COMMON_FIELDS | own
        for k in sl:
            if k not in allowed:
                errors.append(f"slide {i+1} ({t}): поле «{k}» не известно этой раскладке. Допустимые: {', '.join(sorted(own))} и общие {', '.join(sorted(COMMON_FIELDS - {'notes_', 'comment'}))}")
        for k in required:
            v = sl.get(k)
            if v is None or v == "" or v == []:      # 0 is a value: percent=0 is valid
                errors.append(f"slide {i+1} ({t}): нет обязательного поля «{k}»")
        for lk, keys in lists.items():
            v = sl.get(lk)
            seq = v if isinstance(v, list) else ([v] if isinstance(v, dict) else [])
            for j, it in enumerate(seq):
                if isinstance(it, dict):
                    for k in it:
                        if k not in keys:
                            errors.append(f"slide {i+1} ({t}): {lk}[{j}] поле «{k}» не известно. Допустимые: {', '.join(sorted(keys))}")
    errors += check_nested(spec)
    return errors

def _list(v):
    """A list field as the checks may walk it: a scalar or object in its place is reported
    by check_nested as "должно быть списком" and never iterated character by character."""
    return v if isinstance(v, list) else []

# Cards that draw a photo surface: kind=photo (explicit, or the default of the 4th stairs step)
# needs its photo, otherwise the card renders as an empty frame.
PHOTO_CARD_DEFAULTS = {"cards": lambda j: "ink", "stairs": lambda j: ("ink", "glass", "caramel", "photo")[j % 4],
                       "kpi": lambda j: "glass"}

def check_nested(spec):
    """Element types and required nested fields: a missing num or a string where an object
    belongs becomes a schema error naming the slide, the field and what is expected."""
    errors = []
    for i, sl in enumerate(spec.get("slides", [])):
        if not isinstance(sl, dict):
            continue
        t = sl.get("type")
        if not isinstance(t, str) or t not in SCHEMA:
            continue
        n = i + 1
        _, _, lists = SCHEMA[t]
        for f in sorted(DICT_FIELDS.get(t, ())):
            v = sl.get(f)
            if v is None:
                continue
            if not isinstance(v, dict):
                errors.append(f"slide {n} ({t}): поле «{f}» должно быть объектом")
                continue
            for k in sorted(ELEM_REQUIRED.get(t, {}).get(f, ())):
                if v.get(k) is None or v.get(k) == "":
                    errors.append(f"slide {n} ({t}): {f}: нет обязательного поля «{k}»")
        for f in sorted(STR_LISTS.get(t, ())):
            v = sl.get(f)
            if v is None:
                continue
            if not isinstance(v, list) or not v or not all(isinstance(x, str) and x.strip() for x in v):
                errors.append(f"slide {n} ({t}): {f} - список строк")
        for lk, keys in lists.items():
            if lk in DICT_FIELDS.get(t, ()) or (t in STR_LISTS and lk in STR_LISTS[t]):
                continue
            v = sl.get(lk)
            if v is None:
                continue          # absent or explicit null: the layouts read it as empty
            if not isinstance(v, list):
                errors.append(f"slide {n} ({t}): поле «{lk}» должно быть списком")
                continue
            req = ELEM_REQUIRED.get(t, {}).get(lk, ())
            for j, it in enumerate(v):
                if not isinstance(it, dict):
                    errors.append(f"slide {n} ({t}): {lk}[{j}] должен быть объектом, а не "
                                  f"{type(it).__name__}")
                    continue
                for k in sorted(req):
                    if it.get(k) is None or it.get(k) == "":
                        errors.append(f"slide {n} ({t}): {lk}[{j}] нет обязательного поля «{k}»")
        if t == "table":
            cols, rows = sl.get("columns"), sl.get("rows")
            if not isinstance(cols, list) or not cols or not all(isinstance(c, str) and c for c in cols):
                errors.append(f"slide {n} (table): columns - непустой список строк")
            if (not isinstance(rows, list) or not rows
                    or not all(isinstance(r, list) and all(isinstance(c, str) for c in r) for r in rows)):
                errors.append(f"slide {n} (table): rows - список строк таблицы (список списков строк)")
        if t in PHOTO_CARD_DEFAULTS:
            for j, it in enumerate(_list(sl.get("items"))):
                if (isinstance(it, dict) and it.get("kind", PHOTO_CARD_DEFAULTS[t](j)) == "photo"
                        and not (isinstance(it.get("photo"), str) and it["photo"].strip())):
                    errors.append(f"slide {n} ({t}): items[{j}] - карточка kind=photo требует поле photo "
                                  "(ключ библиотеки или имя из --photo) или другой kind: ink, glass, caramel")
        if t == "forecast":
            for j, it in enumerate(_list(sl.get("items"))):
                if not isinstance(it, dict):
                    continue
                ms = it.get("metrics")
                if ms is None:
                    continue
                if not isinstance(ms, list):
                    errors.append(f"slide {n} (forecast): items[{j}].metrics должен быть списком")
                    continue
                for m2, m in enumerate(ms):
                    if not isinstance(m, dict):
                        errors.append(f"slide {n} (forecast): items[{j}].metrics[{m2}] должен быть объектом")
                        continue
                    for k in sorted(set(m) - {"num", "label", "copper"}):
                        errors.append(f"slide {n} (forecast): items[{j}].metrics[{m2}] поле «{k}» не известно. "
                                      "Допустимые: copper, label, num")
                    if not (isinstance(m.get("num"), str) and m["num"]):
                        errors.append(f"slide {n} (forecast): items[{j}].metrics[{m2}] нет обязательного поля «num»")
        if t == "layers":
            for j2, node in enumerate(_list(sl.get("layers"))):
                if not isinstance(node, dict):
                    continue
                its = node.get("items")
                if its is not None and (not isinstance(its, list)
                                        or not all(isinstance(x2, str) and x2.strip() for x2 in its)):
                    errors.append(f"slide {n} (layers): layers[{j2}].items - список строк")
        if t == "tree":
            for j2, node in enumerate(_list(sl.get("children"))):
                if not isinstance(node, dict):
                    continue
                its = node.get("items")
                if its is not None and (not isinstance(its, list)
                                        or not all(isinstance(x2, str) and x2.strip() for x2 in its)):
                    errors.append(f"slide {n} (tree): children[{j2}].items - список строк")
    return errors
# ------------------------------------------------------------------ values (closed sets, numbers, CSS fragments)
# Fields that reach a class name, an inline style or a lookup table must have a known shape. A wrong value
# stops the build with code 3 instead of producing a broken slide or markup the spec author did not mean.
KINDS = {"ink", "glass", "caramel", "photo"}
NODE_KINDS = {"ink", "glass", "caramel"}
ENUMS = {"align": {"left", "right"}, "pos": {"left", "peak", "right"}, "tone": {"bronze", "latte", "cold"},
         "dir": {"up", "down", "flat"}, "left_kind": NODE_KINDS, "right_kind": NODE_KINDS}
_POS = r"(?:-?\d{1,3}(?:\.\d{1,3})?%|left|right|center|top|bottom)"
FOCUS_RE = re.compile(rf"^{_POS}(?:\s+{_POS})?$")
FR_RE = re.compile(r"^\d{1,2}(?:\.\d{1,3})?fr$")
NUMBERS = {"bar": (0, 100), "value": (0, 100), "percent": (0, 100), "x": (0, 100), "y": (0, 100),
           "num_size": (8, 400), "total": (1, 500), "highlight_col": (0, 20)}

def _is_number(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)) or (
        isinstance(v, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", v.strip()) is not None)

def check_values(spec):
    errors = []
    def visit(obj, path):
        if isinstance(obj, dict):
            for k, v in obj.items():
                where = f"{path}.{k}"
                if k == "kind" and not (isinstance(v, str) and v in KINDS):
                    errors.append(f"{where}: kind должен быть одним из {', '.join(sorted(KINDS))}, а не «{v}»")
                elif k in ENUMS and not (isinstance(v, str) and v in ENUMS[k]):
                    errors.append(f"{where}: допустимо {', '.join(sorted(ENUMS[k]))}, а не «{v}»")
                elif k == "focus" and v is not None and not (isinstance(v, str) and FOCUS_RE.match(v.strip())):
                    errors.append(f"{where}: focus задаётся как \"50% 30%\", а не «{v}»")
                elif k == "widths" and not (isinstance(v, list) and all(isinstance(x, str) and FR_RE.match(x) for x in v)):
                    errors.append(f"{where}: widths - список вида [\"2fr\", \"1fr\"]")
                elif k in NUMBERS and v is not None:
                    lo, hi = NUMBERS[k]
                    if not _is_number(v) or not lo <= float(v) <= hi:
                        errors.append(f"{where}: нужно число от {lo} до {hi}, а не «{v}»")
                elif k == "delta" and v is not None and not isinstance(v, str):
                    if not isinstance(v, dict) or not (isinstance(v.get("text"), str) and v.get("text").strip()):
                        errors.append(f"{where}: delta - строка или объект с обязательным полем text")
                elif k == "number" and v is not None and not (_is_number(v) or (isinstance(v, str) and len(v) <= 6)):
                    errors.append(f"{where}: number - номер раздела, число или до 6 знаков")
                elif k in ("photo", "card_title", "title", "text", "num", "meta") and v is not None and not isinstance(v, str):
                    errors.append(f"{where}: нужна строка")
                visit(v, where)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                visit(v, f"{path}[{i}]")
    if not isinstance(spec.get("slides"), list) or not spec["slides"]:
        return ["корень: нужен непустой массив slides"]
    for i, sl in enumerate(spec["slides"]):
        if not isinstance(sl, dict):
            errors.append(f"slide {i+1}: слайд должен быть объектом")
            continue
        visit(sl, f"slide {i+1}")
        if isinstance(sl.get("type"), str) and sl.get("type") in schemes.LAYOUTS:
            for node in [*_list(sl.get("items")), *_list(sl.get("layers")), *_list(sl.get("quadrants")),
                         *_list(sl.get("children"))]:
                if isinstance(node, dict) and node.get("kind") == "photo":
                    errors.append(f"slide {i+1}: узлы схем бывают ink, glass или caramel, не photo")
    for key in ("brand", "doc_title", "meta"):
        if key in spec and not isinstance(spec[key], str):
            errors.append(f"корень: {key} должен быть строкой")
    return errors

# ------------------------------------------------------------------ chrome (glow, grain, logo, footer)
GRAIN = '<div class="grain"><svg><rect width="100%" height="100%" filter="url(#noise)"/></svg></div>'
NOISE = '<svg width="0" height="0" style="position:absolute"><filter id="noise"><feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" stitchTiles="stitch"/><feColorMatrix values="0 0 0 0 0.9 0 0 0 0 0.85 0 0 0 0 0.78 0 0 0 0.9 0"/></filter><defs><linearGradient id="pg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#DCC8A8"/><stop offset="1" stop-color="#C6A274"/></linearGradient></defs></svg>'

def chrome(sl, n, total, brand, default_meta):
    is_photo = sl["type"] in PHOTO_BG and sl.get("photo") or (sl["type"] == "quote" and sl.get("photo"))
    atm = f'<div class="glow{" photo" if is_photo else ""}"></div><div class="vignette"></div>{GRAIN}'
    out = [f'<div class="logo">{T(brand)}</div>']
    meta = sl.get("meta") or (default_meta if is_photo else None)
    out.append(f'<div class="pageno">{T(meta).upper() if meta else f"{n:02d} / {total:02d}"}</div>')
    if not sl.get("nofooter") and sl["type"] != "thanks":
        left = []
        if sl.get("pill"):
            left.append(pill(sl["pill"], "acc", 900))
        if sl.get("foot"):
            left.append(f"<span>{T(sl['foot'])}</span>")
        line = "" if sl.get("nofootline") else '<div class="line"></div>'
        out.append(f'<div class="footer">{line}<div style="display:flex;gap:40px;align-items:center">{"".join(left)}</div><div style="letter-spacing:0.24em">{n:02d} / {total:02d}</div></div>')
    return atm, "".join(out)

# Runtime fitting: every .pill and .num is nowrap. If text is wider than the box the
# browser shrinks the font step by step (pill down to 12px, number down to 60%), then
# lets a pill wrap onto two lines as a last resort. Runs on load and before screenshots.
FIT_JS = """<script>
window.btFit = function(){
  document.querySelectorAll('.pill').forEach(function(p){
    p.classList.remove('wrap'); p.style.fontSize='';
    var fs = parseFloat(getComputedStyle(p).fontSize), base = fs;
    while (p.scrollWidth > p.clientWidth + 1 && fs > 12) { fs -= 0.5; p.style.fontSize = fs + 'px'; }
    if (p.scrollWidth > p.clientWidth + 1) { p.classList.add('wrap'); p.style.fontSize = Math.max(fs, base - 1) + 'px'; }
  });
  document.querySelectorAll('.num').forEach(function(n){
    if (!n.dataset.fs) n.dataset.fs = getComputedStyle(n).fontSize;
    n.style.fontSize = n.dataset.fs;
    n.style.whiteSpace = '';
    var fs = parseFloat(n.dataset.fs), min = fs * 0.5, box = n.parentElement, cs = getComputedStyle(box);
    var avail = box.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    while (n.scrollWidth > avail + 1 && fs > min) { fs -= 2; n.style.fontSize = fs + 'px'; }
    if (n.scrollWidth > avail + 1) { n.style.whiteSpace = 'normal'; n.style.lineHeight = '1.05'; }
  });
  document.documentElement.dataset.btFit = '1';
};
if (!window.btRender) document.querySelectorAll('img.anim[data-gif]').forEach(function(i){ i.src = i.dataset.gif; });
(document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve()).then(window.btFit);
</script>"""

# The HTML is one file: everything is embedded, and the policy forbids any request a viewer's browser
# could make (no remote fonts, images, frames, connections or form posts). Inline style and the fitting
# script below are the only active content.
CSP = ("default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; "
       "script-src 'unsafe-inline'; media-src data:; base-uri 'none'; form-action 'none'")

_CSS = {}

def inline_css():
    """deck.css with fonts.css inlined and every bundled woff2 embedded; nothing refers to a file."""
    if "css" not in _CSS:
        fonts_dir = os.path.join(ASSETS, "fonts")
        def font(m):
            name = m.group(1)
            path = os.path.join(fonts_dir, name)
            if os.path.dirname(os.path.abspath(path)) != os.path.abspath(fonts_dir) or not os.path.isfile(path):
                raise RuntimeError(f"fonts.css ссылается на отсутствующий шрифт {name}")
            with open(path, "rb") as f:
                return "url(data:font/woff2;base64," + base64.b64encode(f.read()).decode("ascii") + ")"
        with open(os.path.join(ASSETS, "fonts.css"), encoding="utf-8") as f:
            fonts_css = re.sub(r"url\(fonts/([A-Za-z0-9._-]+\.woff2)\)", font, f.read())
        with open(os.path.join(ASSETS, "deck.css"), encoding="utf-8") as f:
            deck_css = f.read().replace("@import url('fonts.css');", "")
        if "url(fonts/" in fonts_css or "@import" in deck_css:
            raise RuntimeError("в CSS осталась внешняя ссылка")
        _CSS["css"] = fonts_css + "\n" + deck_css
    return _CSS["css"]

def ofl_notice():
    """Both bundled fonts are SIL OFL 1.1; the single-file HTML carries the two licence
    texts, so the notice travels with the embedded WOFF2 copies."""
    parts = []
    for family, name in (("Inter", "LICENSE-Inter.txt"), ("Unbounded", "LICENSE-Unbounded.txt")):
        path = os.path.join(ASSETS, "fonts", name)
        with open(path, encoding="utf-8") as f:
            parts.append(f"--- {family} ({name}) ---\n{f.read()}")
    return "\n\n".join(parts)

def build_html(spec):
    slides = spec["slides"]
    total = len(slides)
    brand = spec.get("brand") or spec.get("doc_title") or ""
    default_meta = spec.get("meta", "")
    parts = [f'<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
             f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
             f'<title>{htmlmod.escape(spec.get("doc_title", "Premium Black deck"))}</title>'
             f'<meta name="generator" content="kirill-menshov-premium-black v{VERSION}">'
             f'<style>{inline_css()}</style></head><body data-skill-version="{VERSION}">'
             f'<div hidden id="font-licences">{htmlmod.escape(ofl_notice())}</div>{NOISE}']
    for i, sl in enumerate(slides):
        n = i + 1
        t = sl.get("type")
        if t not in LAYOUTS:
            sys.exit(f"Слайд {n}: неизвестный тип «{t}». Доступные: {', '.join(LAYOUTS)}")
        sl = dict(sl)
        sl["_brand"] = brand
        inner = LAYOUTS[t](sl).replace("{PAGE}", f"{n:02d}").replace("{TOTAL}", f"{total:02d}")
        atm, chr_ = chrome(sl, n, total, brand, default_meta)
        cls = "slide" + (" photo" if (t in PHOTO_BG and sl.get("photo")) else "") + (" day" if sl.get("day") else "")
        # atmosphere goes after bg layers (if any) and before content
        if t in PHOTO_BG and sl.get("photo") or (t == "quote" and sl.get("photo")):
            idx = inner.find('<div class="tintall"></div>') + len('<div class="tintall"></div>')
            inner = inner[:idx] + atm + inner[idx:]
        else:
            inner = atm + inner
        parts.append(f'<!-- {n:02d} {t} -->\n<section class="{cls}" data-n="{n}">{inner}{chr_}</section>\n')
    parts.append(FIT_JS + "</body></html>")
    return "\n".join(parts)

# ------------------------------------------------------------------ render, PDF, report
def _pdf_text(value):
    """PDF text string: ASCII literal or UTF-16BE hex."""
    value = str(value or "")
    if value.isascii():
        return "(" + value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"
    return "<FEFF" + value.encode("utf-16-be").hex().upper() + ">"

def write_pdf(pngs, path, scale, title, lossless=False):
    """One page per slide, 20 x 11.25 in, written page by page: no deck keeps all its decoded
    pixels in memory. Default: JPEG quality 95 as /DCTDecode (the original default, a few
    hundred KB per slide). --pdf-lossless: /FlateDecode RGB, pixel-exact and several times larger."""
    from PIL import Image
    import zlib
    offsets, count = [], 2 + 3 * len(pngs) + 1
    with open(path, "xb") as out:
        def obj(body, stream=None):
            offsets.append(out.tell())
            out.write(f"{len(offsets)} 0 obj\n".encode() + body)
            if stream is not None:
                out.write(b"\nstream\n" + stream + b"\nendstream")
            out.write(b"\nendobj\n")
        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        kids = " ".join(f"{3 + 3 * i} 0 R" for i in range(len(pngs)))
        obj(b"<< /Type /Catalog /Pages 2 0 R >>")
        obj(f"<< /Type /Pages /Kids [{kids}] /Count {len(pngs)} >>".encode())
        for i, p in enumerate(pngs):
            with Image.open(p) as im:
                rgb = im.convert("RGB")
            w, h = rgb.size
            if lossless:
                filt, data = "/FlateDecode", zlib.compress(rgb.tobytes(), 6)
            else:
                buf = io.BytesIO()
                rgb.save(buf, format="JPEG", quality=95)
                filt, data = "/DCTDecode", buf.getvalue()
            pw, ph = w * 72 / (96 * scale), h * 72 / (96 * scale)
            n = 3 + 3 * i
            obj(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.2f} {ph:.2f}] /Resources << /XObject << /Im0 {n + 2} 0 R >> >> "
                f"/Contents {n + 1} 0 R >>".encode())
            content = f"q {pw:.2f} 0 0 {ph:.2f} 0 0 cm /Im0 Do Q".encode()
            obj(f"<< /Length {len(content)} >>".encode(), content)
            obj(f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceRGB /BitsPerComponent 8 "
                f"/Filter {filt} /Length {len(data)} >>".encode(), data)
        obj(f"<< /Title {_pdf_text(title)} /Creator (kirill-menshov-premium-black v{VERSION}) >>".encode())
        xref = out.tell()
        out.write(f"xref\n0 {count + 1}\n0000000000 65535 f \n".encode())
        for off in offsets:
            out.write(f"{off:010d} 00000 n \n".encode())
        out.write(f"trailer\n<< /Size {count + 1} /Root 1 0 R /Info {count} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())

def check_version_sync():
    """Warn if SKILL.md `version` differs from VERSION (single source of truth is premium_common.VERSION)."""
    try:
        md = open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8").read()
        m = re.search(r'^version:\s*"?([^"\s]+)"?\s*$', md, flags=re.M)
        if m and m.group(1) != VERSION:
            print(f"WARN версия в SKILL.md ({m.group(1)}) не совпадает со сборщиком ({VERSION}), обнови обе и CHANGELOG.md")
    except OSError:
        pass

def output_plan(args, stem):
    """Output names by flag: --render always writes PNG slides and a PDF (with --pptx too);
    --pptx alone writes only the PPTX; --pdf names or triggers the PDF by itself."""
    html_name = pc.output_name(args.out, stem, ".html")
    base = os.path.splitext(html_name)[0]
    pdf_name = pc.output_name(args.pdf, base, ".pdf") if (args.pdf or args.render) else None
    pptx_name = pc.output_name(args.pptx, base, ".pptx") if args.pptx else None
    return html_name, pdf_name, pptx_name

def parse_args(argv):
    ap = argparse.ArgumentParser(prog="build_deck.py", add_help=True,
                                 description="Premium Black: spec.json -> HTML, PNG, PDF, PPTX in a new job folder")
    ap.add_argument("spec", nargs="?", help="spec.json in an Ouroboros working folder or assets/example_*.json")
    ap.add_argument("-V", "--version", action="store_true")
    ap.add_argument("--photo", action="append", metavar="NAME=PATH",
                    help="own photo for THIS build, repeatable; NAME is what the spec's photo field says "
                         "(without = it is the file's base name)")
    ap.add_argument("--out", help="name of the HTML file (bare name)")
    ap.add_argument("--render", action="store_true", help="PNG slides and a PDF")
    ap.add_argument("--scale", type=int, choices=(1, 2), default=1)
    ap.add_argument("--pdf", help="name of the PDF file (bare name)")
    ap.add_argument("--pdf-lossless", action="store_true", help="pixel-exact PDF, several times larger")
    ap.add_argument("--pptx", help="name of the PPTX file (bare name)")
    ap.add_argument("--flat", action="store_true", help="PPTX of full slide pictures, no editable text")
    ap.add_argument("--gif", action="store_true", help="also play the living line in the HTML (3-6 MB per slide)")
    ap.add_argument("--allow-hygiene", action="store_true")
    return ap.parse_args(argv)

def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.version:
        print(f"kirill-menshov-premium-black v{VERSION} (upstream {pc.UPSTREAM_VERSION})")
        return 0
    if not args.spec:
        print(__doc__)
        return 1
    check_version_sync()
    print(f"kirill-menshov-premium-black v{VERSION}")
    started = time.monotonic()
    report = {"status": "error", "version": VERSION}
    job = None
    code = 1
    try:
        state = pc.state_dir()
        spec_path = pc.resolve_spec(args.spec, state)
        try:
            with open(spec_path, encoding="utf-8") as f:
                spec = json.load(f)
        except ValueError as exc:
            raise SystemExit(f"spec.json не читается как JSON: {exc}") from None
        if not isinstance(spec, dict):
            raise SystemExit("spec.json: корень должен быть объектом с полями brand, meta, doc_title, slides")
        schema_errors = check_schema(spec) if isinstance(spec.get("slides"), list) else []
        schema_errors += check_values(spec)
        if schema_errors:
            print("Спецификация не соответствует каталогу раскладок (references/layouts.md):")
            for e in schema_errors:
                print(" -", e)
            report.update(error="schema", details=schema_errors)
            code = pc.EXIT_SCHEMA
            return code
        brand = spec.get("brand") or spec.get("doc_title") or ""
        brand_warns = []
        if not spec.get("brand"):
            brand_warns.append("корень: нет поля brand - в левом верхнем углу использую doc_title. "
                               "Задай короткое имя презентации или программы (до 32 знаков), например «Стратегия ИИ · 2027»")
            print("WARN " + brand_warns[-1])
        if len(strip_tags(brand)) > 32:
            brand_warns.append(f"brand длиннее 32 знаков ({len(strip_tags(brand))}), сократи: это метка в углу, а не заголовок")
            print("WARN " + brand_warns[-1])
        errors, warns = hygiene(spec)
        for w in warns:
            print("WARN", w)
        if errors and not args.allow_hygiene:
            print("Гигиена текста не пройдена:")
            for e in errors:
                print(" -", e)
            report.update(error="hygiene", details=errors)
            code = pc.EXIT_HYGIENE
            return code
        stem = os.path.splitext(os.path.basename(str(spec_path)))[0]
        html_name, pdf_name, pptx_name = output_plan(args, stem)
        base = os.path.splitext(html_name)[0]
        declared = parse_photos(args.photo)
        job = pc.new_job_dir(state, spec.get("doc_title") or stem)
        CTX.update(state=state, photos={}, declared=declared)
        schemes.configure(pc.cache_dir(state, "anim", VERSION), make_gif=bool(pptx_name and not args.flat) or args.gif,
                          embed_gif=args.gif)
        html = build_html(spec)
        html_path = job / html_name
        html_path.write_text(html, encoding="utf-8")
        files = {"html": str(html_path)}
        report.update(job_dir=str(job), files=files, slides=len(spec["slides"]),
                      warnings=warns + brand_warns + schemes.build_warnings(),
                      own_photos=sorted(declared))
        print(f"HTML: {html_path} ({len(spec['slides'])} слайдов, {len(html.encode('utf-8')) // 1024} КБ, без внешних ссылок)")
        if args.render or pdf_name or pptx_name:
            import render_deck
            png_dir = job / "png"
            work = (job / "_work") if (pptx_name and not args.flat) else None
            if work is not None:
                work.mkdir()
            info = render_deck.render(html_path, png_dir, state, scale=args.scale, work_dir=work)
            report.update(chromium=info["chromium"], outside_requests=info["outside_requests"])
            if info["outside_requests"]:
                print("WARN рендер заблокировал внешние запросы:", ", ".join(info["outside_requests"][:5]))
            pngs = render_deck.slide_pngs(png_dir)
            files["png_dir"] = str(png_dir)
            if pdf_name:
                write_pdf(pngs, job / pdf_name, args.scale, spec.get("doc_title") or base, lossless=args.pdf_lossless)
                files["pdf"] = str(job / pdf_name)
                print(f"PDF: {job / pdf_name} ({len(pngs)} слайдов, PNG в {png_dir})")
            if pptx_name:
                import build_pptx
                out = job / pptx_name
                pptx_info = build_pptx.build(png_dir, out, work_dir=work, flat=args.flat, media=dict(schemes.USED),
                                             title=spec.get("doc_title") or base)
                files["pptx"] = str(out)
                if not args.flat:
                    fonts = job / f"{os.path.splitext(pptx_name)[0]} fonts.zip"
                    build_pptx.write_fonts_zip(fonts)
                    files["fonts_zip"] = str(fonts)
                report["pptx"] = pptx_info
                if work is not None:
                    shutil.rmtree(work)          # this build's own scratch folder inside its new job folder
        report["status"] = "ok"
        code = 0
    except pc.Refused as exc:
        print("Отказ:", exc)
        report.update(error="refused", details=[str(exc)])
        code = pc.EXIT_REFUSED
    except SystemExit as exc:                    # layout constraints and spec errors raised by the layouts
        if isinstance(exc.code, str):
            print(exc.code)
            report.update(error="schema", details=[exc.code])
            code = pc.EXIT_SCHEMA
        else:
            raise
    except Exception as exc:                     # noqa: BLE001 - reported as a structured failure
        import render_deck
        if isinstance(exc, render_deck.RendererUnavailable):
            print("Рендер недоступен:", exc)
            report.update(error="renderer", details=[str(exc)],
                          hint="HTML собран; для PNG, PDF и PPTX выполни setup_renderer.py install")
            code = pc.EXIT_RENDERER
        else:
            print(f"Сборка не удалась: {type(exc).__name__}: {exc}")
            report.update(error="failed", details=[f"{type(exc).__name__}: {exc}"])
            code = 1
    finally:
        report["seconds"] = round(time.monotonic() - started, 1)
        if job is not None and job.is_dir():
            report.setdefault("job_dir", str(job))
            (job / "build.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        pc.emit(report)
    return code

if __name__ == "__main__":
    raise SystemExit(main())
