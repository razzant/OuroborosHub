# -*- coding: utf-8 -*-
"""Общее ядро навыка pdf_rezume_perevod.

Здесь нет точек входа: модуль импортируют остальные скрипты навыка.
Отвечает за четыре вещи:
  1. где лежит состояние (строго внутри OUROBOROS_SKILL_STATE_DIR);
  2. единый формат ответа скриптов — один JSON-объект в stdout;
  3. нормализацию и сверку чисел между исходником и русским текстом;
  4. русскую типографику.

Сеть не используется, дочерние процессы не запускаются.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

SKILL_NAME = "pdf_rezume_perevod"
TIME_BUDGET_SEC = 230      # жёсткий предел skill_exec — 300 с
MIN_PYMUPDF = (1, 24, 2)

_JOB_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,90}$")


# --------------------------------------------------------------------------
# Ввод-вывод
# --------------------------------------------------------------------------
def setup_stdio() -> None:
    """Windows-консоль по умолчанию не UTF-8; принудительно включаем."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def out(payload: dict, code: int = 0) -> None:
    """Печатает единственный JSON-ответ и завершает процесс."""
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if len(text.encode("utf-8")) > 250_000:
        text = json.dumps(
            {
                "ok": False,
                "error": "Ответ скрипта превысил лимит вывода.",
                "hint": "Сузьте диапазон страниц (--pages) или используйте --offset.",
            },
            ensure_ascii=False,
        )
        code = 3
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    sys.exit(code)


def fail(error: str, hint: str = "", **extra) -> None:
    payload = {"ok": False, "error": error}
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    out(payload, code=2)


def require_pymupdf():
    try:
        import pymupdf  # type: ignore
    except Exception:
        try:
            import fitz as pymupdf  # type: ignore
        except Exception:
            fail(
                "Не установлена библиотека pymupdf.",
                "Зависимость объявлена в SKILL.md (dependencies: pymupdf) и ставится платформой после ревью навыка. "
                "Запустите ревью навыка заново и включите навык.",
            )
    try:  # MuPDF по умолчанию пишет предупреждения в stdout — это ломает контракт «один JSON»
        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)
    except Exception:
        pass
    ver = getattr(pymupdf, "pymupdf_version_tuple", None)
    if ver is None:
        try:
            ver = tuple(int(x) for x in str(getattr(pymupdf, "VersionBind", "0.0.0")).split(".")[:3])
        except Exception:
            ver = (0, 0, 0)
    if tuple(ver) < MIN_PYMUPDF:
        fail("Установлена слишком старая версия pymupdf: %s." % ".".join(map(str, ver)),
             "Нужна версия не ниже %s: переустановите зависимости навыка (повторное ревью)." % ".".join(map(str, MIN_PYMUPDF)))
    return pymupdf


class JsonArgumentParser(argparse.ArgumentParser):
    """Ошибки разбора аргументов — тем же JSON в stdout, а не текстом argparse в stderr."""

    def error(self, message):  # noqa: D401
        fail("Неверные аргументы: %s" % message, "Формат вызова: %s" % self.format_usage().strip())


class Deadline:
    """Бюджет времени: скрипты сами останавливаются до таймаута skill_exec."""

    def __init__(self, seconds: float = TIME_BUDGET_SEC):
        self.t0 = time.monotonic()
        self.seconds = seconds

    def expired(self) -> bool:
        return (time.monotonic() - self.t0) > self.seconds

    def elapsed(self) -> float:
        return round(time.monotonic() - self.t0, 1)


