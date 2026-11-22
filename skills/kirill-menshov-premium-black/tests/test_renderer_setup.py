"""Offline renderer installation and local photo input/output checks."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import shutil
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import premium_common as pc
import setup_renderer as renderer


class OfflineInstallerTests(unittest.TestCase):
    def setUp(self):
        self.plat = renderer.cft_platform()
        if self.plat is None:
            self.skipTest("Chrome for Testing does not build for this platform")
        self.state = pathlib.Path(self.enter_sandbox()) / "state"
        self.root = pc.browsers_dir(self.state)
        self.rev = "1234"
        self.version = "1.2.3"
        self.dest = renderer.browser_folder(self.state, self.rev)
        self.marker = self.dest / "INSTALLATION_COMPLETE"
        exe = "chrome-headless-shell.exe" if self.plat == "win64" else "chrome-headless-shell"
        self.exe_path = f"chrome-headless-shell-{self.plat}/{exe}"
        self.facts = patch.object(renderer, "playwright_facts", return_value={
            "ok": True, "revision": self.rev, "browser_version": self.version, "playwright": pc.PLAYWRIGHT_VERSION,
        })
        self.facts.start()
        self.addCleanup(self.facts.stop)

    def enter_sandbox(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return temp.name

    def response(self, payload=b"not executable", header="base64"):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(f"chrome-headless-shell-{self.plat}/", b"")
            archive.writestr(self.exe_path, payload)
        data = buffer.getvalue()
        digest = hashlib.md5(data, usedforsecurity=False)
        base64_md5 = base64.b64encode(digest.digest()).decode("ascii")
        response = io.BytesIO(data)
        response.headers = Message()
        response.headers["Content-Length"] = str(len(data))
        response.headers["x-goog-hash"] = "crc32c=AAAAAA=="
        if header is not None:
            value = {"base64": base64_md5, "hex": digest.hexdigest().upper()}.get(header, header)
            response.headers["x-goog-hash"] = "md5=" + value
        return response, base64_md5

    def google(self):
        return renderer.via_google(self.root, self.rev, self.version, time.monotonic() + 10)

    def test_install_base64_then_status_and_repeat_preserves_ready_revision(self):
        response, md5 = self.response()
        with patch.object(renderer.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertFalse(renderer.status(self.state)["installed"])
            result = renderer.install(self.state, "google")
            self.assertEqual(result["status"], "ok", result)
            self.assertTrue(result["installed"])
            report, = result["attempts"]
            self.assertEqual((report["md5"], report["md5_computed"], report["md5_header"]), (md5, md5, md5))
            self.assertIs(report["md5_header_verified"], True)
            self.assertEqual(report["md5_note"], "MD5 — контрольная сумма, не подпись")
            self.assertTrue(self.marker.is_file())
            self.assertEqual((self.dest / self.exe_path).read_bytes(), b"not executable")
            self.marker.write_text("keep this marker", encoding="utf-8")
            self.assertEqual(renderer.install(self.state, "google")["note"], "уже установлен")
            urlopen.assert_called_once()

        with patch.dict(os.environ, {"OUROBOROS_SKILL_STATE_DIR": str(self.state)}):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(renderer.main(["status"]), 0)
            status = json.loads(stdout.getvalue())
        self.assertEqual(status["status"], "ok")
        self.assertIs(status["installed"], True)

        other, _ = self.response(b"would replace if not locked", header="hex")
        with patch.object(renderer.urllib.request, "urlopen", return_value=other) as urlopen:
            result = self.google()
        self.assertTrue(result["ok"], result)
        self.assertIn("сохранена", result["note"])
        self.assertTrue(result["reused"])
        urlopen.assert_not_called()
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "keep this marker")
        self.assertEqual((self.dest / self.exe_path).read_bytes(), b"not executable")
        self.assertEqual(set(self.root.iterdir()), {self.dest, self.root / ".install.lock"})

    def test_incomplete_revision_is_kept_and_absent_hash_is_not_verified(self):
        self.dest.mkdir(parents=True)
        (self.dest / "old").write_bytes(b"partial download")
        response, md5 = self.response(header=None)
        with patch.object(renderer.urllib.request, "urlopen", return_value=response):
            result = self.google()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["md5_computed"], md5)
        self.assertIsNone(result["md5_header"])
        self.assertIs(result["md5_header_verified"], False)
        stale, = self.root.glob(f".stale-{self.dest.name}-*")
        self.assertEqual((stale / "old").read_bytes(), b"partial download")
        self.assertTrue(self.marker.is_file())

    def test_bad_advertised_checksums_do_not_publish(self):
        for value in ("", "not-an-md5", "0" * 32, "AAAAAAAAAAAAAAAAAAAAAA=="):
            with self.subTest(value=value):
                response, _ = self.response(header=value)
                with patch.object(renderer.urllib.request, "urlopen", return_value=response):
                    result = self.google()
                self.assertFalse(result["ok"], result)
                self.assertIn("контрольная сумма MD5", result["error"])
                self.assertEqual(set(self.root.iterdir()), {self.root / ".install.lock"})

    def test_parallel_first_installs_download_and_publish_ready_revision_once(self):
        first, first_md5 = self.response(b"first candidate")
        second, _ = self.response(b"second candidate")

        with (patch.object(renderer.urllib.request, "urlopen", side_effect=[first, second]),
              patch.object(renderer.os, "replace", wraps=os.replace) as publication,
              ThreadPoolExecutor(max_workers=2) as pool):
            futures = [pool.submit(renderer.install, self.state, "google") for _ in range(2)]
            results = [future.result(timeout=12) for future in futures]
            downloads = renderer.urllib.request.urlopen.call_count
        self.assertTrue(all(r["status"] == "ok" for r in results), results)
        # Both outcomes are normal for the second installer: it either saw the ready revision
        # before starting (no attempts) or after taking the lock (a reused attempt). Either way
        # there is exactly one download and one publication, and the bytes are that download's.
        self.assertEqual(downloads, 1)
        publication.assert_called_once()
        attempts = [a for r in results for a in r.get("attempts", [])]
        published = [a for a in attempts if a.get("md5")]
        self.assertEqual(len(published), 1, attempts)
        self.assertEqual(published[0]["md5"], first_md5)
        for other in attempts:
            if other is not published[0]:
                self.assertTrue(other.get("reused"), other)
        self.assertEqual((self.dest / self.exe_path).read_bytes(), b"first candidate")
        self.assertTrue(self.marker.is_file())
        self.assertEqual(set(self.root.iterdir()), {self.dest, self.root / ".install.lock"})

    def test_install_lock_waits_with_timeout_and_leaves_other_lock_alone(self):
        self.root.mkdir(parents=True)
        lock = self.root / ".install.lock"
        holder_code = (
            "import pathlib,sys,time; "
            "sys.path.insert(0, sys.argv[2]); import setup_renderer as r; "
            "h=r._open_lock_file(pathlib.Path(sys.argv[1])); "
            "assert r._try_lock(h); print('locked', flush=True); time.sleep(2); r._unlock(h); h.close()"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(lock), str(pathlib.Path(renderer.__file__).parent)],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        with self.assertRaisesRegex(TimeoutError, r"\.install\.lock"):
            with renderer._install_lock(self.root, time.monotonic() + 0.03):
                self.fail("the lock was already held")
        self.assertTrue(lock.is_file())

    def test_persistent_lock_file_is_reused_and_never_unlinked(self):
        self.root.mkdir(parents=True)
        lock = self.root / ".install.lock"
        lock.write_bytes(b"persistent lock")
        with renderer._install_lock(self.root, time.monotonic() + 2):
            self.assertTrue(lock.is_file())
        self.assertTrue(lock.is_file())
        self.assertEqual(lock.read_bytes(), b"persistent lock")

    def test_auto_fallback_when_cdn_does_not_resolve(self):
        response, _ = self.response()
        with (patch.object(renderer, "resolves", return_value=False),
              patch.object(renderer, "via_playwright") as native,
              patch.object(renderer.urllib.request, "urlopen", return_value=response)):
            result = renderer.install(self.state, "auto")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual([row["source"] for row in result["attempts"]], ["playwright", "google"])
        native.assert_not_called()

    def test_native_installer_uses_same_publication_lock(self):
        def native(root, revision, timeout):
            self.assertEqual(root, self.root)
            self.assertEqual(revision, self.rev)
            self.assertGreater(timeout, 0)
            self.assertTrue((root / ".install.lock").is_file())
            self.dest.mkdir()
            self.marker.touch()
            return {"source": "playwright", "ok": True}

        with (patch.object(renderer, "via_playwright", side_effect=native),
              patch.object(renderer.urllib.request, "urlopen", side_effect=AssertionError("unexpected network"))):
            result = renderer.install(self.state, "playwright")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(set(self.root.iterdir()), {self.dest, self.root / ".install.lock"})


    def fake_installer(self, outcome):
        """A stand-in for `python -m playwright install`: records the browsers path it was given."""
        seen = {}

        def run(cmd, env, **kwargs):
            seen["path"] = pathlib.Path(env["PLAYWRIGHT_BROWSERS_PATH"])
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
            folder = seen["path"] / self.dest.name / pathlib.Path(self.exe_path).parent
            folder.mkdir(parents=True)
            (folder / pathlib.Path(self.exe_path).name).write_bytes(b"playwright build")
            (seen["path"] / self.dest.name / "INSTALLATION_COMPLETE").touch()
            (seen["path"] / ".links").mkdir()
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return run, seen

    def test_playwright_installer_publishes_from_its_own_staging_folder(self):
        run, seen = self.fake_installer("ok")
        with (patch.object(renderer, "resolves", return_value=True),
              patch.object(renderer.subprocess, "run", side_effect=run),
              patch.object(renderer.urllib.request, "urlopen", side_effect=AssertionError("unexpected network"))):
            result = renderer.install(self.state, "auto")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(seen["path"].parent, self.root)
        self.assertNotEqual(seen["path"], self.root)
        self.assertEqual((self.dest / self.exe_path).read_bytes(), b"playwright build")
        self.assertEqual(set(self.root.iterdir()), {self.dest, self.root / ".install.lock"})

    def test_playwright_timeout_tail_cannot_touch_the_google_revision(self):
        run, seen = self.fake_installer("timeout")
        response, _ = self.response(b"google build")
        with (patch.object(renderer, "resolves", return_value=True),
              patch.object(renderer.subprocess, "run", side_effect=run),
              patch.object(renderer.urllib.request, "urlopen", return_value=response)):
            result = renderer.install(self.state, "auto")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual([a["source"] for a in result["attempts"]], ["playwright", "google"])
        self.assertEqual(result["attempts"][0]["staging"], str(seen["path"]))
        published = (self.dest / self.exe_path).read_bytes()
        self.assertEqual(published, b"google build")
        # the orphaned Node driver keeps working in the only path it was given: remove the
        # revision folder there, re-extract, run its cache clean-up
        tail = seen["path"] / self.dest.name
        shutil.rmtree(tail, ignore_errors=True)
        (tail / "partial").mkdir(parents=True)
        (tail / "partial" / "chrome-headless-shell").write_bytes(b"late tail")
        shutil.rmtree(seen["path"] / ".links", ignore_errors=True)
        self.assertEqual((self.dest / self.exe_path).read_bytes(), b"google build")
        self.assertTrue(self.marker.is_file())
        self.assertEqual(renderer.status(self.state)["installed"], True)


class LocalInputOutputTests(unittest.TestCase):
    def test_local_paths_and_nonlocal_addresses(self):
        for local in (r"C:\Users\me\pic.jpg", "c:/Users/me/pic.jpg", r"C:relative\pic.jpg", "C://x", "/tmp/x", "x.png"):
            with self.subTest(local=local):
                pc.refused_if_url(local)
        for remote in ("https://x/y.jpg", "file:///x", "ftp://x", "data:image/png;base64,AA==", "mailto:me@example.org",
                       "https:x", "a+:path", "//host/share/y.jpg", r"\\server\share\y.jpg", r"\\?\C:\pic.jpg",
                       r"/\server\share\y.jpg", "  https://example.org/x.jpg"):
            with self.subTest(remote=remote), self.assertRaises(pc.Refused):
                pc.refused_if_url(remote)

    def test_output_names_are_checked_after_adding_suffix(self):
        for bad in ("CON", "con", "NUL.txt", "COM1", "lpt9.JPG", "PRN.photo", "AUX.jpg", "foo.", "foo .jpg",
                    ".", "..", ".hidden", "x\ny", "x\x7f", "x/y", r"x\y", 'x:*?"<>|', "x" * 117):
            with self.subTest(bad=bad), self.assertRaises(pc.Refused):
                pc.output_name(bad, "default", ".jpg")
        with self.assertRaises(pc.Refused):
            pc.output_name("CO", "default", "N")
        self.assertEqual(pc.output_name("CONCERT", "default", ".jpg"), "CONCERT.jpg")
        self.assertEqual(pc.output_name("photo.JPG", "default", ".jpg"), "photo.JPG")
        self.assertEqual(pc.output_name("", "default", ".jpg"), "default.jpg")
        self.assertEqual(pc.output_name(None, "default", ".jpg"), "default.jpg")
        self.assertEqual(pc.output_name(" Deck v1 ", "default", ".html"), "Deck v1.html")
        self.assertEqual(pc.output_name("foo ", "default", ".jpg"), "foo.jpg")
        self.assertEqual(pc.output_name("x\n", "default", ".jpg"), "x.jpg")

    def test_tone_photo_validates_before_io_and_writes_no_metadata_or_overwrites(self):
        import tone_photo
        from PIL import Image, PngImagePlugin

        def tone(src, dst):
            stdout = io.StringIO()
            with (patch.object(sys, "argv", ["tone_photo.py", str(src), dst]),
                  patch.object(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS), contextlib.redirect_stdout(stdout)):
                code = tone_photo.main()
            return code, json.loads(stdout.getvalue())

        with (patch.object(pc, "state_dir", side_effect=AssertionError("must not access state")),
              patch.object(pathlib.Path, "resolve", side_effect=AssertionError("must not resolve URL"))):
            code, report = tone("https://x/y.jpg", "out")
        self.assertEqual(code, pc.EXIT_REFUSED)
        self.assertIn("внешние адреса", report["error"])

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            src = root / "in.png"
            exif = Image.Exif()
            exif[0x013B] = "TEST-ARTIST"
            pnginfo = PngImagePlugin.PngInfo()
            pnginfo.add_itxt("XML:com.adobe.xmp", "TEST-XMP")
            Image.new("RGB", (40, 20), "brown").save(src, exif=exif, pnginfo=pnginfo, icc_profile=b"TEST-ICC")
            with Image.open(src) as opened:
                self.assertEqual(opened.getexif()[0x013B], "TEST-ARTIST")
                self.assertEqual(opened.info["XML:com.adobe.xmp"], "TEST-XMP")
                self.assertEqual(opened.info["icc_profile"], b"TEST-ICC")
            state = root / "state"
            photos = state / "photos"
            photos.mkdir(parents=True)
            (photos / "out.jpg").write_bytes(b"do not overwrite")
            with patch.dict(os.environ, {"OUROBOROS_SKILL_STATE_DIR": str(state)}):
                code, report = tone(src, "out")
            self.assertEqual(code, 0, report)
            self.assertEqual(set(report), {"status", "file", "size", "spec_value", "build_args", "note"})
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["size"], [40, 20])
            target = photos / "out-2.jpg"
            self.assertEqual(report["file"], str(target))
            self.assertEqual(report["spec_value"], "out-2")
            self.assertEqual(report["build_args"], ["--photo", f"out-2={target}"])
            self.assertIn("--photo out-2=", report["note"])
            self.assertEqual((photos / "out.jpg").read_bytes(), b"do not overwrite")
            with Image.open(target) as toned:
                self.assertEqual(toned.format, "JPEG")
                self.assertFalse(toned.getexif())
                self.assertTrue({"exif", "icc_profile", "xmp"}.isdisjoint(toned.info))
            for token in (b"TEST-ARTIST", b"TEST-XMP", b"TEST-ICC"):
                self.assertNotIn(token, target.read_bytes())

    def test_tone_photo_chooses_a_free_alias_without_renaming_the_file(self):
        import tone_photo
        from PIL import Image

        def tone(src, dst):
            stdout = io.StringIO()
            state = pathlib.Path(src).parent / "state"
            with (patch.object(sys, "argv", ["tone_photo.py", str(src), dst]),
                  patch.dict(os.environ, {"OUROBOROS_SKILL_STATE_DIR": str(state)}),
                  contextlib.redirect_stdout(stdout)):
                self.assertEqual(tone_photo.main(), 0)
            return json.loads(stdout.getvalue())

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            src = root / "R&D source.png"
            Image.new("RGB", (12, 8), "brown").save(src)

            reports = {dst: tone(src, dst) for dst in
                       ("R&D.jpg", "R=D.jpg", "R-D.jpg", "dunes-mist.jpg", "own-dunes-mist.jpg",
                        "DUNES-MIST.jpg", "My Photo.jpg")}
            for dst, report in reports.items():
                target = pathlib.Path(report["file"])
                self.assertTrue(target.is_absolute())
                self.assertEqual(target.parent, root / "state" / "photos")
                self.assertTrue(pc.photo_name_is_valid(report["spec_value"]), report)
                self.assertFalse(pc.photo_name_is_library(report["spec_value"]), report)
                self.assertEqual(report["build_args"], ["--photo", f"{report['spec_value']}={target}"])
            names = {dst: pathlib.Path(r["file"]).name for dst, r in reports.items()}
            keys = {dst: r["spec_value"] for dst, r in reports.items()}
            files = [pathlib.Path(r["file"]) for r in reports.values()]
            for i, first in enumerate(files):                                              # every result is its own file
                for second in files[i + 1:]:
                    self.assertFalse(first.samefile(second), (first, second))
            self.assertEqual(names["dunes-mist.jpg"], "dunes-mist.jpg")
            self.assertEqual(names["own-dunes-mist.jpg"], "own-dunes-mist.jpg")
            self.assertEqual(len(set(keys.values())), len(keys), keys)                    # separate results, separate keys
            self.assertEqual(keys["dunes-mist.jpg"], "own-dunes-mist")
            self.assertEqual(keys["own-dunes-mist.jpg"], "own-own-dunes-mist")
            self.assertEqual(keys["R-D.jpg"], "R-D")
            self.assertEqual(keys["My Photo.jpg"], "My Photo")
            self.assertRegex(keys["R&D.jpg"], r"^own-R-D-[0-9a-f]{8}$")
            self.assertRegex(keys["R=D.jpg"], r"^own-R-D-[0-9a-f]{8}$")
            self.assertIn("--photo 'own-R-D-", reports["R&D.jpg"]["note"])
            # Both case outcomes are valid: a case-sensitive file system keeps DUNES-MIST.jpg next to
            # dunes-mist.jpg; a case-insensitive one finds the name taken and writes DUNES-MIST-2.jpg.
            # Either way the key follows the real file name and differs from own-dunes-mist.
            self.assertIn(names["DUNES-MIST.jpg"], ("DUNES-MIST.jpg", "DUNES-MIST-2.jpg"))
            self.assertEqual(keys["DUNES-MIST.jpg"], names["DUNES-MIST.jpg"][:-4])
            for stem in ("DUNES-MIST", "DUNES-MIST-2"):                                     # both outcomes, without the FS
                self.assertEqual(pc.choose_photo_alias(stem), stem)
                self.assertNotEqual(pc.choose_photo_alias(stem), pc.choose_photo_alias("dunes-mist"))

    def test_tone_photo_result_builds_a_real_deck_with_the_returned_alias_and_path(self):
        import tone_photo
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            state = root / "state"
            src = root / "R&D source.png"
            Image.new("RGB", (24, 16), "brown").save(src)
            stdout = io.StringIO()
            with (patch.object(sys, "argv", ["tone_photo.py", str(src), "R&D.jpg"]),
                  patch.dict(os.environ, {"OUROBOROS_SKILL_STATE_DIR": str(state)}),
                  contextlib.redirect_stdout(stdout)):
                self.assertEqual(tone_photo.main(), 0)
            report = json.loads(stdout.getvalue())

            spec = root / "spec.json"
            spec.write_text(json.dumps({
                "brand": "Проверка",
                "doc_title": "Проверка",
                "slides": [{"type": "title", "title": "Проверка", "photo": report["spec_value"]}],
            }, ensure_ascii=False), encoding="utf-8")
            env = dict(os.environ, OUROBOROS_SKILL_STATE_DIR=str(state))
            built = subprocess.run(
                [sys.executable, "-B", str(pathlib.Path(renderer.__file__).parent / "build_deck.py"),
                 str(spec), *report["build_args"]],
                cwd=root, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            start = built.stdout.rfind("\n{\n") + 1
            built_report = json.loads(built.stdout[start:])
            html = pathlib.Path(built_report["files"]["html"]).read_text(encoding="utf-8")
            self.assertIn("data:image/jpeg;base64,", html)

    def test_tone_photo_refuses_oversize_before_decoding_pixels(self):
        import tone_photo
        from PIL import Image, ImageOps

        class Oversize:
            format = "JPEG"
            size = (tone_photo.MAX_IMAGE_PIXELS + 1, 1)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            src = root / "claimed-large.jpg"
            src.write_bytes(b"metadata only")
            stdout = io.StringIO()
            with (patch.object(sys, "argv", ["tone_photo.py", str(src), "large.jpg"]),
                  patch.dict(os.environ, {"OUROBOROS_SKILL_STATE_DIR": str(root / "state")}),
                  patch.object(Image, "open", return_value=Oversize()),
                  patch.object(ImageOps, "exif_transpose", side_effect=AssertionError("decoded too early")),
                  contextlib.redirect_stdout(stdout)):
                code = tone_photo.main()
            report = json.loads(stdout.getvalue())
        self.assertEqual(code, pc.EXIT_REFUSED)
        self.assertIn("слишком большой", report["error"])


if __name__ == "__main__":
    unittest.main()
