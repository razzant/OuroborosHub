"""Where Amberkeep's two writing commands put their files, and how.

``select OUT.zip`` and ``inspect --rows ROWS.json`` create a new file only in
the folder the command runs in — the task's own folder when the host starts it
there (``run_command(cwd='task_drive')``) — named plainly or by its absolute
path there, and nothing when the command runs inside the installed skill,
where a new file would change the payload its review judged. A file appears
under its name only once complete; an existing name is never replaced; where
that cannot be guaranteed, nothing is written. Reading commands keep working
from the skill folder.

Every check runs an installed *copy* of the skill in the test's own tmp_path,
with the documented command (``python3 -B``), so this suite can never change
the real payload, and the only content is one invented garden note.
"""
from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HUB_SKILL = Path(__file__).resolve().parents[1] / "skills" / "amberkeep"
NOTE = "Марта решила сажать чеснок в октябре, а лук — весной."


def install_copy(tmp_path: Path) -> Path:
    """An installed copy of the payload, as the host keeps one in its bucket."""
    skill = tmp_path / "installed" / "amberkeep"
    shutil.copytree(HUB_SKILL, skill, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return skill


def snapshot(folder: Path) -> dict:
    """Every file of a folder, caches included, with its sha256."""
    return {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(folder.rglob("*")) if path.is_file()}


def run(skill: Path, argv: list[str], cwd: Path):
    """The documented command: the installed script as ``python3 -B``, started in ``cwd``."""
    return subprocess.run([sys.executable, "-B", str(skill / "scripts" / "amberkeep.py"), *argv],
                          cwd=cwd, capture_output=True, text=True, timeout=120)


def ok(result) -> dict:
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def refused(result) -> str:
    assert result.returncode != 0, result.stdout
    doc = json.loads(result.stdout)
    assert "ok" not in doc, "a refusal is never a verdict"
    return doc["refused"]


@pytest.fixture
def setting(tmp_path):
    """An installed copy, an empty task folder and one invented attachment."""
    skill = install_copy(tmp_path)
    task = tmp_path / "task"
    task.mkdir()
    inbox = tmp_path / "attachments"
    inbox.mkdir()
    note = inbox / "garden-note.txt"
    note.write_text(NOTE, encoding="utf-8")
    return skill, task, note


def test_a_transfer_in_the_task_folder_creates_whole_private_files_and_leaves_the_skill(setting):
    skill, task, note = setting
    payload = snapshot(skill)

    shown = ok(run(skill, ["inspect", str(note), "--kind", "text", "--rows", "rows.json"], task))
    rows = Path(shown["rows_document"]["path"])
    assert rows.is_absolute() and rows == (task / "rows.json").resolve()

    picked = ok(run(skill, ["select", "carry.zip", "--original", str(note), "--kind", "text",
                            "--rows", "rows.json"], task))
    package = Path(picked["package"])
    assert package.is_absolute() and package == (task / "carry.zip").resolve(), \
        "the path select reports is the file to hand over, with no guess about where it is"
    assert sorted(path.name for path in task.iterdir()) == ["carry.zip", "rows.json"], \
        "no temporary file is left beside the finished ones"
    if os.name == "posix":
        for made in (rows, package):
            assert stat.S_IMODE(made.stat().st_mode) == 0o600, "owner access only, from the start"

    verified = ok(run(skill, ["verify", "carry.zip"], task))
    assert verified["ok"] is True and verified["record_count"] == picked["record_count"] == 1
    assert ok(run(skill, ["read", "carry.zip"], task))["rows"][0]["text"] == NOTE, \
        "free text travels as written"

    # Create-only: the same name again is refused, and the file stays as it was.
    before = package.read_bytes()
    message = refused(run(skill, ["select", str(package), "--original", str(note), "--kind", "text",
                                  "--rows", "rows.json"], task))
    assert "already exists" in message and "never overwrites" in message
    assert package.read_bytes() == before
    assert sorted(path.name for path in task.iterdir()) == ["carry.zip", "rows.json"]
    assert snapshot(skill) == payload, "the documented command leaves the installed skill as it was"


def test_inside_the_installed_skill_nothing_is_written_and_the_refusal_names_the_route(setting):
    skill, task, note = setting
    payload = snapshot(skill)
    script = (skill / "scripts" / "amberkeep.py").resolve()

    # Reading commands still run from the skill folder.
    receipt = ok(run(skill, ["inspect", str(note), "--kind", "text"], skill))["receipt"]

    for cwd in (skill, skill / "scripts"):
        for argv in (["select", "carry.zip", "--original", str(note), "--kind", "text", "--all",
                      "--receipt", receipt],
                     ["inspect", str(note), "--kind", "text", "--rows", "rows.json"]):
            message = refused(run(skill, argv, cwd))
            assert "inside the installed skill folder" in message, (cwd, argv)
            assert "run_command(cwd='task_drive'" in message and str(script) in message, \
                "the refusal names the exact command to run instead"

    # From the task's folder, a name in the skill is another folder: refused too.
    message = refused(run(skill, ["select", str(skill / "carry.zip"), "--original", str(note),
                                  "--kind", "text", "--all", "--receipt", receipt], task))
    assert "folder this command runs in" in message
    assert snapshot(skill) == payload, "the installed skill is unchanged"
    assert list(task.iterdir()) == []


def test_only_the_folder_the_command_runs_in_takes_a_new_file(setting, tmp_path):
    skill, task, note = setting
    downloads = tmp_path / "home" / "Downloads"
    downloads.mkdir(parents=True)
    receipt = ok(run(skill, ["inspect", str(note), "--kind", "text"], task))["receipt"]

    # A guessed home folder and a sub-folder name are both another folder.
    for out in (str(downloads / "carry.zip"), "exports/carry.zip"):
        message = refused(run(skill, ["select", out, "--original", str(note), "--kind", "text",
                                      "--all", "--receipt", receipt], task))
        assert "folder this command runs in" in message and "cwd='task_drive'" in message, out
    assert list(downloads.iterdir()) == [] and list(task.iterdir()) == []

    # The absolute form of a plain name in this folder is the same file.
    doc = ok(run(skill, ["select", str(task / "carry.zip"), "--original", str(note), "--kind", "text",
                         "--all", "--receipt", receipt], task))
    assert Path(doc["package"]) == (task / "carry.zip").resolve()


def test_help_names_this_copy_and_the_documented_command(setting):
    skill, task, _note = setting
    result = run(skill, ["--help"], task)
    assert result.returncode == 0, result.stderr
    assert f"This copy: {(skill / 'scripts' / 'amberkeep.py').resolve()}" in result.stdout.splitlines(), \
        "the whole path on one line, never wrapped"
    text = " ".join(result.stdout.split())
    assert "cwd='task_drive'" in text and "python3 -B" in text and "never over an existing file" in text


# ------------------------------------------------- the write itself, in process

def load_cli(skill: Path, alias: str):
    spec = importlib.util.spec_from_file_location(alias, skill / "scripts" / "amberkeep.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def in_process(setting, monkeypatch, capsys):
    """The copy's CLI in this process, started in the task folder, plus a pick."""
    skill, task, note = setting
    cli = load_cli(skill, f"amberkeep_output_cli_{os.getpid()}")
    monkeypatch.chdir(task)

    def main(argv: list[str]) -> tuple[int, dict]:
        code = cli.main(argv)
        return code, json.loads(capsys.readouterr().out)

    code, shown = main(["inspect", str(note), "--kind", "text"])
    assert code == 0
    pick = ["--original", str(note), "--kind", "text", "--all", "--receipt", shown["receipt"]]
    return main, task, pick


def test_where_a_finished_file_cannot_be_published_nothing_is_written(in_process, monkeypatch):
    main, task, pick = in_process

    def no_links(*_args, **_kwargs):
        raise PermissionError(errno.EPERM, "hard links are not permitted on this file system")

    monkeypatch.setattr(os, "link", no_links)
    code, doc = main(["select", "carry.zip", *pick])
    assert code == 1 and "cannot publish a finished file" in doc["refused"]
    assert "nothing was written" in doc["refused"]
    assert list(task.iterdir()) == [], "no partial file under the name and no temporary file"


def test_a_failed_write_leaves_nothing_under_the_name(in_process, monkeypatch):
    main, task, pick = in_process

    def full_disk(_descriptor):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full_disk)
    code, doc = main(["select", "carry.zip", *pick])
    assert code == 1 and "No space left" in doc["refused"]
    assert list(task.iterdir()) == [], "no partial file under the name and no temporary file"