# --------------------------------------------------------------------------
# Состояние и задания
# --------------------------------------------------------------------------
def state_dir() -> Path:
    """Корень состояния — только OUROBOROS_SKILL_STATE_DIR (его задаёт skill_exec).

    Вне Ouroboros (локальная отладка, когда нет OUROBOROS_SKILL_NAME) допускается
    PDF_SKILL_STATE_DIR. Запасного пути во временную папку нет: без переменной
    скрипт завершается с ошибкой и ничего не пишет.
    """
    root = os.environ.get("OUROBOROS_SKILL_STATE_DIR", "").strip()
    if not root and not os.environ.get("OUROBOROS_SKILL_NAME"):
        root = os.environ.get("PDF_SKILL_STATE_DIR", "").strip()
    if not root:
        fail("Не задана папка состояния OUROBOROS_SKILL_STATE_DIR.",
             "Скрипты навыка запускаются через skill_exec; для локальной отладки задайте PDF_SKILL_STATE_DIR.")
    p = Path(root).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def jobs_root() -> Path:
    p = state_dir() / "jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def job_dir(job_id: str, must_exist: bool = True) -> Path:
    if not _JOB_RE.match(job_id or ""):
        fail(
            "Некорректный идентификатор задания: %r" % job_id,
            "Возьмите job из ответа pdf.py open (или pdf.py jobs).",
        )
    p = (jobs_root() / job_id).resolve()
    if jobs_root() not in p.parents:
        fail("Задание вне папки состояния навыка.")
    if must_exist and not (p / "meta.json").is_file():
        fail(
            "Задание %s не найдено." % job_id,
            "Сначала выполните pdf.py open <путь к PDF>. Список заданий: pdf.py jobs.",
        )
    return p


def slugify(name: str) -> str:
    table = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
        "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
        "я": "ya",
    }
    s = "".join(table.get(ch, ch) for ch in name.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s or "doc")[:48].strip("-") or "doc"


def output_stem(jdir: Path) -> str:
    """Имя исходного файла без расширения и без служебного префикса загрузки (<hash>_имя.pdf)."""
    meta = read_json(jdir / "meta.json", {})
    name = Path(str(meta.get("source_name") or "document.pdf")).stem
    name = re.sub(r"^[0-9a-f]{16,}_", "", name)          # Ouroboros: <hash32>_имя.pdf
    name = re.sub(r"^[0-9a-f]{8}-", "", name)             # другие загрузчики: <hash8>-имя.pdf
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name)
    name = re.sub(r"_{2,}", "_", name).strip(" ._")
    return (name or "document")[:120]


def output_paths(jdir: Path) -> dict:
    """Готовые файлы называются по исходнику: <название>_summary.pdf, <название>_перевод.pdf."""
    stem = output_stem(jdir)
    out = jdir / "out"
    return {
        "summary_pdf": out / ("%s_summary.pdf" % stem), "summary_html": out / ("%s_summary.html" % stem),
        "summary_json": out / "summary.json",
        "draft_pdf": out / ("%s_summary_ЧЕРНОВИК.pdf" % stem), "draft_html": out / ("%s_summary_ЧЕРНОВИК.html" % stem),
        "draft_json": out / "draft_summary.json",
        "translation_pdf": out / ("%s_перевод.pdf" % stem),
    }


def file_sha(path: Path, limit_mb: int = 512) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path, default=None):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        # молча считать файл пустым нельзя: следующий write затёр бы накопленную работу
        fail("Файл состояния повреждён: %s (%s)." % (path, e),
             "Удалите или восстановите этот файл; остальные данные задания не тронуты.")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_payload(args, what: str = "JSON"):
    """Данные от агента: --json '<строка>' или --file <путь внутри папки состояния навыка>."""
    raw = None
    if getattr(args, "json", None):
        raw = args.json
    elif getattr(args, "file", None):
        p = Path(args.file).expanduser().resolve()
        root = state_dir()
        if root != p and root not in p.parents:
            fail("--file принимает только файлы внутри папки состояния навыка.",
                 "Положите JSON в %s или передайте его строкой через --json." % root)
        if not p.is_file():
            fail("Файл не найден: %s" % p)
        if p.stat().st_size > 5_000_000:
            fail("Файл с данными больше 5 МБ — это не похоже на %s." % what)
        raw = p.read_text(encoding="utf-8-sig")
    if raw is None:
        fail("Не переданы данные (%s)." % what, "Используйте --json '<строка JSON>'.")
    raw = raw.strip()
    # Модели любят заворачивать JSON в ```json ... ```
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        fail(
            "Данные не разбираются как JSON: %s" % e,
            "Проверьте кавычки и запятые; передавайте чистый JSON без пояснений.",
        )


