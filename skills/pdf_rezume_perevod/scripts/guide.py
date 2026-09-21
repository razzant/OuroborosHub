# -*- coding: utf-8 -*-
"""guide.py — печатает редакторские справочники навыка.

Команды:
  guide.py summary     канон одностраничного резюме для руководителя
  guide.py translate   правила перевода делового документа на русский
  guide.py             список разделов

Справочники лежат в references/ рядом со scripts/. Скрипт только читает их.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # в папку навыка не пишется ничего, даже __pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _core as core  # noqa: E402

TOPICS = {
    "summary": ("summary_canon.md", "канон резюме: принципы, структура, голос, стоп-слова, самопроверка"),
    "translate": ("translation_rules.md", "правила перевода: русский вместо кальки, термины, колонтитулы, числа"),
}


def main() -> None:
    core.setup_stdio()
    topic = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""
    if topic not in TOPICS:
        core.out({"ok": True, "topics": {k: v[1] for k, v in TOPICS.items()},
                  "usage": "guide.py summary | guide.py translate"})
    path = Path(__file__).resolve().parent.parent / "references" / TOPICS[topic][0]
    if not path.is_file():
        core.fail("Справочник %s не найден в папке навыка." % TOPICS[topic][0],
                  "Проверьте, что папка references/ загружена вместе с навыком.")
    core.out({"ok": True, "topic": topic, "text": path.read_text(encoding="utf-8")})


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        core.fail("Внутренняя ошибка guide.py: %s: %s" % (type(e).__name__, e))
