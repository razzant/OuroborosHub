"""Shared helpers for the table_tools skill: JSON output, path policy, loading.

Every entry point goes through :func:`resolve_path` so file access stays inside
the Ouroboros working directories, and through :func:`emit` so the agent always
receives machine-readable output it can answer from without guessing.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import pathlib
import re
import sys
import tempfile

MAX_ROWS_DEFAULT = 20000


def use_utf8_streams() -> None:
    """Pin stdout and stderr to UTF-8 so output survives any host code page.

    On Windows a piped child encodes its streams with the ANSI code page of
    the host locale (cp1251 on Russian Windows, cp1252 on Western ones), while
    the Ouroboros host decodes them as UTF-8: Cyrillic turns into U+FFFD, and
    a code page without the character raises UnicodeEncodeError instead.
    ``backslashreplace`` keeps even a lone surrogate (an undecodable file
    name) as a ``\\udcXX`` escape — still valid JSON text — instead of
    crashing. A replaced stream without ``reconfigure`` is left alone.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def emit(payload: dict) -> None:
    """Print one JSON object on stdout, encoded as UTF-8.

    The output is valid UTF-8 once :func:`use_utf8_streams` has run; both
    entry points call it before doing anything else, on every host locale.
    """
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


def fail(message: str, *, hint: str = "", status: str = "error", code: int = 2) -> None:
    payload = {"status": status, "error": message}
    if hint:
        payload["hint"] = hint
    emit(payload)
    raise SystemExit(code)


