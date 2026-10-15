# -*- coding: utf-8 -*-
"""summary.py — сборка одностраничного резюме для руководителя (A4, PDF + HTML).

Команды:
  schema                                   напечатать формат summary.json и бюджет знаков
  build <job> --json '<JSON-строка>' | --file <путь внутри папки состояния>  [--density standard|compact]

build делает всё детерминированно, без участия модели:
  1) проверяет структуру и редакторский канон (длины, стоп-слова, «морали», описательность);
  2) сверяет каждое число резюме с текстом исходного PDF;
  3) расставляет русскую типографику;
  4) верстает страницу A4 (Arial-совместимый шрифт, 12 pt, интервал 1,5), считает страницы
     и заполнение листа, при переполнении говорит, сколько знаков убрать;
  5) сохраняет <название>_summary.pdf, <название>_summary.html и summary.json в папку задания.

Вердикт: "ready" — можно отдавать (<название>_summary.pdf); "fix_required" — исправьте errors и соберите снова
(на диске только <название>_summary_ЧЕРНОВИК.pdf с пометкой «ЧЕРНОВИК»). Если единственная проблема — несколько
лишних строк, скрипт сам убирает последний пункт в пределах канона и сообщает об этом в autofit.
Размеры шрифтов навык не уменьшает: лишнее убирается редактурой текста.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # в папку навыка не пишется ничего, даже __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _core as core  # noqa: E402

# --------------------------------------------------------------------------
# Оформление (повторяет assets/summary_template.html)
# --------------------------------------------------------------------------
INK, INK_SOFT, BRAND, ACCENT = "#15191E", "#4A5560", "#1F4E79", "#2E75B6"
RULE, TINT, TINT_RULE = "#C9D4DF", "#F4F7FA", "#D3DEE8"
MM = 72 / 25.4
A4_W, A4_H = 595.28, 841.89

DENSITY = {
    # base — кегль текста; lh — интервал; h1/h2 — заголовки; mast/foot — шапка и подвал
    "standard": dict(base=12.0, lh=1.5, h1=14.0, h2=14.0, mast=10.0, foot=9.0, idx=21.0, gap=4.6),
    "compact": dict(base=10.0, lh=1.38, h1=13.0, h2=11.5, mast=8.5, foot=8.0, idx=17.0, gap=3.6),
}
# Ориентир объёма: сколько знаков (с пробелами) суммарно помещается в выводы + вопросы.
BUDGET = {
    # Измерено на шаблоне: заголовок в 2 строки, преамбула в 3 строки, подвал в 2–3 строки.
    "standard": dict(total=1800, finding=(140, 240), question=(170, 270), findings=(5, 7), questions=(3, 4)),
    "compact": dict(total=3100, finding=(240, 380), question=(260, 400), findings=(6, 8), questions=(3, 4)),
}

SCHEMA = {
    "masthead_left": "издание/серия · месяц и год (например: McKinsey Global Survey · ноябрь 2025)",
    "masthead_right": "организация-издатель",
    "title": "суть отчёта одной фразой, до 110 знаков, без слов «отчёт о»",
    "lede": "автор(ы), должность, дата, объём, одна фраза о предмете; до 380 знаков. "
            "Слова «Резюме для руководства.» скрипт добавит сам",
    "findings": [{"lead": "Тезис-утверждение до 60 знаков, с точкой, без цифр и двоеточий.",
                  "body": "Тело: факты с числами из документа.", "source_pages": [3, 4]}],
    "questions": [{"q": "Вопрос к руководству?", "body": "Обоснование строго из документа, с числом.",
                   "ask": "Короткий замыкающий вопрос «знаем ли мы…?»", "source_pages": [5]}],
    "source_footer": "полное библиографическое описание источника",
}

# --------------------------------------------------------------------------
# Редакторский канон: проверки
# --------------------------------------------------------------------------
PARASITES = [
    (r"\bдрайвер", "причина / двигатель роста"), (r"\bчеллендж", "сложность / задача"),
    (r"\bимплементац", "внедрение"), (r"ландшафт\w* угроз", "набор угроз / убрать"),
    (r"\bв разрезе\b", "по / в разбивке по"), (r"\bна текущий момент\b", "сейчас"),
    (r"ключевым фактором является", "назвать фактор прямо"), (r"\bв рамках\b", "в / при / убрать"),
    (r"по итогам проведённого анализа", "убрать"), (r"\bстоит отметить\b", "убрать"),
    (r"\bважно понимать\b", "убрать"), (r"\bтаким образом\b", "убрать"),
    (r"\bна регулярной основе\b", "регулярно"), (r"\bвалидир", "проверить / подтвердить"),
    (r"\bявляется\b", "тире или глагол действия"), (r"\bосуществля", "глагол действия"),
    (r"\bданн(ый|ая|ое|ого|ой|ому|ую|ом)\b", "этот"),
]
JARGON = [
    (r"развёртывани|раскатк", "установка; «обновление дошло до всех устройств»"),
    (r"конечн\w+ устройств", "устройства сети / компьютеры сотрудников"),
    (r"\bпаритет", "тот же уровень"), (r"\bинцидент", "взлом / успешная атака / сбой"),
    (r"недофинансирован", "денег почти нет / держится на добровольцах"),
    (r"риск-профил|операционн\w+ риск", "убрать или сказать простыми словами"),
    (r"\bмедианн", "типичный (медианный) — с пояснением"),
    (r"\bкейс", "пример / случай"), (r"\bинсайт", "вывод / наблюдение"),
    (r"\bсофт\w*", "программы"), (r"\bкодинг\w*", "программирование"), (r"\bредизайн\w*", "перестройка"),
]
DESCRIPTIVE = r"в отч[её]те (рассматрива|описыва|анализиру|говорится)|автор\w* (описыва|рассматрива)|речь ид[её]т о|отч[её]т посвящ|в (первой|второй|третьей) части"
MORAL = (r"^это (превращает|указывает|означает|делает|подч[её]ркивает|говорит о|свидетельствует|показывает важность)"
         r"|^это не (вопрос|про|просто)\b|^бизнес опирается|^вс[её] это|^без [^.]{3,80} оста[её]?[тю]тся"
         r"|оста[её]тся декларацией|остаются декларацией")

# Доли и кратности прописью: цифр в них нет, поэтому сверяем по словам источника (англ. и рус.).
SPELLED_RATIOS = [
    (r"в\s+два\s+раза|вдвое|двукратн\w*|удво\w+", r"twice|two times|two-?fold|doubl\w*|\b2x\b|\b2(\.0)? times|в два раза|вдвое|двукратн|удво", "в два раза"),
    (r"в\s+три\s+раза|втрое|тр[её]хкратн\w*|утро\w+", r"three times|three-?fold|tripl\w*|\b3x\b|\b3(\.0)? times|в три раза|втрое|тр[её]хкратн|утро", "в три раза"),
    (r"дв(е|ух|ум) трет\w+", r"two[- ]thirds|две трети|двух третей", "две трети"),
    (r"тр(и|[её]х|[её]м) четверт\w+", r"three[- ]quarters|три четверти|тр[её]х четвертей", "три четверти"),
    (r"(?<!дв[еу] )(?<!двух )(?<!двум )\bтрет(ь|и|ью)\b", r"\bthirds?\b|one[- ]third|\bтрет(ь|и|ью)\b", "треть"),
    (r"(?<!три )(?<!тр[её]х )(?<!тр[её]м )\bчетверт(ь|и|ью)\b", r"quarter|четверт", "четверть"),
    (r"\bполовин\w+", r"\bhalf\b|\bhalve|половин", "половина"),
    (r"\bкажд\w+ (втор|трет|четв[её]рт|пят|десят)\w+", r"(one|1) in (two|three|four|five|ten|2|3|4|5|10)\b|\bhalf\b|third|quarter|fifth|tenth|кажд\w+ (втор|трет|четв|пят|десят)", "каждый N-й"),
    (r"\b(один|одна|двое|два|две|трое|три|четверо|четыре|пять|шесть|семь|восемь|девять) из (тр[её]х|четыр[её]х|пяти|десяти)\b",
     r"\b(one|two|three|four|five|six|seven|eight|nine|\d) (in|out of) (three|four|five|ten|\d+)\b|\bиз (тр[её]х|четыр[её]х|пяти|десяти)\b", "N из M"),
]
ADVICE = r"нужно усилить|следует уделить|необходимо обратить|рекомендуется|важно обеспечить"


def _sentences(text: str) -> list[str]:
    t = re.sub(r"(\b[А-ЯA-Z]\.)\s", r"\1" + core.NBSP, text)  # инициалы не рвут предложение
    t = re.sub(r"\b(г|гг|млн|млрд|трлн|тыс|долл|руб|см|илл|т\.\s?е|т\.\s?д)\.\s(?=[а-яёa-z0-9])", r"\1." + core.NBSP, t)
    parts = re.split(r"(?<=[.!?…])\s+(?=[«\"(]?[А-ЯЁA-Z0-9])", t.strip())
    return [p for p in parts if p.strip()]


def _words(s: str) -> int:
    return len(re.findall(r"[\w%$€₽]+(?:[-–][\w%]+)*", s))


def lint(data: dict, density: str) -> tuple[list[dict], list[dict]]:
    errors, warnings = [], []
    bud = BUDGET[density]

    def err(where, msg, fix=""):
        errors.append({"where": where, "problem": msg, **({"fix": fix} if fix else {})})

    def warn(where, msg, fix=""):
        warnings.append({"where": where, "problem": msg, **({"fix": fix} if fix else {})})

    for key in ("masthead_left", "masthead_right", "title", "lede", "source_footer"):
        if not core.as_text(data.get(key)):
            err(key, "поле пустое")
    findings = data.get("findings") or []
    questions = data.get("questions") or []
    if not isinstance(findings, list) or not isinstance(questions, list):
        err("findings/questions", "должны быть списками")
        return errors, warnings

    lo, hi = bud["findings"]
    if not (lo <= len(findings) <= hi):
        err("findings", "выводов %d, нужно %d–%d при плотности %s" % (len(findings), lo, hi, density))
    lo, hi = bud["questions"]
    if not (lo <= len(questions) <= hi):
        err("questions", "вопросов %d, нужно %d–%d при плотности %s" % (len(questions), lo, hi, density))

    title = core.as_text(data.get("title"))
    if len(title) > 110:
        err("title", "заголовок %d знаков, предел 110" % len(title))
    if re.match(r"(?i)^\s*отч[её]т о", title):
        err("title", "заголовок начинается с «отчёт о» — нужна суть, а не жанр")
    if len(core.as_text(data.get("lede"))) > 380:
        err("lede", "преамбула %d знаков, предел 380" % len(core.as_text(data.get("lede"))))

    def common(where: str, text: str) -> None:
        low = text.lower()
        for rx, fix in PARASITES:
            m = re.search(rx, low)
            if m:
                err(where, "стоп-слово «%s»" % m.group(0), fix)
        for rx, fix in JARGON:
            m = re.search(rx, low)
            if m:
                warn(where, "жаргон «%s»" % m.group(0), fix)
        m = re.search(DESCRIPTIVE, low)
        if m:
            err(where, "пересказ структуры («%s») вместо вывода" % m.group(0), "скажите, что это меняет для нас")
        if "!" in text:
            err(where, "восклицательный знак")
        if re.search(r"[\U0001F300-\U0001FAFF☀-➿]", text):
            err(where, "эмодзи")
        for s in (_sentences(text) if where.startswith(("вывод", "вопрос")) else []):
            n = _words(s)
            if n > 25:
                err(where, "предложение из %d слов (предел 25): «%s…»" % (n, s[:60]), "разбейте на два")
        caps = [w for w in re.findall(r"\b[А-ЯЁ]{4,}\b", text)]
        if len(caps) >= 2 or any(len(w) >= 6 for w in caps):
            err(where, "слова капсом: %s" % ", ".join(caps[:3]), "обычный регистр; капитель в заголовках секций задаёт оформление")
        m = None
        if where.startswith(("вывод", "вопрос")):  # в заголовке и преамбуле допустимо английское название отчёта
            m = re.search(r"(?<![A-Za-z0-9_./-])[a-z]{4,}(?![A-Za-z0-9_./-])(?:\s+[a-z]{3,})*", text)
        if m:
            err(where, "непереведённый английский текст: «%s»" % m.group(0).strip()[:50],
                "переведите на русский; латиницей остаются только названия и аббревиатуры (McKinsey, EBIT)")

    for i, f in enumerate(findings, 1):
        where = "вывод %d" % i
        if not isinstance(f, dict):
            err(where, "пункт должен быть объектом с lead и body")
            continue
        lead, body = core.as_text(f.get("lead")), core.as_text(f.get("body"))
        if not lead or not body:
            err(where, "нужны и lead, и body")
            continue
        if len(lead) > 60:
            err(where + ".lead", "тезис %d знаков, предел 60" % len(lead))
        if not lead.endswith("."):
            err(where + ".lead", "тезис — законченное утверждение с точкой")
        if re.search(r"\d", lead):
            err(where + ".lead", "в тезисе цифры — числа живут только в теле пункта")
        if ":" in lead:
            err(where + ".lead", "в тезисе двоеточие")
        if _words(lead) < 3:
            err(where + ".lead", "тезис — утверждение, а не тема из одного-двух слов")
        if re.match(r"(?i)^(вопрос|ситуация|проблема|тема|роль|влияние|состояние|анализ|обзор|о|об)\s", lead):
            err(where + ".lead", "тезис начинается с существительного-темы («%s…»)" % lead.split()[0],
                "начните с утверждения: «Времени на реакцию почти не осталось.»")
        if not re.search(r"\d", body):
            warn(where + ".body", "в теле нет ни одного числа", "добавьте число из реестра фактов, если оно там есть")
        sents = _sentences(body)
        if sents and re.search(MORAL, sents[-1].strip().lower()):
            err(where + ".body", "пункт заканчивается обобщающей моралью: «%s»" % sents[-1][:70],
                "последнее предложение — факт, причина или следствие с числом")
        total = len(lead) + 1 + len(body)
        lo, hi = bud["finding"]
        if total > hi * 1.25:
            err(where, "пункт %d знаков при ориентире %d–%d" % (total, lo, hi), "сократите: один контраст чисел, без вводных")
        elif total > hi or total < lo * 0.7:
            warn(where, "пункт %d знаков при ориентире %d–%d" % (total, lo, hi))
        common(where, lead + " " + body)

    for i, q in enumerate(questions, 1):
        where = "вопрос %d" % i
        if not isinstance(q, dict):
            err(where, "вопрос должен быть объектом с q, body и ask")
            continue
        qq, body, ask = (core.as_text(q.get(k)) for k in ("q", "body", "ask"))
        if not (qq and body and ask):
            err(where, "нужны q, body и ask")
            continue
        if not qq.endswith("?"):
            err(where + ".q", "вопрос-заголовок должен заканчиваться знаком «?»")
        if not ask.endswith("?"):
            err(where + ".ask", "замыкающий вопрос должен заканчиваться знаком «?»")
        if re.search(ADVICE, (qq + " " + body + " " + ask).lower()):
            err(where, "рекомендация вместо вопроса", "вопрос должен быть проверяемым: срок, ответственный, наличие перечня")
        if not re.search(r"\b(мы|нас|нам|нами|наш\w*)\b", (qq + " " + ask).lower()):
            warn(where, "нет «мы / у нас» — вопрос звучит не как реплика на правлении")
        if not re.search(r"\d", body):
            warn(where + ".body", "в обосновании нет числа из документа")
        for sent in _sentences(body):
            if re.search(MORAL, sent.strip().lower()):
                err(where + ".body", "оценочная фраза, которой нет в документе: «%s»" % sent.strip()[:70],
                    "обоснование вопроса — 1–2 предложения строго из документа, с числом; рассуждения уберите")
        total = len(qq) + len(body) + len(ask) + 2
        lo, hi = bud["question"]
        if total > hi * 1.25:
            err(where, "вопрос %d знаков при ориентире %d–%d" % (total, lo, hi))
        elif total > hi or total < lo * 0.7:
            warn(where, "вопрос %d знаков при ориентире %d–%d" % (total, lo, hi))
        common(where, qq + " " + body + " " + ask)

    common("title", title)
    common("lede", core.as_text(data.get("lede")))
    leads = [core.as_text(f.get("lead")).lower() for f in findings if isinstance(f, dict)]
    if len(set(leads)) < len(leads):
        err("findings", "повторяющиеся тезисы")
    return errors, warnings


def verify_numbers(data: dict, jdir: Path) -> tuple[list[dict], list[dict]]:
    """Каждое число резюме должно существовать в исходном PDF в том же виде."""
    meta = core.read_json(jdir / "meta.json", {})
    total = int(meta.get("pages_extracted", 0))
    page_idx = {n: core.number_index(core.page_text_for_numbers(jdir, n)) for n in range(1, total + 1)}
    doc_idx: set[str] = set()
    for s in page_idx.values():
        doc_idx |= s
    facts = core.read_json(jdir / "facts.json", {"facts": []})
    for f in facts.get("facts", []):
        if f.get("from_image"):
            doc_idx |= core.number_index(" ; ".join(f.get("numbers", [])))
    soft_idx = set(doc_idx) | {str(meta.get("pages_total", ""))}
    errors, warnings = [], []

    def scan(where: str, text: str, pages, strict: bool) -> None:
        missing, minor = core.check_numbers(text, doc_idx if strict else soft_idx)
        if missing:
            (errors if strict else warnings).append(
                {"where": where, "problem": "чисел нет в исходном документе: %s" % ", ".join(missing),
                 "fix": "перенесите число ровно как в источнике; нельзя округлять, складывать и пересчитывать"})
        if minor:
            warnings.append({"where": where, "problem": "одиночные цифры без опоры в тексте: %s" % ", ".join(minor),
                             "fix": "проверьте, что это не пересчёт (например, «в 2 раза» при «doubled» допустимо)"})
        if not isinstance(pages, (list, tuple)):
            pages = [pages] if pages is not None else []
        pages = [int(p) for p in pages if str(p).isdigit()]
        if strict and not pages and re.search(r"\d", text):
            warnings.append({"where": where, "problem": "не указаны source_pages — числа сверены только со всем документом",
                             "fix": "укажите страницы, откуда взяты числа: сверка станет точной"})
        if strict and pages and not missing:
            near: set[str] = set()
            for p in pages:
                for n in (p - 1, p, p + 1):
                    near |= page_idx.get(n, set())
            off, _ = core.check_numbers(text, near)
            if off:
                errors.append({"where": where, "problem": "чисел %s нет на указанных страницах %s (и соседних)" % (", ".join(off), pages),
                               "fix": "исправьте source_pages либо уберите число: оно может быть из другой таблицы или совпадать случайно"})

    page_txt = {n: core.page_text_for_numbers(jdir, n).lower() for n in range(1, total + 1)}
    image_txt = " ; ".join("%s %s" % (f.get("claim", ""), f.get("quote", "")) for f in facts.get("facts", []) if f.get("from_image")).lower()

    def scan_spelled(where: str, text: str, pages) -> None:
        if not isinstance(pages, (list, tuple)):
            pages = [pages] if pages is not None else []
        pages = [int(p) for p in pages if str(p).isdigit()]
        low = text.lower().replace(core.NBSP, " ")
        for ru, src, label in SPELLED_RATIOS:
            m = re.search(ru, low)
            if not m:
                continue
            near = " ".join(page_txt.get(n, "") for p in pages for n in (p - 1, p, p + 1)) if pages else ""
            if pages and re.search(src, near + " " + image_txt):
                if label.startswith("в "):       # кратность: совпадение слова ещё не значит, что сравнение то же
                    warnings.append({"where": where, "problem": "кратность прописью «%s» сверена только по слову в тексте страниц" % m.group(0),
                                     "fix": "убедитесь, что в источнике кратность относится именно к этому показателю; "
                                            "если там две доли («nearly half против 31 percent») — приведите их, а не кратность"})
                continue
            anywhere = [n for n, tx in page_txt.items() if re.search(src, tx)]
            if not pages and anywhere:
                continue
            where_hint = ("в документе такая величина встречается на с. %s — проверьте, что она про тот же показатель, и поправьте source_pages"
                          % ", ".join(map(str, anywhere[:8]))) if anywhere else "в документе такой величины нет"
            errors.append({"where": where, "problem": "«%s» (%s) не подтверждается указанными страницами %s" % (m.group(0), label, pages or "—"),
                           "fix": "%s. Доли и кратности переносятся как в источнике: «nearly half против 31 percent» — это «почти половина против 31%%», "
                                  "а не «в два раза»; не выводите кратность сами." % where_hint})

    t = core.as_text
    for i, f in enumerate(data.get("findings") or [], 1):
        if isinstance(f, dict):
            scan_spelled("вывод %d" % i, "%s %s" % (t(f.get("lead")), t(f.get("body"))), f.get("source_pages"))
    for i, q in enumerate(data.get("questions") or [], 1):
        if isinstance(q, dict):
            scan_spelled("вопрос %d" % i, "%s %s" % (t(q.get("q")), t(q.get("body"))), q.get("source_pages"))
    for i, f in enumerate(data.get("findings") or [], 1):
        if isinstance(f, dict):
            scan("вывод %d" % i, "%s %s" % (t(f.get("lead")), t(f.get("body"))), f.get("source_pages"), True)
    for i, q in enumerate(data.get("questions") or [], 1):
        if isinstance(q, dict):
            scan("вопрос %d" % i, "%s %s %s" % (t(q.get("q")), t(q.get("body")), t(q.get("ask"))), q.get("source_pages"), True)
    # заголовок: без source_pages, сверка со всем документом (предупреждение об этом не нужно)
    missing, _m = core.check_numbers(t(data.get("title")), doc_idx)
    if missing:
        errors.append({"where": "title", "problem": "чисел нет в исходном документе: %s" % ", ".join(missing)})
    scan("lede", t(data.get("lede")), None, False)
    return errors, warnings


# --------------------------------------------------------------------------
# Вёрстка PDF
# --------------------------------------------------------------------------
def _rgb(hex_color: str):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _h(text: str) -> str:
    return core.esc_html(text).replace(core.NBSP, "&#160;")


class Layout:
    """Поток блоков сверху вниз с переносом на новую страницу при переполнении."""

    def __init__(self, pymupdf, density: str):
        self.mu = pymupdf
        self.d = DENSITY[density]
        self.doc = pymupdf.open()
        self.left, self.right = 14 * MM, A4_W - 14 * MM
        self.top, self.bottom = 12 * MM, A4_H - 9 * MM
        self.width = self.right - self.left
        self.items = []  # отложенная отрисовка: (page_index, callable)
        self.page_i = 0
        self.y = self.top
        self.font_b = pymupdf.Font("hebo")
        self.font_r = pymupdf.Font("helv")
        self.limit = self.bottom  # нижняя граница потока; на 1-й странице уменьшается на подвал
        self.flow_height = 0.0   # суммарная высота контента (для оценки переполнения)

    def css(self, size: float, color: str, extra: str = "") -> str:
        return (
            "body{margin:0;padding:0} p{margin:0;padding:0;font-family:sans-serif;font-size:%spt;"
            "line-height:%s;color:%s;text-align:left} b{font-weight:bold} %s" % (size, self.d["lh"], color, extra)
        )

    def measure(self, html: str, css: str, width: float) -> float:
        story = self.mu.Story(html=html, user_css=css)
        _more, filled = story.place(self.mu.Rect(0, 0, width, 50_000))
        return float(filled[3])

    def need(self, h: float) -> None:
        if self.y + h > self.limit + 0.5 and self.y > self.top + 1:
            self.page_i += 1
            self.y = self.top
            self.limit = self.bottom

    def text(self, html: str, css: str, x: float = 0.0, width: float | None = None, after: float = 0.0, tag=None) -> tuple:
        w = (self.width - x) if width is None else width
        h = self.measure(html, css, w)
        self.need(h)
        rect = (self.left + x, self.y, self.left + x + w, self.y + h + 2)
        pi, y0 = self.page_i, self.y
        self.items.append((pi, "html", rect, html, css, tag))
        self.y += h + after
        self.flow_height += h + after
        return pi, y0, y0 + h

    def space(self, h: float) -> None:
        self.y += h
        self.flow_height += h

    def deco(self, kind: str, *payload, page=None) -> None:
        self.items.append((self.page_i if page is None else page, kind, payload))

    def spaced(self, x: float, baseline: float, text: str, bold: bool, size: float, color: str, tracking: float, right: bool = False) -> float:
        font = self.font_b if bold else self.font_r
        widths = [font.text_length(ch, size) + tracking * size for ch in text]
        total = sum(widths) - tracking * size
        x0 = x - total if right else x
        self.deco("spaced", x0, baseline, text, bold, size, color, widths)
        return total

    # ---- отрисовка --------------------------------------------------------
    def paint(self, out_pdf: Path) -> int:
        mu = self.mu
        n_pages = max(i[0] for i in self.items) + 1 if self.items else 1
        for _ in range(n_pages):
            self.doc.new_page(width=A4_W, height=A4_H)
        pages = [self.doc[i] for i in range(n_pages)]  # брать после создания всех: new_page сбрасывает старые ссылки
        order = {"bg": 0, "line": 1, "circle": 1, "spaced": 2, "html": 3}
        for item in sorted(self.items, key=lambda it: order.get(it[1], 9)):
            page = pages[item[0]]
            kind = item[1]
            if kind == "html":
                _pi, _k, rect, html, css, _tag = item
                spare, _scale = page.insert_htmlbox(mu.Rect(*rect), html, css=css, scale_low=1)
                if spare < 0:  # рамка оказалась мала (расхождение измерения и вставки) — даём запас, но не теряем текст
                    page.insert_htmlbox(mu.Rect(rect[0], rect[1], rect[2], rect[3] + 40), html, css=css, scale_low=1)
            else:
                p = item[2]
                if kind == "bg":
                    rect, fill, edge = p
                    page.draw_rect(mu.Rect(*rect), color=None, fill=_rgb(fill))
                    if edge:
                        page.draw_rect(mu.Rect(rect[0], rect[1], rect[0] + 2, rect[3]), color=None, fill=_rgb(edge))
                elif kind == "line":
                    x0, y0, x1, y1, color, width = p
                    page.draw_line((x0, y0), (x1, y1), color=_rgb(color), width=width)
                elif kind == "circle":
                    cx, cy, r, fill = p
                    page.draw_circle((cx, cy), r, color=None, fill=_rgb(fill))
                elif kind == "spaced":
                    x0, baseline, text, bold, size, color, widths = p
                    tw = mu.TextWriter(page.rect)
                    x = x0
                    font = self.font_b if bold else self.font_r
                    for ch, w in zip(text, widths):
                        tw.append((x, baseline), ch, font=font, fontsize=size)
                        x += w
                    tw.write_text(page, color=_rgb(color))
        self.doc.set_metadata({"producer": core.SKILL_NAME, "creator": core.SKILL_NAME})
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.doc.subset_fonts()
        except Exception:
            pass
        self.doc.save(out_pdf, garbage=4, deflate=True)
        return n_pages


def render_pdf(pymupdf, data: dict, density: str, out_pdf: Path) -> dict:
    L = Layout(pymupdf, density)
    d = L.d
    lh = d["lh"]

    # подвал измеряем заранее: на первой странице он прижат к низу
    foot_html = '<p><b style="color:%s">ИСТОЧНИК</b> · %s. Резюме подготовлено исключительно на основе содержания указанного отчёта; внешние данные не привлекались.</p>' % (
        ACCENT, _h(data["source_footer"].rstrip(". ")))
    foot_css = L.css(d["foot"], INK_SOFT)
    foot_h = L.measure(foot_html, foot_css, L.width) + 6 + 1
    L.limit = L.bottom - foot_h - 4

    # шапка
    size = d["mast"]
    base_y = L.y + size
    left_text = data["masthead_left"].upper()
    L.spaced(L.left, base_y, left_text, True, size, BRAND, 0.06)
    L.spaced(L.right, base_y, data["masthead_right"].upper(), False, size, INK_SOFT, 0.06, right=True)
    L.space(size * lh + 3)
    L.deco("line", L.left, L.y, L.right, L.y, BRAND, 1.6)
    L.space(7)

    # заголовок и преамбула
    L.text("<p><b>%s</b></p>" % _h(data["title"]), L.css(d["h1"], BRAND), after=3.5)
    L.text("<p><b>Резюме для руководства.</b> %s</p>" % _h(data["lede"]), L.css(d["base"], INK_SOFT, "b{color:%s}" % INK), after=5.5)
    L.deco("line", L.left, L.y, L.right, L.y, RULE, 0.5)
    content_top = L.y

    def heading(idx: str, title: str, inset: float = 0.0, rule_color: str = RULE) -> None:
        r = d["idx"] / 2
        h = d["idx"] + 1
        L.need(h + d["base"] * lh * 2)
        cy = L.y + r
        x = L.left + inset
        L.deco("circle", x + r, cy, r, ACCENT)
        nw = L.font_b.text_length(idx, d["h2"])
        L.deco("spaced", x + r - nw / 2, cy + d["h2"] * 0.35, idx, True, d["h2"], "#FFFFFF", [nw])
        tx = x + d["idx"] + 6
        tw = L.spaced(tx, cy + d["h2"] * 0.35, title.upper(), True, d["h2"], ACCENT, 0.09)
        L.deco("line", tx + tw + 6, cy, L.right - inset, cy, rule_color, 0.5)
        L.space(h + 4.5)

    # 1. ключевые выводы
    L.space(7.5)
    heading("1", "Ключевые выводы")
    body_css = L.css(d["base"], INK, "b{color:%s}" % BRAND)
    findings = data["findings"]
    for i, f in enumerate(findings):
        html = "<p><b>%s</b> %s</p>" % (_h(f["lead"]), _h(f["body"]))
        pi, y0, _y1 = L.text(html, body_css, x=9, after=(d["gap"] if i < len(findings) - 1 else 0), tag="вывод %d" % (i + 1))
        L.deco("circle", L.left + 1.5, y0 + d["base"] * lh / 2, 1.5, ACCENT, page=pi)

    # 2. вопросы для обсуждения (плашка с фоном)
    L.space(8.5)
    pad_x, pad_top, pad_bot = 8.0, 6.0, 5.0
    seg_start = {}  # страница -> (y0, y1) сегмента плашки

    def mark(pi: int, y0: float, y1: float) -> None:
        a, b = seg_start.get(pi, (y0, y1))
        seg_start[pi] = (min(a, y0), max(b, y1))

    L.need(d["idx"] + d["base"] * lh * 3)
    block_top = L.y
    L.space(pad_top)
    pi0 = L.page_i
    heading("2", "Вопросы для обсуждения", inset=pad_x, rule_color=TINT_RULE)
    mark(pi0, block_top, L.y)
    q_css = L.css(d["base"], INK, "p.q{color:%s;font-weight:bold} b{color:%s}" % (BRAND, INK))
    num_css = L.css(d["base"], ACCENT)
    questions = data["questions"]
    for i, q in enumerate(questions, 1):
        html = '<p class="q">%s</p><p>%s <b>%s</b></p>' % (_h(q["q"]), _h(q["body"]), _h(q["ask"]))
        w = L.width - 2 * pad_x - 17
        pi, y0, y1 = L.text(html, q_css, x=pad_x + 17, width=w, after=(5 if i < len(questions) else 0), tag="вопрос %d" % i)
        L.items.append((pi, "html", (L.left + pad_x, y0, L.left + pad_x + 17, y0 + d["base"] * lh + 2),
                        "<p><b>%02d</b></p>" % i, num_css, None))
        mark(pi, y0 - (pad_top if pi != pi0 and y0 <= L.top + 1 else 0), y1)
    L.space(pad_bot)
    for pi, (a, b) in seg_start.items():
        L.deco("bg", (L.left, a, L.right, b + pad_bot), TINT, ACCENT, page=pi)

    # подвал: внизу последней страницы
    if L.y + foot_h > L.bottom + 0.5:
        L.page_i += 1
        L.y = L.top
    fy = L.bottom - foot_h + 6
    L.deco("line", L.left, fy - 6, L.right, fy - 6, RULE, 0.5)
    L.items.append((L.page_i, "html", (L.left, fy, L.right, L.bottom + 2), foot_html, foot_css, None))

    n_pages = L.paint(out_pdf)

    line = d["base"] * lh
    available = (A4_H - 9 * MM - foot_h - 4) - content_top
    content_h = L.flow_height - (content_top - L.top)  # чистая высота контента без разрывов страниц
    body_chars = sum(len(f["lead"]) + len(f["body"]) for f in findings) + sum(
        len(q["q"]) + len(q["body"]) + len(q["ask"]) for q in questions)
    fit = {"pages": n_pages, "density": density, "body_chars": body_chars}
    if n_pages == 1:
        fit["fill"] = round(content_h / available, 3)
        fit["free_lines"] = max(0, int((available - content_h) // line))
    # Где дешевле всего освободить строку: пункты с самой короткой последней строкой.
    tails = []
    done = pymupdf.open(out_pdf)
    for it in L.items:
        if it[1] == "html" and it[5]:
            rows = {}
            for b in done[it[0]].get_text("dict", clip=pymupdf.Rect(*it[2]))["blocks"]:
                for ln in b.get("lines", []):
                    key = round(ln["bbox"][3] / 3)
                    rows[key] = rows.get(key, "") + "".join(sp["text"] for sp in ln["spans"])
            if rows:
                last = rows[max(rows)].strip()
                tails.append({"where": it[5], "lines": len(rows), "last_line_chars": len(last)})
    done.close()
    fit["items"] = [dict(t) for t in tails]  # все пункты по порядку: сколько строк занимает каждый
    if n_pages > 1:
        over = max(line, content_h - available)
        fit["overflow_lines"] = int(over // line) + 1
        fit["cheapest_cuts"] = sorted(tails, key=lambda t: t["last_line_chars"])[:4]
    return fit


# --------------------------------------------------------------------------
# HTML-версия по шаблону из assets/
# --------------------------------------------------------------------------
def render_html(data: dict, density: str, out_html: Path) -> None:
    tpl_path = Path(__file__).resolve().parent.parent / "assets" / "summary_template.html"
    if not tpl_path.is_file():
        core.fail("Не найден шаблон assets/summary_template.html.", "Проверьте, что папка assets/ загружена вместе с навыком.")
    tpl = tpl_path.read_text(encoding="utf-8")
    e = lambda s: core.esc_html(s).replace(core.NBSP, "&nbsp;")  # noqa: E731
    findings = "\n".join("      <li><b>%s</b> %s</li>" % (e(f["lead"]), e(f["body"])) for f in data["findings"])
    questions = "\n".join(
        '      <li>\n        <span class="q">%s</span>\n        %s\n        <span class="ask">%s</span>\n      </li>'
        % (e(q["q"]), e(q["body"]), e(q["ask"])) for q in data["questions"])
    dens_css = ""
    if density == "compact":
        dens_css = ("<style>body,.page,.lede,.findings li,.qa li,.qa li::before{font-size:10pt;line-height:1.38}"
                    "h1{font-size:13pt}h2,h2 .idx{font-size:11.5pt}h2 .idx{width:17pt;height:17pt}"
                    ".masthead{font-size:8.5pt}footer{font-size:8pt}</style>")
    repl = {
        "{{TITLE}}": e(data["title"]), "{{MASTHEAD_LEFT}}": e(data["masthead_left"]),
        "{{MASTHEAD_RIGHT}}": e(data["masthead_right"]), "{{LEDE}}": e(data["lede"]),
        "{{FINDINGS}}": findings, "{{QUESTIONS}}": questions,
        "{{SOURCE_FOOTER}}": e(data["source_footer"].rstrip(". ")), "{{DENSITY_CSS}}": dens_css,
    }
    # один проход: значение поля, похожее на «{{FINDINGS}}», не развернётся повторно
    tpl = re.sub(r"\{\{[A-Z_]+\}\}", lambda m: repl.get(m.group(0), m.group(0)), tpl)
    out_html.write_text(tpl, encoding="utf-8")


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------
def apply_typography(data: dict) -> dict:
    def t(value) -> str:
        text = core.typo_ru(core.as_text(value))
        # дефис внутри слова — неразрывный: «из-за» и «ИИ-агентов» не рвутся по строкам
        return re.sub(r"(?<=[^\W\d_])-(?=[^\W\d_])", "\u2011", text)

    res = dict(data)
    for k in ("masthead_left", "masthead_right", "title", "lede", "source_footer"):
        res[k] = t(data.get(k))
    res["lede"] = re.sub(r"^\s*Резюме для руководства\.?\s*", "", res["lede"])
    res["findings"] = [dict(f, lead=t(f.get("lead")), body=t(f.get("body"))) for f in (data.get("findings") or []) if isinstance(f, dict)]
    res["questions"] = [dict(q, q=t(q.get("q")), body=t(q.get("body")), ask=t(q.get("ask"))) for q in (data.get("questions") or []) if isinstance(q, dict)]
    return res


def cmd_schema(args) -> None:
    dens = args.density
    core.out({"ok": True, "summary_json": SCHEMA, "density": dens, "budget_chars": BUDGET[dens],
              "numbering": "В ответах build пункты называются «вывод 1», «вопрос 2» — нумерация с единицы, в порядке следования в JSON.",
              "note": "Бюджет — ориентир: точный ответ «влезло или нет» даёт только build. "
                      "Плотность standard — требование к формату (Arial 12 pt, интервал 1,5); compact — только по просьбе пользователя."})


def cmd_build(args) -> None:
    pymupdf = core.require_pymupdf()
    jdir = core.job_dir(args.job)
    data = core.load_payload(args, "summary.json")
    if not isinstance(data, dict):
        core.fail("summary.json должен быть объектом.", "Формат: summary.py schema")
    density = args.density
    asked = core.as_text(getattr(args, "user_request", None))
    if density == "compact" and len(re.sub(r"\W+", "", asked)) < 8:
        core.fail("Плотность compact (10 pt) включается только по прямой просьбе пользователя.",
                  "Оформление резюме фиксировано: 12 pt, интервал 1,5 — собирайте без --density и сокращайте текст до ~1 800 знаков "
                  "(summary.py schema). Если пользователь сам попросил мелкий шрифт или больше текста — повторите вызов с "
                  "--user-request '<его слова дословно>'.")
    errors, warnings = lint(data, density)
    structural = [e for e in errors if e["problem"] in ("поле пустое",) or "должн" in e["problem"] or "нужны" in e["problem"]]
    if structural and not (data.get("findings") and data.get("questions")):
        core.out({"ok": False, "error": "Структура summary.json неполная.", "verdict": "fix_required", "errors": errors,
                  "hint": "Формат: summary.py schema"}, code=2)
    n_err, n_warn = verify_numbers(data, jdir)
    errors += n_err
    warnings += n_warn

    facts = core.read_json(jdir / "facts.json", {"facts": [], "skipped_pages": []})
    meta = core.read_json(jdir / "meta.json", {})
    seen = {f["page"] for f in facts.get("facts", [])} | set(facts.get("skipped_pages", []))
    unread = [n for n in range(1, int(meta.get("pages_total", 0)) + 1) if n not in seen]
    if unread:
        warnings.append({"where": "reestr", "problem": "страницы не прочитаны: %s" % ", ".join(map(str, unread[:30])),
                         "fix": "дочитайте их (pdf.py pages) и сдайте факты либо отметьте facts.py skip"})

    clean = apply_typography(data)
    for k in ("masthead_left", "masthead_right", "title", "lede", "source_footer"):
        clean[k] = clean.get(k) or "—"
    out_dir = jdir / "out"
    out_dir.mkdir(exist_ok=True)
    tmp_pdf = out_dir / "build.tmp.pdf"
    fit = render_pdf(pymupdf, clean, density, tmp_pdf)

    # Страховка: если кроме переполнения ошибок нет и лишних строк немного, убираем последние пункты
    # (сначала четвёртый вопрос, затем последний вывод) — только целиком и только в пределах канона.
    autofit = []
    bud = BUDGET[density]
    while fit["pages"] > 1 and not errors and fit.get("overflow_lines", 99) <= 6 and len(autofit) < 2:
        if len(clean["questions"]) > bud["questions"][0]:
            autofit.append("вопрос %d: %s" % (len(clean["questions"]), clean["questions"][-1]["q"]))
            clean["questions"] = clean["questions"][:-1]
        elif len(clean["findings"]) > bud["findings"][0]:
            autofit.append("вывод %d: %s" % (len(clean["findings"]), clean["findings"][-1]["lead"]))
            clean["findings"] = clean["findings"][:-1]
        else:
            break
        fit = render_pdf(pymupdf, clean, density, tmp_pdf)

    if fit["pages"] > 1:
        errors.append({"where": "page", "problem": "резюме заняло %d стр.; лишних строк: %d" % (fit["pages"], fit["overflow_lines"]),
                       "fix": "нужно освободить строк: %d. Если лишних строк 1–3 — возьмите пункты из fit.cheapest_cuts: уберите в каждом чуть больше "
                              "знаков, чем в его последней строке (last_line_chars), и строка исчезнет. Если лишних строк больше — уберите самый слабый "
                              "вывод целиком (сколько строк занимает каждый пункт — в fit.items; между пунктами ещё около трети строки). "
                              "Шрифт не уменьшается." % fit["overflow_lines"]})
    elif fit.get("fill", 1) < 0.85:
        warnings.append({"where": "page", "problem": "лист заполнен на %d%%, свободно строк: %d" % (round(fit["fill"] * 100), fit["free_lines"]),
                         "fix": "добавьте недостающий вывод из реестра или причину «почему так» в короткие пункты — не «воду»"})
    if autofit and fit["pages"] == 1:
        warnings.append({"where": "page", "problem": "чтобы уместиться в лист, скрипт убрал: " + "; ".join(autofit),
                         "fix": "если убранный пункт важнее оставшихся — верните его и сократите другие; иначе ничего делать не нужно"})

    verdict = "ready" if not errors else "fix_required"
    names = core.output_paths(jdir)
    final = {"pdf": names["summary_pdf"], "html": names["summary_html"], "json": names["summary_json"]}
    draft = {"pdf": names["draft_pdf"], "html": names["draft_html"], "json": names["draft_json"]}
    target = final if verdict == "ready" else draft
    for legacy in ("summary_ru.pdf", "summary_ru.html", "draft_summary_ru.pdf", "draft_summary_ru.html"):
        if (out_dir / legacy).is_file():
            (out_dir / legacy).unlink()     # имена прежних версий: не оставляем старый файл рядом с новым
    if verdict == "ready":
        import os
        os.replace(tmp_pdf, target["pdf"])
        for path in draft.values():           # готовый результат вытесняет черновики
            if path.is_file():
                path.unlink()
    else:
        _stamp_draft(pymupdf, tmp_pdf, target["pdf"])
        tmp_pdf.unlink()
    render_html(clean, density, target["html"])
    core.write_json(target["json"], clean)

    resp = {"ok": True, "verdict": verdict, "fit": fit, "errors": errors, "warnings": warnings[:25],
            "outputs": {k: str(v) for k, v in target.items()}, "file_name": target["pdf"].name}
    if autofit:
        resp["autofit"] = {"dropped": autofit}
    if density == "compact":
        resp["density_note"] = ("Собрано уплотнённо (10 pt) по просьбе пользователя: «%s». Скажите ему об этом одной строкой." % asked[:200])
    if verdict == "ready":
        resp["next"] = ("Готово: сразу отправьте пользователю файл outputs.pdf инструментом отправки файлов (в Ouroboros — send_file) "
                        "под его собственным именем file_name — не переименовывайте; HTML — если просил. "
                        "Одной строкой перечислите warnings и то, что убрал autofit, если это было.")
    else:
        resp["deliverable"] = False
        if final["pdf"].is_file():
            resp["last_ready_pdf"] = str(final["pdf"])
        resp["next"] = ("Исправьте errors в summary.json и снова вызовите build. Файлы в outputs — ЧЕРНОВИК с пометкой поперёк листа: "
                        "пользователю его не отдавайте и не называйте готовым резюме.")
    core.out(resp)


def _stamp_draft(pymupdf, src: Path, dst: Path) -> None:
    """Пометка «ЧЕРНОВИК» поперёк каждой страницы: черновик нельзя принять за результат."""
    doc = pymupdf.open(src)
    font = pymupdf.Font("hebo")
    for page in doc:
        tw = pymupdf.TextWriter(page.rect, opacity=0.18, color=(0.8, 0.1, 0.1))
        text = "ЧЕРНОВИК"
        size = 92
        width = font.text_length(text, size)
        pivot = pymupdf.Point(page.rect.width / 2, page.rect.height / 2)
        tw.append((pivot.x - width / 2, pivot.y + size / 3), text, font=font, fontsize=size)
        tw.write_text(page, morph=(pivot, pymupdf.Matrix(35)))
    doc.save(dst, garbage=4, deflate=True)
    doc.close()


def main() -> None:
    core.setup_stdio()
    ap = core.JsonArgumentParser(prog="summary.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("schema"); p.add_argument("--density", choices=list(DENSITY), default="standard"); p.set_defaults(fn=cmd_schema)
    p = sub.add_parser("build"); p.add_argument("job"); p.add_argument("--json"); p.add_argument("--file")
    p.add_argument("--density", choices=list(DENSITY), default="standard")
    p.add_argument("--user-request", dest="user_request"); p.set_defaults(fn=cmd_build)
    args = ap.parse_args()
    if not getattr(args, "fn", None):
        core.fail("Не указана команда.", "Доступно: schema, build.")
    args.fn(args)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        core.fail("Внутренняя ошибка summary.py: %s: %s" % (type(e).__name__, e))
