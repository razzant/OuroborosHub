# -*- coding: utf-8 -*-
"""pdf.py — приём PDF и чтение страниц.

Команды:
  open   <путь к PDF>             создать (или продолжить) задание, извлечь текст страниц
  pages  <job> [--pages 1-8]      напечатать текст страниц (порциями, с ограничением объёма)
  render <job> --pages 3 [--translated] [--dpi 110]
                                  отрисовать страницы в PNG (для просмотра графиков и проверки перевода)
  jobs                            список заданий в папке состояния

Входной PDF только читается и копируется в папку задания; всё остальное пишется
внутрь OUROBOROS_SKILL_STATE_DIR/jobs/<job>/.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # в папку навыка не пишется ничего, даже __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _core as core  # noqa: E402

MAX_PDF_MB = 300
PAGES_OUT_CAP = 40_000  # знаков текста на один вызов pages


def cmd_open(args) -> None:
    pymupdf = core.require_pymupdf()
    src = Path(args.pdf).expanduser()
    if not src.is_file():
        core.fail("Файл не найден: %s" % src, "Передайте полный путь к PDF-файлу.")
    size_mb = src.stat().st_size / 1e6
    if size_mb > MAX_PDF_MB:
        core.fail("Файл %.0f МБ больше лимита %d МБ." % (size_mb, MAX_PDF_MB))
    with open(src, "rb") as fh:
        if b"%PDF" not in fh.read(1024):
            core.fail("Это не PDF-файл (нет сигнатуры %PDF).", "Навык работает только с PDF.")

    # Сначала проверяем исходный файл (только чтение), и лишь потом копируем его в задание.
    try:
        probe = pymupdf.open(src)
    except Exception as e:
        core.fail("PDF не открывается: %s" % e, "Файл повреждён или это не PDF.")
    if probe.needs_pass:
        core.fail("PDF защищён паролем.", "Попросите у пользователя версию без пароля.")
    if probe.page_count == 0:
        core.fail("В PDF нет страниц.")
    probe.close()

    sha = core.file_sha(src)
    job = "%s-%s" % (core.slugify(src.stem), sha[:8])
    jdir = core.job_dir(job, must_exist=False)
    (jdir / "pages").mkdir(parents=True, exist_ok=True)
    local = jdir / "source.pdf"
    if not local.is_file() or local.stat().st_size != src.stat().st_size:
        part = jdir / "source.pdf.part"   # копия через временное имя: обрыв не оставит усечённый файл
        shutil.copyfile(src, part)
        os.replace(part, local)
    doc = pymupdf.open(local)

    deadline = core.Deadline()
    meta = core.read_json(jdir / "meta.json", {}) or {}
    done = int(meta.get("pages_extracted", 0))
    total = doc.page_count
    page_info = meta.get("page_info", [])
    for n in range(done + 1, total + 1):
        page = doc[n - 1]
        text = page.get_text("text", sort=True)
        (jdir / "pages" / ("%04d.txt" % n)).write_text(text, encoding="utf-8")
        letters = sum(ch.isalpha() for ch in text)
        page_info.append(
            {
                "n": n,
                "chars": len(text),
                "letters": letters,
                "images": len(page.get_images()),
                "w": round(page.rect.width, 1),
                "h": round(page.rect.height, 1),
            }
        )
        done = n
        if deadline.expired():
            break

    textless = [p["n"] for p in page_info if p["letters"] < 25]
    md = doc.metadata or {}
    meta.update(
        {
            "job": job,
            "source_name": src.name,
            "sha256": sha,
            "pages_total": total,
            "pages_extracted": done,
            "page_info": page_info,
            "pdf_title": (md.get("title") or "").strip(),
            "pdf_author": (md.get("author") or "").strip(),
            "pdf_created": (md.get("creationDate") or "").strip(),
            "pages_without_text": textless,
        }
    )
    core.write_json(jdir / "meta.json", meta)

    complete = done >= total
    total_chars = sum(p["chars"] for p in page_info)
    resp = {
        "ok": True,
        "job": job,
        "complete": complete,
        "pages_total": total,
        "pages_extracted": done,
        "text_chars": total_chars,
        "pdf_title": meta["pdf_title"],
        "pdf_author": meta["pdf_author"],
        "pages_without_text": textless[:60],
        "job_dir": str(jdir),
        "elapsed_sec": deadline.elapsed(),
    }
    if not complete:
        resp["next"] = "Документ большой: повторите тот же вызов pdf.py open — извлечение продолжится со страницы %d." % (done + 1)
    elif len(textless) > total * 0.5:
        resp["warnings"] = [
            "В PDF почти нет текстового слоя (скан). Текст этим навыком не извлечь: "
            "посмотрите страницы через pdf.py render и своим зрением, либо попросите у пользователя PDF с текстовым слоем."
        ]
    else:
        resp["next"] = "Читайте страницы порциями: pdf.py pages %s --pages 1-8" % job
    core.out(resp)


def _compact(text: str) -> str:
    """Убирает поля пробелов, оставшиеся от вёрстки PDF: агенту они стоят токенов и ничего не дают."""
    lines = [re.sub(r"[ \t\u00a0]{2,}", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def cmd_pages(args) -> None:
    jdir = core.job_dir(args.job)
    meta = core.read_json(jdir / "meta.json", {})
    total = int(meta.get("pages_extracted", 0))
    wanted = core.parse_pages(args.pages, total)
    pages, used, last = [], 0, None
    for n in wanted:
        text = _compact(core.page_text(jdir, n))
        if pages and used + len(text) > PAGES_OUT_CAP:
            break
        if len(text) > PAGES_OUT_CAP:
            text = text[:PAGES_OUT_CAP] + "\n[…страница обрезана по лимиту вывода…]"
        pages.append({"page": n, "text": text})
        used += len(text)
        last = n
    rest = [n for n in wanted if last is not None and n > last]
    resp = {"ok": True, "job": args.job, "pages_total": meta.get("pages_total"), "pages": pages}
    if rest:
        resp["next_pages"] = "%d-%d" % (rest[0], rest[-1])
        resp["note"] = "Показано до страницы %d из запрошенных; остальное — следующим вызовом." % last
    core.out(resp)


def cmd_render(args) -> None:
    pymupdf = core.require_pymupdf()
    jdir = core.job_dir(args.job)
    meta = core.read_json(jdir / "meta.json", {})
    pdf_path = jdir / "source.pdf"
    if args.translated:
        pdf_path = core.output_paths(jdir)["translation_pdf"]
        if not pdf_path.is_file():
            core.fail("Перевод ещё не собран.", "Сначала translate.py build %s" % args.job)
    doc = pymupdf.open(pdf_path)
    wanted = core.parse_pages(args.pages, doc.page_count)[:12]
    dpi = max(50, min(int(args.dpi), 200))
    folder = jdir / ("png_ru" if args.translated else "png")
    folder.mkdir(exist_ok=True)
    files = []
    for n in wanted:
        target = folder / ("%04d.png" % n)
        doc[n - 1].get_pixmap(dpi=dpi).save(target)
        files.append({"page": n, "png": str(target)})
    core.out(
        {
            "ok": True,
            "job": args.job,
            "files": files,
            "note": "Откройте PNG своим инструментом просмотра изображений. За один вызов — не больше 12 страниц.",
            "pages_total": meta.get("pages_total"),
        }
    )


def cmd_jobs(_args) -> None:
    rows = []
    for d in sorted(core.jobs_root().iterdir()):
        meta = core.read_json(d / "meta.json")
        if meta:
            rows.append(
                {
                    "job": meta.get("job"),
                    "source": meta.get("source_name"),
                    "pages": meta.get("pages_total"),
                    "summary_ready": core.output_paths(d)["summary_pdf"].is_file(),
                    "translation_ready": core.output_paths(d)["translation_pdf"].is_file(),
                }
            )
    core.out({"ok": True, "jobs": rows})


def main() -> None:
    core.setup_stdio()
    ap = core.JsonArgumentParser(prog="pdf.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("open"); p.add_argument("pdf"); p.set_defaults(fn=cmd_open)
    p = sub.add_parser("pages"); p.add_argument("job"); p.add_argument("--pages"); p.set_defaults(fn=cmd_pages)
    p = sub.add_parser("render"); p.add_argument("job"); p.add_argument("--pages", required=True)
    p.add_argument("--dpi", default=110, type=int); p.add_argument("--translated", action="store_true"); p.set_defaults(fn=cmd_render)
    p = sub.add_parser("jobs"); p.set_defaults(fn=cmd_jobs)
    args = ap.parse_args()
    if not getattr(args, "fn", None):
        core.fail("Не указана команда.", "Доступно: open, pages, render, jobs.")
    args.fn(args)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # любая неожиданная ошибка — понятным JSON, а не трейсбеком
        core.fail("Внутренняя ошибка pdf.py: %s: %s" % (type(e).__name__, e))
