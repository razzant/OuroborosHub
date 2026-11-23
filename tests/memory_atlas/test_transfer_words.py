"""Words of a transferred record, checked without a host or a browser: app/words.js run by Node over synthetic facts.

The core's contract (author kind ``source_author``, ``metadata.transfer``, ``written_at``, ``stated_period``, period
basis ``stated`` and the ``period_bases`` list) arrives as the facts of the record entry the core answers; the widget
builds its words only from those facts. Nothing here starts a server or a browser. ``node`` must be on PATH.

This module imports neither pytest nor conftest: pytest collects its plain ``test_*`` functions, and a run where pytest
is not installed calls the same functions directly from a plain script, which is a standalone assertion run and never
reported as a pytest result.
"""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAYLOAD = HERE.parents[1] / "skills" / "memory-atlas"
DRIVER = HERE / "js" / "words_driver.mjs"


@functools.lru_cache(maxsize=None)
def _got() -> dict:
    node = shutil.which("node")
    assert node, "node is required to check app/words.js (install Node 20 or newer)"
    with tempfile.TemporaryDirectory(prefix="atl-words-") as tmp:
        module = Path(tmp) / "words.mjs"
        module.write_text((PAYLOAD / "app" / "words.js").read_text(encoding="utf-8"), encoding="utf-8")
        run = subprocess.run([node, str(DRIVER), str(module)], capture_output=True, text=True, timeout=60, check=False)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_a_source_author_names_author_model_and_basis():
    got = _got()
    assert got["known_source"] == "Written by Maya Ode · model model-x · signed in the source"
    assert got["known_importer"] == "Written by Maya Ode · model model-x · stated by the importer"
    assert got["source_author_without_rec"] == got["known_source"]


def test_every_unknown_part_is_said_one_by_one():
    got = _got()
    assert got["unknown_author_known_model"] == "Author not stated · model model-x · signed in the source"
    assert got["known_author_unknown_model"] == "Written by Maya Ode · model not stated · signed in the source"
    assert got["blank_label"] == "Author not stated · model model-x · signed in the source"
    everything = "Author not stated · model not stated · attribution not stated"
    assert got["all_unknown"] == everything and got["all_unknown_without_rec"] == everything   # nothing silently dropped


def test_a_source_author_that_is_not_the_contracts_shape_falls_back_without_inventing():
    got = _got()
    # no basis word (or one this widget does not know): the core's own signature words when it gave some ...
    assert got["no_basis_core_words"] == "Core signature: source_author (core words)"
    assert got["future_basis_core_words"] == "Core signature: core words"
    # ... else what is there, each missing part named as not stated
    assert got["no_basis_no_words"] == "Author not stated · model not stated · attribution not stated"
    assert got["label_only_no_basis_no_words"] == "Written by Maya Ode · model not stated · attribution not stated"
    assert got["future_basis_no_words"] == "Written by Maya Ode · model model-x · attribution not stated"
    assert got["byline_only"] == "Core signature: source_author (Maya; model m; signed in the source)"   # an entry with a signature, no author


def test_a_transferred_helper_is_not_labelled_as_the_earlier_dialogue_file():
    got = _got()
    assert got["old_helper"] == "Imported from the earlier dialogue file"   # the historical helper, as always
    assert got["old_helper_with_core_byline"] == "Imported from the earlier dialogue file"   # the core's byline never replaces it
    assert got["old_helper_no_rec"] == "Imported from the earlier dialogue file"
    assert got["old_helper_with_fields"] == "Imported from the earlier dialogue file"   # even with the very fields a transfer would carry
    # the actual old-writer shape, carried by a transfer: worded from its structured facts, never the raw kind name
    human = "Retold by an earlier helper · recorded writer old_consolidator · retelling by Light, not lived"
    assert got["moved_helper"] == human and got["moved_helper_without_core_byline"] == human
    assert got["moved_helper_with_operation"] == "Retold by an earlier helper · recorded writer w · operation fold · a"
    assert got["moved_helper_only_kind"] == "Retold by an earlier helper"   # nothing it does not hold
    assert got["moved_helper_blank_fields"] == "Retold by an earlier helper"
    assert got["moved_host"] == "Recorded by the host · operation coverage-gap · source-gap fact"
    assert got["moved_host_only_kind"] == "Recorded by the host"
    assert got["host_without_transfer"] == "Recorded by the host"   # an ordinary host record is not reworded
    assert "dialogue file" not in got["moved_helper"] and "legacy_helper" not in got["moved_helper"]


def test_the_usual_labels_are_kept():
    got = _got()
    assert got["mind"] == "By Ouroboros, root, task t1"
    assert got["helper_light"] == "Draft by a helper" and got["helper_light_signed"] == "Draft by Light"
    assert got["host"] == "Recorded by the host"
    assert got["unknown_kind"] == "future_kind" and got["none"] == "Author not recorded"


def test_only_the_transfer_marker_makes_a_transfer():
    got = _got()
    assert got["is_transfer"] == [True, False, False, False, False]
    assert got["dates_plain"] == "" and got["words_plain"] == ""


