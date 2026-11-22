#!/usr/bin/env python3
"""
Premium Black: explicit setup of the slide renderer.

Usage:
  setup_renderer.py status                   what is installed (no network, writes nothing)
  setup_renderer.py install [--source S]     download the pinned Chromium headless shell into
                                             <state>/ms-playwright (network, about 105 MB)
  setup_renderer.py check                    start the installed browser once on a built-in page

--source auto (default) runs Playwright's own installer and, when its CDN cannot be reached,
downloads the same Chrome for Testing build from Google's official bucket
(storage.googleapis.com/chrome-for-testing-public), checks its size and any advertised MD5 and unpacks it
where Playwright expects it. --source playwright or --source google forces one of them.

HTML is built without the renderer. PNG, PDF and PPTX need it. The browser goes only into this
skill's own state folder: never the global Playwright cache, the system Chrome or a browser
profile. build_deck.py never downloads anything; it reports a missing renderer with exit code 4.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import pathlib
import platform
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import premium_common as pc

BROWSER = "chromium-headless-shell"
PLAYWRIGHT_CDN = "cdn.playwright.dev"
CFT_URL = "https://storage.googleapis.com/chrome-for-testing-public/{version}/{platform}/chrome-headless-shell-{platform}.zip"
BUDGET = 280                       # seconds; skill_exec allows at most 300
MAX_ZIP = 400 * 1024 * 1024


def playwright_facts() -> dict:
    try:
        import playwright
        from importlib.metadata import version
        installed = version("playwright")
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return {"ok": False, "error": f"Python-пакет playwright не установлен: {exc}"}
    package = pathlib.Path(playwright.__file__).resolve().parent / "driver" / "package" / "browsers.json"
    revision = browser_version = None
    try:
        for row in json.loads(package.read_text(encoding="utf-8"))["browsers"]:
            if row.get("name") == BROWSER:
                revision, browser_version = row.get("revision"), row.get("browserVersion")
    except (OSError, ValueError, KeyError):
        pass
    if revision is None:
        return {"ok": False, "playwright": installed, "error": "в пакете playwright нет сведений о headless shell"}
    return {"ok": installed == pc.PLAYWRIGHT_VERSION, "playwright": installed, "expected": pc.PLAYWRIGHT_VERSION,
            "revision": revision, "browser_version": browser_version}


def cft_platform() -> str | None:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "mac-arm64" if machine in ("arm64", "aarch64") else "mac-x64"
    if sys.platform.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux64"
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return "win64"
    return None                    # e.g. Linux arm64: Google publishes no such build; use Playwright's CDN


def browser_folder(state: pathlib.Path, revision: str) -> pathlib.Path:
    return pc.browsers_dir(state) / f"chromium_headless_shell-{revision}"


def status(state: pathlib.Path) -> dict:
    facts = playwright_facts()
    root = pc.browsers_dir(state)
    result = {"browsers_path": str(root), **facts}
    if "revision" in facts:
        folder = browser_folder(state, facts["revision"])
        result["installed"] = (folder / "INSTALLATION_COMPLETE").is_file()
        result["folder"] = str(folder)
    else:
        result["installed"] = False
    if not facts.get("ok"):
        result["hint"] = (f"Нужен playwright=={pc.PLAYWRIGHT_VERSION} из install_specs скилла: включи скилл заново "
                          "(Enable ставит зависимости в его изолированное окружение)")
    elif not result["installed"]:
        result["hint"] = "Браузер рендера не установлен: setup_renderer.py install (около 105 МБ, нужна сеть)"
    return result


def resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def via_playwright(root: pathlib.Path, timeout: float) -> dict:
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(root))
    try:
        run = subprocess.run([sys.executable, "-m", "playwright", "install", BROWSER], env=env,
                             capture_output=True, text=True, timeout=max(30, timeout))
    except subprocess.TimeoutExpired:
        return {"source": "playwright", "ok": False, "error": "установщик Playwright не уложился во время"}
    tail = (run.stdout + run.stderr).strip().splitlines()[-4:]
    return {"source": "playwright", "ok": run.returncode == 0, "installer_exit": run.returncode, "installer_tail": tail}


def _safe_extract(archive: pathlib.Path, target: pathlib.Path) -> None:
    """Regular files and folders only, every path inside target, executable bits kept."""
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            name = info.filename
            parts = pathlib.PurePosixPath(name).parts
            mode = (info.external_attr >> 16) & 0xFFFF
            if name.startswith("/") or "\\" in name or ":" in name or ".." in parts or not parts:
                raise RuntimeError(f"небезопасный путь в архиве: {name!r}")
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"символическая ссылка в архиве не поддерживается: {name!r}")
            dest = target.joinpath(*parts)
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(dest, "xb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            if mode & 0o111:
                os.chmod(dest, 0o755)


def _open_lock_file(path: pathlib.Path):
    """Open the persistent install-lock file.  It is never unlinked."""
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    return handle


def _try_lock(handle) -> bool:
    """Acquire an OS-held exclusive lock without blocking."""
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise
    return True


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _install_lock(root: pathlib.Path, deadline: float):
    """Hold an OS lock for the whole install lifecycle.

    The lock file is persistent on purpose: unlinking a lock pathname is racy because
    another process may already hold the old inode.  The operating system releases
    the lock when the process exits, so a dead installer cannot strand it.
    """
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".install.lock"
    handle = _open_lock_file(lock)
    acquired = False
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            if _try_lock(handle):
                acquired = True
                break
            time.sleep(min(0.1, remaining))
        if not acquired:
            raise TimeoutError("истекло время установки: не удалось получить блокировку .install.lock")
        yield handle
    finally:
        try:
            if acquired:
                _unlock(handle)
        finally:
            handle.close()


def via_google(root: pathlib.Path, revision: str, version: str, deadline: float) -> dict:
    plat = cft_platform()
    if plat is None:
        return {"source": "google", "ok": False, "error": f"для {sys.platform}/{platform.machine()} нет сборки Chrome for Testing"}
    url = CFT_URL.format(version=version, platform=plat)
    work = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        with _install_lock(root, deadline):
            dest = root / f"chromium_headless_shell-{revision}"
            if (dest / "INSTALLATION_COMPLETE").is_file():
                return {"source": "google", "ok": True, "url": url, "reused": True,
                        "note": "готовая ревизия сохранена"}

            work = pathlib.Path(tempfile.mkdtemp(prefix=".download-", dir=root))
            archive = work / "headless-shell.zip"
            digest, size = hashlib.md5(usedforsecurity=False), 0
            request = urllib.request.Request(url, headers={"User-Agent": f"{pc.SKILL_NAME}-setup/{pc.VERSION}"})
            with urllib.request.urlopen(request, timeout=60) as response, open(archive, "xb") as out:
                expected = int(response.headers.get("Content-Length") or 0)
                hashes = ",".join(response.headers.get_all("x-goog-hash") or [])
                header_md5s = [h[4:].strip() for h in map(str.strip, hashes.split(",")) if h[:4].lower() == "md5="]
                if not 0 < expected <= MAX_ZIP:
                    raise RuntimeError(f"неожиданный размер архива: {expected}")
                while True:
                    if time.monotonic() > deadline:
                        raise RuntimeError("загрузка не уложилась во время; повтори setup_renderer.py install --source google")
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > expected:
                        raise RuntimeError(f"архив больше объявленного размера: {expected} байт")
                    out.write(chunk)
                    digest.update(chunk)
            if size != expected:
                raise RuntimeError(f"архив получен не полностью: {size} из {expected} байт")
            actual_md5 = base64.b64encode(digest.digest()).decode("ascii")
            header_md5 = None
            for header_md5 in header_md5s:
                if len(header_md5) == 32:
                    try:
                        header_md5 = base64.b64encode(bytes.fromhex(header_md5)).decode("ascii")
                    except ValueError:
                        pass                   # falls through to the same checksum error
                if header_md5 != actual_md5:
                    raise RuntimeError("контрольная сумма MD5 архива не совпала с объявленной Google")
            unpacked = work / "unpacked"
            unpacked.mkdir()
            _safe_extract(archive, unpacked)
            top = unpacked / f"chrome-headless-shell-{plat}"
            exe = top / ("chrome-headless-shell.exe" if plat == "win64" else "chrome-headless-shell")
            if not exe.is_file():
                raise RuntimeError("в архиве нет chrome-headless-shell")
            staging = work / "revision"
            staging.mkdir()
            top.rename(staging / top.name)
            (staging / "INSTALLATION_COMPLETE").touch(exist_ok=False)
            result = {"source": "google", "ok": True, "url": url, "bytes": size, "md5": actual_md5,
                      "md5_computed": actual_md5, "md5_header": header_md5,
                      "md5_header_verified": header_md5 is not None,
                      "md5_note": "MD5 — контрольная сумма, не подпись"}
            if (dest / "INSTALLATION_COMPLETE").is_file():
                result["note"] = "готовая ревизия сохранена"
            else:
                if os.path.lexists(dest):
                    dest.rename(root / f".stale-{dest.name}-{secrets.token_hex(16)}")
                os.replace(staging, dest)
            return result
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return {"source": "google", "ok": False, "url": url, "error": str(exc)[:400]}
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


def install(state: pathlib.Path, source: str = "auto") -> dict:
    before = status(state)
    if not before.get("ok"):
        return {**before, "status": "error"}
    if before.get("installed"):
        return {**before, "status": "ok", "note": "уже установлен"}
    started = time.monotonic()
    deadline = started + BUDGET
    root = pc.browsers_dir(state)
    root.mkdir(parents=True, exist_ok=True)
    attempts = []
    if source in ("auto", "playwright"):
        if source == "auto" and not resolves(PLAYWRIGHT_CDN):
            attempts.append({"source": "playwright", "ok": False, "error": f"{PLAYWRIGHT_CDN} не разрешается в DNS"})
        else:
            # Playwright writes the same revision folder directly: keep it out of Google's publication step.
            try:
                with _install_lock(root, deadline):
                    current = status(state)
                    if current["installed"]:
                        return {**current, "status": "ok", "note": "уже установлен"}
                    attempts.append(via_playwright(root, (deadline - time.monotonic()) * (0.6 if source == "auto" else 1)))
            except OSError as exc:
                attempts.append({"source": "playwright", "ok": False, "error": str(exc)[:400]})
    if source in ("auto", "google") and not status(state)["installed"]:
        attempts.append(via_google(root, before["revision"], before["browser_version"], deadline))
    after = status(state)
    return {**after, "status": "ok" if after["installed"] else "error",
            "seconds": round(time.monotonic() - started, 1), "attempts": attempts}


def check(state: pathlib.Path) -> dict:
    before = status(state)
    if not (before.get("ok") and before.get("installed")):
        return {**before, "status": "error"}
    import render_deck
    started = time.monotonic()
    try:
        info = render_deck.smoke(state)
    except render_deck.RendererUnavailable as exc:
        return {**before, "status": "error", "error": str(exc)}
    return {**before, "status": "ok", "seconds": round(time.monotonic() - started, 1), **info}


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    command = args[0] if args else "status"
    source = "auto"
    if len(args) == 3 and args[1] == "--source" and args[2] in ("auto", "playwright", "google") and command == "install":
        source = args[2]
    elif command not in ("status", "install", "check") or len(args) > 1:
        print(__doc__)
        return 1
    try:
        state = pc.state_dir()
    except pc.Refused as exc:
        pc.emit({"status": "error", "error": str(exc)})
        return pc.EXIT_REFUSED
    if command == "status":
        result = status(state)
        result["status"] = "ok" if result.get("ok") and result.get("installed") else "missing"
    elif command == "install":
        result = install(state, source)
    else:
        result = check(state)
    pc.emit({"command": command, **result})
    return 0 if result["status"] == "ok" else pc.EXIT_RENDERER


if __name__ == "__main__":
    raise SystemExit(main())
