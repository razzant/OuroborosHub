---
name: table_tools
description: Inspect, filter, sort, group, and aggregate CSV and XLSX tables while reporting truncation and non-numeric values honestly.
version: 0.1.1
type: script
runtime: python3
permissions: [fs]
when_to_use: The user attached a CSV or XLSX file and asks to inspect, summarize, filter, compare, group, sort, or calculate from its data.
timeout_sec: 120
scripts:
  - name: read_table.py
    description: Inspect a CSV or XLSX table, its sheets, columns, types, missing values, and sample rows.
  - name: query_table.py
    description: Filter, select, sort, group, and aggregate rows from a CSV or XLSX table.
install_specs:
  - kind: pip
    package: openpyxl
---

# Table tools

This skill reads tabular files and answers from their data by inspecting,
filtering, comparing, grouping, sorting, and calculating.

## Inspect first

`skill_exec(skill="table_tools", script="scripts/read_table.py", args=["<file path>"])`

The result describes columns, row count, dominant column types, empty cells,
mixed types, sample values, and the first rows (`--head N`). Start by briefly
describing the table's structure and notable gaps before calculating.

For a workbook with several sheets, the overview lists every sheet. If the
intended sheet is unclear, ask the user and pass `--sheet "Sheet name"` rather
than silently choosing. Empty and duplicate headers are normalized and reported
in `header_notes`. Empty cells remain empty; they are never converted to zero.

## Query the data

`skill_exec(skill="table_tools", script="scripts/query_table.py", args=["<file path>", "--where", "Region==West", "--agg", "sum,mean", "--on", "Revenue", "--group-by", "Month"])`

Available operations include `--select`, repeatable `--where` conditions,
`--where-mode all|any`, `--group-by`, `--agg count,sum,mean,min,max` with
`--on`, `--sort "Column:desc"`, and `--limit`.

Results always report rows before and after filtering. Numeric aggregates also
report non-numeric and empty cells that were excluded, so the answer never
pretends mixed data was uniformly numeric.

## Large tables

The scripts scan a bounded number of rows (`--max-rows`, default 20000):
reading stops right after the cap instead of walking the whole file.
`scanned_rows` counts the rows the results describe, `truncated` says whether
the table continues past them, and `total_rows` is `null` when the scan was
truncated (the true total is unknown without a full pass). Aggregates computed
on a truncated scan carry `partial: true`. If a result covers only a slice,
state that clearly and offer a narrower query.

## Output and delivery convention

The normal result is structured JSON for answering in chat. If the task also
requires a generated file, write it into the task-visible working directory
supplied for the task, such as its task drive or Deliverables path, and deliver
it with `send_file`. NEVER construct download URLs.

## Boundaries

- This version reads tables; full Excel-style editing and workbook authoring
  are out of scope.
- Complex pivot tables and BI reports are out of scope; grouping and basic
  aggregates are supported.
- Legacy binary `.xls` is refused explicitly. Ask for `.xlsx` or CSV instead.
- File access is restricted to Ouroboros working directories, task results and
  drives, the skill state directory, Deliverables, uploads, and the system
  temporary directory.
