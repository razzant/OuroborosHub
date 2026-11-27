"""Optional installed-host integration; no provider/model/network requests."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_real_child_context_and_actor_file_readers(tmp_path):
    if importlib.util.find_spec("ouroboros") is None:
        pytest.skip("Ouroboros checkout required on PYTHONPATH for host reader integration")
    script = r'''
import hashlib, json, os
from pathlib import Path
from types import SimpleNamespace
from ouroboros.tools.registry import ToolContext, ToolRegistry
from ouroboros.artifacts import read_actor_source_bytes
from ouroboros.tools.shell import _run_script
from results import result_handler, store_state_bytes
root=Path(os.environ["FIXTURE_ROOT"])
repo=root/"repo"; repo.mkdir()
data=root/"data"; data.mkdir()
project=root/"project"; project.mkdir()
api=SimpleNamespace(get_state_dir=lambda:str(data/"state/skills/google-workspace"))
text=json.dumps({"text":"完整 ответ 🧪"*60000,"end":"END-OF-FULL-SOURCE"},ensure_ascii=False)
handler=result_handler(api,"workspace_request",lambda **kw:text)
registry=ToolRegistry(repo,data)
for mode in ["presence","project","subagent"]:
    task="google-"+mode
    drive=data if mode=="presence" else data/"state/headless_tasks"/task/"data"
    drive.mkdir(parents=True,exist_ok=True)
    ctx=ToolContext(repo_dir=repo,drive_root=drive,task_id=task,budget_drive_root=data,
        workspace_root=project if mode!="presence" else None,
        workspace_mode="external" if mode!="presence" else "",task_depth=1 if mode=="subagent" else 0)
    if mode=="subagent": ctx.task_constraint={"mode":"local_readonly_subagent"}
    if mode=="presence": ctx.task_metadata={"_presence_turn":True,"presence":{}}
    # isolated_signing_probe.py covers real child dispatch; check the returned read recipe for each actor here.
    registry.set_context(ctx)
    payload=json.loads(handler(ctx))
    assert payload["stored"] and payload["actor_readable"] and payload["result_complete"]
    ref=payload["source_ref"]
    path=Path(payload["path"])
    assert path.read_text(encoding="utf-8")==text
    assert hashlib.sha256(path.read_bytes()).hexdigest()==payload["sha256"]
    assert payload["read"]["tool"]=="read_file"
    head=registry.execute("read_file",dict(payload["read"]["arguments"]))
    assert "完整 ответ" in head,head
    tail=registry.execute("read_file",{"root":"artifact_store","path":str(path),"start_char":len(text)-200})
    assert "END-OF-FULL-SOURCE" in tail,tail
    if mode!="subagent":
        code="import hashlib,pathlib;print(hashlib.sha256(pathlib.Path("+repr(str(path))+").read_bytes()).hexdigest())"
        output=_run_script(ctx,script=code,cwd="artifact_store")
        assert payload["sha256"] in output,output
    binary_paths=[]
    for mime,extension,content in [("application/pdf","pdf",b"%PDF-1.7\n\xfffixture"),
                                    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document","docx",b"PK\x03\x04\xfffixture")]:
        stored=store_state_bytes(api,content,name="fixture",extension=extension)
        export=json.dumps({**stored,"mime_type":mime,"name":"fixture","file_id":"fixture"})
        binary=json.loads(result_handler(api,"drive_export",lambda:export)(ctx))
        assert "read" not in binary and "source_ref" not in binary
        assert "Binary file" in binary["read_hint"] and binary["mime_type"]==mime
        assert Path(binary["path"]).read_bytes()==content
        assert binary["path"]!=stored["path"]
        binary_paths.append((Path(binary["path"]),content))
    if mode != "presence":
        # Make this fixture drive unavailable; core tests own the cleanup policy.
        drive.rename(drive.with_name("retired-data"))
        assert not drive.exists()
        assert all(path.read_bytes()==raw for path,raw in binary_paths)
        assert read_actor_source_bytes(data,task,ref)==text.encode("utf-8")
        restored=ToolContext(repo_dir=repo,drive_root=data,task_id=task)
        registry.set_context(restored)
        assert "END-OF-FULL-SOURCE" in registry.execute("read_file",{"root":"artifact_store","path":str(path),"start_char":len(text)-200})
    print(mode,"PASS")
'''
    env = dict(os.environ, FIXTURE_ROOT=str(tmp_path), OUROBOROS_DATA_DIR=str(tmp_path / "data"),
               OUROBOROS_REPO_DIR=str(tmp_path / "repo"),
               OUROBOROS_SETTINGS_PATH=str(tmp_path / "settings.json"),
               OUROBOROS_RUNTIME_MODE="advanced", OUROBOROS_SAFETY_MODE="off")
    skill = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = skill + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.splitlines() == ["presence PASS", "project PASS", "subagent PASS"]
