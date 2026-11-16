// Pure check of app/words.js (no DOM, no host): prints one JSON object of results. Argument 1: the words.js module path.
// Freeze the display year: the old-year assertions must not depend on the test machine's clock.
const ActualDate = Date;
globalThis.Date = class extends ActualDate { constructor(...args) { super(...(args.length ? args : ['2026-11-16T12:00:00Z'])); } };
const W = await import(process.argv[2]);
const out = {};
const src = (extra) => Object.assign({ kind: 'source_author', label: 'Maya Ode', model: 'model-x', basis: 'source' }, extra || {});
const tr = { operator: 'op-a', tool: 'importer 1.2', task_id: 't-9' };
const rec = (author, extra) => Object.assign({ author, ts: '2026-11-16T10:20:00+00:00', byline: 'core words', transfer: tr, written_at: '2026-03-03' }, extra || {});

// ---- a source author: author, model and basis, each stated or "not stated" -----------------------------------------------
out.known_source = W.byline(src(), '', rec(src()));
out.known_importer = W.byline(src({ basis: 'importer' }), '', rec(src({ basis: 'importer' })));
out.unknown_author_known_model = W.byline(src({ label: null }), '', rec(src({ label: null })));
out.known_author_unknown_model = W.byline(src({ model: null }), '', rec(src({ model: null })));
const none = { kind: 'source_author', label: null, model: null, basis: 'unknown' };
out.all_unknown = W.byline(none, '', rec(none));
out.all_unknown_without_rec = W.byline(none);
out.blank_label = W.byline(src({ label: '   ' }), '', rec(src()));
out.source_author_without_rec = W.byline(src());
// not the contract's shape: no basis word (or one this widget does not know)
out.no_basis_core_words = W.byline({ kind: 'source_author' }, '', { byline: 'source_author (core words)', transfer: tr });
out.no_basis_no_words = W.byline({ kind: 'source_author' }, '', { transfer: tr });
out.label_only_no_basis_no_words = W.byline({ kind: 'source_author', label: 'Maya Ode' }, '', { transfer: tr });
out.future_basis_core_words = W.byline(src({ basis: 'operator' }), '', rec(src({ basis: 'operator' })));
out.future_basis_no_words = W.byline(src({ basis: 'operator' }), '', { transfer: tr });
out.byline_only = W.byline(undefined, '', { byline: 'source_author (Maya; model m; signed in the source)', transfer: tr });
// ---- the usual labels, and the historical helper against the same writer kind carrying a transfer ------------------------
out.old_helper = W.byline({ kind: 'legacy_helper' }, '', { ts: '2026-06-01T00:00:00+00:00' });
out.old_helper_with_core_byline = W.byline({ kind: 'legacy_helper' }, '', { byline: 'legacy_helper (old_consolidator)', ts: '2026-06-01T00:00:00+00:00' });
out.old_helper_no_rec = W.byline({ kind: 'legacy_helper' });
// the signature an old installation's helper carries (the shape the importer of the earlier dialogue file wrote)
const oldWriter = { kind: 'legacy_helper', writer: 'old_consolidator', attribution: 'retelling by Light, not lived' };
const oldByline = 'legacy_helper (recorded writer "old_consolidator"; attribution "retelling by Light, not lived")';
out.old_helper_with_fields = W.byline(oldWriter, '', { byline: oldByline, ts: '2026-06-01T00:00:00+00:00' });
out.moved_helper = W.byline(oldWriter, '', rec(oldWriter, { byline: oldByline }));
out.moved_helper_without_core_byline = W.byline(oldWriter, '', rec(oldWriter, { byline: undefined }));
out.moved_helper_with_operation = W.byline({ kind: 'legacy_helper', writer: 'w', operation: 'fold', attribution: 'a' }, '', rec({ kind: 'legacy_helper' }));
out.moved_helper_only_kind = W.byline({ kind: 'legacy_helper' }, '', rec({ kind: 'legacy_helper' }, { byline: 'legacy_helper (nothing more recorded)' }));
out.moved_helper_blank_fields = W.byline({ kind: 'legacy_helper', writer: '  ', attribution: 7 }, '', rec({ kind: 'legacy_helper' }));
out.moved_host = W.byline({ kind: 'host', operation: 'coverage-gap', attribution: 'source-gap fact' }, '', rec({ kind: 'host' }));
out.moved_host_only_kind = W.byline({ kind: 'host' }, '', rec({ kind: 'host' }));
out.host_without_transfer = W.byline({ kind: 'host', operation: 'coverage-gap', attribution: 'source-gap fact' }, '', { byline: 'host (x)' });
out.mind = W.byline({ kind: 'mind' }, 'root, task t1', { byline: 'mind (root, task t1)' });
out.helper_light = W.byline({ kind: 'helper' }, '', {});
out.helper_light_signed = W.byline({ kind: 'helper' }, 'Light', { byline: 'helper (Light)' });
out.host = W.byline({ kind: 'host' });
out.unknown_kind = W.byline({ kind: 'future_kind' });
out.none = W.byline(null);
// ---- what is a transfer, and its two dates and its operator, apart --------------------------------------------------------
out.is_transfer = [W.isTransfer(rec(src())), W.isTransfer({}), W.isTransfer({ transfer: null }), W.isTransfer(null), W.isTransfer({ transfer: 'x' })];
out.dates = W.transferDates(rec(src()));
out.dates_unknown_written = W.transferDates(rec(src(), { written_at: null }));
out.dates_datetime = W.transferDates(rec(src(), { written_at: '2025-12-31T23:30:00+00:00' }));
out.dates_plain = W.transferDates({ ts: '2026-11-16T10:20:00+00:00' });
out.words = W.transferWords(rec(src()));
out.words_unknown = W.transferWords(rec(src(), { transfer: { operator: null, tool: null, task_id: null } }));
out.words_plain = W.transferWords({});
// ---- periods ------------------------------------------------------------------------------------------------------------
const span = { start: '2026-03-01T00:00:00+00:00', end: '2026-03-05T00:00:00+00:00', incomplete: false };
out.p_stated = W.periodWords({ period: 'x', source_period: span, period_basis: 'stated', period_bases: ['stated'] }, false);
out.p_stated_incomplete = W.periodWords({ period: 'x', source_period: Object.assign({}, span, { incomplete: true }), period_basis: 'stated' }, false);
out.p_mixed_both = W.periodWords({ period: 'x', source_period: span, period_basis: 'mixed', period_bases: ['block', 'stated'] }, false);
out.p_mixed_three = W.periodWords({ period: 'x', source_period: span, period_basis: 'mixed', period_bases: ['rows', 'block', 'stated'] }, false);
out.p_mixed_stated = W.periodWords({ period: 'x', source_period: span, period_basis: 'mixed', period_bases: ['rows', 'stated'] }, false);
out.p_mixed_rows_block = W.periodWords({ period: 'x', source_period: span, period_basis: 'mixed', period_bases: ['rows', 'block'] }, false);
out.p_mixed_without_bases = W.periodWords({ period: 'x', source_period: span, period_basis: 'mixed' }, false);
out.p_mixed_incomplete = W.periodWords({ period: 'x', source_period: Object.assign({}, span, { incomplete: true }), period_basis: 'mixed', period_bases: ['block', 'stated'] }, false);
out.p_block = W.periodWords({ period: 'x', source_period: span, period_basis: 'block' }, false);
out.p_rows = W.periodWords({ period: 'x', source_period: span, period_basis: 'rows' }, false);
out.p_unstated = W.periodWords({ period: 'period not stated by its source', source_period: null, period_basis: 'stated', period_bases: ['stated'] }, false);
out.p_unknown = W.periodWords({ period: 'period unknown', source_period: null, period_basis: 'rows' }, false);
out.p_none = W.periodWords({ ts: '2026-11-16T10:20:00+00:00' }, false);
out.p_long_stated = W.periodWords({ period: 'x', source_period: span, period_basis: 'stated' }, true);
const address = { kind: 'record', id: 't1', field: ['author'] };
const ref = { ref: 'value', address };
const excerpt = (preview) => ({ ref: 'text', preview, chars: 500, address });
out.projected_author = W.byline(src({ label: excerpt('Maya'), model: excerpt('model') }));
out.ref_author = W.byline(src({ label: ref, model: ref }));
out.author_object_ref = W.byline(ref);
out.ref_transfer = W.transferWords({ transfer: ref });
out.projected_transfer = W.transferWords({ transfer: { operator: excerpt('op'), tool: ref, task_id: excerpt('task') } });
const signature = { preview: 'source_author (Maya', chars: 900, derived_from: address };
out.derived_signature = W.byline(null, '', { byline: signature });
out.signature_address = W.metadataAddress(signature);
out.ref_signature = W.byline(null, '', { byline: ref });
const controlled = src({ label: 'Maya\u202e\u2066', model: 'm\u200b', basis: 'importer' });
out.safe_metadata = W.byline(controlled);
out.original_label = controlled.label;
out.format_only = W.metadataValue("\ufeff");
out.safe_signature = W.byline(null, '', { byline: 'name\u2069\u202c' });
const past = { source_period: { start: '2025-11-10', end: '2025-11-14' }, period_basis: 'stated' };
out.past_row = W.periodWords(past, false);
out.past_title = W.periodWords(past, true);
out.past_open = W.spanWords({ start: '2025-11-10' }, false);
// Nested projections must keep the core's period, including a projected period's own address.
const corePeriod = '2026-07-01 – 2026-07-31 (stated by its source)';
const july = { start: '2026-07-01', end: '2026-07-31', incomplete: false };
const periodEntry = (source_period, period = corePeriod) => ({ source_period, period, period_basis: 'stated' });
out.nested_periods = {};
for (const [name, source_period] of Object.entries({
  start_ref: { ...july, start: ref }, end_ref: { ...july, end: ref },
  start_preview: { ...july, start: excerpt('2026-07-01') }, end_preview: { ...july, end: excerpt('2026-07-31') },
  incomplete_ref: { ...july, incomplete: ref }, incomplete_preview: { ...july, incomplete: excerpt('false') },
  start_and_flag_ref: { ...july, start: ref, incomplete: ref }, whole_ref: ref,
  basic_iso: { ...july, start: '20260701', end: '20260731' },
  basic_iso_one_side: { ...july, start: '20260701' },
  week_iso: { ...july, start: '2026-W27-3', end: '2026-W31-5' },
})) { out.nested_periods[name] = [false, true].map((long) => W.periodWords(periodEntry(source_period), long)); }
const periodAddress = { kind: 'record', id: 't1', field: ['metadata', 'stated_period'] };
const periodPreview = { preview: corePeriod, chars: 500, derived_from: periodAddress };
const periodRef = { ref: 'value', address: periodAddress };
out.period_preview = W.periodWords(periodEntry({ ...july, start: ref }, periodPreview));
out.period_ref = W.periodWords(periodEntry({ ...july, incomplete: ref }, periodRef));
out.period_ref_address = W.metadataAddress(periodRef);
out.period_preview_address = W.metadataAddress(periodPreview);
out.partial_start = W.periodWords(periodEntry({ start: '2026-07-01', end: null, incomplete: true }));
out.partial_end = W.periodWords(periodEntry({ start: null, end: '2026-07-31', incomplete: false }));
out.complete_false = W.periodWords(periodEntry(july));
const offsetPeriod = '2026-07-01 21:30 → 2026-08-01 02:30 (stated by its source)';
out.offset_period = [false, true].map((long) => W.periodWords(periodEntry({
  start: '2026-07-02T00:30:00+03:00', end: '2026-07-31T23:30:00-03:00', incomplete: false,
}, offsetPeriod), long));
out.utc_period = W.periodWords(periodEntry({ start: '2026-07-01T21:30:00+00:00', end: '2026-07-31T02:30:00Z', incomplete: false }));
out.empty_span = W.periodWords(periodEntry({ start: null, end: null }, 'period unknown'));
const controls = 'A\n\r\t\u0000\u0085\u2028\u2029\u202eB';
out.all_controls = W.metadataText(controls);
out.controls_preview = W.metadataValue(excerpt(controls));
out.original_controls = controls;
console.log(JSON.stringify(out));