def as_text(value) -> str:
    """Строка из значения JSON: null и не-строки не превращаются в 'None' / '{...}'."""
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return str(value).strip()


def parse_pages(spec: str | None, total: int) -> list[int]:
    """'1-5,8,10-12' -> [1,2,3,4,5,8,10,11,12]; None -> все страницы."""
    if not spec:
        return list(range(1, total + 1))
    pages: list[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        m = re.match(r"^(\d+)(?:-(\d+))?$", part)
        if not m:
            fail("Не понимаю диапазон страниц: %r" % spec, "Формат: 1-5 или 3 или 1-5,8,10-12.")
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a > b:
            a, b = b, a
        a, b = max(1, a), min(total, b)  # «1-99999999999» не должно крутить цикл до таймаута
        for n in range(a, b + 1):
            if n not in pages:
                pages.append(n)
    if not pages:
        fail("В диапазоне %r нет страниц документа (всего страниц: %d)." % (spec, total))
    return pages


def page_text(jdir: Path, n: int) -> str:
    p = jdir / "pages" / ("%04d.txt" % n)
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def page_text_for_numbers(jdir: Path, n: int) -> str:
    """Текст страницы без её собственного номера в колонтитуле.

    Иначе номер страницы «подтверждал» бы любое такое же число в резюме («3%» на странице 3).
    """
    lines = page_text(jdir, n).splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.strip()]
    for i in idx[:2] + idx[-2:]:
        lines[i] = re.sub(r"(^|\s)%d(?=\s*$)" % n, r"\1", lines[i])
        lines[i] = re.sub(r"^(\s*)%d(?=\s|$)" % n, r"\1", lines[i])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Числа: нормализация и сверка
# --------------------------------------------------------------------------
_SPACES = "     "
_NUM_RE = re.compile(r"(?<![\w.,])\d[\d\s.,]*\d|\d")


def _clean(text: str) -> str:
    for ch in _SPACES:
        text = text.replace(ch, " ")
    return text.replace("−", "-")


def _strip_zeros(s: str) -> str:
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def number_forms(token: str) -> set[str]:
    """Все разумные прочтения числовой записи.

    '1,719' -> {'1719', '1.719'}; '48 185' -> {'48185', '48', '185'};
    '16,5' -> {'16.5'}; '1.200' -> {'1.2', '1200'}.
    Неоднозначность (запятая — разряд или дробь?) решается при сравнении:
    число считается подтверждённым, если совпало хотя бы одно прочтение.
    """
    t = _clean(token).strip().strip(".,")
    forms: set[str] = set()
    if not t or not re.search(r"\d", t):
        return forms
    m_ru = re.fullmatch(r"(\d{1,3}(?: \d{3})+)[.,](\d+)", t)
    if m_ru:  # 12 345,67
        forms.add(_strip_zeros("%s.%s" % (m_ru.group(1).replace(" ", ""), m_ru.group(2))))
        return forms
    parts_space = t.split()
    if len(parts_space) > 1:
        # '48 185' — либо одно число с разрядами, либо два отдельных.
        if all(re.fullmatch(r"\d{3}", p) for p in parts_space[1:]) and re.fullmatch(r"\d{1,3}", parts_space[0]):
            forms.add(_strip_zeros("".join(parts_space)))
        for p in parts_space:
            if p.strip("0.,"):
                forms |= number_forms(p)
        return forms
    seps = [c for c in t if c in ".,"]
    if not seps:
        forms.add(_strip_zeros(t.lstrip("0") or "0"))
        return forms
    digits_only = re.sub(r"[.,]", "", t)
    if len(seps) == 1:
        a, b = re.split(r"[.,]", t)
        forms.add(_strip_zeros("%s.%s" % (a.lstrip("0") or "0", b)))  # дробь
        if len(b) == 3:
            forms.add(_strip_zeros(digits_only.lstrip("0") or "0"))  # разряды
    else:
        last = max(t.rfind("."), t.rfind(","))
        head = re.sub(r"[.,]", "", t[:last])
        tail = t[last + 1:]
        if len(set(seps)) == 1:
            forms.add(_strip_zeros(digits_only.lstrip("0") or "0"))  # 1,234,567
        else:
            forms.add(_strip_zeros("%s.%s" % (head.lstrip("0") or "0", tail)))  # 1,234.56
            forms.add(_strip_zeros(digits_only.lstrip("0") or "0"))
    return forms


