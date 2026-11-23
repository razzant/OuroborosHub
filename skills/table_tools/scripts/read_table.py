#!/usr/bin/env python3
"""Describe a tabular file so the agent can orient before doing any arithmetic.

Reports sheets, columns, row counts, per-column type mix, empty cells and a
small sample of rows. Never invents values: empty stays empty, and a truncated
scan is reported as truncated.
"""

from __future__ import annotations

import argparse

import table_common as common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Describe a CSV or .xlsx table")
    parser.add_argument("path", help="path to the CSV or .xlsx file")
    parser.add_argument("--sheet", default="", help="Excel sheet name (xlsx only)")
    parser.add_argument("--head", type=int, default=10,
                        help="how many sample rows to return (default 10)")
    parser.add_argument("--max-rows", type=int, default=common.MAX_ROWS_DEFAULT,
                        help="max data rows to scan (default 20000)")
    parser.add_argument("--sheets-only", action="store_true",
                        help="list workbook sheets without reading any of them")
    parser.add_argument("--no-sample", action="store_true",
                        help="omit the sample rows, return structure only")
    return parser


def sample_rows(table: dict, limit: int) -> list[dict]:
    rows = []
    for record in table["rows"][:max(0, limit)]:
        rows.append({
            name: common.classify(record.get(name))[1]
            for name in table["columns"]
        })
    return rows


def main() -> int:
    common.use_utf8_streams()
    args = build_parser().parse_args()
    path = common.resolve_path(args.path, must_exist=True)
    if args.max_rows <= 0:
        common.fail("--max-rows must be positive")

    kind = common.detect_format(path)
    sheets = common.list_sheets(path) if kind == "xlsx" else []

    if args.sheets_only:
        if kind != "xlsx":
            common.fail("--sheets-only applies to .xlsx files; a CSV has no sheets")
        common.emit({
            "status": "ok",
            "file": path.name,
            "format": kind,
            "sheets": sheets,
            "note": "pick a sheet with --sheet, then read it",
        })
        return 0

    table = common.load_table(path, sheet=args.sheet, max_rows=args.max_rows)
    ambiguous = bool(sheets) and not args.sheet and len(sheets) > 1

    payload = {
        "status": "ok",
        "file": path.name,
        "format": table["format"],
        "columns": table["columns"],
        "column_count": len(table["columns"]),
        "total_rows": table["total_rows"],
        "scanned_rows": table["scanned_rows"],
        "truncated": table["truncated"],
        "header_notes": table["header_notes"],
        "column_profiles": common.profile_columns(table),
    }
    if sheets:
        payload["sheets"] = sheets
        payload["sheet"] = table["meta"].get("sheet", "")
        payload["sheet_ambiguous"] = ambiguous
        if ambiguous:
            payload["note"] = (
                "the workbook has several sheets and none was requested; the first "
                "one was read — confirm the intended sheet with the user before "
                "reporting numbers"
            )
    else:
        payload["csv"] = table["meta"]
    if table["truncated"]:
        payload["truncation_note"] = (
            f"this table is large: only the first {table['scanned_rows']} rows "
            "were scanned and the total is unknown without a full pass — "
            "say so and narrow the question"
        )
    if not args.no_sample:
        payload["sample_rows"] = sample_rows(table, args.head)
    common.emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
