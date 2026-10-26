#!/usr/bin/env python3
"""Compatibility CLI: canonical entries now generate the v1 catalog."""
from pathlib import Path
import sys

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_index import build_catalog, main, validate_catalog, _canonical_name  # noqa: F401

if __name__ == '__main__':
    raise SystemExit(main())
