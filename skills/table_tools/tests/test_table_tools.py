from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    if name != "table_common" and "table_common" not in sys.modules:
        _load("table_common")
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_header_normalization_and_numeric_classification_are_explicit():
    common = _load("table_common")
    names, notes = common.normalize_headers(["Name", "", "Name"])
    assert names == ["Name", "column_2", "Name_2"]
    assert len(notes) == 2
    assert common.to_number("1 234,56") == 1234.56
    assert common.classify("") == ("empty", None)


def test_filters_and_aggregates_report_excluded_values():
    query = _load("query_table")
    condition = query.parse_condition("Region:contains:west")
    assert query.matches({"Region": "Northwest"}, condition) is True
    result = query.aggregate(
        [{"Revenue": "10"}, {"Revenue": "bad"}, {"Revenue": ""}],
        ["count", "sum", "mean"],
        "Revenue",
    )
    assert result["count"] == 3
    assert result["sum"] == 10.0
    assert result["mean"] == 10.0
    assert result["non_numeric_cells_skipped"] == 1
    assert result["empty_cells"] == 1
    assert "partial" not in result
    partial = query.aggregate([{"Revenue": "10"}], ["sum"], "Revenue", partial=True)
    assert partial["partial"] is True


def test_bounded_scan_stops_reading_and_reports_truncation_honestly(tmp_path):
    common = _load("table_common")

    csv_path = tmp_path / "big.csv"
    lines = ["id,value"] + [f"{i},{i * 10}" for i in range(70)]
    csv_path.write_text("\n".join(lines), encoding="utf-8")

    table = common.load_table(csv_path, max_rows=1)
    assert table["truncated"] is True
    assert table["scanned_rows"] == 1
    assert table["total_rows"] is None
    assert len(table["rows"]) == 1

    # The row iterator is abandoned right after the truncation probe: at most
    # the header, the stored row and one probe row are ever pulled.
    pulled = 0

    def counting_rows():
        nonlocal pulled
        for index in range(70):
            pulled += 1
            yield ["head_a", "head_b"] if index == 0 else [str(index), "x"]

    scanned = common._scan_rows(counting_rows(), 1, "empty")
    assert scanned["truncated"] is True
    assert scanned["total_rows"] is None
    assert len(scanned["rows"]) == 1
    assert pulled <= 3

    # Blank rows cannot make the scan walk the whole file: physical pulls are
    # bounded by the fixed blank allowance even when every tail row is empty.
    blank_pulled = 0

    def blank_tail_rows():
        nonlocal blank_pulled
        blank_pulled += 1
        yield ["head_a", "head_b"]
        blank_pulled += 1
        yield ["1", "x"]
        for _ in range(5000):
            blank_pulled += 1
            yield ["", ""]

    blank = common._scan_rows(blank_tail_rows(), 1, "empty")
    assert blank["truncated"] is True
    assert blank["total_rows"] is None
    assert blank_pulled <= common.BLANK_SCAN_ALLOWANCE + 5

    # A modest blank PREFIX within the allowance still finds the header.
    def blank_prefix_rows():
        for _ in range(150):
            yield ["", ""]
        yield ["head_a", "head_b"]
        yield ["1", "x"]
        yield ["2", "y"]

    prefixed = common._scan_rows(blank_prefix_rows(), 10, "empty")
    assert prefixed["header_row"] == ["head_a", "head_b"]
    assert prefixed["truncated"] is False
    assert prefixed["total_rows"] == 2

    # An untruncated scan still reports an exact total.
    full = common.load_table(csv_path, max_rows=100)
    assert full["truncated"] is False
    assert full["total_rows"] == full["scanned_rows"] == 70


def test_header_beyond_scan_budget_is_reported_as_unscanned_not_empty(capsys):
    common = _load("table_common")

    # A header that sits past the physical budget is a refusal to guess, not
    # an "empty table": the scanner never saw the rest of the file.
    def late_header_rows():
        for _ in range(300):
            yield ["", ""]
        yield ["head_a", "head_b"]
        yield ["1", "x"]

    with pytest.raises(SystemExit):
        common._scan_rows(late_header_rows(), 10, "empty")
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "header_not_found_within_scan_budget"
    assert "not scanned" in payload["error"]


