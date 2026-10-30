"""Real global ToolRegistry and MCP manager over one isolated fixture installation.

Enter through safe_test before imports. No policy, registry handler or transport
is replaced; optional binding exercises canonical ready-skill resolution. The
``loop`` command runs one tool round through the loop's own executor and result
consumer; the assistant turn is fixed here and no model is called.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

os.environ['OUROBOROS_RUNTIME_MODE'] = 'cyber_pro'
os.environ['OUROBOROS_SAFETY_MODE'] = 'off'
sys.path.insert(0, os.getcwd())
from ouroboros import config, mcp_client, mcp_task_sessions
from ouroboros.tools.registry import ToolContext, ToolRegistry
from ouroboros.utils import atomic_write_json


def source_witness(meta: dict, data: pathlib.Path, task_id: str) -> dict | None:
    """The response bytes Core retained from the SDK, before any projection."""
    source = meta.get('mcp_response_source')
    if not source:
        return None
    import hashlib
    p = pathlib.Path(source['path'])
    if not p.is_absolute():
        p = data / 'task_results' / 'artifacts' / task_id / p
    saved = p.read_bytes()
    return {'sha256': hashlib.sha256(saved).hexdigest(), 'size': len(saved),
            'normalized_response': json.loads(saved)}


def loop_round(registry: ToolRegistry, ctx: ToolContext, data: pathlib.Path, name: str, args: dict) -> dict:
    """One assistant tool call through handle_tool_calls -> process_tool_results."""
    from ouroboros import loop_tool_execution as loop
    from ouroboros.llm_messages import _MessageShapingMixin
    call = {'id': 'call-model-image', 'type': 'function',
            'function': {'name': 'mcp_chrome__' + name, 'arguments': json.dumps(args)}}
    messages = [{'role': 'user', 'content': 'Show me the connected tab.'},
                {'role': 'assistant', 'content': '', 'tool_calls': [call]}]
    trace, consumed = {'tool_calls': []}, []
    original_process = loop.process_tool_results
    def process(results, *a, **k):
        consumed.extend(results)
        return original_process(results, *a, **k)
    # Observation spy delegates to the production consumer; it keeps the typed row.
    loop.process_tool_results = process
    live, ctx.messages = ctx.messages, messages
    stateful = loop.StatefulToolExecutor()
    (data / 'logs').mkdir(parents=True, exist_ok=True)
    try:
        errors = loop.handle_tool_calls([call], registry, data / 'logs', ctx.task_id, stateful,
                                        messages, trace, lambda *_a, **_k: None)
    finally:
        stateful.shutdown(wait=True)
        loop.process_tool_results = original_process
        ctx.messages = live
    row = consumed[-1]
    typed = row['tool_result']
    # Core's own send-copy transform; a message-shape witness, not a provider request.
    wire = _MessageShapingMixin._copy_messages_with_cache_policy(
        messages, allow_message_cache_control=False, flatten_tool_content_blocks=True)
    return {'errors': errors, 'is_error': row['is_error'], 'status': typed.status, 'code': typed.code,
            'meta': dict(typed.meta), 'source_witness': source_witness(typed.meta, data, ctx.task_id),
            'messages': json.loads(json.dumps(messages, default=str)),
            'wire_witness': json.loads(json.dumps(wire, default=str)),
            'trace': json.loads(json.dumps(trace['tool_calls'], default=str))}


def main(root: str, skill: str, state: str, mode: str = 'plain') -> None:
    data = pathlib.Path(root) / 'data'
    raw = {'id': 'chrome', 'enabled': True, 'transport': 'stdio', 'command': 'node',
           'args': [str(pathlib.Path(skill) / 'scripts/mcp_server.mjs')],
           'cwd': skill, 'env': {'OUROBOROS_SKILL_STATE_DIR': state},
           'browser_bridge': True, 'browser_protocol': 'ouroboros_chrome'}
    if mode == 'skill-bound':
        raw['browser_skill_binding'] = {'skill_name': 'chrome_browser',
                                        'script_path': 'scripts/mcp_server.mjs'}
    if mode == 'post-state-fault':
        proxy = pathlib.Path(__file__).with_name('post_state_fault.mjs')
        raw['args'] = [str(proxy), *raw['args']]
    # Settings belong solely to safe_test's private root, never the owner.
    atomic_write_json(config.SETTINGS_PATH, {'MCP_ENABLED': True, 'MCP_SERVERS': [raw],
                                           'MCP_TOOL_TIMEOUT_SEC': 30,
                                           'OUROBOROS_RUNTIME_MODE': 'cyber_pro',
                                           'OUROBOROS_SAFETY_MODE': 'off'})
    ctx = ToolContext(repo_dir=pathlib.Path.cwd(), drive_root=data, task_id='task-core-test',
                      task_lifecycle_bound=True,
                      task_metadata={'budget_drive_root': str(data)}, messages=[])
    from ouroboros.task_results import write_task_result
    write_task_result(data, 'task-core-test', 'running', root_task_id='task-core-test')
    ctx.task_attempt = 1
    manager = mcp_client.get_manager()
    manager.reconfigure({'MCP_ENABLED': True, 'MCP_SERVERS': [raw], 'MCP_TOOL_TIMEOUT_SEC': 30})
    registry = ToolRegistry(ctx.repo_dir, data)
    registry.set_context(ctx)
    from ouroboros import owner_pause
    returned = {}
    original_returned = owner_pause.record_mcp_call_returned
    def record_returned(name):
        returned[name] = returned.get(name, 0) + 1
        return original_returned(name)
    # Observation spy delegates to the production accounting owner; no policy changes.
    owner_pause.record_mcp_call_returned = record_returned
    try:
        for line in sys.stdin:
            request = json.loads(line)
            answer = {'id': request['id']}
            try:
                if request['cmd'] == 'discover':
                    # Exercise the same initial task schema path as an ordinary turn.
                    # No manual manager.refresh_server call can supply missing authority.
                    registry.schemas()
                    answer['value'] = [row for row in manager.list_tools_for_registry()
                                       if row['name'].startswith('mcp_chrome__')]
                elif request['cmd'] == 'stop':
                    answer['value'] = mcp_task_sessions.stop_task(ctx)
                elif request['cmd'] == 'loop':
                    answer['value'] = loop_round(registry, ctx, data, request['name'], request.get('args', {}))
                else:
                    result = registry.execute_result('mcp_chrome__' + request['name'],
                                                     request.get('args', {}))
                    # Keep the global consumer's exact text, including its untrusted wrapper.
                    answer['value'] = {'status': result.status, 'text': result.text,
                                       'code': result.code, 'meta': dict(result.meta),
                                       'returned_calls': returned.get('mcp_chrome__' + request['name'], 0)}
                    witness = source_witness(result.meta, data, ctx.task_id)
                    if witness:
                        answer['value']['source_witness'] = witness
            except Exception as exc:
                answer['error'] = type(exc).__name__ + ': ' + str(exc)
            print(json.dumps(answer), flush=True)
    finally:
        owner_pause.record_mcp_call_returned = original_returned
        mcp_task_sessions.stop_task(ctx)


if __name__ == '__main__':
    main(*sys.argv[1:])
