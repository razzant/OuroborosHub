"""Attachment adapter contract; actual installed OOP consumer checked separately."""
import hashlib
import importlib
import json
import sys
import types
from pathlib import Path

import pytest
from starlette.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType('_attachment_test')
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg
sys.path.insert(0, str(ROOT))
adapter = importlib.import_module('_attachment_test.attachment_tool')


@pytest.fixture
def case(tmp_path, monkeypatch):
    fp = tmp_path / 'voice.ogg'
    data = b'OggSbounded-audio'
    fp.write_bytes(data)
    entry = dict(ordinal=0, status='staged', root='artifact_store', relpath='voice.ogg', mime='audio/ogg', sha256=hashlib.sha256(data).hexdigest())
    ctx = types.SimpleNamespace(task_contract={'attachment_manifest': [entry]})
    calls = []
    def bind(context, **kwargs):
        assert context is ctx
        assert kwargs == dict(root='artifact_store', path='voice.ogg', operation='read')
        calls.append('bind')
        return types.SimpleNamespace(target_path=fp)
    def admit(context, path, *, max_bytes):
        assert context is ctx and path == str(fp) and max_bytes == 393216
        calls.append('admit')
        return fp, ''
    monkeypatch.setitem(sys.modules, 'ouroboros.tool_access', types.SimpleNamespace(build_resolved_resource_binding=bind))
    monkeypatch.setitem(sys.modules, 'ouroboros.tools.media', types.SimpleNamespace(_resolve_local_file=admit))
    def worker(state, method, path, body, mime):
        assert method == 'POST' and path == '/transcribe' and body == data and mime == 'audio/ogg'
        calls.append('worker')
        return JSONResponse({'text': 'local words', 'timings': {'model_loaded': False}})
    return ctx, entry, fp, calls, worker


def run(case, ordinal=0):
    ctx, entry, fp, calls, worker = case
    return json.loads(adapter.transcribe_attachment(ctx, ordinal, state_dir=fp.parent, worker=worker))


def test_success_and_worker_error(case):
    assert run(case) == {'ok': True, 'text': 'local words', 'timings': {'model_loaded': False}}
    assert case[3] == ['bind', 'admit', 'worker']
    def fail(*args):
        return JSONResponse({'error': {'code': 'worker_unavailable', 'message': 'Unavailable'}}, status_code=503)
    assert json.loads(adapter.transcribe_attachment(case[0], 0, state_dir=case[2].parent, worker=fail))['ok'] is False


@pytest.mark.parametrize('ordinal', [-1, True, '0', 1])
def test_bad_or_missing_number(case, ordinal):
    assert run(case, ordinal)['ok'] is False
    assert not case[3]


@pytest.mark.parametrize('field,value', [('status', 'rejected'), ('root', 'user_files'), ('relpath', ''), ('sha256', 'bad'), ('mime', 'text/plain')])
def test_rejected_or_changed_attachment(case, field, value):
    case[1][field] = value
    assert run(case)['ok'] is False
    assert 'worker' not in case[3]


def test_duplicate_number(case):
    case[0].task_contract['attachment_manifest'].append(dict(case[1]))
    assert run(case)['ok'] is False
    assert not case[3]


def test_host_denial_no_fallback(case, monkeypatch):
    media = sys.modules['ouroboros.tools.media']
    monkeypatch.setattr(media, '_resolve_local_file', lambda *a, **k: (None, 'protected artifact'))
    result = run(case)
    assert result['error']['code'] == 'attachment_blocked'
    assert 'worker' not in case[3]


def test_bounded_read_and_hash(case):
    case[2].write_bytes(b'OggS' + b'x' * 393216)
    assert run(case)['ok'] is False
    assert 'worker' not in case[3]