# --------------------------------------------------------------------------- #
# UTF-8 output regression (OuroborosHub issue #346)
# --------------------------------------------------------------------------- #
#
# On Windows a piped child writes stdout in the host's ANSI code page, while
# the Ouroboros host decodes it as UTF-8: Cyrillic columns became U+FFFD (or a
# UnicodeEncodeError crash on Western locales). The tests below force the same
# broken condition on any platform via PYTHONIOENCODING, so they fail on 0.1.0
# and pass once the scripts pin their streams to UTF-8.

_CODE_PAGES = ["cp1252", "cp1251"]


def _run_script(script: str, csv_path: Path, args: list, encoding: str):
    """Run an entry point in a child interpreter with a forced stdout code page.

    ``PYTHONIOENCODING`` reproduces what a piped Windows child does by default
    (the ANSI code page of the host locale) on any test platform;
    ``PYTHONUTF8=0`` keeps a developer's UTF-8 mode from hiding the bug. The
    state dir makes the CSV's own directory an allowed working root for the
    child, so the temp file location is portable and locale-independent.
    """
    env = {
        key: value for key, value in os.environ.items()
        if key not in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO")
    }
    env["OUROBOROS_SKILL_STATE_DIR"] = str(csv_path.parent)
    env["PYTHONIOENCODING"] = encoding
    env["PYTHONUTF8"] = "0"
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), str(csv_path), *args],
        capture_output=True,
        env=env,
        check=False,
    )


@pytest.mark.parametrize("encoding", _CODE_PAGES)
def test_read_table_reports_cyrillic_headers_as_utf8_json(tmp_path, encoding):
    state = tmp_path / "state"
    state.mkdir()
    csv_path = state / "cars.csv"
    rows = ["Марка,Количество,Масса_кг", "ГАЗ,3,1200.5", "УАЗ,2,1600"]
    csv_path.write_text("\n".join(rows), encoding="utf-8")

    result = _run_script("read_table.py", csv_path, [], encoding)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout.decode("utf-8"))
    assert payload["status"] == "ok"
    assert payload["columns"] == ["Марка", "Количество", "Масса_кг"]


@pytest.mark.parametrize("encoding", _CODE_PAGES)
def test_query_table_groups_a_cyrillic_column_as_utf8_json(tmp_path, encoding):
    state = tmp_path / "state"
    state.mkdir()
    csv_path = state / "cars.csv"
    rows = ["Марка,Количество", "ГАЗ,3", "УАЗ,2", "ГАЗ,1"]
    csv_path.write_text("\n".join(rows), encoding="utf-8")

    result = _run_script("query_table.py", csv_path,
                         ["--group-by", "Марка", "--agg", "count"], encoding)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout.decode("utf-8"))
    assert payload["status"] == "ok"
    assert payload["group_by"] == "Марка"
    assert [entry["group"] for entry in payload["groups"]] == ["ГАЗ", "УАЗ"]
    assert [entry["count"] for entry in payload["groups"]] == [2, 1]


@pytest.mark.parametrize("encoding", _CODE_PAGES)
def test_the_dash_in_the_excluded_values_warning_survives_as_utf8(tmp_path, encoding):
    state = tmp_path / "state"
    state.mkdir()
    csv_path = state / "plain.csv"
    rows = ["Region,Revenue", "West,10", "West,oops"]
    csv_path.write_text("\n".join(rows), encoding="utf-8")

    result = _run_script("query_table.py", csv_path,
                         ["--agg", "sum", "--on", "Revenue"], encoding)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout.decode("utf-8"))
    assert payload["aggregates"]["sum"] == 10.0
    assert "\u2014" in payload["aggregates"]["warning"]


@pytest.mark.parametrize("script,args", [
    ("read_table.py", ["--head", "Марка"]),
    ("query_table.py", ["--limit", "Марка"]),
])
@pytest.mark.parametrize("encoding", _CODE_PAGES)
def test_argparse_error_reports_cyrillic_on_stderr_as_utf8(tmp_path, script, args, encoding):
    state = tmp_path / "state"
    state.mkdir()
    csv_path = state / "cars.csv"
    csv_path.write_text("Марка,Количество\nГАЗ,3\n", encoding="utf-8")

    result = _run_script(script, csv_path, args, encoding)

    assert result.returncode == 2
    stderr = result.stderr.decode("utf-8")
    assert "Марка" in stderr
