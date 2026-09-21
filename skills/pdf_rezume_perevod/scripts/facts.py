# -*- coding: utf-8 -*-
"""facts.py — реестр фактов документа (память агента вне контекста).

Зачем: агент читает документ порциями и не должен тащить все страницы в контексте.
Каждый факт сразу сдаётся в реестр; скрипт тут же сверяет числа факта с текстом
страницы и возвращает отказ, если числа на странице нет. Так «выдуманные» и
пересчитанные цифры отсекаются до того, как попадут в резюме.

Команды:
  add   <job> --json '[{...}, ...]' | --file <путь>   добавить факты
  skip  <job> --pages 1,2,15                           отметить страницы без фактов (обложка, оглавление)
  list  <job> [--relevance high,medium] [--offset N]   компактный реестр для сборки резюме
  stats <job>                                          покрытие страниц и состав реестра
  drop  <job> --ids f003,f010                          удалить факты

Поля факта: page (обязательно), kind, topic, claim (по-русски, одно предложение),
numbers (список строк — ровно как в источнике), attribution, quote (исходная
формулировка до 200 знаков), relevance (high|medium|low), why, from_image (true,
если число прочитано с графика глазами и его нет в текстовом слое).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # в папку навыка не пишется ничего, даже __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _core as core  # noqa: E402

KINDS = {"fact", "trend", "forecast", "estimate", "opinion", "recommendation", "term"}
RELEVANCE = {"high", "medium", "low"}
LIST_CAP = 60_000


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", " ", (s or "").lower()).strip()


def _load(jdir: Path) -> dict:
    return core.read_json(jdir / "facts.json", {"seq": 0, "facts": [], "skipped_pages": []})


def cmd_add(args) -> None:
    jdir = core.job_dir(args.job)
    meta = core.read_json(jdir / "meta.json", {})
    total = int(meta.get("pages_extracted", 0))
    data = core.load_payload(args, "список фактов")
    if isinstance(data, dict):
        # допускаем формат {"3": {...}, "4": [{...}]} — ключ как номер страницы
        if all(re.fullmatch(r"\d+", str(k)) for k in data.keys()):
            flat = []
            for k, v in data.items():
                for item in v if isinstance(v, list) else [v]:
                    if isinstance(item, dict):
                        item.setdefault("page", int(k))
                        flat.append(item)
            data = flat
        else:
            data = [data]
    if not isinstance(data, list):
        core.fail("Ожидался JSON-массив фактов.")

    store = _load(jdir)
    known = {(f["page"], _norm(f["claim"])) for f in store["facts"]}
    accepted, rejected = [], []
    for raw in data:
        if not isinstance(raw, dict):
            rejected.append({"fact": str(raw)[:80], "reason": "факт должен быть объектом"})
            continue
        claim = core.as_text(raw.get("claim"))
        try:
            if isinstance(raw.get("page"), bool):
                raise ValueError
            page = int(raw.get("page"))
        except Exception:
            rejected.append({"claim": claim[:80], "reason": "нет номера страницы page"})
            continue
        if not (1 <= page <= total):
            rejected.append({"claim": claim[:80], "reason": "страницы %s нет в документе" % page})
            continue
        if len(claim) < 8:
            rejected.append({"page": page, "reason": "пустое утверждение claim"})
            continue
        if (page, _norm(claim)) in known:
            continue
        numbers = raw.get("numbers") or []
        if not isinstance(numbers, (list, tuple)):
            numbers = [numbers]
        numbers = [core.as_text(x) for x in numbers if core.as_text(x)]
        from_image = bool(raw.get("from_image"))

        # Сверка чисел: и из numbers, и из самого claim — с текстом ЭТОЙ страницы
        # (плюс соседние: абзац может переходить через границу страницы).
        ctx = " ".join(core.page_text_for_numbers(jdir, n) for n in (page - 1, page, page + 1) if 1 <= n <= total)
        idx = core.number_index(ctx)
        # числа, которые агент сам объявил в numbers, сверяются строго — без поблажки одиночным цифрам
        declared_missing, declared_minor = core.check_numbers(" ; ".join(numbers), idx)
        missing, _minor = core.check_numbers(claim, idx)
        missing = list(dict.fromkeys(declared_missing + declared_minor + missing))
        if missing and not from_image:
            rejected.append(
                {
                    "page": page,
                    "claim": claim[:120],
                    "reason": "чисел нет в тексте страницы: %s" % ", ".join(missing),
                    "fix": "Перепишите число ровно как в источнике (без округления и пересчёта). "
                           "Если число прочитано с графика глазами — добавьте \"from_image\": true.",
                }
            )
            continue

        quote = (core.as_text(raw.get("quote")) or core.as_text(raw.get("quote_en")))[:240]
        quote_found = None
        if quote:
            q = _norm(quote)
            quote_found = bool(q) and q[:60] in _norm(ctx)

        kind = core.as_text(raw.get("kind")).lower() or "fact"
        relevance = (core.as_text(raw.get("relevance")) or core.as_text(raw.get("business_relevance")) or "medium").lower()
        store["seq"] += 1
        fact = {
            "id": "f%03d" % store["seq"],
            "page": page,
            "kind": kind if kind in KINDS else "fact",
            "topic": core.as_text(raw.get("topic"))[:60],
            "claim": claim[:600],
            "numbers": numbers[:12],
            "attribution": (core.as_text(raw.get("attribution")) or None),
            "quote": quote,
            "relevance": relevance if relevance in RELEVANCE else "medium",
            "why": core.as_text(raw.get("why"))[:160],
        }
        if from_image:
            fact["from_image"] = True
        if quote_found is False:
            fact["quote_not_found"] = True
        store["facts"].append(fact)
        known.add((page, _norm(claim)))
        accepted.append(fact["id"])

    core.write_json(jdir / "facts.json", store)
    resp = {
        "ok": True,
        "accepted": len(accepted),
        "accepted_ids": accepted,
        "rejected": rejected,
        "facts_total": len(store["facts"]),
    }
    if rejected:
        resp["note"] = "Отклонённые факты (rejected) в реестр НЕ попали: исправьте и сдайте повторно."
    resp["next"] = ("Читайте следующую порцию страниц (pdf.py pages) и сдавайте факты; когда документ прочитан — "
                    "facts.py stats %s, затем facts.py list %s." % (args.job, args.job))
    core.out(resp)


def cmd_skip(args) -> None:
    jdir = core.job_dir(args.job)
    meta = core.read_json(jdir / "meta.json", {})
    pages = core.parse_pages(args.pages, int(meta.get("pages_extracted", 0)))
    store = _load(jdir)
    store["skipped_pages"] = sorted(set(store.get("skipped_pages", [])) | set(pages))
    core.write_json(jdir / "facts.json", store)
    core.out({"ok": True, "skipped_pages": store["skipped_pages"],
              "next": "Проверьте покрытие: facts.py stats %s" % args.job})


def _coverage(jdir: Path, store: dict) -> dict:
    meta = core.read_json(jdir / "meta.json", {})
    total = int(meta.get("pages_total", 0))
    seen = {f["page"] for f in store["facts"]} | set(store.get("skipped_pages", []))
    unread = [n for n in range(1, total + 1) if n not in seen]
    return {"pages_total": total, "pages_reviewed": total - len(unread), "pages_not_reviewed": unread[:80]}


def cmd_stats(args) -> None:
    jdir = core.job_dir(args.job)
    store = _load(jdir)
    by_kind, by_rel = {}, {}
    for f in store["facts"]:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        by_rel[f["relevance"]] = by_rel.get(f["relevance"], 0) + 1
    cov = _coverage(jdir, store)
    resp = {"ok": True, "facts_total": len(store["facts"]), "by_kind": by_kind, "by_relevance": by_rel}
    resp.update(cov)
    if cov["pages_not_reviewed"]:
        resp["next"] = "Есть непрочитанные страницы: дочитайте их или отметьте facts.py skip."
    else:
        resp["next"] = "Все страницы просмотрены: facts.py list %s, затем summary.py schema и summary.py build." % args.job
    core.out(resp)


def cmd_list(args) -> None:
    jdir = core.job_dir(args.job)
    store = _load(jdir)
    allow = {x.strip() for x in (args.relevance or "high,medium,low").split(",")}
    rows, used, shown = [], 0, 0
    facts = [f for f in store["facts"] if f["relevance"] in allow]
    offset = max(0, args.offset or 0)
    for f in facts[offset:]:
        line = "%s | с.%d | %s | %s | %s | %s" % (
            f["id"], f["page"], f["kind"], f["relevance"], f["topic"], f["claim"],
        )
        if f["numbers"]:
            line += " | числа: " + "; ".join(f["numbers"])
        if f.get("attribution"):
            line += " | источник оценки: " + f["attribution"]
        if f.get("from_image"):
            line += " | [с графика, в тексте нет]"
        if used + len(line) > LIST_CAP:
            break
        rows.append(line)
        used += len(line)
        shown += 1
    resp = {"ok": True, "format": "id | страница | вид | важность | тема | утверждение | числа", "facts": rows,
            "shown": shown, "matched": len(facts)}
    if offset + shown < len(facts):
        resp["next"] = "Продолжение: facts.py list %s --offset %d" % (args.job, offset + shown)
    resp.update(_coverage(jdir, store))
    core.out(resp)


def cmd_drop(args) -> None:
    jdir = core.job_dir(args.job)
    store = _load(jdir)
    ids = {x.strip() for x in args.ids.split(",") if x.strip()}
    before = len(store["facts"])
    store["facts"] = [f for f in store["facts"] if f["id"] not in ids]
    core.write_json(jdir / "facts.json", store)
    core.out({"ok": True, "removed": before - len(store["facts"]), "facts_total": len(store["facts"])})


def main() -> None:
    core.setup_stdio()
    ap = core.JsonArgumentParser(prog="facts.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("add"); p.add_argument("job"); p.add_argument("--json"); p.add_argument("--file"); p.set_defaults(fn=cmd_add)
    p = sub.add_parser("skip"); p.add_argument("job"); p.add_argument("--pages", required=True); p.set_defaults(fn=cmd_skip)
    p = sub.add_parser("list"); p.add_argument("job"); p.add_argument("--relevance"); p.add_argument("--offset", type=int); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("stats"); p.add_argument("job"); p.set_defaults(fn=cmd_stats)
    p = sub.add_parser("drop"); p.add_argument("job"); p.add_argument("--ids", required=True); p.set_defaults(fn=cmd_drop)
    args = ap.parse_args()
    if not getattr(args, "fn", None):
        core.fail("Не указана команда.", "Доступно: add, skip, list, stats, drop.")
    args.fn(args)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        core.fail("Внутренняя ошибка facts.py: %s: %s" % (type(e).__name__, e))
