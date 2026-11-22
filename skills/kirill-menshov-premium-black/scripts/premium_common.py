"""Shared paths and policies for the Premium Black skill (OuroborosHub adaptation).

Every write of this skill lands in a new job folder under ``<state>/outputs/``
or in the renderer/animation caches under ``<state>/``. Nothing outside those
folders is created, changed or deleted. Reads are equally narrow: the skill's
own assets, the one spec file the caller explicitly names and the photo files
the operator declares for THIS build with ``--photo имя=путь`` (each checked
against network addresses and re-encoded without metadata); the skill's state
folder holds the toned copies ``tone_photo.py`` produced. There is no list of
"working folders": the folder next to the spec grants no reading of its
neighbours, and nothing is read from other tasks' zones or from the network.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
import secrets
import sys

SKILL_NAME = "kirill-menshov-premium-black"
VERSION = "2.3.2-hub.1"          # keep in sync with SKILL.md `version` and the last CHANGELOG.md entry
UPSTREAM_VERSION = "2.3.2"       # Kirill Menshov's original release this adaptation is based on
PLAYWRIGHT_VERSION = "1.65.2"    # must equal the pinned `playwright==` in SKILL.md install_specs

SKILL_DIR = pathlib.Path(__file__).resolve().parent.parent
ASSETS = SKILL_DIR / "assets"
PHOTO_LIBRARY = ASSETS / "photos"

EXIT_HYGIENE = 2
EXIT_SCHEMA = 3
EXIT_RENDERER = 4
EXIT_REFUSED = 5


class Refused(Exception):
    """An input or output the policy does not allow; exit code 5."""


def state_dir() -> pathlib.Path:
    raw = os.environ.get("OUROBOROS_SKILL_STATE_DIR", "").strip()
    if not raw:
        raise Refused("OUROBOROS_SKILL_STATE_DIR не задан: скилл пишет только в свою папку состояния. "
                      "Ouroboros задаёт её сам; для ручного запуска укажи переменную явно.")
    path = pathlib.Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def contained(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def refused_if_url(text: str) -> None:
    """Network addresses are never read: the build and the toner stay offline."""
    prefix = text.lstrip()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]+:", prefix) or re.match(r"^[/\\]{2}", prefix):
        raise Refused(f"внешние адреса не поддерживаются, скилл не ходит в сеть: {text[:120]}")


def resolve_spec(raw: str, state: pathlib.Path) -> pathlib.Path:
    """The one spec file this build reads: named explicitly by the caller, or a bundled example.

    The caller puts spec.json in the working folder of its task and passes the path; a relative
    path that exists only inside the skill refers to the bundled examples. The spec's own folder
    grants no reading of its neighbours: an own photo enters the build only through the
    operator's --photo declaration (build_deck.parse_photos).
    """
    text = str(raw or "").strip()
    if not text:
        raise Refused("не указан путь к spec.json")
    refused_if_url(text)
    path = pathlib.Path(text).expanduser()
    if not path.is_absolute() and not path.exists() and (SKILL_DIR / path).exists():
        path = SKILL_DIR / path
    try:
        path = path.resolve(strict=True)
    except OSError:
        raise Refused(f"файл спецификации не найден: {text}") from None
    if not path.is_file():
        raise Refused(f"спецификация должна быть обычным файлом, а не папкой: {path}")
    return path


SAFE_NAME = re.compile(r"[^/\\:\x00-\x1f\x7f-\x9f*?\"<>|]{1,120}")
PHOTO_NAME = re.compile(r"^[^/\\\x00-\x1f*?\"<>|:&=]{1,120}$")
DEVICE_NAME = re.compile(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$", re.IGNORECASE)


def output_name(raw: str | None, default_stem: str, suffix: str) -> str:
    """A bare file name for a job output. Directories are refused: every file goes to the job folder.

    The verdict is about the FINAL name, after the suffix is added: a stem that only turns into
    a reserved Windows device once the suffix is there ("CON" -> "CON.html") is refused as well.
    """
    stem = (raw or "").strip() or f"{default_stem}{suffix}"
    if stem.endswith((" ", ".")):
        raise Refused(f"имя файла «{stem}» не переносимо: основа не может заканчиваться пробелом или точкой")
    name = stem if stem.lower().endswith(suffix.lower()) else stem + suffix
    base = name.split(".", 1)[0]
    if DEVICE_NAME.fullmatch(base.rstrip(" ")):
        raise Refused(f"имя файла «{name}» зарезервировано в Windows; выбери другое")
    if (not base or not SAFE_NAME.fullmatch(name) or name.startswith(".")
            or base.endswith((" ", ".")) or name.endswith((" ", "."))):
        raise Refused(f"имя файла «{name}» должно быть простым именем без папок, служебных знаков, "
                      "точки в начале или пробела/точки в конце основы, "
                      f"например «Deck Name v1{suffix}»; файлы пишутся в новую папку задания")
    return name


def library_photos() -> dict[str, pathlib.Path]:
    """The bundled photo library as one exact table: key -> file.

    Built from the folder listing with exact (case-sensitive) names, so the answer does
    not depend on whether the file system ignores case: "DUNES-MIST" is not a library key
    on macOS or Windows either. The builder's collision check and its reader both use
    this table; nothing opens a library path built from a spec string.
    """
    try:
        return {path.name[:-4]: path for path in PHOTO_LIBRARY.iterdir()
                if path.name.endswith(".jpg") and path.is_file()}
    except OSError:
        return {}


def library_photo_keys() -> set[str]:
    return set(library_photos())


def library_photo_key(name: str, library=None) -> str | None:
    """The library key a spec value names ("dunes-mist" or "dunes-mist.jpg"), exact spelling only."""
    keys = library if library is not None else library_photos()
    text = str(name or "")
    if text in keys:
        return text
    if text.endswith(".jpg") and text[:-4] in keys:
        return text[:-4]
    return None


def photo_name_is_library(name: str, library=None) -> bool:
    """Whether ``name`` would collide with a bundled photo key (same exact table as the reader)."""
    return library_photo_key(name, library) is not None


def photo_name_is_valid(name: str) -> bool:
    """Whether a name is accepted as an own-photo key by the builder."""
    return bool(PHOTO_NAME.fullmatch(str(name or "")))


OWN_PREFIX = "own-"


def choose_photo_alias(stem: str, library=None) -> str:
    """The own-photo key for a toned file, without renaming the file.

    Keeps the keys of separate tone_photo results apart in the cases it covers:
    - a stem that is a valid key, not a library key and does not start with ``own-``
      is used as is;
    - any other stem gets the ``own-`` prefix ("dunes-mist" -> "own-dunes-mist",
      "own-dunes-mist" -> "own-own-dunes-mist"): as-is keys never start with ``own-``;
    - when characters had to be replaced ("R&D" and "R=D" both read "R-D") or the key
      had to be shortened, a short hash of the exact stem is appended; an 8-hex hash makes a
      clash unlikely, not impossible, and a clash in one build is refused as a duplicate name.
    """
    import hashlib
    keys = library if library is not None else library_photos()
    text = str(stem or "").strip() or "photo"
    if (photo_name_is_valid(text) and not photo_name_is_library(text, keys)
            and not text.startswith(OWN_PREFIX)):
        return text
    readable = OWN_PREFIX + re.sub(r"[&=]", "-", text)
    if readable != OWN_PREFIX + text or len(readable) > 120:
        tag = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:8]
        readable = f"{readable[:111].rstrip(' .')}-{tag}"
    if not photo_name_is_valid(readable) or photo_name_is_library(readable, keys):
        raise Refused("не удалось выбрать свободное имя фотографии")
    return readable


def slug(text: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "-", text or "").strip("-")     # ASCII folder names travel everywhere
    return (s[:40].strip("-") or "deck").lower()


def new_job_dir(state: pathlib.Path, label: str) -> pathlib.Path:
    """A fresh folder for one build. It never existed before, so nothing in it belongs to anyone else."""
    outputs = state / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    for _ in range(20):
        job = outputs / f"{stamp}-{slug(label)}-{secrets.token_hex(3)}"
        try:
            job.mkdir()
        except FileExistsError:
            continue
        return job
    raise RuntimeError("не удалось создать новую папку задания")


def cache_dir(state: pathlib.Path, *parts: str) -> pathlib.Path:
    path = state.joinpath("cache", *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def browsers_dir(state: pathlib.Path) -> pathlib.Path:
    """Private Playwright browser folder of this skill (never the global cache)."""
    return state / "ms-playwright"


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()
