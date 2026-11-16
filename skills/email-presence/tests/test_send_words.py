"""email_send is judged as its exact call: Safety and an owner card cover everything the recipient receives.

0.10.11 first declared ``text: body`` on email_send. The Host then read ``body`` alone as the send's words, so an
owner card holding only the body and recipient released a call whose subject, HTML alternative, attached files or
display names said something else, without a Safety reading. That declaration is withdrawn (the 0.10.7 behaviour):
the card must hold the whole call. These tests run the real Host Safety exit functions against the shipped manifest,
and against the withdrawn ``text: body`` declaration as the negative control.

Known cost of exact-call judgment, asserted rather than hidden: a DANGEROUS reading is keyed by the exact call, so
the same words under another request_id are read again, and the turn's send record carries no words for mail.
"""
import copy
import json
import os
from types import SimpleNamespace

import pytest

from test_declared_target_preparation import MANIFEST

BODY, TO = 'Thank you.', 'reader@example.invalid'
BASE = {'account_id': 'google:fixture', 'to': [TO], 'body': BODY, 'subject': 'Thanks', 'request_id': 'one'}
GROUP_ADDRESS = f'write: {TO} · account {BASE["account_id"]}'
PARTIAL_CARD = f'Send this reply?\n{BODY}\n{TO}\n{GROUP_ADDRESS}'
# Each variant keeps the card's body and recipient but changes something else the recipient reads.
VARIANTS = {
    'subject': {'subject': 'Wire the outstanding balance today'},
    'html': {'html_body': '<p>Entirely different, unapproved words.</p>'},
    'html_type': {'html_body': '<p>Entirely different, unapproved words.</p>', 'body_type': 'html'},
    'attachment': {'attachments': [{'filename': 'statement.pdf', 'path': '/private/statement.pdf'}]},
    'display_name': {'display_names': {TO: 'Approved by the board'}},
}


def withdrawn_manifest():
    """The de9a5f49 manifest: identical except ``text: body`` on email_send (the negative control)."""
    manifest = copy.deepcopy(MANIFEST)
    row = next(tool for tool in manifest['tools'] if tool['name'] == 'email_send')
    row['text'] = 'body'
    return manifest


@pytest.fixture
def host(tmp_path, monkeypatch):
    source = os.environ.get('OUROBOROS_SOURCE_DIR')
    if not source:
        pytest.skip('Set OUROBOROS_SOURCE_DIR for real Host consumers')
    monkeypatch.syspath_prepend(source)
    from ouroboros import extension_registry_state as registry, place_guard, safety, tool_effects
    from ouroboros.extension_surface_names import extension_surface_name
    from tests._safety_fixtures import supervisor
    actual_effect, actual_facts = tool_effects.effect_of, place_guard.call_place_facts
    harness = supervisor.__wrapped__(monkeypatch, tmp_path)
    h = next(harness)
    monkeypatch.setattr(tool_effects, 'effect_of', actual_effect)
    monkeypatch.setattr(place_guard, 'call_place_facts', actual_facts)
    name = extension_surface_name('email-presence', 'email_send')
    ext = {'skill': 'email-presence', 'extension_generation': 'mail-effect'}
    monkeypatch.setitem(registry._tools, name, ext)

    def install(manifest):
        bundle = registry._ExtensionRegistrations(generation_digest='mail-effect',
            loaded_skill=SimpleNamespace(manifest=SimpleNamespace(raw_extra=manifest, plugin_api={"version": manifest["plugin_api"]})))
        monkeypatch.setitem(registry._extensions, 'email-presence', bundle)

    def subject(args):
        return safety._exit_subject(h.ctx, name, args, actual_effect(name, args=args))

    def card(text):
        h.ctx._owner_directives = [{'source': 'owner_quiz_answer', 'quiz_id': 'q-body-only',
                                    'reply_to': {'address': 'quiz q-body-only', 'text': text}}]

    install(MANIFEST)
    try:
        yield SimpleNamespace(h=h, name=name, safety=safety, place_guard=place_guard, install=install,
                              subject=subject, card=card, effect=lambda args: actual_effect(name, args=args))
    finally:
        next(harness, None)


