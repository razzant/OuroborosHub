# -*- coding: utf-8 -*-
"""translate.py — перевод PDF на русский с сохранением исходной вёрстки.

Принцип: страница не пересобирается. Скрипт находит текстовые блоки, агент переводит
их по идентификаторам, скрипт убирает исходный текст из блока и вписывает русский в ту
же рамку (тем же кеглем, цветом и начертанием; если русский длиннее — кегль плавно
уменьшается). Графики, иллюстрации, фон, таблицы и цвета остаются оригинальными.

Команды:
  extract  <job> [--pages 1-10] [--all]     блоки для перевода (по умолчанию — только непереведённые)
  put      <job> --json '{"p3u2": "…"}' | --file <путь>   сдать переводы блоков
  glossary <job> [--json '{"term": "перевод"}']            единый словарь терминов документа
  status   <job>                             прогресс и предупреждения
  build    <job> [--paper a4|source] [--allow-missing]      собрать <название>_перевод.pdf

Ограничение: нужен текстовый слой. Страницы-сканы копируются как есть и перечисляются в отчёте.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import statistics
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # в папку навыка не пишется ничего, даже __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _core as core  # noqa: E402

UNITS_VERSION = 3        # версия разбивки страницы на блоки; при смене кэш units.json строится заново
EXTRACT_CAP = 12_000   # одна порция extract = один put; укладывается в лимит командной строки любой ОС
A4 = (595.28, 841.89)
BULLET_RE = re.compile(r"^\s*(?:[—–\-•▪■●◦]|\d{1,2}[.)])\s+")
BULLET_ONLY_RE = re.compile(r"^\s*[—–\-•▪■●◦·]\s*$")


# --------------------------------------------------------------------------
# Разбор страницы на блоки перевода
# --------------------------------------------------------------------------
def _line_text(line: dict) -> str:
    return "".join(sp["text"] for sp in line["spans"])


def _is_bold(sp: dict) -> bool:
    name = sp.get("font", "").lower()
    return bool(sp["flags"] & 16) or any(k in name for k in ("bold", "black", "heavy", "semibold", "demi", "medium"))


def _line_style(line: dict) -> tuple:
    best, size, color, bold, italic = 0, 10.0, 0, False, False
    for sp in line["spans"]:
        n = len(sp["text"].strip())
        if n > best:
            best = n
            size, color = float(sp["size"]), int(sp["color"])
            name = sp.get("font", "").lower()
            bold = _is_bold(sp)
            italic = bool(sp["flags"] & 2) or "italic" in name or "oblique" in name
    return size, color, bold, italic


def _line_weight(line: dict) -> str:
    flags = [_is_bold(sp) for sp in line["spans"] if sp["text"].strip()]
    if flags and all(flags):
        return "all"
    return "none" if not any(flags) else "mixed"


def _rich_text(lines: list[dict]) -> tuple[str, bool]:
    """Текст блока с разметкой <b>…</b>, если внутри смешаны полужирный и обычный.

    Возвращает (текст, mixed). Типичный случай — пункт списка, где первая фраза выделена.
    """
    segs: list[list] = []  # [текст, bold, sup]
    plain = ""
    for ln in lines:
        first = True
        for sp in ln["spans"]:
            t = re.sub(r"\s+", " ", sp["text"])
            if not t.strip() and not segs:
                continue
            if first and plain:
                if plain.endswith("-") and len(plain) > 2 and plain[-2].isalpha() and t.lstrip()[:1].islower():
                    plain = plain[:-1]
                    segs[-1][0] = segs[-1][0].rstrip()[:-1]
                    t = t.lstrip()
                elif not plain.endswith(" "):
                    t = " " + t.lstrip()
            first = False
            b = _is_bold(sp)
            # верхний индекс — маркер сноски («лидеры¹»): короткий кусок из цифр или знаков
            sup = bool(sp["flags"] & 1) and len(t.strip()) <= 3 and not re.search(r"[^\W\d_]{2,}", t)
            if segs and segs[-1][1] == b and segs[-1][2] == sup:
                segs[-1][0] += t
            else:
                segs.append([t, b, sup])
            plain += t
    letters_b = sum(len(re.findall(r"[^\W\d_]", t)) for t, b, _s in segs if b)
    letters_r = sum(len(re.findall(r"[^\W\d_]", t)) for t, b, _s in segs if not b)
    mixed = letters_b >= 3 and letters_r >= 3
    has_sup = any(sp for _t, _b, sp in segs)
    if not mixed and not has_sup:
        return re.sub(r"\s+", " ", plain).strip(), False
    out = ""
    for t, b, sp in segs:
        if sp and t.strip():
            out += "%s<sup>%s</sup>%s" % (" " if t.startswith(" ") else "", t.strip(), " " if t.endswith(" ") else "")
        elif mixed and b and t.strip():
            lead = " " if t.startswith(" ") else ""
            tail = " " if t.endswith(" ") else ""
            out += "%s<b>%s</b>%s" % (lead, t.strip(), tail)
        else:
            out += t
    return re.sub(r"\s+", " ", out).strip(), mixed


def _join_lines(texts: list[str]) -> str:
    out = ""
    for t in texts:
        t = re.sub(r"\s+", " ", t).strip()
        if not t:
            continue
        if out.endswith("-") and len(out) > 2 and out[-2].isalpha() and t[:1].islower():
            out = out[:-1] + t          # перенос слова
        elif out:
            out += " " + t
        else:
            out = t
    return out


def _needs_translation(text: str) -> bool:
    latin = len(re.findall(r"[A-Za-z]", text))
    return latin >= 2 and len(re.findall(r"[A-Za-z]{2,}", text)) >= 1


_FUNC_WORDS = {"the", "a", "an", "of", "to", "and", "or", "in", "for", "with", "that", "by", "at", "on", "from", "as",
               "is", "are", "was", "were", "be", "their", "its", "this", "these", "than", "which", "who", "but", "not", "&"}
_END_PUNCT_RE = re.compile(r"[.!?:;…][\"'»”’)\]]*\d{0,2}$")


def _is_horizontal(ln: dict) -> bool:
    return abs(ln["dir"][0] - 1) < 0.02 and abs(ln["dir"][1]) < 0.02


def _strong_link(a: str, b: str) -> bool:
    """Предыдущий кусок явно оборван: кончается служебным словом, запятой, дефисом или косой чертой."""
    a = a.strip()
    last = re.sub(r"[^\w&]+$", "", a.split()[-1].lower()) if a.split() else ""
    return a[-1:] in ",-–—/" or last in _FUNC_WORDS or (b[:1] == "(" and len(a) < 40)


def _continues(prev_text: str, next_text: str, strong_only: bool = False) -> bool:
    """Следующий кусок продолжает фразу предыдущего (абзац, разрезанный в PDF на два блока)."""
    a = _strip_tags(prev_text).strip()
    b = _strip_tags(next_text).strip()
    if not a or not b or _END_PUNCT_RE.search(a):
        return False
    if BULLET_RE.match(b) or b[:1] in "•·▪■●◦—–":
        return False                      # пункт списка со своим маркером — всегда отдельный блок
    if _strong_link(a, b):
        return True
    if strong_only:
        return False
    first = re.search(r"[^\W\d_]", b)
    return bool(first and first.group(0).islower())


def _merge_groups(groups: list[list[dict]]) -> list[list[dict]]:
    """Склеивает то, что PDF хранит разными блоками, хотя это один текст.

    1. Продолжение абзаца: первая строка абзаца или хвост цитаты часто лежат отдельным блоком.
    2. Подпись в несколько строк, сохранённая построчно: с общим правым краем («Software / coding /
       agents» у столбца диаграммы) или по центру (шапка колонки).
    Правила намеренно осторожные: лишняя склейка портит вёрстку сильнее, чем пропущенная.
    Поиск соседей идёт по сетке координат, поэтому плотные таблицы в тысячи ячеек не тормозят разбор.
    """
    text_cache: dict[int, str] = {}

    def text_of(g) -> str:
        key = (id(g), len(g))
        if key not in text_cache:
            text_cache[key] = _rich_text(g)[0]
        return text_cache[key]

    alive = {id(g) for g in groups}

    # ---- 1. продолжение абзаца -------------------------------------------------------------
    grid: dict[int, list] = {}
    for g in groups:
        grid.setdefault(int(g[0]["bbox"][0] // 3), []).append(g)
    for cell in grid.values():
        cell.sort(key=lambda g: g[0]["bbox"][1])

    def below(a):
        la = a[-1]
        sa, ca, _ba, ia = _line_style(la)
        key = int(la["bbox"][0] // 3)
        best = None
        for k in (key - 1, key, key + 1):
            for b in grid.get(k, ()):
                if b is a or id(b) not in alive:
                    continue
                lb = b[0]
                step = lb["bbox"][1] - la["bbox"][1]
                if step < 0.95 * sa:
                    continue
                if step > 1.75 * sa:
                    break
                if abs(la["bbox"][0] - lb["bbox"][0]) > 2.5:
                    continue
                sb, cb, _bb, ib = _line_style(lb)
                if abs(sa - sb) > 0.03 * sa or ca != cb or ia != ib:
                    continue
                if {_line_weight(la), _line_weight(lb)} == {"all", "none"}:
                    continue
                pitches = [g[i + 1]["bbox"][1] - g[i]["bbox"][1] for g in (a, b) for i in range(len(g) - 1)]
                if pitches and abs(step - statistics.median(pitches)) > 0.25 * sa:
                    continue
                widest = max(l["bbox"][2] - l["bbox"][0] for l in a + b)
                if la["bbox"][2] - la["bbox"][0] < 0.7 * widest:
                    continue                                   # предыдущая строка короткая — абзац закончился
                # две одиночные строки подряд — чаще список или ячейки, чем абзац: нужна явная оборванность
                both_single = len(a) == 1 and len(b) == 1
                if not _continues(text_of(a), text_of(b), strong_only=both_single):
                    continue
                if best is None or step < best[0]:
                    best = (step, b)
        return best[1] if best else None

    for a in sorted(groups, key=lambda g: g[0]["bbox"][1]):
        if id(a) not in alive:
            continue
        for _ in range(60):
            # короткая последняя строка — подпись или ячейка, а не оборванный абзац
            if len(_line_text(a[-1]).strip()) < (25 if len(a) == 1 else 20):
                break
            b = below(a)
            if b is None:
                break
            a.extend(b)
            alive.discard(id(b))

    # ---- 2. подписи в несколько строк, сохранённые построчно -------------------------------
    def edge(g, kind):
        vals = [l["bbox"][2] if kind == "right" else (l["bbox"][0] + l["bbox"][2]) / 2 for l in g]
        return max(vals) - min(vals), sum(vals) / len(vals)

    for kind, tol, cap in (("right", 1.5, 4), ("center", 2.0, 5)):
        labels = []
        for g in groups:
            if id(g) in alive and len(g) <= 4:
                spread, pos = edge(g, kind)
                if spread < tol:
                    labels.append((g, pos))
        cols: dict[int, list] = {}
        for g, pos in labels:
            cols.setdefault(int(pos // 2), []).append((g, pos))
        for col in cols.values():
            col.sort(key=lambda t: t[0][0]["bbox"][1])
        done: set[int] = set()
        for g, pos in sorted(labels, key=lambda t: t[0][0]["bbox"][1]):
            if id(g) in done or id(g) not in alive:
                continue
            chain, cur, cur_pos = [g], g, pos
            while len(chain) < 12:                              # цепочку строим целиком: длинная — это список
                la = cur[-1]
                sa, ca, ba, _ia = _line_style(la)
                nxt = None
                for k in (int(cur_pos // 2) - 1, int(cur_pos // 2), int(cur_pos // 2) + 1):
                    for h, hpos in cols.get(k, ()):
                        step = h[0]["bbox"][1] - la["bbox"][1]
                        if step < 0.9 * sa or id(h) in done or id(h) not in alive or any(h is c for c in chain):
                            continue
                        if step > 1.4 * sa:
                            break
                        sh, ch, bh, _ih = _line_style(h[0])
                        if abs(hpos - cur_pos) < tol and abs(sh - sa) <= 0.03 * sa and ch == ca and bh == ba:
                            if nxt is None or step < nxt[0]:
                                nxt = (step, h, hpos)
                if nxt is None:
                    break
                chain.append(nxt[1])
                cur, cur_pos = nxt[1], nxt[2]
            for c in chain:
                done.add(id(c))
            if len(chain) < 2 or sum(len(c) for c in chain) > cap:
                continue
            # у подписи в несколько строк первая строка начинается с заглавной, остальные — со строчной;
            # если и первая со строчной («low / mid / top»), это отдельные подписи — нужна явная оборванность
            head = re.search(r"[^\W_]", _strip_tags(text_of(chain[0])))
            weak_ok = bool(head and not head.group(0).islower())
            if not all(_continues(text_of(x), text_of(y), strong_only=not weak_ok) for x, y in zip(chain, chain[1:])):
                continue
            for c in chain[1:]:
                chain[0].extend(c)
                alive.discard(id(c))
    return [g for g in groups if id(g) in alive]


def page_units(pymupdf, page, pno: int) -> list[dict]:
    flags = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
    raw = page.get_text("dict", flags=flags)
    units: list[dict] = []

    def flush(lines: list[dict]) -> None:
        if not lines:
            return
        text, mixed = _rich_text(lines)
        if not text:
            return
        sizes = [(_line_style(ln), len(_line_text(ln))) for ln in lines]
        (size, color, bold, italic), _n = max(sizes, key=lambda s: s[1])
        if mixed:
            bold = False  # полужирное задаётся тегами внутри текста
        x0 = min(ln["bbox"][0] for ln in lines); y0 = min(ln["bbox"][1] for ln in lines)
        x1 = max(ln["bbox"][2] for ln in lines); y1 = max(ln["bbox"][3] for ln in lines)
        pitch = 1.2
        if len(lines) > 1:
            steps = [b["bbox"][1] - a["bbox"][1] for a, b in zip(lines, lines[1:]) if b["bbox"][1] > a["bbox"][1]]
            if steps and size > 0:
                pitch = min(1.7, max(1.0, statistics.median(steps) / size))
        align = "left"
        if len(lines) > 1:
            lefts = [ln["bbox"][0] for ln in lines]; rights = [ln["bbox"][2] for ln in lines]
            mids = [(a + b) / 2 for a, b in zip(lefts, rights)]
            spread = lambda v: max(v) - min(v)  # noqa: E731
            if spread(rights) < 1.5 and spread(lefts) > 1.5:
                align = "right"
            elif spread(mids) < (3 if spread(lefts) > 6 else 1.5) and spread(lefts) > 0.5:
                align = "center"
        units.append({
            "text": text, "bbox": [round(v, 2) for v in (x0, y0, x1, y1)],
            "lines": [[round(v, 2) for v in ln["bbox"]] for ln in lines],
            "size": round(size, 2), "color": "#%06x" % color, "bold": bold, "italic": italic,
            "pitch": round(pitch, 2), "align": align, "n_lines": len(lines), "rich": mixed,
        })

    groups: list[list[dict]] = []
    rotated: list[tuple[int, int, dict]] = []
    for bi, block in enumerate(raw.get("blocks", [])):
        if block.get("type") != 0:
            continue
        for li, ln in enumerate(block.get("lines", [])):
            if _line_text(ln).strip() and not _is_horizontal(ln):
                rotated.append((bi, li, ln))
        # маркеры списка («—», «•») — отдельные «линии»: оставляем их на странице как есть
        lines = [ln for ln in block.get("lines", [])
                 if _line_text(ln).strip() and _is_horizontal(ln) and not BULLET_ONLY_RE.match(_line_text(ln))]
        if not lines:
            continue
        lines.sort(key=lambda ln: (round(ln["bbox"][1], 1), ln["bbox"][0]))
        # табличная строка: две «линии» на одной высоте, разнесённые по горизонтали (в любом порядке:
        # значение столбца может стоять и правее, и левее подписи)
        tabular = False
        if len(lines) <= 80:
            for k, a in enumerate(lines):
                for b in lines[k + 1:]:
                    ha = min(a["bbox"][3] - a["bbox"][1], b["bbox"][3] - b["bbox"][1])
                    overlap = min(a["bbox"][3], b["bbox"][3]) - max(a["bbox"][1], b["bbox"][1])
                    apart = max(b["bbox"][0] - a["bbox"][2], a["bbox"][0] - b["bbox"][2])
                    if ha > 0 and overlap > 0.6 * ha and apart > 0.8 * ha:
                        tabular = True
                        break
                if tabular:
                    break
        if tabular:
            groups.extend([ln] for ln in lines)
            continue
        cur: list[dict] = []
        for ln in lines:
            if cur:
                ps, _pc, pb, _pi = _line_style(cur[-1])
                s, _c, b, _i = _line_style(ln)
                gap = ln["bbox"][1] - cur[-1]["bbox"][3]
                # смена начертания рвёт абзац, только если строки целиком разные (заголовок над текстом)
                # и между ними есть дополнительный просвет; выделенная первая фраза пункта абзац не рвёт
                weight_break = ({_line_weight(cur[-1]), _line_weight(ln)} == {"all", "none"}) and gap > 0.35 * ps
                # строки одного абзаца всегда перекрываются по горизонтали; иначе это соседняя подпись
                apart = ln["bbox"][0] > cur[-1]["bbox"][2] + 1 or ln["bbox"][2] < cur[-1]["bbox"][0] - 1
                new_par = (
                    abs(s - ps) > 0.12 * ps
                    or weight_break
                    or apart
                    or gap > 0.75 * ps
                    or BULLET_RE.match(_line_text(ln))
                )
                if new_par:
                    groups.append(cur)
                    cur = []
            cur.append(ln)
        if cur:
            groups.append(cur)

    for g in _merge_groups(groups):
        g.sort(key=lambda ln: (round(ln["bbox"][1], 1), ln["bbox"][0]))
        flush(g)

    # наклонный и вертикальный текст (подписи колонок под 45°, названия осей): по одной строке на блок
    if rotated:
        import math
        # текст по дуге (круговые схемы) приходит обрывками «Cli», «ma», «te» с разным наклоном у каждого —
        # такие не трогаем: берём только строки, чей угол повторяется на странице или кратен 45°
        def ang(ln):
            return round(math.degrees(math.atan2(ln["dir"][1], ln["dir"][0])) * 2) / 2
        angles = [ang(ln) for _b, _l, ln in rotated]
        distinct = len(set(angles))
        keep = []
        for (bi, li, ln), a in zip(rotated, angles):
            text = re.sub(r"\s+", " ", _line_text(ln)).strip()
            letters = len(re.findall(r"[^\W\d_]", text))
            shared = sum(1 for b in angles if abs(b - a) < 0.75)
            regular = abs(a / 45 - round(a / 45)) * 45 < 1.0
            if letters >= 4 and (shared >= 2 or (regular and distinct <= 6)) and not (distinct > 12 and shared < 3):
                keep.append((bi, li, ln))
        rotated = keep
    if rotated:
        rawc = page.get_text("rawdict", flags=flags)
        for bi, li, ln in rotated:
            text = re.sub(r"\s+", " ", _line_text(ln)).strip()
            try:
                all_chars = [c for sp in rawc["blocks"][bi]["lines"][li]["spans"] for c in sp["chars"]]
            except (IndexError, KeyError):
                all_chars = []
            chars = [c for c in all_chars if c["c"].strip()]
            if len(text) < 2 or len(chars) < 2:
                continue
            size, color, bold, italic = _line_style(ln)
            dx, dy = ln["dir"]
            p0 = chars[0]["origin"]
            adv = max(0.3 * size, ((chars[-1]["origin"][0] - chars[-2]["origin"][0]) ** 2
                                   + (chars[-1]["origin"][1] - chars[-2]["origin"][1]) ** 2) ** 0.5)
            p1 = (chars[-1]["origin"][0] + dx * adv, chars[-1]["origin"][1] + dy * adv)
            units.append({
                "text": text, "bbox": [round(v, 2) for v in ln["bbox"]], "lines": [],
                "chars": [[round(v, 2) for v in c["bbox"]] for c in all_chars],   # с пробелами: убираем строку целиком
                "rot": [round(dx, 4), round(dy, 4)], "p0": [round(p0[0], 2), round(p0[1], 2)],
                "p1": [round(p1[0], 2), round(p1[1], 2)],
                "size": round(size, 2), "color": "#%06x" % color, "bold": bold, "italic": italic,
                "pitch": 1.2, "align": "left", "n_lines": 1, "rich": False,
            })

    # буквица: одна заглавная буква крупным кеглем — приклеиваем к абзацу справа
    sizes = [u["size"] for u in units for _ in range(max(1, len(u["text"]) // 40))]
    body = statistics.median(sizes) if sizes else 10.0
    for u in list(units):
        if re.fullmatch(r"[A-Z]", u["text"].strip()) and u["size"] >= 1.8 * body:
            cands = [v for v in units if v is not u and re.sub(r"^(?:<[bi]>)+", "", v["text"])[:1].islower()
                     and v["bbox"][0] >= u["bbox"][0] and v["bbox"][1] < u["bbox"][3] + body
                     and v["bbox"][3] > u["bbox"][1] - body]
            if cands:
                tgt = min(cands, key=lambda v: abs(v["bbox"][1] - u["bbox"][1]) + abs(v["bbox"][0] - u["bbox"][2]))
                letter = u["text"].strip()
                tgt["text"] = re.sub(r"^((?:<[bi]>)*)", lambda m: m.group(1) + letter, tgt["text"], count=1)
                tgt["lines"] = u["lines"] + tgt["lines"]
                units.remove(u)

    # подписи с общим правым краем (категории на графиках) — выравнивание вправо;
    # совпадения с числами на осях не считаем: сверяемся только с текстовыми подписями
    def flush_right(u) -> bool:
        rights = [lb[2] for lb in u["lines"]] or [u["bbox"][2]]
        return max(rights) - min(rights) < 1.5

    labels = [u for u in units if u["n_lines"] <= 3 and not u.get("rot") and _needs_translation(u["text"]) and flush_right(u)]
    for u in labels:
        mates = [v for v in labels if abs(v["bbox"][2] - u["bbox"][2]) < 1.5 and abs(v["size"] - u["size"]) < 0.6]
        if len(mates) >= 3 and max(v["bbox"][0] for v in mates) - min(v["bbox"][0] for v in mates) > 6:
            u["align"] = "right"
    # шапка колонок: если в ряду есть блоки по центру, то и соседи этого ряда (в одну строку
    # или с равными строками, где выравнивание не определить) — тоже по центру
    centered = [u for u in units if u["align"] == "center" and u["n_lines"] <= 4]
    for u in units:
        if u.get("rot") or u["align"] != "left" or u["n_lines"] > 3 or not _needs_translation(u["text"]):
            continue
        if u["n_lines"] > 1:
            mids = [(lb[0] + lb[2]) / 2 for lb in u["lines"]]
            if max(mids) - min(mids) > 1.5:
                continue
        row = [v for v in centered if abs(v["bbox"][1] - u["bbox"][1]) < 2.5 and abs(v["size"] - u["size"]) < 0.3]
        if len(row) >= 2:
            u["align"] = "center"

    out = []
    for i, u in enumerate(units, 1):
        u["id"] = "p%du%d" % (pno, i)
        u["page"] = pno
        u["translate"] = _needs_translation(u["text"])
        role = "b"
        if u["size"] >= 1.25 * body or (u["bold"] and len(u["text"]) < 90 and u["n_lines"] <= 2):
            role = "h"
        elif u["size"] <= 0.82 * body:
            role = "s"
        u["role"] = role
        out.append(u)
    return out


def load_units(pymupdf, jdir: Path, deadline=None) -> dict:
    """Кэш блоков всего документа: tr/units.json (строится один раз, порциями)."""
    path = jdir / "tr" / "units.json"
    store = core.read_json(path, {"v": UNITS_VERSION, "pages_done": 0, "units": []})
    meta = core.read_json(jdir / "meta.json", {})
    total = int(meta.get("pages_total", 0))
    if store.get("v") != UNITS_VERSION:
        # разбивка на блоки от прежней версии навыка: строим заново, готовые переводы перенесём по тексту
        tr_dir = jdir / "tr"
        had_units = bool(store.get("units"))
        if had_units:
            core.write_json(tr_dir / "units.old.json", store)
            # старые переводы и служебные файлы сразу убираем в сторону: id блоков меняются, и put,
            # пришедший до конца разбора большого документа, не должен смешаться со старыми id
            for name in ("translations.json", "autofill.json"):
                f, old = tr_dir / name, tr_dir / name.replace(".json", ".old.json")
                if f.is_file():
                    os.replace(f, old)      # предыдущая резервная копия (от более старой миграции) больше не нужна
            for name in ("warnings.json", "progress.json", "fit.json", "work.pdf"):
                f = tr_dir / name
                if f.is_file():
                    f.unlink()
        store = {"v": UNITS_VERSION, "pages_done": 0, "units": [], "migrate": had_units}
        core.write_json(path, store)
    if store["pages_done"] >= total:
        if store.get("migrate"):
            _migrate_translations(jdir, store)
            core.write_json(path, store)
        return store
    doc = pymupdf.open(jdir / "source.pdf")
    for pno in range(store["pages_done"] + 1, total + 1):
        page = doc[pno - 1]
        if page.rotation:
            page.remove_rotation()
        store["units"].extend(page_units(pymupdf, page, pno))
        store["pages_done"] = pno
        if deadline is not None and deadline.expired():
            break
    if store["pages_done"] >= total and store.get("migrate"):
        _migrate_translations(jdir, store)
    core.write_json(path, store)
    return store


def _read_loose(path: Path, default):
    """Чтение вспомогательного файла миграции: испорченный или отсутствующий файл — просто «ничего нет»."""
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _migrate_translations(jdir: Path, store: dict) -> None:
    """Переносит переводы со старой разбивки на новую: совпали страница и исходный текст — перевод тот же.

    Блоки, которые новая версия режет иначе (склеенные абзацы, подписи), останутся непереведёнными —
    агент увидит их в extract и переведёт заново уже целыми. Переводы, сданные уже по новым id, не трогаем.
    """
    tr_dir = jdir / "tr"
    old_units = _read_loose(tr_dir / "units.old.json", {})
    old_units = old_units.get("units", []) if isinstance(old_units, dict) else []
    old_tr = _read_loose(tr_dir / "translations.old.json", {})
    old_auto = _read_loose(tr_dir / "autofill.old.json", {})
    if not isinstance(old_tr, dict):
        old_tr = {}
    if not isinstance(old_auto, dict):
        old_auto = {}
    # одинаковый текст может встретиться на странице дважды с разными переводами — раздаём по порядку
    queue: dict = {}
    for u in old_units:
        if isinstance(u, dict) and u.get("id") in old_tr:
            queue.setdefault((u.get("page"), _norm_src(u.get("text", ""))), []).append(u["id"])
    new_tr = core.read_json(tr_dir / "translations.json", {})
    new_auto = core.read_json(tr_dir / "autofill.json", {})
    old_to_new: dict = {}
    kept = 0
    for u in store["units"]:
        ids = queue.get((u["page"], _norm_src(u["text"])))
        if not ids or not u["translate"]:
            continue
        old_id = ids.pop(0) if len(ids) > 1 else ids[0]
        old_to_new.setdefault(old_id, u["id"])
        if u["id"] not in new_tr:
            new_tr[u["id"]] = old_tr[old_id]
            kept += 1
            src = old_auto.get(old_id)
            if src:
                new_auto[u["id"]] = src          # пока старый id источника; ниже заменим на новый
    for uid, src in list(new_auto.items()):
        if src in old_to_new:
            new_auto[uid] = old_to_new[src]
        elif src not in new_tr:
            new_auto.pop(uid)
    core.write_json(tr_dir / "translations.json", new_tr)
    core.write_json(tr_dir / "autofill.json", new_auto)
    store.pop("migrate", None)
    store["migrated"] = {"kept": kept, "of": len(old_tr)}


class _PageRect:
    """Минимальная замена pymupdf.Rect страницы для расчёта рамок без открытия PDF."""

    def __init__(self, size):
        w, h = size or (595.0, 842.0)
        self.x0, self.y0, self.x1, self.y1, self.width, self.height = 0.0, 0.0, float(w), float(h), float(w), float(h)


def _norm_src(t: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", t)).strip().lower()


def _load_tr(jdir: Path) -> dict:
    return core.read_json(jdir / "tr" / "translations.json", {})


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------
def cmd_extract(args) -> None:
    pymupdf = core.require_pymupdf()
    jdir = core.job_dir(args.job)
    deadline = core.Deadline(200)
    store = load_units(pymupdf, jdir, deadline)
    meta = core.read_json(jdir / "meta.json", {})
    total = int(meta.get("pages_total", 0))
    if store["pages_done"] < total:
        core.out({"ok": True, "complete": False, "pages_indexed": store["pages_done"], "pages_total": total,
                  "next": "Документ большой: повторите translate.py extract — разбор продолжится."})
    tr = _load_tr(jdir)
    wanted = set(core.parse_pages(args.pages, total))
    by_page: dict = {}
    for u in store["units"]:
        by_page.setdefault(u["page"], []).append(u)
    page_sizes = {p["n"]: (p["w"], p["h"]) for p in meta.get("page_info", [])}
    by_key: dict = {}
    for u in store["units"]:
        if u["translate"]:
            by_key.setdefault(_norm_src(u["text"]), []).append(u)
    shown, used, seen_src, last_page = [], 0, {}, None
    pages_sorted = sorted(wanted)
    stop_page = None
    for u in store["units"]:
        if u["page"] not in wanted or not u["translate"]:
            continue
        if stop_page is not None and u["page"] >= stop_page:
            continue
        if not args.all and u["id"] in tr:
            continue
        key = _norm_src(u["text"])
        if key in seen_src:
            continue
        item = {"id": u["id"], "t": u["text"], "role": u["role"]}
        if u.get("rot"):
            item["note"] = "наклонная подпись в одну строку — переводите предельно коротко"
        if len(by_key.get(key, [])) > 1:
            item["same"] = len(by_key[key]) - 1      # столько одинаковых блоков во всём документе переведутся заодно
        if u["n_lines"] <= 3 or len(u["text"]) < 90:  # короткие блоки (заголовки, подписи) — рамка тесная, считаем реальную вместимость
            # перевод разойдётся по всем одинаковым блокам документа, поэтому ориентир — самая тесная из их рамок
            twins = [o for o in by_key.get(key, [u]) if o["n_lines"] <= 3 or len(o["text"]) < 90][:40] or [u]
            fits, widths = [], []
            for o in twins:
                if o.get("rot"):
                    _anchor, max_len = _rot_plan(by_page.get(o["page"], [])).get(o["id"], ("center", 40.0))
                    fits.append(max(3, int(max_len / (0.56 * o["size"] * 0.85))))
                    continue
                page_units_ = [q for q in by_page.get(o["page"], []) if not q.get("rot")]
                xs = [q["bbox"] for q in page_units_]
                box, _al = _expand_rect(o, [q for q in page_units_ if q is not o], _PageRect(page_sizes.get(o["page"])),
                                        min(b[0] for b in xs), max(b[2] for b in xs))
                fits.append(_capacity(o, box))
                if o["n_lines"] > 1 or o["align"] == "center":
                    widths.append(_per_line(o, box))
            item["fit"] = min(fits)
            if widths and min(widths) < 24:
                item["w"] = min(widths)
        cost = len(u["text"]) + 40
        if shown and used + cost > EXTRACT_CAP:
            stop_page = u["page"]
            # страницу показываем целиком или не показываем вовсе
            shown = [s for s in shown if int(s["id"][1:].split("u")[0]) < stop_page] or shown
            continue
        shown.append(item)
        seen_src[key] = item
        used += cost
        last_page = u["page"]
    resp = {
        "ok": True, "job": args.job, "units": shown, "count": len(shown),
        "glossary": core.read_json(jdir / "tr" / "glossary.json", {}),
        "legend": "id — идентификатор блока; t — исходный текст (теги <b>…</b> — полужирное выделение: сохраните их в переводе вокруг тех же по смыслу слов; "
                  "<sup>…</sup> — маркер сноски верхним индексом: оставьте его как есть на том же месте); "
                  "role: h — заголовок или крупный текст, b — основной текст, s — мелкий (сноски, подписи); "
                  "fit — сколько знаков перевода помещается в рамку блока без заметного уменьшения шрифта; w — сколько знаков в одной строке узкой рамки: слово длиннее w не поместится, берите слова короче; same — сколько ещё блоков с тем же текстом во всём документе (переведутся автоматически; fit у таких блоков — по самой тесной рамке).",
    }
    if stop_page is not None:
        rest = [p for p in pages_sorted if p >= stop_page]
        resp["next_pages"] = "%d-%d" % (rest[0], rest[-1])
    if not shown:
        resp["note"] = "На этих страницах нечего переводить (всё переведено или нет текста)."
    else:
        resp["next"] = "Переведите блоки и сдайте: translate.py put %s --json '{\"%s\": \"…\", …}'" % (args.job, shown[0]["id"])
    core.out(resp)


def _latin_share(text: str) -> float:
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text)
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ch.isascii()) / len(letters)


# Кальки машинного перевода: в деловом русском тексте им есть нормальная замена.
CALQUES = [
    (r"\bкодинг\w*", "программирование"), (r"\bсофт(?!вер)\w{0,3}\b", "программы / ПО"), (r"\bредизайн\w*", "перестройка"),
    (r"\bворкфлоу\b", "рабочие процессы"), (r"\bимплементац\w+", "внедрение"), (r"\bдрайв(ить|ер\w*)\b", "фактор роста / двигать"),
    (r"\bкейс\w{0,3}\b", "пример / случай"), (r"\bинсайт\w*", "вывод / наблюдение"), (r"\bвалидир\w+", "проверять / подтверждать"),
    (r"\bхай-?перформер\w*", "лидеры"), (r"\bэнтерпрайз\w*", "крупные компании"),
]


def _calques(text: str) -> list[str]:
    low = text.lower()
    return ["«%s» → %s" % (m.group(0), fix) for rx, fix in CALQUES for m in [re.search(rx, low)] if m]


def cmd_put(args) -> None:
    pymupdf = core.require_pymupdf()
    jdir = core.job_dir(args.job)
    store = load_units(pymupdf, jdir, core.Deadline(200))
    by_id = {u["id"]: u for u in store["units"]}
    data = core.load_payload(args, "переводы блоков")
    if isinstance(data, list):
        data = {str(d.get("id")): d.get("ru") or d.get("t") or d.get("text") for d in data if isinstance(d, dict)}
    if not isinstance(data, dict):
        core.fail("Ожидался объект {id блока: перевод}.")
    tr = _load_tr(jdir)
    glossary = core.read_json(jdir / "tr" / "glossary.json", {})
    auto = core.read_json(jdir / "tr" / "autofill.json", {})      # id -> id блока, с которого скопирован перевод
    issues = core.read_json(jdir / "tr" / "warnings.json", {})    # id -> нерешённые замечания (их повторит build)
    saved, unknown, warnings, bad_values = 0, [], [], []
    for uid, ru in data.items():
        uid = str(uid)
        u = by_id.get(uid)
        if u is None:
            unknown.append(uid)
            continue
        if not isinstance(ru, str) or not ru.strip():
            bad_values.append(uid)   # null, число, объект или пустая строка — не перевод
            continue
        ru = core.typo_ru(re.sub(r"\s+", " ", ru).strip())
        mark = len(warnings)
        src_plain, ru_plain = _strip_tags(u["text"]), _strip_tags(ru)
        src_idx = core.number_index(src_plain)
        missing, _minor = core.check_numbers(ru_plain, src_idx)
        if missing:
            warnings.append({"id": uid, "problem": "в переводе есть числа, которых нет в исходном блоке: %s" % ", ".join(missing)})
        ru_idx = core.number_index(ru_plain)
        lost = [t for t in core.find_number_tokens(src_plain) if not (core.number_forms(t) & ru_idx) and len(t) > 1]
        if lost:
            warnings.append({"id": uid, "problem": "в переводе пропали числа: %s" % ", ".join(lost[:6])})
        if u.get("rich") and "<b>" not in ru.lower():
            warnings.append({"id": uid, "problem": "в исходном блоке первая фраза выделена <b>…</b>, в переводе выделения нет"})
        if "<sup>" in u["text"].lower() and "<sup>" not in ru.lower():
            warnings.append({"id": uid, "problem": "в исходном блоке есть маркер сноски <sup>…</sup>, в переводе он потерян"})
        if len(ru_plain) > 40 and _latin_share(ru_plain) > 0.6:
            warnings.append({"id": uid, "problem": "перевод почти целиком латиницей — похоже, блок не переведён"})
        bad_words = _calques(ru_plain)
        if bad_words:
            warnings.append({"id": uid, "problem": "калька вместо русского слова: %s" % "; ".join(bad_words[:4])})
        src_left = src_plain.lower()
        for term, val in sorted(glossary.items(), key=lambda kv: -len(kv[0])):   # длинные термины первыми
            # термин ищем целым словом («AI» не должен находиться внутри «maintain»), допускаем множественное число
            term_rx = re.compile(r"(?<![a-z])%s(?:e?s)?(?![a-z])" % re.escape(term.lower())) if term else None
            if term_rx and term_rx.search(src_left):
                # короткий термин внутри уже учтённого длинного («The state of AI» в названии опроса) не проверяем
                src_left = term_rx.sub(" ", src_left)
                if term_rx.search(ru_plain.lower()):
                    continue      # оставлен как в оригинале (часть названия) — это не ошибка
                # расшифровка в скобках нужна только при первом упоминании: сверяем основную часть
                main = re.sub(r"\([^)]*\)", " ", str(val))
                # основа слова без окончания: «боты» → «бот» найдётся в «чат-ботов»
                stems = [(w if len(w) <= 3 else w[:max(3, len(w) - 2)]).lower() for w in re.findall(r"[А-Яа-яЁёA-Za-z]{2,}", main)]
                ok_main = stems and all(st in ru_plain.lower() for st in stems)
                ok_term = bool(re.search(r"\(.*%s.*\)" % re.escape(term), str(val))) and term.lower() in ru_plain.lower()
                if not (ok_main or ok_term):
                    warnings.append({"id": uid, "problem": "термин «%s» переведён не по словарю («%s»)" % (term, val)})
        tr[uid] = ru
        auto.pop(uid, None)
        saved += 1
        mine = [w["problem"] for w in warnings[mark:]]
        if mine:
            issues[uid] = mine
        else:
            issues.pop(uid, None)
        # одинаковые блоки (колонтитулы, повторяющиеся подписи) переводим заодно — и обновляем при исправлении
        key = _norm_src(u["text"])
        for o in store["units"]:
            if o["id"] != uid and o["translate"] and _norm_src(o["text"]) == key and (o["id"] not in tr or o["id"] in auto):
                tr[o["id"]] = ru
                auto[o["id"]] = uid
    core.write_json(jdir / "tr" / "translations.json", tr)
    core.write_json(jdir / "tr" / "autofill.json", auto)
    core.write_json(jdir / "tr" / "warnings.json", issues)
    left = sum(1 for u in store["units"] if u["translate"] and u["id"] not in tr)
    resp = {"ok": True, "saved": saved, "left_untranslated": left, "warnings": warnings[:40]}
    if unknown:
        resp["unknown_ids"] = unknown[:20]
    if bad_values:
        resp["ignored_empty_or_non_text"] = bad_values[:20]
    if warnings:
        resp["note"] = "Проверьте блоки из warnings и при необходимости сдайте их заново (put с тем же id перезаписывает перевод)."
    resp["next"] = ("Остались непереведённые блоки: translate.py extract %s" % args.job) if left else ("Всё переведено: translate.py build %s" % args.job)
    core.out(resp)


def cmd_glossary(args) -> None:
    jdir = core.job_dir(args.job)
    path = jdir / "tr" / "glossary.json"
    glossary = core.read_json(path, {})
    if args.json or args.file:
        data = core.load_payload(args, "словарь терминов")
        if not isinstance(data, dict):
            core.fail("Словарь — объект {термин: перевод}.")
        for k, v in data.items():
            k = str(k).strip()
            if len(k) < 2:
                continue  # пустой термин совпадал бы с любым блоком
            if not isinstance(v, str) or not v.strip():
                glossary.pop(k, None)
            else:
                glossary[k[:80]] = v.strip()[:120]
        core.write_json(path, glossary)
    resp = {"ok": True, "glossary": glossary, "terms": len(glossary)}
    weak = ["%s: %s" % (k, "; ".join(_calques(v))) for k, v in glossary.items() if _calques(v)]
    if weak:
        resp["warnings"] = weak[:20]
        resp["note"] = "В словаре кальки — замените их русскими словами (повторный glossary с тем же ключом перезаписывает перевод)."
    core.out(resp)


def cmd_status(args) -> None:
    pymupdf = core.require_pymupdf()
    jdir = core.job_dir(args.job)
    store = load_units(pymupdf, jdir, core.Deadline(200))
    tr = _load_tr(jdir)
    meta = core.read_json(jdir / "meta.json", {})
    per_page = {}
    for u in store["units"]:
        if u["translate"]:
            a, b = per_page.get(u["page"], (0, 0))
            per_page[u["page"]] = (a + 1, b + (1 if u["id"] in tr else 0))
    todo = [p for p, (a, b) in sorted(per_page.items()) if b < a]
    total = sum(a for a, _ in per_page.values())
    done = sum(b for _, b in per_page.values())
    resp = {
        "ok": True, "pages_total": meta.get("pages_total"), "pages_indexed": store["pages_done"],
        "units_total": total, "units_translated": done,
        "pages_with_untranslated": todo[:80],
        "pages_without_text": meta.get("pages_without_text", [])[:60],
        "translation_pdf": str(core.output_paths(jdir)["translation_pdf"]) if core.output_paths(jdir)["translation_pdf"].is_file() else None,
        "unresolved_warnings": [{"id": k, "problems": v} for k, v in core.read_json(jdir / "tr" / "warnings.json", {}).items()][:30],
    }
    if store.get("migrated"):
        resp["resegmented"] = dict(store["migrated"], note="Навык обновился и режет страницы на блоки точнее (целые абзацы, подписи, "
                                   "наклонный текст). Совпавшие переводы перенесены, id блоков новые; остальное доперевести: translate.py extract.")
    core.out(resp)


# --------------------------------------------------------------------------
# Сборка переведённого PDF
# --------------------------------------------------------------------------
def _expand_rect(u: dict, others: list[dict], page_rect, text_left: float, text_right: float) -> tuple[list[float], str]:
    """Рамка для русского текста: исходная рамка + свободное место рядом. Возвращает (rect, align)."""
    x0, y0, x1, y1 = u["bbox"]
    h = y1 - y0
    align = u["align"]
    single = u["n_lines"] == 1

    def v_overlap(o) -> bool:
        return min(y1, o["bbox"][3]) - max(y0, o["bbox"][1]) > 0.3 * h

    if single:
        right_nb = [o["bbox"][0] for o in others if o["bbox"][0] >= x1 - 1 and v_overlap(o)]
        left_nb = [o["bbox"][2] for o in others if o["bbox"][2] <= x0 + 1 and v_overlap(o)]
        gap_right = (min(right_nb) - x1) if right_nb else None
        hugging = gap_right is not None and gap_right < 3 * u["size"] and not left_nb and x0 > page_rect.width * 0.35
        if align == "right" or (align == "left" and hugging):
            # подпись прижата правым краем (колонтитул у номера страницы, категории графика): растём влево
            align = "right"
            limit = (max(left_nb) + 8) if left_nb else text_left
            x0 = min(x0, max(limit, x0 - 0.8 * (x1 - x0)))
        elif align == "left":
            limit = (min(right_nb) - 8) if right_nb else text_right
            x1 = max(x1, limit)

    if align == "center" and u["n_lines"] <= 4:
        # шапка колонки: растём в обе стороны поровну, до середины просвета с соседями по ряду
        right_nb = [o["bbox"][0] for o in others if o["bbox"][0] >= x1 - 1 and v_overlap(o)]
        left_nb = [o["bbox"][2] for o in others if o["bbox"][2] <= x0 + 1 and v_overlap(o)]
        room_l = (x0 - max(left_nb)) / 2 - 1.5 if left_nb else 0.4 * (x1 - x0)
        room_r = (min(right_nb) - x1) / 2 - 1.5 if right_nb else 0.4 * (x1 - x0)
        grow = max(0.0, min(room_l, room_r, 0.6 * (x1 - x0)))
        x0, x1 = x0 - grow, x1 + grow

    # вниз — до ближайшего блока под этим, оставляя не меньше трети исходного просвета.
    # «Под этим» — по середине рамки: рамки соседних строк в PDF часто перекрываются на пункт-другой.
    floor = page_rect.y1 - 8
    mid = (y0 + y1) / 2
    for o in others:
        ox0, oy0, ox1, _oy1 = o["bbox"]
        if oy0 > mid and min(x1, ox1) - max(x0, ox0) > 2:
            gap = oy0 - y1
            floor = min(floor, oy0 - max(1.5, 0.35 * gap) if gap > 0 else y1)
    line_h = u["size"] * u["pitch"]
    if single and u["size"] < 12:
        cap = 0.25 * u["size"]          # мелкая подпись остаётся в одну строку и ужимается кеглем
    else:
        cap = max(line_h * 1.5, 0.3 * h)
    extra = max(0.0, min(floor - y1, cap))
    return [x0, y0 - 0.5, x1 + 1.0, y1 + extra], align


def _rot_plan(page_units_: list[dict]) -> dict:
    """Для наклонных подписей страницы: к какому концу они привязаны и какая длина строки доступна.

    Подписи колонок под 45° обычно кончаются у колонки (общая линия концов) — тогда перевод
    тоже должен кончаться там же; подписи под осью — начинаются от оси. Одиночная — по центру.
    """
    plan: dict = {}
    rots = [u for u in page_units_ if u.get("rot")]
    for u in rots:
        mates = [v for v in rots if abs(v["rot"][0] - u["rot"][0]) < 0.05 and abs(v["rot"][1] - u["rot"][1]) < 0.05
                 and abs(v["size"] - u["size"]) < 0.6]
        length = lambda v: ((v["p1"][0] - v["p0"][0]) ** 2 + (v["p1"][1] - v["p0"][1]) ** 2) ** 0.5  # noqa: E731
        if len(mates) < 2:
            plan[u["id"]] = ("center", length(u) * 1.15)
            continue
        def spread(key):  # noqa: E306
            xs = [v[key][0] for v in mates]; ys = [v[key][1] for v in mates]
            return min(max(xs) - min(xs), max(ys) - min(ys))
        anchor = "end" if spread("p1") < spread("p0") else "start"
        plan[u["id"]] = (anchor, max(length(v) for v in mates) * 1.08)
    return plan


def _per_line(u: dict, rect: list[float]) -> int:
    """Сколько знаков помещается в одну строку рамки (при кегле 0,85): слово длиннее не влезет."""
    return max(1, int((rect[2] - rect[0]) / (0.56 * u["size"] * 0.85)))


def _capacity(u: dict, rect: list[float]) -> int:
    """Сколько знаков русского текста помещается в рамку при допустимом уменьшении кегля до 0,85."""
    size = u["size"] * 0.85
    per_line = max(1.0, (rect[2] - rect[0]) / (0.56 * size))
    lines = max(1, int((rect[3] - rect[1] + 0.6) // (size * u["pitch"])))
    return max(3, int(per_line * lines * 0.97))


_TAG_RE = re.compile(r"</?(?:b|i|sup)>", re.I)
GLUE = ("\x00", None, False)   # маркер: этот кусок продолжает слово, пробел перед ним не нужен (перенос по дефису)


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text)


class Typesetter:
    """Набор русского текста в рамку встроенными шрифтами PyMuPDF (Helvetica/Arial-совместимые, с кириллицей).

    Свой простой набор вместо insert_htmlbox: шрифты встраиваются в документ один раз
    (а не на каждый блок), файл не раздувается, сборка в разы быстрее.
    """

    SCALES = [1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5, 0.45, 0.4, 0.35, 0.3]

    def __init__(self, pymupdf):
        self.mu = pymupdf
        self.fonts = {
            (False, False): pymupdf.Font("helv"), (True, False): pymupdf.Font("hebo"),
            (False, True): pymupdf.Font("heit"), (True, True): pymupdf.Font("hebi"),
        }
        self.fallback = pymupdf.Font("cjk")  # редкие символы, которых нет в основном шрифте
        # Во встроенных наклонных шрифтах у кириллической «т» сломана метрика: глиф обычной ширины,
        # а шаг — как у рукописной «m», из-за чего слово рвётся («коммен т арий»). Ставим её сами:
        # (сдвиг глифа влево, шаг) в долях кегля.
        self.fix = {id(self.fonts[(False, True)]): {"т": (-0.18, 0.47)},
                    id(self.fonts[(True, True)]): {"т": (-0.158, 0.50)}}

    def _pieces(self, text: str, key: tuple, sup: bool = False) -> list[tuple]:
        """Делит текст на куски по наличию глифов: (текст, шрифт, верхний индекс)."""
        font = self.fonts[key]
        out, cur, cur_fb = [], "", False
        for ch in text:
            fb = not font.has_glyph(ord(ch)) and ch != core.NBSP
            if fb != cur_fb and cur:
                out.append((cur, self.fallback if cur_fb else font, sup))
                cur = ""
            cur_fb = fb
            cur += ch
        if cur:
            out.append((cur, self.fallback if cur_fb else font, sup))
        fix = self.fix.get(id(font))
        if fix:
            split = []
            for t, f, sp in out:
                if f is font and any(ch in fix for ch in t):
                    split.extend((part, f, sp) for part in re.split("(%s)" % "|".join(map(re.escape, fix)), t) if part)
                else:
                    split.append((t, f, sp))
            out = split
        return out

    SUP_SCALE, SUP_RAISE = 0.62, 0.33      # верхний индекс: кегль и подъём в долях основного кегля

    def adv(self, text: str, font, size: float, sup: bool = False) -> float:
        if font is None:
            return 0.0                      # маркер переноса
        if sup:
            size *= self.SUP_SCALE
        fix = self.fix.get(id(font))
        if fix and text in fix:
            return fix[text][1] * size
        return font.text_length(text, size)

    def put(self, tw, x: float, y: float, text: str, font, size: float, sup: bool = False) -> float:
        """Ставит кусок текста и возвращает новую позицию по x."""
        if font is None:
            return x
        if sup:
            y -= self.SUP_RAISE * size
            size *= self.SUP_SCALE
        fix = self.fix.get(id(font))
        dx = fix[text][0] * size if fix and text in fix else 0.0
        tw.append((x + dx, y), text, font=font, fontsize=size)
        return x + self.adv(text, font, size)

    def chunks(self, ru: str, bold: bool, italic: bool) -> list[list[tuple]]:
        """Неразрывные цепочки: слово вместе с прилипшими знаками и тегами внутри."""
        ru = ru.replace("\u202f", core.NBSP).replace("₽", "руб.")
        chunks: list[list[tuple]] = []
        cur: list[tuple] = []
        b, i, sup, pos = bold, italic, False, 0
        for m in list(_TAG_RE.finditer(ru)) + [None]:
            seg = ru[pos:m.start()] if m else ru[pos:]
            parts = re.split(r"( +)", seg)
            for part in parts:
                if not part:
                    continue
                if part.isspace() and core.NBSP not in part:
                    if cur:
                        chunks.append(cur)
                        cur = []
                else:
                    cur.extend(self._pieces(part, (b, i), sup))
            if m:
                tag = m.group(0).lower()
                on = not tag.startswith("</")
                if "sup" in tag:
                    sup = on
                elif "b" in tag:
                    b = on or bold
                else:
                    i = on or italic
                pos = m.end()
        if cur:
            chunks.append(cur)
        return chunks

    def _w(self, chunk: list[tuple], size: float) -> float:
        return sum(self.adv(text, font, size, sup) for text, font, sup in chunk)

    @staticmethod
    def _split_hyphen(chunk: list[tuple]) -> list[list[tuple]] | None:
        """Слово с дефисом («Агенты-разработчики») делится по дефису, когда целиком не влезает в строку."""
        out, cur = [], []
        for text, font, sup in chunk:
            if font is None:
                continue
            parts = re.split(r"(?<=[^\W\d_][-‑])(?=[^\W\d_])", text)
            for k, part in enumerate(parts):
                if k:
                    out.append(cur)
                    cur = [GLUE]
                cur.append((part, font, sup))
        out.append(cur)
        return out if len(out) > 1 else None

    def attempts(self, max_scale: float = 1.0) -> list[tuple[float, bool]]:
        """Порядок подбора: сначала без переноса по дефису (до 0,75), потом с переносом на любом кегле."""
        scales = [sc for sc in self.SCALES if sc <= max_scale + 1e-6]
        return [(sc, False) for sc in scales if sc >= 0.75] + [(sc, True) for sc in scales]

    def wrap(self, chunks: list, size: float, max_w: float, hyphen: bool = True) -> tuple[list, bool]:
        space = self.fonts[(False, False)].text_length(" ", size)
        lines, cur, cur_w, overflow = [], [], 0.0, False
        queue = list(chunks)
        while queue:
            ch = queue.pop(0)
            w = self._w(ch, size)
            glue = bool(ch) and ch[0] is GLUE
            if w > max_w + 0.5:
                sub = self._split_hyphen(ch) if hyphen else None
                if sub:
                    queue[0:0] = sub
                    continue
                overflow = True
            gap = 0.0 if (glue or not cur) else space
            if cur and cur_w + gap + w > max_w + 0.5:
                lines.append((cur, cur_w))
                cur, cur_w, gap = [], 0.0, 0.0
            cur_w += gap + w
            cur.append(ch)
        if cur:
            lines.append((cur, cur_w))
        return lines, overflow

    def fit(self, rect, ru: str, u: dict) -> float:
        """Наибольший масштаб кегля, при котором текст помещается в рамку."""
        font0 = self.fonts[(False, False)]
        chunks = self.chunks(ru, u["bold"], u["italic"])
        for scale, hyphen in self.attempts():
            size = u["size"] * scale
            lines, overflow = self.wrap(chunks, size, rect.width, hyphen)
            need = (len(lines) - 1) * size * u["pitch"] + (font0.ascender - font0.descender) * size
            if need <= rect.height + 0.6 and not overflow:
                return scale
        return self.SCALES[-1]

    def draw(self, page, rect, ru: str, u: dict, align: str, max_scale: float = 1.0) -> float:
        font0 = self.fonts[(False, False)]
        asc, desc = font0.ascender, font0.descender
        chunks = self.chunks(ru, u["bold"], u["italic"])
        if not chunks:
            return 1.0
        chosen = None
        for scale, hyphen in self.attempts(max_scale):
            size = u["size"] * scale
            lines, overflow = self.wrap(chunks, size, rect.width, hyphen)
            need = (len(lines) - 1) * size * u["pitch"] + (asc - desc) * size
            if (need <= rect.height + 0.6 and not overflow) or (scale == self.SCALES[-1] and hyphen):
                chosen = (scale, size, lines)
                break
        scale, size, lines = chosen
        space = font0.text_length(" ", size)
        color = tuple(int(u["color"][k:k + 2], 16) / 255 for k in (1, 3, 5))
        tw = self.mu.TextWriter(page.rect)
        y = rect.y0 + asc * size
        for line, line_w in lines:
            if align == "center":
                x = rect.x0 + (rect.width - line_w) / 2
            elif align == "right":
                x = rect.x1 - line_w
            else:
                x = rect.x0
            for n, chunk in enumerate(line):
                if n and not (chunk and chunk[0] is GLUE):
                    x += space
                for text, font, sup in chunk:
                    x = self.put(tw, x, y, text, font, size, sup)
            y += size * u["pitch"]
        tw.write_text(page, color=color)
        return scale


def _draw_rotated(pymupdf, ts: Typesetter, page, ru: str, u: dict, anchor: str, max_len: float) -> float:
    """Одна строка вдоль исходного направления; длиннее доступного — уменьшаем кегль."""
    import math
    text = re.sub(r"\s+", " ", _strip_tags(ru)).strip()
    pieces = ts._pieces(text, (u["bold"], u["italic"]))
    scale, size, width = 1.0, u["size"], 0.0
    for scale in ts.SCALES:
        size = u["size"] * scale
        width = sum(ts.adv(t, font, size, sp) for t, font, sp in pieces)
        if width <= max_len:
            break
    dx, dy = u["rot"]
    if anchor == "end":
        sx, sy = u["p1"][0] - dx * width, u["p1"][1] - dy * width
    elif anchor == "start":
        sx, sy = u["p0"]
    else:
        mx, my = (u["p0"][0] + u["p1"][0]) / 2, (u["p0"][1] + u["p1"][1]) / 2
        sx, sy = mx - dx * width / 2, my - dy * width / 2
    color = tuple(int(u["color"][k:k + 2], 16) / 255 for k in (1, 3, 5))
    tw = pymupdf.TextWriter(page.rect)
    x = sx
    for t, font, sp in pieces:
        x = ts.put(tw, x, sy, t, font, size, sp)
    angle = math.degrees(math.atan2(dy, dx))
    tw.write_text(page, color=color, morph=(pymupdf.Point(sx, sy), pymupdf.Matrix(ROT_SIGN * angle)))
    return scale


ROT_SIGN = -1.0   # знак поворота TextWriter относительно направления строки MuPDF (проверено на выводе get_text)


def build_page(pymupdf, ts: Typesetter, page, units: list[dict], tr: dict) -> list[dict]:
    """Заменяет текст на странице на месте. Возвращает отчёт о тесных блоках."""
    if page.rotation:
        page.remove_rotation()
    todo = [u for u in units if u["id"] in tr]
    report: list[dict] = []
    if not todo:
        return report
    for u in todo:
        for lb in u["lines"]:
            hh = lb[3] - lb[1]
            # рамку удаления сужаем по вертикали, чтобы не задеть соседние строки
            r = pymupdf.Rect(lb[0] - 0.5, lb[1] + 0.18 * hh, lb[2] + 0.5, lb[3] - 0.18 * hh)
            page.add_redact_annot(r, fill=False, cross_out=False)
        for cb in u.get("chars", []):
            # наклонная строка: убираем по буквам — общая рамка строки под 45° накрыла бы соседей
            if cb[2] - cb[0] < 0.4 or cb[3] - cb[1] < 0.4:
                continue
            mx, my = 0.22 * (cb[2] - cb[0]), 0.22 * (cb[3] - cb[1])
            page.add_redact_annot(pymupdf.Rect(cb[0] + mx, cb[1] + my, cb[2] - mx, cb[3] - my), fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    flat = [u for u in units if not u.get("rot")]
    text_right = max([u["bbox"][2] for u in flat] + [page.rect.x1 * 0.6])
    text_left = min([u["bbox"][0] for u in flat] + [page.rect.x1 * 0.4])
    rot_plan = _rot_plan(units)
    for u in todo:
        if u.get("rot"):
            anchor, max_len = rot_plan.get(u["id"], ("center", 40.0))
            sc = _draw_rotated(pymupdf, ts, page, tr[u["id"]], u, anchor, max_len)
            if sc < 0.8:
                n = len(_strip_tags(tr[u["id"]]))
                report.append({"id": u["id"], "scale": round(sc, 2), "ru_chars": n,
                               "target_chars": max(3, min(n - 1, int(max_len / (0.56 * u["size"] * 0.85))))})
    todo = [u for u in todo if not u.get("rot")]
    # Первый проход — какой масштаб нужен каждому блоку; абзацы одного кегля выравниваем
    # по самому тесному (но не ниже 0,75), чтобы соседние абзацы не «прыгали» размером.
    plan = []
    for u in todo:
        box, align = _expand_rect(u, [o for o in flat if o is not u], page.rect, text_left, text_right)
        rect = pymupdf.Rect(*box)
        plan.append((u, rect, align, ts.fit(rect, tr[u["id"]], u)))
    group_scale: dict = {}
    for u, _rect, _align, sc in plan:
        if u["n_lines"] > 1 and sc >= 0.75:
            key = (round(u["size"], 1), u["bold"])
            group_scale[key] = min(group_scale.get(key, 1.0), sc)
    for u, rect, align, sc in plan:
        cap = group_scale.get((round(u["size"], 1), u["bold"]), 1.0) if u["n_lines"] > 1 else 1.0
        scale = ts.draw(page, rect, tr[u["id"]], u, align, max_scale=min(sc, cap))
        if sc < 0.8:
            n = len(_strip_tags(tr[u["id"]]))
            report.append({"id": u["id"], "scale": round(sc, 2), "ru_chars": n,
                           "target_chars": min(n - 1, _capacity(u, [rect.x0, rect.y0, rect.x1, rect.y1]))})
    return report


def _edge_colors(pymupdf, page):
    """Средний цвет верхней/нижней и левой/правой кромок страницы; None для белой кромки."""
    try:
        pix = page.get_pixmap(dpi=12, alpha=False)
    except Exception:
        return {}
    w, h = pix.width, pix.height
    if w < 4 or h < 4:
        return {}
    xs = [int(w * k / 6) for k in range(1, 6)]
    ys = [int(h * k / 6) for k in range(1, 6)]
    edges = {"top": [(x, 1) for x in xs], "bottom": [(x, h - 2) for x in xs],
             "left": [(1, y) for y in ys], "right": [(w - 2, y) for y in ys]}
    out = {}
    for name, pts in edges.items():
        cols = [pix.pixel(x, y)[:3] for x, y in pts]
        dark = [c for c in cols if min(c) < 235]
        if len(dark) >= 4:  # кромка почти вся цветная — страница «в обрез»
            out[name] = tuple(sum(c[i] for c in cols) / len(cols) / 255 for i in range(3))
    return out


def cmd_build(args) -> None:
    pymupdf = core.require_pymupdf()
    jdir = core.job_dir(args.job)
    deadline = core.Deadline(220)
    store = load_units(pymupdf, jdir, deadline)
    meta = core.read_json(jdir / "meta.json", {})
    total = int(meta.get("pages_total", 0))
    if store["pages_done"] < total:
        core.out({"ok": True, "done": False, "next": "Разбор документа не закончен: повторите translate.py build."})
    tr = _load_tr(jdir)
    missing = [u["id"] for u in store["units"] if u["translate"] and u["id"] not in tr]
    if missing and not args.allow_missing:
        pages = sorted({int(m[1:].split("u")[0]) for m in missing})
        core.fail("Не переведено блоков: %d (страницы: %s)." % (len(missing), ", ".join(map(str, pages[:25]))),
                  "Допереведите: translate.py extract %s --pages %d-%d. Если пользователь согласен на частичный перевод — добавьте --allow-missing."
                  % (args.job, pages[0], pages[-1]))

    by_page: dict[int, list[dict]] = {}
    for u in store["units"]:
        by_page.setdefault(u["page"], []).append(u)

    # Рабочая копия документа правится на месте и сохраняется по ходу: сборка возобновляема.
    tr_dir = jdir / "tr"
    work_path, prog_path, fit_path = tr_dir / "work.pdf", tr_dir / "progress.json", tr_dir / "fit.json"
    progress = core.read_json(prog_path, {}) if work_path.is_file() else {}
    fit_all = core.read_json(fit_path, {}) if progress else {}
    src = pymupdf.open(jdir / "source.pdf")
    work = pymupdf.open(work_path) if progress else pymupdf.open(jdir / "source.pdf")

    ts = Typesetter(pymupdf)
    interrupted = False
    for pno in range(1, total + 1):
        units = by_page.get(pno, [])
        sig = hashlib.sha256(repr([(u["id"], tr.get(u["id"])) for u in units]).encode("utf-8")).hexdigest()[:16]
        if progress.get(str(pno)) == sig:
            continue
        if str(pno) in progress:  # перевод страницы изменился: возвращаем исходную страницу и делаем заново
            work.delete_page(pno - 1)
            work.insert_pdf(src, from_page=pno - 1, to_page=pno - 1, start_at=pno - 1)
        fit_all[str(pno)] = build_page(pymupdf, ts, work[pno - 1], units, tr)
        progress[str(pno)] = sig
        if deadline.expired():
            interrupted = pno < total
            break
    tmp_path = tr_dir / "work.tmp.pdf"
    work.save(tmp_path, garbage=1, deflate=True)
    work.close()
    os.replace(tmp_path, work_path)
    core.write_json(prog_path, progress)
    core.write_json(fit_path, fit_all)
    if interrupted:
        core.out({"ok": True, "done": False, "pages_ready": len(progress), "pages_total": total,
                  "next": "Время вызова на исходе: повторите translate.py build — сборка продолжится со следующей страницы."})

    if deadline.elapsed() > 110:
        core.out({"ok": True, "done": False, "pages_ready": len(progress), "pages_total": total,
                  "next": "Страницы готовы, осталась финальная сборка файла: повторите translate.py build."})

    # итоговый файл
    work = pymupdf.open(work_path)
    banded = 0
    if args.paper == "a4":
        out_doc = pymupdf.open()
        for i in range(work.page_count):
            r = work[i].rect
            W, H = (A4[1], A4[0]) if r.width > r.height else A4
            k = min(W / r.width, H / r.height)
            w, h = r.width * k, r.height * k
            same_shape = abs(W / H - r.width / r.height) < 0.01
            edges = {} if same_shape else _edge_colors(pymupdf, work[i])
            banded += 1 if edges else 0
            page = out_doc.new_page(width=W, height=H)
            x_pad, y_pad = (W - w) / 2, (H - h) / 2
            # поля A4 вокруг страницы другого формата красим в цвет её кромки, если страница «в обрез»
            if y_pad > 0.5:
                if "top" in edges:
                    page.draw_rect(pymupdf.Rect(0, 0, W, y_pad + 1), color=None, fill=edges["top"])
                if "bottom" in edges:
                    page.draw_rect(pymupdf.Rect(0, H - y_pad - 1, W, H), color=None, fill=edges["bottom"])
            if x_pad > 0.5:
                if "left" in edges:
                    page.draw_rect(pymupdf.Rect(0, 0, x_pad + 1, H), color=None, fill=edges["left"])
                if "right" in edges:
                    page.draw_rect(pymupdf.Rect(W - x_pad - 1, 0, W, H), color=None, fill=edges["right"])
            page.show_pdf_page(pymupdf.Rect(x_pad, y_pad, x_pad + w, y_pad + h), work, i)
    else:
        out_doc = work
    title = (meta.get("pdf_title") or meta.get("source_name") or "").strip()
    out_doc.set_metadata({"title": (title + " — перевод на русский").strip(" —"), "producer": core.SKILL_NAME})
    out_dir = jdir / "out"
    out_dir.mkdir(exist_ok=True)
    pdf_path = core.output_paths(jdir)["translation_pdf"]
    for legacy in (out_dir / "translation_ru.pdf",):
        if legacy.is_file() and legacy != pdf_path:
            legacy.unlink()
    try:
        out_doc.subset_fonts()
    except Exception:
        pass  # без подмножества шрифтов файл просто чуть больше
    # garbage=4 склеивает одинаковые объекты (файл выходит не больше исходного), но на огромных документах медленный
    heavy = work_path.stat().st_size > 150_000_000
    out_doc.save(pdf_path, garbage=1 if heavy else 4, deflate=True)

    tight = [dict(x, page=int(p)) for p, rep in fit_all.items() for x in rep]
    tight.sort(key=lambda x: x["scale"])
    resp = {
        "ok": True, "done": True, "pdf": str(pdf_path), "file_name": pdf_path.name, "pages": total, "paper": args.paper,
        "units_translated": sum(1 for u in store["units"] if u["id"] in tr),
        "units_missing": len(missing),
        "pages_without_text": meta.get("pages_without_text", [])[:60],
        "tight_units": tight[:25], "tight_total": len(tight),
    }
    if banded:
        resp["paper_note"] = ("Исходные страницы не формата A4, и у %d из них фон «в обрез»: на листе A4 они получили поля в цвет кромки. "
                              "Если пользователю важнее вид, чем формат для печати, соберите с --paper source — страницы останутся исходного размера." % banded)
    issues = core.read_json(jdir / "tr" / "warnings.json", {})
    open_issues = [{"id": k, "problems": v} for k, v in issues.items() if k in tr]
    if open_issues:
        resp["unresolved_warnings"] = open_issues[:30]
        resp["unresolved_total"] = len(open_issues)
        resp["quality"] = "check_required"
    if resp["units_translated"] == 0:
        resp["warnings"] = ["В документе не нашлось текста для перевода (скан, пустые страницы или текст уже на русском): файл собран без изменений."]
    if tight:
        resp["note"] = ("tight_units — блоки, где русский текст пришлось заметно уменьшить (scale < 0,8). "
                        "Сократите перевод примерно до target_chars знаков без потери смысла, сдайте через put и соберите снова. "
                        "Блоки со scale ≥ 0,65 обычно читаются нормально; ниже — исправьте обязательно.")
    if open_issues:
        resp["next"] = ("Сначала разберите unresolved_warnings: это замечания put, которые остались неисправленными (числа, непереведённые блоки, термины). "
                        "Исправьте блоки повторным put и соберите снова; если замечание ложное — скажите об этом пользователю при сдаче.")
    else:
        resp["next"] = "Проверьте 2–3 страницы глазами: pdf.py render %s --pages 1-3 --translated. Затем отдайте PDF пользователю." % args.job
    core.out(resp)


def main() -> None:
    core.setup_stdio()
    ap = core.JsonArgumentParser(prog="translate.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("extract"); p.add_argument("job"); p.add_argument("--pages"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_extract)
    p = sub.add_parser("put"); p.add_argument("job"); p.add_argument("--json"); p.add_argument("--file"); p.set_defaults(fn=cmd_put)
    p = sub.add_parser("glossary"); p.add_argument("job"); p.add_argument("--json"); p.add_argument("--file"); p.set_defaults(fn=cmd_glossary)
    p = sub.add_parser("status"); p.add_argument("job"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("build"); p.add_argument("job"); p.add_argument("--paper", choices=["a4", "source"], default="a4")
    p.add_argument("--allow-missing", action="store_true"); p.set_defaults(fn=cmd_build)
    args = ap.parse_args()
    if not getattr(args, "fn", None):
        core.fail("Не указана команда.", "Доступно: extract, put, glossary, status, build.")
    args.fn(args)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        core.fail("Внутренняя ошибка translate.py: %s: %s" % (type(e).__name__, e))
