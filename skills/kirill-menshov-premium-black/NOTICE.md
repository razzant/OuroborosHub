# Notice: Kirill Menshov Premium Black (OuroborosHub adaptation)

## Original work

The Premium Black design system, its layouts, the deck builder and its scripts, the text
standard, the reference documents and the selection of the photo library are the work of
**Kirill Menshov (Кирилл Меньшов)**. This package adapts his release **2.3.2** of 2026-11-20
(history in `CHANGELOG.md`) for OuroborosHub. It is published as a Community skill, maintained
through the `ouroboros-agent` GitHub account, and is not an Official release.

## License status

The original package contains no license file and no license statement for its code, design,
documentation or photographs. This adaptation adds none and grants no license beyond what the
original author provides. Reuse outside this skill should be agreed with the author.

The bundled fonts have their own licence (SIL Open Font License 1.1, below).

## Photographs (`assets/photos/`)

- 23 JPEG files, byte-identical to the original 2.3.2 package. `sand-veins.jpg` and
  `sand-ripples.jpg` are the same file in the original, so the library holds 22 distinct images.
- The original package does not name the photographers or the source of the photographs. The
  files carry no embedded metadata (only a JFIF header: no EXIF, GPS, XMP, IPTC, ICC profile or
  comment), so there was neither sensitive metadata to remove nor a credit line to carry over.

## Fonts (`assets/fonts/`)

| Font | Copyright | Licence text in this package | Upstream source |
|---|---|---|---|
| Inter 4.001 | Copyright 2016 The Inter Project Authors (https://github.com/rsms/inter) | `assets/fonts/LICENSE-Inter.txt` | https://github.com/rsms/inter/blob/master/LICENSE.txt (SHA-256 `262481e844521b326f5ecd053e59b98c8b2da78c8ee1bdbb6e8174305e54935a`) |
| Unbounded 1.701 | Copyright 2022 The Unbounded Project Authors (https://github.com/googlefonts/unbounded) | `assets/fonts/LICENSE-Unbounded.txt` | https://github.com/googlefonts/unbounded/blob/main/OFL.txt (SHA-256 `31e5d4e83955e7103c34570dd49b0570ef490800bd65b42923c0dd02445263b3`) |

Both fonts are licensed under the SIL Open Font License, Version 1.1; neither licence declares a
Reserved Font Name. The Inter files state "Inter UI and Inter is a trademark of rsms." The WOFF2
subsets (Latin and Cyrillic) and the six static TTF instances were prepared by the original
author from the variable fonts; the copyright and licence records inside the font files are
unchanged. The licence texts were added in this adaptation. The fonts archive written next to
an editable PPTX contains the six TTFs with both licence texts.

## Software used at run time (not part of this payload)

Ouroboros installs the declared Python packages (numpy, Pillow, python-pptx, Playwright for
Python 1.65.2) into the skill's isolated environment. `scripts/setup_renderer.py install` puts
Playwright's pinned Chromium headless shell (Chrome for Testing 159.0.8154.6, shipped with its
own `LICENSE.headless_shell`) into the skill's state folder, from Playwright's CDN or, when that
cannot be reached, from Google's official Chrome for Testing bucket. None of these files are
redistributed by this package.

## Changes made in the adaptation

See `CHANGELOG.md`, entry 2.3.2-hub.1: self-contained HTML, a new output folder per build, no
network during builds, a Python renderer with the browser sandbox on, input checks, metadata-free
own photos, synthetic example content and the fonts archive. Layout code, CSS, fonts, photographs
and the living-line algorithm are the original author's.

---

Кратко по-русски: автор дизайн-системы, кода, раскладок, документации и подбора фотографий -
Кирилл Меньшов (оригинал 2.3.2). Лицензия на эти материалы в оригинале не указана, и адаптация
её не добавляет. Шрифты Inter и Unbounded распространяются по SIL OFL 1.1, тексты лицензий
приложены. Источник фотографий в оригинале не указан, метаданных в файлах нет.