def find_number_tokens(text: str) -> list[str]:
    """Числовые записи в тексте, в порядке появления."""
    text = _clean(text)
    tokens = []
    for m in _NUM_RE.finditer(text):
        tok = m.group(0).strip().rstrip(".,")
        # '2026 12' не склеиваем, если это не разряды по три цифры
        parts = tok.split()
        if len(parts) > 1 and not (
            re.fullmatch(r"\d{1,3}", parts[0]) and all(re.fullmatch(r"\d{3}", p) for p in parts[1:-1])
            and re.fullmatch(r"\d{3}(?:[.,]\d+)?", parts[-1])
        ):
            tokens.extend(p.rstrip(".,") for p in parts if re.search(r"\d", p))
        else:
            tokens.append(tok)
    return [t for t in tokens if t]


_EN_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_EN_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
            "eighty": 80, "ninety": 90}
_EN_WORD_RE = re.compile(
    r"\b(?:(%s)(?:[-\s](%s))?|(%s))(?:\s+(hundred|thousand|million|billion))?\b"
    % ("|".join(_EN_TENS), "|".join(list(_EN_UNITS)[1:10]), "|".join(_EN_UNITS)),
    re.I,
)


def spelled_numbers(text: str) -> set[str]:
    """Числа, записанные английскими словами: 'Forty percent' -> 40, 'Thirty-seven' -> 37.

    Отчёты часто начинают предложение числом прописью; без этого «40%» в резюме
    выглядело бы как число, которого нет в источнике.
    """
    found: set[str] = set()
    for m in _EN_WORD_RE.finditer(text):
        tens, unit, single, mult = m.groups()
        if tens:
            val = _EN_TENS[tens.lower()] + (_EN_UNITS[unit.lower()] if unit else 0)
        else:
            val = _EN_UNITS[single.lower()]
        found.add(str(val))
        if mult and mult.lower() in ("hundred", "thousand"):
            found.add(str(val * (100 if mult.lower() == "hundred" else 1000)))
    return found


def number_index(text: str) -> set[str]:
    """Множество всех прочтений всех чисел текста — «словарь чисел» источника."""
    idx: set[str] = set()
    for tok in find_number_tokens(text):
        idx |= number_forms(tok)
    return idx | spelled_numbers(_clean(text))


_STRONG_AFTER = re.compile(r"^\s*(?:%|процент|млн|млрд|трлн|тыс|п\.\s?п|б\.\s?п|раз|крат|x\b|×|х\b)", re.I)


def is_minor_number(token: str, context_after: str = "", context_before: str = "") -> bool:
    """Одиночная цифра — слабый сигнал (номер сноски, «в 1 функции»), но не рядом с единицей или валютой."""
    t = _clean(token).strip()
    if not re.fullmatch(r"\d", t):
        return False
    if _STRONG_AFTER.match(context_after) or re.search(r"[$€£₽]\s?$", context_before):
        return False
    return True


def _token_confirmed(tok: str, source_index: set[str]) -> bool:
    if re.fullmatch(r"\d{1,3}(?: \d{3})+[.,]\d+", tok):  # 12 345,67
        return bool(number_forms(tok) & source_index)
    parts = tok.split()
    if len(parts) > 1:
        joined = _strip_zeros("".join(parts))
        if joined in source_index:
            return True
        return all(number_forms(p) & source_index for p in parts)
    return bool(number_forms(tok) & source_index)


