"""Conditional places-v1 fallback shape against the frozen Host endpoint, not a historical Host qualification."""
from dataclasses import replace

from lib.runtime import EmailRuntime
from lib.store import EmailStore
from test_binding_core import DeliveryClient, release_hold

pytest_plugins = ['test_binding_core']


def test_fallback_keeps_rfc_thread_but_folder_has_no_thread(tmp_path, core_host):
    places, drive, adapter = core_host
    root = '<thread@example.invalid>'
    places.connect_place(drive, connector='email-presence', provider='email', account_id='a', state='active')
    row = places.connect_place(drive, connector='email-presence', provider='email', account_id='a',
                               conversation_id=root, state='paused')
    places._write(drive, row.place_id, lambda p: replace(p, thread_id=root))
    # Simulate the advertised legacy capability combination only. The endpoint implementation is current Host.
    adapter._identity({'places_version': 1, 'presence_delivery_version': 1})
    assert 'paused' in adapter.place_hold('a', root, thread_id=root)
    assert 'paused' in adapter.draft_folder_hold('a', root, thread_id=root)
    assert not adapter.draft_folder_hold('a', 'Drafts')
    store = EmailStore(tmp_path / 'mail')
    store.enqueue_outbox(request_id='old', recipients=['peer@example.invalid'], subject='s', body='b',
                         in_reply_to=root, references=[root])
    delivery = DeliveryClient({'EMAIL_USER': 'owner@example.invalid'})
    runtime = EmailRuntime(store, delivery, adapter, account='a', account_loader=lambda: {'sending': 'on'})
    assert runtime.process_outbox() and not delivery.sent
    assert store.receipt('old')['state'] == 'held' and store.receipt('old')['attempts'] == 0
    places.edit_place(drive, row.place_id, 'set_state', author='owner', state='active')
    release_hold(store)
    assert runtime.process_outbox() and len(delivery.sent) == 1
