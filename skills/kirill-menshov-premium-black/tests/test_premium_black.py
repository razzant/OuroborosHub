"""Checks for the Premium Black skill (OuroborosHub adaptation).

Run with the skill's own dependencies (numpy, Pillow, python-pptx, playwright):
    python -m pytest -p no:cacheprovider -q skills/kirill-menshov-premium-black/tests

Every case builds inside a new temporary data root and never touches the network. Rendering
cases need an installed renderer: point KMPB_TEST_BROWSERS at an `ms-playwright` folder that
`scripts/setup_renderer.py install` filled (it is linked read-only into each temporary state).
Without it they are skipped; KMPB_REQUIRE_RENDER=1 turns that skip into a failure.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

SKILL = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
NAME = "kirill-menshov-premium-black"
BROWSERS = os.environ.get("KMPB_TEST_BROWSERS", "").strip()
REQUIRE_RENDER = os.environ.get("KMPB_REQUIRE_RENDER") == "1"


def digest(path: pathlib.Path) -> tuple[str, int, int]:
    st = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns


class Sandbox:
    """A fresh Ouroboros-like data root: <root>/data/state/skills/<name>, uploads, a task folder."""

    def __init__(self, browsers: str = ""):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="kmpb-test-")).resolve()
        self.data = self.root / "data"
        self.state = self.data / "state" / "skills" / NAME
        self.uploads = self.data / "uploads"
        self.task = self.data / "task_drives" / "t1"
        for folder in (self.state, self.uploads, self.task):
            folder.mkdir(parents=True)
        if browsers:
            (self.state / "ms-playwright").symlink_to(pathlib.Path(browsers).resolve(), target_is_directory=True)

    def run(self, script: str, *args: str, timeout: int = 280):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env["OUROBOROS_SKILL_STATE_DIR"] = str(self.state)
        proc = subprocess.run([sys.executable, "-I", "-B", str(SCRIPTS / script), *args], cwd=self.task,
                              env=env, capture_output=True, text=True, timeout=timeout)
        out = proc.stdout
        start = out.rfind("\n{\n") + 1 if out.rfind("\n{\n") >= 0 else (0 if out.startswith("{") else -1)
        report = json.loads(out[start:]) if start >= 0 else None
        return proc.returncode, out + proc.stderr, report

    def jobs(self) -> list[pathlib.Path]:
        outputs = self.state / "outputs"
        return sorted(p for p in outputs.iterdir() if p.is_dir()) if outputs.is_dir() else []

    def spec(self, slides, name="deck.json", **root) -> pathlib.Path:
        body = {"brand": "Проверка · 2027", "doc_title": "Test deck", **root, "slides": slides}
        path = self.task / name
        path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        return path

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def jpeg_with_markers(path: pathlib.Path, fmt="JPEG"):
    from PIL import Image
    im = Image.new("RGB", (640, 360), (180, 140, 100))
    exif = Image.Exif()
    exif[0x013B] = "KMPB-ARTIST-MARKER"
    exif[0x8298] = "KMPB-COPYRIGHT-MARKER"
    im.save(path, fmt, exif=exif.tobytes())


def jpeg_with_comment_marker(path: pathlib.Path):
    """A JPEG with a COM (comment) segment - metadata that must not reach the deck."""
    jpeg_with_markers(path)
    raw = path.read_bytes()
    note = b"KMPB-COM-MARKER"
    com = b"\xff\xfe" + (len(note) + 2).to_bytes(2, "big") + note
    path.write_bytes(raw[:2] + com + raw[2:])


TITLE = {"type": "title", "photo": "dunes-mist", "eyebrow": "Проверка", "title": "Платформа\nданных",
         "sub": "Подзаголовок", "pill": "Ноябрь 2027"}


class OfflineBuildTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)

    def build(self, *args):
        return self.box.run("build_deck.py", *args)

    def html_of(self, report) -> str:
        return pathlib.Path(report["files"]["html"]).read_text(encoding="utf-8")

    def test_examples_build_to_single_file_html_without_external_references(self):
        for example, slides in (("assets/example_spec.json", 24), ("assets/example_schemes_spec.json", 8)):
            with self.subTest(example=example):
                code, out, report = self.build(example)
                self.assertEqual(code, 0, out)
                html = self.html_of(report)
                job = pathlib.Path(report["job_dir"])
                self.assertEqual(job.parent, self.box.state / "outputs")
                self.assertIn('http-equiv="Content-Security-Policy"', html)
                self.assertEqual(html.count('<section class="slide'), slides)
                self.assertEqual(len(re.findall(r'(?:src|href)\s*=\s*"(?!data:)', html)), 0)
                for needle in ("url(http", "url(fonts/", "@import", "file://", str(SKILL), str(self.box.root)):
                    self.assertNotIn(needle, html)
                self.assertEqual(html.count("data:font/woff2;base64,"), 8)
                self.assertEqual(report["warnings"], [])

    def test_each_build_uses_a_new_folder_and_leaves_existing_files_untouched(self):
        self.box.spec([TITLE])
        (self.box.task / "deck_png").mkdir()
        sentinels = [self.box.task / "deck.html", self.box.task / "deck_png" / "s_01.png",
                     self.box.state / "outputs" / "keep.png"]
        (self.box.state / "outputs").mkdir()
        for i, path in enumerate(sentinels):
            path.write_bytes(f"sentinel {i}".encode())
        before = [digest(p) for p in sentinels]
        first = self.build("deck.json")
        second = self.build("deck.json")
        self.assertEqual((first[0], second[0]), (0, 0), first[1] + second[1])
        self.assertNotEqual(first[2]["job_dir"], second[2]["job_dir"])
        self.assertEqual([digest(p) for p in sentinels], before)
        self.assertEqual(sorted(p.name for p in (self.box.task / "deck_png").iterdir()), ["s_01.png"])

    def test_output_names_must_be_bare_file_names(self):
        self.box.spec([TITLE])
        for args in (("--out", "../escape.html"), ("--pdf", "/tmp/escape.pdf"), ("--pptx", "sub/dir.pptx")):
            with self.subTest(args=args):
                code, out, report = self.build("deck.json", *args)
                self.assertEqual(code, 5, out)
                self.assertEqual(report["error"], "refused")
        self.assertEqual(self.box.jobs(), [])

    def test_photos_from_the_web_or_outside_the_spec_folder_are_refused(self):
        outside = str((SKILL / "assets" / "photos" / "dunes-mist.jpg").resolve())
        (self.box.uploads / "stranger.jpg").write_bytes(b"not mine")
        link = self.box.task / "escape.jpg"
        link.symlink_to(self.box.uploads / "stranger.jpg")
        for photo in ("https://example.org/x.jpg", "//example.org/x.jpg", "../x.jpg", "./../x.jpg", outside,
                      "file:///etc/hosts", str(self.box.uploads / "stranger.jpg"), "./escape.jpg",
                      "not-in-library"):
            with self.subTest(photo=photo):
                self.box.spec([{**TITLE, "photo": photo}])
                code, out, report = self.build("deck.json")
                self.assertEqual(code, 5, out)

    def test_own_photo_is_embedded_without_metadata(self):
        jpeg_with_comment_marker(self.box.task / "own.jpg")
        jpeg_with_markers(self.box.task / "upload.webp", "WEBP")
        self.assertIn(b"KMPB-ARTIST-MARKER", (self.box.task / "own.jpg").read_bytes())
        self.assertIn(b"KMPB-COM-MARKER", (self.box.task / "own.jpg").read_bytes())
        self.box.spec([{**TITLE, "photo": "own.jpg"},
                       {**TITLE, "photo": "upload.webp"}])
        code, out, report = self.build("deck.json",
                                        "--photo", f"own.jpg={self.box.task / 'own.jpg'}",
                                        "--photo", f"upload.webp={self.box.task / 'upload.webp'}")
        self.assertEqual(code, 0, out)
        images = [base64.b64decode(m) for m in re.findall(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", self.html_of(report))]
        self.assertEqual(len(images), 2)
        for data in images:
            self.assertNotIn(b"KMPB-", data)
            self.assertNotIn(b"Exif", data[:256])
            self.assertNotIn(b"KMPB-COM-MARKER", data)
        self.assertEqual(report["own_photos"], ["own.jpg", "upload.webp"])

    def test_photo_declarations_are_checked_and_per_build(self):
        jpeg_with_markers(self.box.uploads / "far.jpg")
        jpeg_with_comment_marker(self.box.task / "own.jpg")
        self.box.spec([{**TITLE, "photo": "дача"}])
        for arg in (f"дача=https://example.org/x.jpg",
                    f"dunes-mist={self.box.uploads / 'far.jpg'}",
                    f"дача={self.box.uploads / 'нет.jpg'}"):
            with self.subTest(arg=arg):
                code, out, report = self.build("deck.json", "--photo", arg)
                self.assertEqual(code, 5, out)
                self.assertEqual(self.box.jobs(), [])
        code, out, report = self.build("deck.json", "--photo", f"дача={self.box.uploads / 'far.jpg'}",
                                       "--photo", f"дача={self.box.uploads / 'far.jpg'}")
        self.assertEqual(code, 5, out)          # one name twice
        self.assertEqual(self.box.jobs(), [])
        # an explicitly passed file is this build's input, wherever the operator keeps it
        code, out, report = self.build("deck.json", "--photo", f"дача={self.box.uploads / 'far.jpg'}")
        self.assertEqual(code, 0, out)
        self.assertEqual(report["own_photos"], ["дача"])
        self.assertNotIn(b"KMPB-", base64.b64decode(
            re.findall(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", self.html_of(report))[0]))
        # a spec path alone is not permission, even for a file next to the spec
        self.box.spec([{**TITLE, "photo": "./own.jpg"}])
        code, out, report = self.build("deck.json")
        self.assertEqual(code, 5, out)
        self.assertIn("--photo", out)

    def test_values_that_reach_markup_are_checked(self):
        card = {"title": "Пункт", "text": "Описание"}
        bad = [
            {"type": "cards", "title": "Т", "items": [{**card, "kind": 'ink" onclick="x'}, card]},
            {**TITLE, "focus": "50% 30%; background:url(https://example.org/x)"},
            {"type": "table", "title": "Т", "columns": ["A", "B"], "rows": [["1", "2"]], "widths": ["1fr); x:url(y"]},
            {"type": "year-kpi", "title": "Т", "lead": {"photo": "dunes-sky", "title": "Г"},
             "items": [{"num": "1", "label": "м", "bar": "17%"}]},
            {"type": "section", "title": "Раздел", "number": "<b>12345</b>"},
            {"type": "kpi", "title": "Т", "items": [{"num": "1", "label": "м", "delta": {"text": "x", "dir": "sideways"}}]},
            {"type": "flow", "title": "Т", "items": [{**card, "kind": "photo"}, card, card]},
            {"type": "cards", "title": "Т", "items": [{**card, "kind": ["ink"]}, card]},
            {"type": "kpi", "title": "Т", "items": [{"num": 90, "label": "м"}]},
        ]
        for slide in bad:
            with self.subTest(slide=slide["type"]):
                self.box.spec([slide])
                code, out, report = self.build("deck.json")
                self.assertEqual(code, 3, out)
                self.assertEqual(report["error"], "schema")

    def test_text_is_escaped_including_quotes(self):
        self.box.spec([{**TITLE, "title": 'Заголовок "в кавычках" <script>x()</script>', "sub": "a & b"}])
        code, out, report = self.build("deck.json")
        self.assertEqual(code, 0, out)
        html = self.html_of(report)
        self.assertNotIn("<script>x()", html)
        self.assertIn("&lt;script&gt;x()", html)
        self.assertIn("&quot;в кавычках&quot;", html)

    def test_text_hygiene_and_unknown_fields_stop_the_build(self):
        self.box.spec([{**TITLE, "title": "Тире — длинное"}])
        self.assertEqual(self.build("deck.json")[0], 2)
        self.box.spec([{**TITLE, "unknown_field": 1}])
        self.assertEqual(self.build("deck.json")[0], 3)

    def test_spec_url_and_folder_are_refused(self):
        for spec in ("https://example.org/spec.json", str(self.box.task)):
            with self.subTest(spec=spec):
                code, out, report = self.build(spec)
                self.assertEqual(code, 5, out)

    def test_null_and_nonstring_slides_stop_the_build(self):
        """slides:[null] and a missing or non-string type are schema errors (exit 3), not crashes."""
        self.box.spec([None], name="nulls.json")
        code, out, report = self.build("nulls.json")
        self.assertEqual(code, 3, out)
        self.assertEqual(report["error"], "schema")
        self.box.spec([{"title": "Т"}], name="notype.json")            # no type at all
        code, out, report = self.build("notype.json")
        self.assertEqual(code, 3, out)
        self.box.spec([{"type": None, "title": "Т"}], name="nulltype.json")
        code, out, report = self.build("nulltype.json")
        self.assertEqual(code, 3, out)
        self.assertTrue(any("type" in str(d) for d in report["details"]), out)

    def test_num_size_numeric_string_actually_shrinks(self):
        """num_size='96' (valid numeric string) shrinks a wide number; a fitting size stays exact."""
        sys.path.insert(0, str(SCRIPTS))
        import build_deck
        st = build_deck.num_style("15,5 млрд ₽", "96", 472)     # inner width of a narrow 3-KPI card
        m = re.search(r"font-size:([\d.]+)px", st)
        self.assertIsNotNone(m, st)
        self.assertLess(float(m.group(1)), 96.0)
        self.assertNotIn("96.0", st)
        self.assertEqual(build_deck.num_style("5", "96", 2000), "font-size:96px;")

    def test_malformed_optional_containers_are_schema_errors(self):
        lead = {"photo": "dunes-mist", "title": "Г"}
        quadrant = {"title": "К", "text": "т"}
        bad = [
            {"type": "year-kpi", "title": "Т", "lead": lead, "items": None},
            {"type": "year-kpi", "title": "Т", "lead": None,
             "items": [{"num": "1", "label": "м"}]},
            {"type": "year-kpi", "title": "Т", "lead": lead,
             "items": [{"num": "1", "label": "м", "delta": 5}]},
            {"type": "forecast", "title": "Т", "items": "не список"},
            {"type": "forecast", "title": "Т",
             "items": [{"pill": "п", "label": "л", "metrics": [["1"]]}]},
            {"type": "layers", "title": "Т",
             "layers": [{"title": "а", "items": ["б", ["в"]]}]},
            {"type": "tree", "title": "Т", "root": {"title": "к"},
             "children": [{"title": "а", "items": "б"}]},
            {"type": "matrix", "title": "Т", "quadrants": [quadrant] * 4, "points": "не список"},
        ]
        for slide in bad:
            with self.subTest(slide=slide["type"]):
                self.box.spec([slide])
                code, out, report = self.build("deck.json")
                self.assertEqual(code, 3, out)
                self.assertEqual(report["error"], "schema")
        # an explicit null in an optional container normalizes to empty, it is not a crash
        self.box.spec([{"type": "pictogram", "title": "Д", "percent": 50, "cards": None}])
        code, out, report = self.build("deck.json")
        self.assertEqual(code, 0, out)

    def test_pictogram_total_100_fits_the_band(self):
        """total=100: ten rows of pictograms still fit the 520px band, scaled, not cropped."""
        self.box.spec([{"type": "pictogram", "title": "Д", "percent": 64, "total": 100,
                        "num": "64", "sub": "из каждой сотни",
                        "cards": [{"pill": "п", "num": "0", "label": "н"}]}])
        code, out, report = self.build("deck.json")
        self.assertEqual(code, 0, out)
        html = self.html_of(report)
        sizes = re.findall(r'viewBox="0 0 40 60" width="([\d.]+)" height="([\d.]+)"', html)
        self.assertEqual(len(sizes), 100)
        for w, h in sizes:
            self.assertLessEqual(float(h), 52.01)
        # the default 50 keeps the original ten-rows-of-five look untouched
        self.box.spec([{"type": "pictogram", "title": "Д", "percent": 50,
                        "cards": [{"pill": "п", "num": "0", "label": "н"}]}])
        code, out, report = self.build("deck.json")
        self.assertEqual(code, 0, out)
        self.assertIn('width="46" height="69"', self.html_of(report))

    def test_brand_notices_land_in_build_json(self):
        """Missing and over-long brand notices reach build.json, not only stdout."""
        path = self.box.task / "nobrand.json"
        path.write_text(json.dumps({"doc_title": "Test deck", "slides": [TITLE]}, ensure_ascii=False),
                        encoding="utf-8")
        code, out, report = self.build("nobrand.json")
        self.assertEqual(code, 0, out)
        data = json.loads(pathlib.Path(report["job_dir"], "build.json").read_text(encoding="utf-8"))
        self.assertTrue(any("brand" in w for w in data["warnings"]), data.get("warnings"))
        self.box.spec([TITLE], brand="И" * 33)
        code, out, report = self.build("deck.json")
        self.assertEqual(code, 0, out)
        data = json.loads(pathlib.Path(report["job_dir"], "build.json").read_text(encoding="utf-8"))
        self.assertTrue(any("32" in w for w in data["warnings"]), data.get("warnings"))

    def test_team_photo_keys_are_identifiers_not_prose(self):
        """photos keys are identifiers: hygiene does not lint them, the photo lookup decides."""
        self.box.spec([{"type": "team", "title": "Т",
                        "photos": ["dunes-mist", "dunes-mist", "dunes-mist", "dunes-mist", "ключ!"]}])
        code, out, report = self.build("deck.json")
        self.assertEqual(code, 5, out)          # refused by the photo lookup, not exit 2 hygiene
        self.assertEqual(report["error"], "refused")

    def test_photo_error_names_the_actual_authority(self):
        """The spec-path refusal asks for the full path of the chosen file, never a relative alias."""
        self.box.spec([{**TITLE, "photo": "../own.jpg"}])
        code, out, report = self.build("deck.json")
        self.assertEqual(code, 5, out)
        self.assertIn("полный путь к выбранному файлу", out)
        self.assertNotIn("./имя.jpg", out)

    def test_render_deck_has_no_escape_warnings(self):
        """The renderer module compiles without invalid-escape warnings."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        proc = subprocess.run([sys.executable, "-W", "error::DeprecationWarning", "-W", "error::SyntaxWarning",
                               "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import render_deck",
                               str(SCRIPTS)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_toned_photo_from_the_state_is_accepted(self):
        jpeg_with_markers(self.box.task / "in.jpg")
        first = self.box.run("tone_photo.py", str(self.box.task / "in.jpg"), "toned.jpg")
        self.assertEqual(first[0], 0, first[1])
        self.box.spec([{**TITLE, "photo": "toned.jpg"}])
        code, out, report = self.build("deck.json", "--photo", f"toned.jpg={first[2]['file']}")
        self.assertEqual(code, 0, out)
        images = re.findall(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", self.html_of(report))
        self.assertEqual(len(images), 1)
        self.assertNotIn(b"KMPB-", base64.b64decode(images[0]))

    def test_zero_share_and_numeric_string_values_build(self):
        """percent=0 is a valid share, and numeric strings (num_size, bar, value,
        highlight_col, total) work wherever a number is accepted."""
        self.box.spec([
            {"type": "pictogram", "title": "Д", "percent": 0, "cards": [{"pill": "п", "num": "0", "label": "н"}]},
            {"type": "pictogram", "title": "Д", "percent": "25", "total": "50",
             "num": "64", "sub": "субтекст"},
            {"type": "kpi", "title": "Т", "items": [{"num": "15,5 млрд", "label": "м", "num_size": "96"}]},
            {"type": "ring-bars", "title": "Т", "ring": {"value": "25", "label": "м"},
             "bars": [{"label": "а", "value": "64"}, {"label": "б", "value": 26}, {"label": "в", "value": 10}]},
            {"type": "table", "title": "Т", "columns": ["А", "Б"], "rows": [["1", "2"], ["3", "4"]],
             "highlight_col": "1"},
            {"type": "year-kpi", "title": "Т", "lead": {"photo": "dunes-mist", "title": "Г"},
             "items": [{"num": "1", "label": "м", "bar": "50"}]},
        ])
        code, out, report = self.build("deck.json")
        self.assertEqual(code, 0, out)
        html = self.html_of(report)
        self.assertIn('font-size:96px', html)
        self.assertIn('grid-template-columns:', html)

    def test_missing_nested_fields_and_wrong_types_are_schema_errors(self):
        quadrant = {"title": "К", "text": "т"}
        bad = [
            {"type": "kpi", "title": "Т", "items": [{"label": "м"}]},                                   # no num
            {"type": "kpi", "title": "Т", "items": [{"num": "1", "delta": {"dir": "up"}}]},             # delta without text
            {"type": "year-kpi", "title": "Т", "lead": {"title": "Г"},
             "items": [{"num": "1", "label": "м"}]},                                                    # no lead.photo
            {"type": "year-kpi", "title": "Т", "lead": "не объект", "items": [{"num": "1", "label": "м"}]},
            {"type": "five", "title": "Т", "items": [{"title": "т"}]},                                  # no photo
            {"type": "timeline", "title": "Т", "items": [{"label": "м"}]},                              # no num
            {"type": "ring-bars", "title": "Т", "ring": {"label": "м"}, "bars": [{"value": 5}]},       # no ring.value / bars.label
            {"type": "ring-bars", "title": "Т", "ring": "не объект", "bars": [{"label": "м", "value": 1}]},
            {"type": "market", "title": "Т", "photo": "dunes-mist", "levels": [{"meta": "м"}]},        # no levels[].num
            {"type": "model", "title": "Т", "photo": "dunes-mist", "rows": [{"label": "м"}]},          # no rows[].num
            {"type": "cards", "title": "Т", "items": ["строка", {"title": "а", "text": "б"}, {"title": "в"}]},
            {"type": "team", "title": "Т", "photos": [1, 2, 3, 4, 5]},
            {"type": "table", "title": "Т", "columns": ["А"], "rows": [["а"], "б"]},
            {"type": "forecast", "title": "Т",
             "items": [{"pill": "п", "label": "л", "metrics": [{"label": "м"}]}]},                     # no metrics[].num
            {"type": "forecast", "title": "Т",
             "items": [{"pill": "п", "label": "л", "metrics": [{"num": "1", "чужое": 1}]}]},
            {"type": "matrix", "title": "Т", "quadrants": [quadrant] * 4,
             "points": [{"y": 5, "label": "л"}]},                                                      # no points.x
        ]
        for slide in bad:
            with self.subTest(slide=slide["type"]):
                self.box.spec([slide])
                code, out, report = self.build("deck.json")
                self.assertEqual(code, 3, out)
                self.assertEqual(report["error"], "schema")

    def test_html_carries_both_font_licences(self):
        self.box.spec([TITLE])
        code, out, report = self.build("deck.json")
        self.assertEqual(code, 0, out)
        html = self.html_of(report)
        self.assertIn('<div hidden id="font-licences">', html)
        self.assertEqual(html.count("SIL OPEN FONT LICENSE Version 1.1"), 2)
        self.assertIn("The Inter Project Authors", html)
        self.assertIn("The Unbounded Project Authors", html)

    def test_day_anim_disabling_is_reported(self):
        self.box.spec([{"type": "flow", "title": "Т", "day": True, "anim": True,
                        "items": [{"title": "а", "text": "б"}, {"title": "в", "text": "г"}, {"title": "д", "text": "е"}]}])
        code, out, report = self.build("deck.json")
        self.assertEqual(code, 0, out)
        self.assertTrue(any("световая линия рассчитана на тёмную сцену" in w for w in report["warnings"]), report["warnings"])

    def test_output_plan_render_also_writes_pdf_with_pptx(self):
        """--render always plans PNG+PDF (with --pptx too); --pptx alone plans only the PPTX."""
        sys.path.insert(0, str(SCRIPTS))
        import build_deck
        def plan(**kw):
            ns = argparse.Namespace(**{"out": None, "pdf": None, "render": False, "pptx": None, **kw})
            return build_deck.output_plan(ns, "deck")
        self.assertEqual(plan(), ("deck.html", None, None))
        self.assertEqual(plan(render=True), ("deck.html", "deck.pdf", None))
        self.assertEqual(plan(pptx="Deck v1.pptx"), ("deck.html", None, "Deck v1.pptx"))
        self.assertEqual(plan(render=True, pptx="Deck v1.pptx"), ("deck.html", "deck.pdf", "Deck v1.pptx"))
        self.assertEqual(plan(pdf="X.pdf"), ("deck.html", "X.pdf", None))

    def test_slide_files_are_ordered_numerically(self):
        """A 100+ slide deck keeps slide order: s_100 never sorts between s_10 and s_11."""
        sys.path.insert(0, str(SCRIPTS))
        import render_deck
        for n, names in ((105, [f"s_{i:03d}.png" for i in range(1, 106)]),
                         (100, [f"s_{i:02d}.png" for i in range(1, 101)])):    # the old 2-digit naming
            with self.subTest(slides=n):
                box = pathlib.Path(tempfile.mkdtemp(prefix="kmpb-order-"))
                self.addCleanup(shutil.rmtree, box, ignore_errors=True)
                for name in names:
                    (box / name).write_bytes(b"x")
                (box / "notes.txt").write_bytes(b"x")     # not a slide file
                got = [p.name for p in render_deck.slide_pngs(box)]
                self.assertEqual(len(got), n)
                self.assertEqual(got, names)     # deck order 1..n, whatever the digit width

    def test_missing_renderer_keeps_the_html_and_exits_4(self):
        if (self.box.state / "ms-playwright").exists():
            self.skipTest("renderer linked into this sandbox")
        self.box.spec([TITLE])
        code, out, report = self.build("deck.json", "--render")
        self.assertEqual(code, 4, out)
        self.assertTrue(pathlib.Path(report["files"]["html"]).is_file())
        self.assertFalse((self.box.state / "ms-playwright").exists())     # nothing was downloaded

    def test_tone_photo_writes_new_metadata_free_files(self):
        jpeg_with_markers(self.box.uploads / "in.jpg")
        first = self.box.run("tone_photo.py", str(self.box.uploads / "in.jpg"), "toned.jpg")
        second = self.box.run("tone_photo.py", str(self.box.uploads / "in.jpg"), "toned.jpg")
        self.assertEqual((first[0], second[0]), (0, 0), first[1] + second[1])
        files = [pathlib.Path(first[2]["file"]), pathlib.Path(second[2]["file"])]
        self.assertEqual([f.name for f in files], ["toned.jpg", "toned-2.jpg"])
        for f in files:
            self.assertEqual(f.parent, self.box.state / "photos")
            self.assertNotIn(b"KMPB-", f.read_bytes())
        from_web = self.box.run("tone_photo.py", "https://example.org/x.jpg", "x.jpg")
        self.assertEqual(from_web[0], 5)
        folder = self.box.run("tone_photo.py", str(self.box.uploads), "x.jpg")
        self.assertEqual(folder[0], 5)
        bad_name = self.box.run("tone_photo.py", str(self.box.uploads / "in.jpg"), "../x.jpg")
        self.assertEqual(bad_name[0], 5)


class PackageTests(unittest.TestCase):
    def test_versions_agree(self):
        common = (SCRIPTS / "premium_common.py").read_text(encoding="utf-8")   # read, not imported: no bytecode in the payload
        version = re.search(r'(?m)^VERSION = "([^"]+)"', common).group(1)
        playwright = re.search(r'(?m)^PLAYWRIGHT_VERSION = "([^"]+)"', common).group(1)
        manifest = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertRegex(manifest, rf"(?m)^version: {re.escape(version)}$")
        self.assertIn(f'package: "playwright=={playwright}"', manifest)
        changelog = (SKILL / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(re.search(r"(?m)^## (\S+)", changelog).group(1), version)

    def test_examples_keep_their_layouts_and_hold_only_synthetic_content(self):
        expected = {
            "example_spec.json": ["title", "title-short", "section", "table", "cards", "year-kpi", "cards", "kpi",
                                  "stairs", "timeline", "statement", "cards", "five", "curve", "team", "banner",
                                  "section", "pictogram", "ring-bars", "market", "model", "cards", "forecast", "thanks"],
            "example_schemes_spec.json": ["loop", "flow", "layers", "cycle", "matrix", "tree", "compare", "funnel"],
        }
        library = {p.stem for p in (SKILL / "assets" / "photos").glob("*.jpg")}

        def photo_values(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "photo" and isinstance(v, str) and v:
                        yield v
                    else:
                        yield from photo_values(v)
            elif isinstance(node, list):
                for v in node:
                    yield from photo_values(v)

        for name, types in expected.items():
            spec = json.loads((SKILL / "assets" / name).read_text(encoding="utf-8"))
            self.assertEqual([s["type"] for s in spec["slides"]], types)
            self.assertIn(spec.get("brand"), {"Стратегия технологий", "Платформа ИИ · 2026"})
            used = list(photo_values(spec))
            used += [p for s in spec["slides"] for p in (s.get("photos") or [])]
            if name == "example_spec.json":
                self.assertTrue(used)
            else:
                self.assertEqual(used, [])     # the schemes example carries layouts only, no photos
            for key in used:
                self.assertIn(key, library)
            if name == "example_spec.json":
                self.assertEqual(spec["slides"][-1]["contacts"], ["team@example.org", "Ноябрь 2026"])

    def test_photo_library_and_font_licences(self):
        photos = sorted(p.stem for p in (SKILL / "assets" / "photos").glob("*.jpg"))
        self.assertEqual(len(photos), 23)
        fonts = SKILL / "assets" / "fonts"
        upstream = {"LICENSE-Inter.txt": ("262481e844521b326f5ecd053e59b98c8b2da78c8ee1bdbb6e8174305e54935a",
                                          "Copyright (c) 2016 The Inter Project Authors"),
                    "LICENSE-Unbounded.txt": ("31e5d4e83955e7103c34570dd49b0570ef490800bd65b42923c0dd02445263b3",
                                              "Copyright 2022 The Unbounded Project Authors")}
        for name, (sha, line) in upstream.items():
            data = (fonts / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), sha)
            self.assertIn(line.encode(), data)
            self.assertIn(b"SIL OPEN FONT LICENSE Version 1.1", data)
        css = (SKILL / "assets" / "fonts.css").read_text(encoding="utf-8")
        for font in re.findall(r"url\(fonts/([^)]+)\)", css):
            self.assertTrue((fonts / font).is_file(), font)


@unittest.skipUnless(BROWSERS or REQUIRE_RENDER, "set KMPB_TEST_BROWSERS to an installed ms-playwright folder")
class RenderTests(unittest.TestCase):
    def setUp(self):
        if not BROWSERS:
            self.fail("KMPB_REQUIRE_RENDER=1 but KMPB_TEST_BROWSERS is not set")
        self.box = Sandbox(BROWSERS)
        self.addCleanup(self.box.cleanup)

    def test_schemes_example_renders_to_png_pdf_and_editable_pptx(self):
        from pptx import Presentation
        sentinel = self.box.task / "keep.png"
        sentinel.write_bytes(b"sentinel")
        before = digest(sentinel)
        code, out, report = self.box.run("build_deck.py", "assets/example_schemes_spec.json",
                                         "--pdf", "Schemes v1.pdf", "--pptx", "Schemes v1.pptx")
        self.assertEqual(code, 0, out)
        self.assertEqual(report["outside_requests"], [])
        job = pathlib.Path(report["job_dir"])
        self.assertEqual(len(list((job / "png").glob("s_*.png"))), 8)
        pdf = (job / "Schemes v1.pdf").read_bytes()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(len(re.findall(rb"/Type\s*/Page[^s]", pdf)), 8)
        prs = Presentation(str(job / "Schemes v1.pptx"))
        self.assertEqual(len(prs.slides), 8)
        texts = [sh for s in prs.slides for sh in s.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
        self.assertGreater(len(texts), 100)
        self.assertEqual(prs.core_properties.author, "")
        self.assertEqual(report["pptx"]["animated_media"], 3)
        with zipfile.ZipFile(job / "Schemes v1 fonts.zip") as z:
            names = z.namelist()
        self.assertEqual(len(names), 9)
        self.assertIn("Premium Black fonts/LICENSE-Inter.txt", names)
        self.assertFalse((job / "_work").exists())
        self.assertEqual(digest(sentinel), before)

    def test_lossless_pdf_and_flat_pptx(self):
        from PIL import Image
        jpeg_with_markers(self.box.task / "own.jpg")
        self.box.spec([TITLE, {**TITLE, "type": "section", "photo": "own.jpg", "number": 2}])
        code, out, report = self.box.run("build_deck.py", "deck.json", "--pdf", "Deck.pdf", "--pdf-lossless",
                                         "--pptx", "Deck.pptx", "--flat",
                                         "--photo", f"own.jpg={self.box.task / 'own.jpg'}")
        self.assertEqual(code, 0, out)
        job = pathlib.Path(report["job_dir"])
        pdf = (job / "Deck.pdf").read_bytes()
        self.assertEqual(len(re.findall(rb"/Type\s*/Page[^s]", pdf)), 2)
        self.assertIn(b"/FlateDecode", pdf)
        self.assertFalse((job / "Deck fonts.zip").exists())
        with Image.open(job / "png" / "s_01.png") as im:
            self.assertEqual(im.size, (1920, 1080))

    def test_render_with_pptx_also_writes_pdf(self):
        self.box.spec([TITLE, {**TITLE, "type": "section", "number": 2}])
        code, out, report = self.box.run("build_deck.py", "deck.json", "--render", "--pptx", "Deck v1.pptx")
        self.assertEqual(code, 0, out)
        self.assertIn("pdf", report["files"])
        self.assertIn("pptx", report["files"])
        self.assertEqual(len(list(pathlib.Path(report["job_dir"]).glob("*.pdf"))), 1)

    def test_day_cards_keep_day_bronze_in_pptx(self):
        """Day-glass card headings are editable text in the day bronze, not the night latte."""
        card = {"title": "Заголовок", "text": "Описание"}
        self.box.spec([{"type": "cards", "title": "Т", "day": True,
                        "items": [dict(card), dict(card), dict(card)]}], name="day.json")
        code, out, report = self.box.run("build_deck.py", "day.json", "--pptx", "Day v1.pptx")
        self.assertEqual(code, 0, out)
        from pptx import Presentation
        prs = Presentation(str(pathlib.Path(report["files"]["pptx"])))
        colours = [str(r.font.color.rgb) for sh in prs.slides[0].shapes
                   if sh.has_text_frame and sh.text_frame.text.strip() == "Заголовок"
                   for p in sh.text_frame.paragraphs for r in p.runs]
        self.assertEqual(len(colours), 3, colours)
        self.assertTrue(all(c == "A67C3F" for c in colours), colours)

    def test_delta_text_sits_after_the_arrow(self):
        """The delta's editable text starts after the 16px arrow and the 8px gap, not on top of it."""
        self.box.spec([{"type": "kpi", "title": "Т",
                        "items": [{"num": "15,5 млрд", "label": "метка", "delta": "+4,1 млн за год"}]}],
                       name="delta.json")
        code, out, report = self.box.run("build_deck.py", "delta.json")
        self.assertEqual(code, 0, out)
        sys.path.insert(0, str(SCRIPTS))
        import render_deck
        work = pathlib.Path(report["job_dir"]) / "work"
        work.mkdir()
        info = render_deck.render(pathlib.Path(report["files"]["html"]),
                                  pathlib.Path(report["job_dir"]) / "png2", self.box.state,
                                  work_dir=work)
        self.assertEqual(info["outside_requests"], [])
        geo = json.loads((work / "geometry.json").read_text(encoding="utf-8"))
        blocks = geo["slides"][0]["blocks"]
        delta = next(b for b in blocks if any("за год" in r["text"] for r in b["runs"]))
        label = next(b for b in blocks if any(r["text"].strip() == "метка" for r in b["runs"]))
        self.assertGreaterEqual(delta["x"] - label["x"], 24, (delta, label))

    def test_chart_and_ring_captions_stay_editable_text(self):
        """The SVG captions of curve (axis labels) and ring-bars (share and label) must be
        editable text blocks of the PPTX, not pixels of the background picture."""
        curve = {"type": "curve", "title": "Т", "y_label": "Зрелость продукта", "x_label": "Зрелость команды",
                 "notes": [{"pos": "left", "pill": "п", "text": "т"}]}
        ring = {"type": "ring-bars", "title": "Т", "ring": {"value": 64, "label": "команд"},
                "bars": [{"label": "Ждут данные дольше суток", "value": 64}]}
        self.box.spec([curve, ring])
        code, out, report = self.box.run("build_deck.py", "deck.json", "--pptx", "Charts v1.pptx")
        self.assertEqual(code, 0, out)
        from pptx import Presentation
        prs = Presentation(str(pathlib.Path(report["files"]["pptx"])))
        texts = [sh.text_frame.text for s in prs.slides for sh in s.shapes
                 if sh.has_text_frame and sh.text_frame.text.strip()]
        for needle in ("ЗРЕЛОСТЬ ПРОДУКТА", "ЗРЕЛОСТЬ КОМАНДЫ", "64%", "КОМАНД"):
            self.assertIn(needle, texts)
        # the share itself is Unbounded editable text, not part of the background picture
        slide2 = prs.slides[1]
        self.assertTrue(any(sh.has_text_frame and "64%" in sh.text_frame.text and
                            "Unbounded" in (sh.text_frame.paragraphs[0].runs[0].font.name or "")
                            for sh in slide2.shapes))


if __name__ == "__main__":
    unittest.main()