def check_numbers(text: str, source_index: set[str]) -> tuple[list[str], list[str]]:
    """Сверяет числа русского текста со «словарём чисел» источника.

    Возвращает (не найдены в источнике, слабые сигналы). Слабый сигнал —
    одиночная цифра без процента и дроби: «в 2 раза» при «doubled» в оригинале.
    """
    missing, minor = [], []
    clean = _clean(text)
    for m in _NUM_RE.finditer(clean):
        raw = m.group(0).strip().rstrip(".,")
        for tok in find_number_tokens(raw):
            if not number_forms(tok) or _token_confirmed(tok, source_index):
                continue
            after = clean[m.end():m.end() + 8]
            before = clean[max(0, m.start() - 2):m.start()]
            if is_minor_number(tok, after, before):
                if tok not in minor:
                    minor.append(tok)
            elif tok not in missing:
                missing.append(tok)
    return missing, minor


# --------------------------------------------------------------------------
# Русская типографика
# --------------------------------------------------------------------------
NBSP = " "
_UNITS = (
    r"г\.|гг\.|год[а-я]*|лет|дн[а-я]*|день|месяц[а-я]*|недел[а-я]*|час[а-я]*|минут[а-я]*|"
    r"млн|млрд|трлн|тыс\.|руб\.|долл\.|раз[а]?|человек|п\.\s?п\.|б\.\s?п\.|%|км|кг|т|шт\.|"
    r"январ[а-я]+|феврал[а-я]+|март[а-я]*|апрел[а-я]+|ма[йя]|июн[а-я]+|июл[а-я]+|август[а-я]*|"
    r"сентябр[а-я]+|октябр[а-я]+|ноябр[а-я]+|декабр[а-я]+"
)


def typo_ru(text: str) -> str:
    """Бережная типографика: ничего не меняет по смыслу и не трогает цифры."""
    if not text:
        return text
    t = text.replace("&nbsp;", NBSP).replace("&amp;", "&")
    t = re.sub(r"[ \t]+", " ", t).strip()
    t = t.replace("...", "…")
    # тире
    t = re.sub(r"(?<=\S) [-–] (?=\S)", NBSP + "— ", t)
    t = re.sub(r"(?<=\S) — ", NBSP + "— ", t)
    # диапазоны чисел: 10-18 -> 10–18
    t = re.sub(r"(?<![\d-])(\d{1,4}(?:[.,]\d+)?)-(\d{1,4}(?:[.,]\d+)?%?)(?![\d-])", r"\1–\2", t)
    # кавычки: "текст" -> «текст»
    t = re.sub(r'(^|[\s(\[])"(?=\S)', r"\1«", t)
    t = re.sub(r'(?<=\S)"(?=$|[\s.,;:!?)\]…])', "»", t)
    # разряды: 48 185 -> 48⍽185
    t = re.sub(r"(?<=\d) (?=\d{3}(?!\d))", NBSP, t)
    # проценты без пробела
    t = re.sub(r"(?<=\d)\s+%", "%", t)
    # число + единица / слово-счётчик
    t = re.sub(r"(?<=\d) (?=(?:%s)(?![а-яё]))" % _UNITS, NBSP, t, flags=re.I)
    # инициалы и сокращения
    t = re.sub(r"\b([A-ZА-ЯЁ]\.) ?([A-ZА-ЯЁ]\.) (?=[A-ZА-ЯЁ])", r"\1\2" + NBSP, t)
    t = re.sub(r"\b(см\.|рис\.|илл\.|табл\.|стр\.|гл\.|п\.|№) (?=\S)", r"\1" + NBSP, t)
    # короткие предлоги и союзы не висят в конце строки
    t = re.sub(r"(?<![^\s«(])([ВвКкСсУуОоИиАа]|[Нн][аеио]|[Пп]о|[Зз]а|[Ии]з|[Оо]т|[Дд]о|[Вв]о|[Сс]о|[Оо]б) (?=\S)", r"\1" + NBSP, t)
    return t


def esc_html(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
