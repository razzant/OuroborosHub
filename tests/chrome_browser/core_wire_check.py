"""Replay recorded guard questions through the host's own protocol functions.

Run from an Ouroboros core checkout, through its isolated launcher, after the
headless test has written guard-facts.json:

    python -I -S scripts/safe_test.py --temp-parent /tmp -- .venv/bin/python \
        <hub>/tests/chrome_browser/core_wire_check.py <hub>/.chrome_browser_evidence/guard-facts.json

It checks what needs no task context: the state object of every question, and
that step numbers strictly increase within each invocation. URL policy and the
session's lease bookkeeping are the host's and are not replayed here.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.getcwd())
from ouroboros import mcp_chrome_protocol as protocol  # noqa: E402


def main(path: str) -> int:
    facts = json.load(open(path, encoding='utf-8'))
    owner = ('task-test', '1')
    last: dict[str, int] = {}
    problems: list[str] = []
    kinds: dict[str, int] = {}
    for index, fact in enumerate(facts):
        kinds[fact.get('kind')] = kinds.get(fact.get('kind'), 0) + 1
        try:
            protocol.validate_state(fact.get('state'), owner)
        except protocol.ChromeStateError as exc:
            problems.append(f'#{index} {fact.get("kind")}: {exc}')
        invocation = fact.get('invocation_id')
        if invocation is None:
            continue  # the host refuses a question outside any call before it reads anything else
        reason = protocol.invocation_reason(fact, (invocation, '', {}), last.get(invocation, 0))
        if reason:
            problems.append(f'#{index} {fact.get("kind")} step {fact.get("step_id")}: {reason}')
        else:
            last[invocation] = fact['step_id']
        if fact.get('kind') == 'primitive' and fact.get('operation') not in protocol.OPERATIONS | {'connect', 'resume'}:
            problems.append(f'#{index}: operation {fact.get("operation")} is unknown to the host')
    print(json.dumps({'questions': len(facts), 'kinds': kinds, 'invocations': len(last), 'problems': problems}, indent=1))
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