def test_source_and_local_dates_stay_apart():
    got = _got()
    assert got["dates"] == "Written 3 Mar 2026 (as its source states) · transferred here 16 Nov 2026, 10:20"
    assert got["dates_unknown_written"] == "Written date not stated (as its source states) · transferred here 16 Nov 2026, 10:20"
    assert got["dates_datetime"].startswith("Written 31 Dec 2025, 23:30 (as its source states)")
    assert got["words"] == "by op-a with importer 1.2, task t-9"
    assert got["words_unknown"] == "by operator not recorded with tool not recorded, task not recorded"


def test_periods_name_their_basis_and_never_borrow_a_date():
    got = _got()
    assert got["p_stated"] == "1–5 Mar (stated by its source)"
    assert got["p_stated_incomplete"] == "1–5 Mar (incomplete, stated by its source)"
    assert got["p_mixed_both"] == "1–5 Mar (partly block period, partly stated by its source)"
    assert got["p_mixed_three"] == "1–5 Mar (partly block period, partly stated by its source)"   # local rows say nothing
    assert got["p_mixed_stated"] == "1–5 Mar (partly stated by its source)"
    assert got["p_mixed_rows_block"] == "1–5 Mar (partly block period)"
    assert got["p_mixed_without_bases"] == "1–5 Mar (partly block period)"   # an answer without period_bases: as before
    assert got["p_mixed_incomplete"] == "1–5 Mar (incomplete, partly block period, partly stated by its source)"
    assert got["p_block"] == "1–5 Mar (block period)" and got["p_rows"] == "1–5 Mar"
    assert got["p_unstated"] == "period not stated by its source"   # the core's words, never the local date
    assert got["p_unknown"] == "" and got["p_none"] is None
    assert got["p_long_stated"] == "1–5 March (stated by its source)"


def test_projected_metadata_is_known_and_keeps_its_existing_address():
    got = _got()
    assert got["projected_author"] == "Written by Maya… (excerpt) · model model… (excerpt) · signed in the source"
    assert got["ref_author"] == "Written by recorded (open full value) · model recorded (open full value) · signed in the source"
    assert got["ref_transfer"] == "transfer details recorded (open full value)"
    assert got["projected_transfer"] == "by op… (excerpt) with recorded (open full value), task task… (excerpt)"
    assert got["derived_signature"] == "Core signature: source_author (Maya… (excerpt)"
    assert got["signature_address"] == {"kind": "record", "id": "t1", "field": ["author"]}
    assert got["ref_signature"] == "Core signature: recorded (open full value)"
    assert got["author_object_ref"] == "Author recorded (open full value)"


def test_metadata_controls_are_visible_without_changing_original_values():
    got = _got()
    assert got["safe_metadata"] == r"Written by Maya\u{202E}\u{2066} · model m\u{200B} · stated by the importer"
    assert got["format_only"] == r"\u{FEFF}"
    assert got["original_label"] == "Maya\u202e\u2066"
    assert got["safe_signature"] == r"Core signature: name\u{2069}\u{202C}"


def test_other_year_periods_show_the_year_in_rows_and_headings():
    got = _got()
    assert got["past_row"] == "10–14 Nov 2025 (stated by its source)"
    assert got["past_title"] == "10–14 November 2025 (stated by its source)"
    assert got["past_open"] == "from 10 Nov 2025"


def test_nested_period_projections_and_core_iso_forms_keep_the_core_words():
    got = _got()
    expected = "2026-07-01 – 2026-07-31 (stated by its source)"
    for case, words in got["nested_periods"].items():
        assert words == [expected, expected], case
    assert got["period_preview"] == expected + "… (excerpt)"
    assert got["period_ref"] == "recorded (open full value)"
    assert got["period_ref_address"] == got["period_preview_address"] == {
        "kind": "record", "id": "t1", "field": ["metadata", "stated_period"]}


def test_actual_partial_unknown_and_false_periods_remain_distinct():
    got = _got()
    assert got["partial_start"] == "from 1 Jul (incomplete, stated by its source)"
    assert got["partial_end"] == "until 31 Jul (stated by its source)"
    assert got["complete_false"] == "1–31 Jul (stated by its source)"
    assert got["empty_span"] == ""


def test_stated_offset_days_keep_core_utc_words_and_utc_dates_still_shorten():
    got = _got()
    expected = "2026-07-01 21:30 → 2026-08-01 02:30 (stated by its source)"
    assert got["offset_period"] == [expected, expected]
    assert got["utc_period"] == got["complete_false"] == "1–31 Jul (stated by its source)"


def test_metadata_controls_and_separators_are_visible_in_strings_and_previews():
    got = _got()
    expected = r"A\u{A}\u{D}\u{9}\u{0}\u{85}\u{2028}\u{2029}\u{202E}B"
    assert got["all_controls"] == expected
    assert got["controls_preview"] == expected + "… (excerpt)"
    assert got["original_controls"] == "A\n\r\t\x00\x85\u2028\u2029\u202eB"
