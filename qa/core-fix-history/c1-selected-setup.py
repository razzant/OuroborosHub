def test_long_iso_period_is_bounded_in_every_api_reader_and_its_original_is_readable(tmp_path):
    from tests.test_memory_reader_bounds import whole, beside_text, SMALL, PER_ENTRY
    shared.world(tmp_path)
    start = '2026-07-01T00:00:00.' + '1' * 100000 + '+00:00'
    stated = {**STATED, 'start': start}
    record = _moved(tmp_path, 't-long-period', 'Words.', room='1', stated=stated)
    record['ts'] = '2026-08-10T00:00:00+00:00'
    store = ChronicleStore(tmp_path)
    with store.log_path.open('a', encoding='utf-8') as journal:
        journal.write(json.dumps({'kind': 'transaction', 'schema': 1, 'records': [record], 'scan_state': {}}) + '\n')
    store.index_path.unlink()
    return record
