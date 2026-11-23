#!/usr/bin/env python3
"""Filter, sort, group and aggregate rows of a CSV or .xlsx table.

Every answer carries its own provenance: rows before and after the filter, how
many cells were non-numeric or empty in an aggregated column, and whether the
scan was truncated. That is what keeps the agent from reporting a number the
table never contained.
"""

from __future__ import annotations

import argparse

import table_common as common

_SYMBOL_OPS = (">=", "<=", "!=", "==", ">", "<")
_WORD_OPS = ("contains", "startswith", "notempty", "empty")
_AGGS = ("count", "sum", "mean", "min", "max")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query a CSV or .xlsx table")
    parser.add_argument("path", help="path to the CSV or .xlsx file")
    parser.add_argument("--sheet", default="", help="Excel sheet name (xlsx only)")
    parser.add_argument("--max-rows", type=int, default=common.MAX_ROWS_DEFAULT,
                        help="max data rows to scan (default 20000)")
    parser.add_argument("--select", default="",
                        help="comma-separated columns to return")
    parser.add_argument("--where", action="append", default=[],
                        help="condition, e.g. 'Region==Moscow', 'Sum>=100', "
                             "'Name:contains:ivan', 'Comment empty'")
    parser.add_argument("--where-mode", choices=("all", "any"), default="all",
                        help="combine conditions with AND (default) or OR")
    parser.add_argument("--group-by", default="", help="column to group by")
    parser.add_argument("--agg", default="",
                        help="comma-separated aggregates: count,sum,mean,min,max")
    parser.add_argument("--on", default="", help="numeric column for the aggregates")
    parser.add_argument("--sort", default="", help="'Column' or 'Column:desc'")
    parser.add_argument("--limit", type=int, default=50,
                        help="max rows/groups to return (default 50)")
    return parser


def parse_condition(spec: str) -> tuple[str, str, str]:
    text = spec.strip()
    if not text:
        common.fail("empty --where condition")
    parts = text.split(":")
    if len(parts) >= 2 and parts[1].strip().lower() in _WORD_OPS:
        op = parts[1].strip().lower()
        return parts[0].strip(), op, ":".join(parts[2:]).strip()
    for op in _SYMBOL_OPS:
        index = text.find(op)
        if index > 0:
            return text[:index].strip(), op, text[index + len(op):].strip()
    words = text.split(None, 2)
    if len(words) >= 2 and words[1].lower() in _WORD_OPS:
        op = words[1].lower()
        return words[0].strip(), op, (words[2].strip() if len(words) > 2 else "")
    common.fail(
        f"cannot parse --where '{spec}'",
        hint="use 'Column==value', 'Column>=10', 'Column:contains:text' "
             "or 'Column empty'",
    )
    raise AssertionError("unreachable")  # pragma: no cover


def _compare(left, right, op: str) -> bool:
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    return left <= right


def matches(record: dict, condition: tuple[str, str, str]) -> bool:
    column, op, raw = condition
    kind, value = common.classify(record.get(column))
    if op == "empty":
        return kind == "empty"
    if op == "notempty":
        return kind != "empty"
    if kind == "empty":
        return False
    if op in ("contains", "startswith"):
        text = str(value).lower()
        needle = raw.lower()
        return needle in text if op == "contains" else text.startswith(needle)
    target = common.to_number(raw)
    numeric = kind == "number" and target is not None
    if op in ("==", "!="):
        if numeric:
            equal = abs(float(value) - target) < 1e-9
        else:
            equal = str(value).strip().lower() == raw.strip().lower()
        return equal if op == "==" else not equal
    if numeric:
        return _compare(float(value), target, op)
    return _compare(str(value), raw, op)


def sort_key(record: dict, column: str):
    kind, value = common.classify(record.get(column))
    if kind == "empty":
        return (2, 0.0, "")
    if kind == "number":
        return (0, float(value), "")
    return (1, 0.0, str(value).lower())


def aggregate(rows: list[dict], aggs: list[str], on: str, *,
              partial: bool = False) -> dict:
    """Aggregate *rows*; ``partial=True`` marks results computed on a
    truncated scan, which describe only the scanned slice of the table."""
    result: dict = {"rows": len(rows)}
    if partial:
        result["partial"] = True
    if "count" in aggs:
        result["count"] = len(rows)
    numeric_aggs = [name for name in aggs if name != "count"]
    if not numeric_aggs:
        return result
    numbers: list[float] = []
    skipped = 0
    empty = 0
    for record in rows:
        kind, value = common.classify(record.get(on))
        if kind == "number":
            numbers.append(float(value))
        elif kind == "empty":
            empty += 1
        else:
            skipped += 1
    result["numeric_cells"] = len(numbers)
    result["non_numeric_cells_skipped"] = skipped
    result["empty_cells"] = empty
    if not numbers:
        result["note"] = (
            f"column '{on}' has no numeric values in the selected rows; "
            "no aggregate can be reported"
        )
        return result
    for name in numeric_aggs:
        if name == "sum":
            result["sum"] = sum(numbers)
        elif name == "mean":
            result["mean"] = sum(numbers) / len(numbers)
        elif name == "min":
            result["min"] = min(numbers)
        elif name == "max":
            result["max"] = max(numbers)
    if skipped:
        result["warning"] = (
            f"{skipped} non-numeric value(s) in '{on}' were excluded from the "
            "aggregates — mention this when reporting the result"
        )
    return result


