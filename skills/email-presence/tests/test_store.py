from lib.store import EmailStore


def test_inbox_outbox_are_durable_and_idempotent(tmp_path):
    s=EmailStore(tmp_path); m={"folder":"INBOX","uid":3,"message_id":"<m>","sender":"a@example.com","subject":"Hi","body":"Body","references":[],"recipients":["bot@example.com"]}
    _row,inserted=s.ingest(m); assert inserted; assert s.ingest(m)[1] is False
    item=s.claim_inbox(); assert item and item.uid==3; s.set_host_reference(item.row_id,item.lease_token,"completed:x"); s.complete_inbox(item.row_id,item.lease_token); assert s.status()["inbox"]["completed"]==1
    assert s.enqueue_outbox(request_id="r1",recipients=["a@example.com"],subject="Re",body="ok"); assert not s.enqueue_outbox(request_id="r1",recipients=["a@example.com"],subject="Re",body="ok")
    out=s.claim_outbox(); assert out and out.request_id=="r1"; s.complete_outbox(out.row_id,out.lease_token); assert s.status()["outbox"]["completed"]==1


def test_the_first_runtime_record_written_under_a_key_stays(tmp_path):
    s = EmailStore(tmp_path)
    assert s.put_first('event', {'reply_targets': [1]})
    assert not s.put_first('event', {'reply_targets': [2], 'gmail_connection': {'id': 'later'}})
    assert s.get('event') == {'reply_targets': [1]}