def skill_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def state_dir() -> pathlib.Path:
    raw = os.environ.get("OUROBOROS_SKILL_STATE_DIR", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser().resolve()
    return skill_dir() / "_state"


def data_root() -> pathlib.Path | None:
    """Derive the Ouroboros data root from the injected state dir.

    The host sets ``OUROBOROS_SKILL_STATE_DIR`` to ``<data>/state/skills/<name>``,
    so the data root is three levels up. Returns ``None`` when the layout does
    not match (then only the state dir and the temp dir are reachable).
    """
    sd = state_dir()
    if sd.parent.name == "skills" and sd.parent.parent.name == "state":
        return sd.parent.parent.parent
    return None


def allowed_roots() -> list[pathlib.Path]:
    roots: list[pathlib.Path] = [state_dir()]
    root = data_root()
    if root is not None:
        roots += [
            root / "uploads",
            root / "task_results",
            root / "task_drives",
            # Both Deliverables layouts are real: cloud installs keep it inside
            # the data root (/data/Deliverables), desktop installs beside it
            # (~/Ouroboros/Deliverables). Non-existent ones never match.
            root / "Deliverables",
            root.parent / "Deliverables",
        ]
    roots.append(pathlib.Path(tempfile.gettempdir()).resolve())
    seen: list[pathlib.Path] = []
    for candidate in roots:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _contained(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_path(raw: str, *, must_exist: bool) -> pathlib.Path:
    """Resolve *raw* and refuse anything outside the allowed working roots."""
    if not str(raw).strip():
        fail("empty path")
    path = pathlib.Path(str(raw)).expanduser()
    try:
        path = path.resolve()
    except OSError as exc:
        fail(f"cannot resolve path: {exc}")
    roots = allowed_roots()
    if not any(_contained(path, root) for root in roots):
        fail(
            f"path outside the allowed working directories: {path}",
            hint="allowed roots: " + ", ".join(str(r) for r in roots),
        )
    if must_exist and not path.is_file():
        fail(f"file not found: {path}")
    return path


def add_isolated_site_packages() -> None:
    """Make this skill's reviewed isolated dependencies importable."""
    env_root = skill_dir() / ".ouroboros_env"
    if not env_root.is_dir():
        return
    patterns = (
        "python/lib/python*/site-packages",
        "python/Lib/site-packages",
        "lib/python*/site-packages",
    )
    for pattern in patterns:
        for candidate in sorted(env_root.glob(pattern)):
            entry = str(candidate)
            if candidate.is_dir() and entry not in sys.path:
                sys.path.insert(0, entry)


def require(module: str, package: str):
    add_isolated_site_packages()
    try:
        return __import__(module)
    except ImportError as exc:
        fail(
            f"dependency '{package}' is not installed for this skill ({exc})",
            hint="install the skill dependencies from the Skills panel, then retry",
            status="dependency_missing",
            code=3,
        )


# --------------------------------------------------------------------------- #
# Format detection
# --------------------------------------------------------------------------- #

_XLSX_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def detect_format(path: pathlib.Path) -> str:
    """Return 'csv' or 'xlsx'; refuse formats this skill cannot honestly read."""
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError as exc:
        fail(f"cannot read file: {exc}", status="unreadable")
    suffix = path.suffix.lower()
    if head.startswith(_OLE2_MAGIC):
        fail(
            "this is a legacy binary Excel file (.xls), not .xlsx",
            hint="re-save the file as .xlsx or CSV and send it again",
            status="legacy_xls_format",
            code=5,
        )
    if head.startswith(_XLSX_MAGIC):
        return "xlsx"
    if suffix in (".xlsx", ".xlsm"):
        fail(
            f"file has an Excel extension but is not a valid .xlsx container: {path.name}",
            hint="the file may be corrupt or renamed; ask for the original file",
            status="unsupported_format",
            code=5,
        )
    return "csv"


# --------------------------------------------------------------------------- #
# Value typing
# --------------------------------------------------------------------------- #

_DATE_RE = re.compile(
    r"^(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?$"
)
_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def to_number(raw) -> float | None:
    """Best-effort numeric read of a cell. Returns None when it is not a number.

    Handles ``1 234,56`` (space thousands + decimal comma) and ``1,234.56``.
    Percentages and currency strings are deliberately NOT coerced: silently
    reinterpreting them would risk reporting a number the table never stated.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    if not _NUM_RE.match(text):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def classify(value) -> tuple[str, object]:
    """Return (kind, normalized) where kind is empty|number|date|text."""
    if value is None:
        return "empty", None
    if isinstance(value, (_dt.datetime, _dt.date)):
        return "date", value.isoformat()
    if isinstance(value, bool):
        return "text", str(value)
    if isinstance(value, (int, float)):
        return "number", float(value)
    text = str(value).strip()
    if not text:
        return "empty", None
    number = to_number(text)
    if number is not None:
        return "number", number
    if _DATE_RE.match(text):
        return "date", text
    return "text", text


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def normalize_headers(raw_row: list) -> tuple[list[str], list[str]]:
    """Turn a raw first row into usable column names, reporting what was odd."""
    notes: list[str] = []
    names: list[str] = []
    for index, cell in enumerate(raw_row, start=1):
        text = "" if cell is None else str(cell).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            text = f"column_{index}"
            notes.append(f"column {index} had an empty header, named '{text}'")
        base = text
        counter = 2
        while text in names:
            text = f"{base}_{counter}"
            counter += 1
        if text != base:
            notes.append(f"duplicate header '{base}' renamed to '{text}'")
        names.append(text)
    return names, notes


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in (";", ",", "\t", "|")}
        best = max(counts, key=lambda key: counts[key])
        return best if counts[best] else ","


BLANK_SCAN_ALLOWANCE = 200


def _scan_rows(row_iter, max_rows: int, empty_message: str) -> dict:
    """Consume at most the header plus ``max_rows`` non-empty data rows.

    One extra row is probed only to learn whether the table continues past the
    cap; the iterator is then abandoned, so the tail is never read. The number
    of PHYSICAL pulls is bounded regardless of content: blank rows draw from a
    fixed ``BLANK_SCAN_ALLOWANCE`` budget, so an empty-row tail (common in
    formatted XLSX) cannot make the scan walk the whole file. When the scan
    stopped early the true total is unknown: ``total_rows`` is None.
    """
    header_row: list | None = None
    rows: list[list] = []
    truncated = False
    physical_budget = max_rows + BLANK_SCAN_ALLOWANCE + 2
    physical = 0
    for raw_row in row_iter:
        physical += 1
        if physical > physical_budget:
            truncated = True
            break
        row = list(raw_row)
        if not any(str(cell).strip() for cell in row if cell is not None):
            continue
        if header_row is None:
            header_row = row
            continue
        if len(rows) >= max_rows:
            truncated = True
            break
        rows.append(row)
    if header_row is None:
        if truncated:
            # The physical budget ran out before any non-empty row appeared.
            # The remainder was never read, so "empty" would be a guess.
            fail(
                f"no header row found within the scan budget of "
                f"{physical_budget} physical rows; the rest of the file was "
                "not scanned",
                hint=(
                    "the file starts with a very long run of blank rows; "
                    "remove them so the header appears near the top"
                ),
                status="header_not_found_within_scan_budget",
                code=6,
            )
        fail(empty_message, status="empty_table", code=6)
    return {
        "header_row": header_row,
        "rows": rows,
        "truncated": truncated,
        "total_rows": None if truncated else len(rows),
    }


def _read_csv_rows(path: pathlib.Path, max_rows: int) -> dict:
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                delimiter = _sniff_delimiter(handle.read(8192))
                handle.seek(0)
                try:
                    scanned = _scan_rows(csv.reader(handle, delimiter=delimiter),
                                         max_rows, "the file has no readable rows")
                except csv.Error as exc:
                    fail(f"cannot parse this CSV file: {exc}", status="unreadable")
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            fail(f"cannot read file: {exc}", status="unreadable")
        scanned["meta"] = {"encoding": encoding, "delimiter": delimiter}
        return scanned
    fail("cannot decode this CSV file with utf-8, cp1251 or latin-1",
         status="unreadable")
    raise AssertionError("unreachable")  # pragma: no cover


def list_sheets(path: pathlib.Path) -> list[dict]:
    """Cheap workbook overview: every sheet with its declared size."""
    openpyxl = require("openpyxl", "openpyxl")
    try:
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured JSON
        fail(f"cannot open the workbook: {exc}", status="unreadable")
    sheets = []
    try:
        for sheet in book.worksheets:
            sheets.append({
                "name": sheet.title,
                "rows": int(sheet.max_row or 0),
                "columns": int(sheet.max_column or 0),
            })
    finally:
        book.close()
    return sheets


def _read_xlsx_rows(path: pathlib.Path, sheet_name: str, max_rows: int) -> dict:
    openpyxl = require("openpyxl", "openpyxl")
    try:
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        fail(f"cannot open the workbook: {exc}", status="unreadable")
    try:
        names = [sheet.title for sheet in book.worksheets]
        if sheet_name and sheet_name not in names:
            fail(
                f"sheet '{sheet_name}' not found",
                hint="available sheets: " + ", ".join(names),
                status="sheet_not_found",
                code=7,
            )
        target = book[sheet_name] if sheet_name else book.worksheets[0]
        scanned = _scan_rows(target.iter_rows(values_only=True), max_rows,
                             f"sheet '{target.title}' has no readable rows")
        scanned["meta"] = {"sheet": target.title, "sheets": names}
        return scanned
    finally:
        book.close()


def load_table(path: pathlib.Path, *, sheet: str = "",
               max_rows: int = MAX_ROWS_DEFAULT) -> dict:
    """Load one table as {columns, rows (list[dict]), total_rows, truncated, meta}.

    The scan is genuinely bounded: reading stops right after ``max_rows`` data
    rows (plus a one-row probe that only detects truncation). ``scanned_rows``
    counts the rows the results describe, and ``total_rows`` is None when the
    scan was truncated — the true total is unknown without a full pass.
    """
    kind = detect_format(path)
    if kind == "xlsx":
        loaded = _read_xlsx_rows(path, sheet, max_rows)
    else:
        if sheet:
            fail("a CSV file has no sheets; drop the --sheet argument")
        loaded = _read_csv_rows(path, max_rows)
    columns, header_notes = normalize_headers(loaded["header_row"])
    records: list[dict] = []
    ragged = 0
    for raw_row in loaded["rows"]:
        if len(raw_row) > len(columns):
            ragged += 1
        records.append({
            name: (raw_row[index] if index < len(raw_row) else None)
            for index, name in enumerate(columns)
        })
    if ragged:
        header_notes.append(
            f"{ragged} row(s) had more cells than headers; extra cells were ignored"
        )
    return {
        "format": kind,
        "columns": columns,
        "header_notes": header_notes,
        "rows": records,
        "total_rows": loaded["total_rows"],
        "scanned_rows": len(records),
        "truncated": loaded["truncated"],
        "meta": loaded["meta"],
    }


def profile_columns(table: dict) -> list[dict]:
    """Per-column type mix, empties and a small sample — the orientation payload."""
    profiles = []
    for name in table["columns"]:
        counts = {"number": 0, "date": 0, "text": 0, "empty": 0}
        numbers: list[float] = []
        samples: list[str] = []
        for record in table["rows"]:
            kind, value = classify(record.get(name))
            counts[kind] += 1
            if kind == "number":
                numbers.append(float(value))
            if kind != "empty" and len(samples) < 3:
                text = str(value)
                samples.append(text if len(text) <= 60 else text[:57] + "...")
        filled = counts["number"] + counts["date"] + counts["text"]
        dominant = "empty"
        if filled:
            dominant = max(("number", "date", "text"), key=lambda kind: counts[kind])
        profile = {
            "name": name,
            "dominant_type": dominant,
            "mixed_types": sum(
                1 for kind in ("number", "date", "text") if counts[kind]
            ) > 1,
            "empty_cells": counts["empty"],
            "type_counts": counts,
            "samples": samples,
        }
        if numbers:
            profile["numeric"] = {
                "count": len(numbers),
                "min": min(numbers),
                "max": max(numbers),
                "sum": sum(numbers),
                "mean": sum(numbers) / len(numbers),
            }
        profiles.append(profile)
    return profiles