def project(record: dict, columns: list[str]) -> dict:
    return {name: common.classify(record.get(name))[1] for name in columns}


def validate_columns(names: list[str], columns: list[str], label: str) -> None:
    for name in names:
        if name and name not in columns:
            common.fail(
                f"{label}: column '{name}' is not in this table",
                hint="available columns: " + ", ".join(columns),
                status="unknown_column",
                code=8,
            )


def main() -> int:
    common.use_utf8_streams()
    args = build_parser().parse_args()
    path = common.resolve_path(args.path, must_exist=True)
    if args.max_rows <= 0 or args.limit <= 0:
        common.fail("--max-rows and --limit must be positive")

    aggs = [item.strip().lower() for item in args.agg.split(",") if item.strip()]
    for name in aggs:
        if name not in _AGGS:
            common.fail(f"unknown aggregate '{name}'",
                        hint="supported: " + ", ".join(_AGGS))
    if any(name != "count" for name in aggs) and not args.on:
        common.fail("--agg sum/mean/min/max needs --on <numeric column>")

    if common.detect_format(path) == "xlsx" and not args.sheet:
        sheets = common.list_sheets(path)
        if len(sheets) > 1:
            common.fail(
                "this workbook has several sheets; computing on a guessed sheet "
                "would be dishonest",
                hint="ask the user which sheet, then pass --sheet: "
                     + ", ".join(sheet["name"] for sheet in sheets),
                status="sheet_required",
                code=7,
            )

    table = common.load_table(path, sheet=args.sheet, max_rows=args.max_rows)
    columns = table["columns"]
    conditions = [parse_condition(spec) for spec in args.where]
    validate_columns([condition[0] for condition in conditions], columns, "--where")
    selected = [item.strip() for item in args.select.split(",") if item.strip()]
    validate_columns(selected, columns, "--select")
    validate_columns([args.group_by], columns, "--group-by")
    validate_columns([args.on], columns, "--on")
    sort_column, _, sort_dir = args.sort.partition(":")
    sort_column = sort_column.strip()
    validate_columns([sort_column], columns, "--sort")

    rows = table["rows"]
    if conditions:
        checker = all if args.where_mode == "all" else any
        rows = [record for record in rows
                if checker(matches(record, condition) for condition in conditions)]

    payload: dict = {
        "status": "ok",
        "file": path.name,
        "columns": columns,
        "total_rows": table["total_rows"],
        "scanned_rows": table["scanned_rows"],
        "truncated": table["truncated"],
        "matched_rows": len(rows),
        "filters": [f"{col} {op} {value}".strip() for col, op, value in conditions],
        "header_notes": table["header_notes"],
    }
    if table["format"] == "xlsx":
        payload["sheet"] = table["meta"].get("sheet", "")
    if table["truncated"]:
        payload["truncation_note"] = (
            f"only the first {table['scanned_rows']} rows were scanned (the total "
            "is unknown without a full pass) — these results describe that slice, "
            "not the whole table"
        )

    if args.group_by:
        groups: dict[str, list[dict]] = {}
        for record in rows:
            key = common.classify(record.get(args.group_by))[1]
            groups.setdefault("" if key is None else str(key), []).append(record)
        wanted = aggs or ["count"]
        entries = [
            {"group": key,
             **aggregate(bucket, wanted, args.on, partial=table["truncated"])}
            for key, bucket in sorted(groups.items())
        ]
        payload["group_by"] = args.group_by
        payload["group_count"] = len(entries)
        payload["groups"] = entries[:args.limit]
        payload["groups_omitted"] = max(0, len(entries) - args.limit)
        common.emit(payload)
        return 0

    if aggs:
        payload["aggregates"] = aggregate(rows, aggs, args.on,
                                          partial=table["truncated"])
        payload["aggregated_column"] = args.on
        common.emit(payload)
        return 0

    if sort_column:
        rows = sorted(rows, key=lambda record: sort_key(record, sort_column),
                      reverse=sort_dir.strip().lower() in ("desc", "descending"))
        payload["sorted_by"] = args.sort
    output_columns = selected or columns
    payload["rows"] = [project(record, output_columns) for record in rows[:args.limit]]
    payload["rows_returned"] = len(payload["rows"])
    payload["rows_omitted"] = max(0, len(rows) - args.limit)
    common.emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