def test_shipped_manifest_declares_no_partial_words():
    row = next(tool for tool in MANIFEST['tools'] if tool['name'] == 'email_send')
    assert 'text' not in row and 'sends' not in row and row['effect'] == 'message'


@pytest.mark.parametrize('variant', sorted(VARIANTS))
def test_body_only_card_never_releases_other_content(host, variant):
    args = {**BASE, **VARIANTS[variant]}
    words, destinations, exact, _ = host.subject(args)
    assert exact and destinations == [host.name]
    # The card subject is the whole call: every field the recipient reads, display names and file paths included.
    for value in VARIANTS[variant].values():
        assert json.dumps(value, ensure_ascii=False, sort_keys=True)[1:-1] in words
    host.card(PARTIAL_CARD)
    assert host.safety._owner_yes(host.h.ctx, lambda: host.subject(args)) == ''
    readings = len(host.h.calls)
    allowed, _ = host.safety.check_safety(host.name, args, ctx=host.h.ctx)
    assert allowed and len(host.h.calls) == readings + 1, 'Safety must read the changed call itself'


@pytest.mark.parametrize('variant', sorted(VARIANTS))
def test_negative_control_withdrawn_text_body_released_changed_content_unread(host, variant):
    host.install(withdrawn_manifest())
    args = {**BASE, **VARIANTS[variant]}
    words, destinations, exact, _ = host.subject(args)
    assert not exact and words == BODY
    # Current grouped declarations also require every exact address on the card. Naming those addresses
    # still approves no subject, HTML, attachment or display name: this remains the partial-words counterexample.
    assert destinations == [TO, GROUP_ADDRESS]
    host.card(PARTIAL_CARD)  # exactly the card the shipped-manifest test refuses
    assert host.safety._owner_yes(host.h.ctx, lambda: host.subject(args)) == 'q-body-only'
    readings = len(host.h.calls)
    allowed, note = host.safety.check_safety(host.name, args, ctx=host.h.ctx)
    assert allowed and len(host.h.calls) == readings and 'q-body-only' in note


def test_card_quoting_the_exact_call_still_releases_it(host):
    args = {**BASE, **VARIANTS['html']}
    host.card('Send exactly this?\n' + host.subject(args)[0])
    readings = len(host.h.calls)
    allowed, note = host.safety.check_safety(host.name, args, ctx=host.h.ctx)
    assert allowed and len(host.h.calls) == readings and 'q-body-only' in note
    # One changed byte is a different call.
    changed = {**args, 'html_body': args['html_body'] + ' '}
    assert host.safety._owner_yes(host.h.ctx, lambda: host.subject(changed)) == ''


def test_known_exact_call_costs_are_disclosed_not_hidden(host):
    """A DANGEROUS reading holds the exact call; another request_id is read again, and no words are recorded."""
    h = host.h
    first, second = dict(BASE), {**BASE, 'request_id': 'two'}
    assert host.safety._words_key(host.subject(first)) == ''
    host.place_guard._note_message_call(h.ctx, host.name, first, ['fixture-conversation'], host.effect(first))
    assert 'text' not in h.ctx._place_message_calls[-1]
    h.replies.append({'status': 'DANGEROUS', 'reason': 'fixture privacy hold'})
    assert not host.safety.check_safety(host.name, first, ctx=h.ctx)[0]
    readings = len(h.calls)
    h.later()
    h.replies.append({'status': 'DANGEROUS', 'reason': 'fixture privacy hold'})
    allowed, _ = host.safety.check_safety(host.name, second, ctx=h.ctx)
    assert not allowed and len(h.calls) == readings + 1, 'the new request_id is read again, not held by words'


def test_a_text_argument_cannot_select_words_for_a_send(tmp_path):
    """The Host's default words selector is an argument named ``text``. email_send has no such argument, so a call
    carrying one fails in the handler before anything is prepared or queued."""
    from lib.service import MailService, register_tools
    handlers = {}
    service = MailService.__new__(MailService)
    register_tools(SimpleNamespace(register_tool=lambda name, handler, **_: handlers.update({name: handler})), service)
    with pytest.raises(TypeError, match='text'):
        handlers['email_send'](**BASE, text=BODY)
