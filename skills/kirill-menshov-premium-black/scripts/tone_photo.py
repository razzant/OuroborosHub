#!/usr/bin/env python3
"""
Premium Black: bring any photograph (generated, stock, from the source document) to the library grade.

The 23 library photos share one look: warm hue 20-35 degrees, saturation around 0.3, espresso shadows,
ivory highlights, fine grain. This script maps a new picture onto the same duotone curve and blends a
little of the original colour back, so a generated or borrowed frame sits next to library frames
without a visible seam.

Usage:
  tone_photo.py in.jpg out.jpg [--mix 0.18] [--dark 0.0] [--grain 3] [--max 2400]

  in.jpg  a JPEG, PNG or WebP file you explicitly name (typically from the working folder of
          your task or the folder where the user's picture landed); network addresses are refused
  out.jpg a bare file name; the result is a NEW file in <state>/photos/ (a suffix is added if the
          name is taken), written without EXIF or other metadata

  --mix   share of the original (desaturated) colour kept, 0 = pure duotone, default 0.18
  --dark  extra darkening 0..0.5 for frames that carry text (title, section)
  --grain noise sigma in 8-bit units, 0 disables
  --max   longest side in px

Then hand the result to the build: build_deck.py spec.json --photo <имя>=<путь из file>,
and put <имя> (the printed spec_value) in the spec's photo field. A path inside the spec is not
a permission to read the file.
"""
import argparse, io, os, shlex, sys, warnings
import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import premium_common as pc

ESPRESSO = np.array([0x15, 0x11, 0x0E], np.float32) / 255
MID = np.array([0xB8, 0x86, 0x5A], np.float32) / 255       # warm mid between mocha and caramel
IVORY = np.array([0xF3, 0xEB, 0xDD], np.float32) / 255
MAX_IMAGE_PIXELS = 60_000_000

def duotone(L):
    """L in 0..1 -> RGB through espresso -> mid -> ivory."""
    a = np.clip(L / 0.55, 0, 1)[..., None]
    b = np.clip((L - 0.55) / 0.45, 0, 1)[..., None]
    rgb = ESPRESSO * (1 - a) + MID * a
    return rgb * (1 - b) + IVORY * b

def tone(im, mix=0.18, dark=0.0, grain=3.0, seed=3):
    arr = np.asarray(im.convert("RGB"), np.float32) / 255
    L = 0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]
    # gentle S-curve and optional darkening
    L = np.clip((L - 0.5) * 1.08 + 0.5, 0, 1)
    L = L * (1 - dark)
    base = duotone(L)
    # keep a little of the original colour, desaturated
    gray = L[..., None]
    desat = gray * 0.6 + arr * 0.4
    out = base * (1 - mix) + desat * mix
    if grain > 0:
        rng = np.random.default_rng(seed)
        out = out + rng.normal(0, grain / 255, L.shape)[..., None]
    return Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8), "RGB")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("dst")
    ap.add_argument("--mix", type=float, default=0.18)
    ap.add_argument("--dark", type=float, default=0.0)
    ap.add_argument("--grain", type=float, default=3.0)
    ap.add_argument("--max", type=int, default=2400)
    a = ap.parse_args()
    try:
        pc.refused_if_url(a.src)
        name = pc.output_name(a.dst, "toned", ".jpg")
        if not (0 <= a.mix <= 1 and 0 <= a.dark <= 0.5 and 0 <= a.grain <= 20 and 256 <= a.max <= 4000):
            raise pc.Refused("параметры вне диапазона: --mix 0..1, --dark 0..0.5, --grain 0..20, --max 256..4000")
        try:
            src = pc.pathlib.Path(a.src).expanduser().resolve(strict=True)
        except OSError:
            raise pc.Refused(f"снимок не найден: {a.src}") from None
        if not src.is_file() or src.stat().st_size > 30 * 1024 * 1024:
            raise pc.Refused(f"нужен обычный файл до 30 МБ: {src}")
        previous_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                opened = Image.open(src)
            with opened:
                if opened.format not in ("JPEG", "PNG", "WEBP"):
                    raise pc.Refused(f"поддерживаются JPEG, PNG и WebP, а не {opened.format}")
                width, height = opened.size
                if width * height > MAX_IMAGE_PIXELS:
                    raise pc.Refused(f"снимок слишком большой: {width}x{height} пикселей, максимум {MAX_IMAGE_PIXELS}")
                im = ImageOps.exif_transpose(opened)
                im.thumbnail((a.max, a.max), Image.LANCZOS)
                out = tone(im, a.mix, a.dark, a.grain)
        finally:
            Image.MAX_IMAGE_PIXELS = previous_limit
        buf = io.BytesIO()
        out.info.clear()                            # only pixels, no source EXIF/ICC/XMP
        out.save(buf, format="JPEG", quality=90)
        folder = pc.state_dir() / "photos"
        if folder.is_symlink() or folder.resolve() != folder:
            raise pc.Refused("папка photos не должна перенаправлять запись за пределы своего пути")
        folder.mkdir(parents=True, exist_ok=True)
        stem, ext = os.path.splitext(name)
        for k in range(1, 1000):
            target = folder / (name if k == 1 else pc.output_name(f"{stem}-{k}{ext}", "toned", ".jpg"))
            try:
                with open(target, "xb") as f:         # never replaces an existing file
                    f.write(buf.getvalue())
                break
            except FileExistsError:
                continue
        else:
            raise pc.Refused("слишком много снимков с таким именем, выбери другое")
        # the key for the spec, derived from the real file name (see choose_photo_alias for covered collisions)
        photo_name = pc.choose_photo_alias(os.path.splitext(os.path.basename(target))[0])
    except pc.Refused as exc:
        pc.emit({"status": "error", "error": str(exc)})
        return pc.EXIT_REFUSED
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        pc.emit({"status": "error", "error": f"снимок слишком большой: максимум {MAX_IMAGE_PIXELS} пикселей"})
        return pc.EXIT_REFUSED
    except (OSError, ValueError) as exc:
        pc.emit({"status": "error", "error": f"снимок не читается: {exc}"})
        return pc.EXIT_REFUSED
    photo_operand = f"{photo_name}={target}"
    photo_arg = f"--photo {shlex.quote(photo_operand)}"
    pc.emit({"status": "ok", "file": str(target), "size": [out.size[0], out.size[1]],
             "spec_value": photo_name, "build_args": ["--photo", photo_operand],
             "note": f"передай снимок в сборку как {photo_arg}, "
                     f"а в поле photo укажи имя «{photo_name}»"})
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
