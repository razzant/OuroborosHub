#!/usr/bin/env python3
"""Run this payload's focused unittest checks, optionally retaining JUnit XML."""
import argparse
from pathlib import Path
import time
import unittest
import xml.etree.ElementTree as ET


class Result(unittest.TextTestResult):
    def __init__(self, *args):
        super().__init__(*args)
        self.cases = []

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        self.cases.append((test, time.monotonic() - self.started))
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", type=Path)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent))
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    if args.junit:
        root = ET.Element("testsuite", tests=str(result.testsRun), failures=str(len(result.failures)),
                          errors=str(len(result.errors)), skipped=str(len(result.skipped)), name="github")
        for test, duration in result.cases:
            case = ET.SubElement(root, "testcase", name=test.id(), time=f"{duration:.6f}")
            for kind, rows in (("failure", result.failures), ("error", result.errors), ("skipped", result.skipped)):
                for failed, text in rows:
                    if failed is test or getattr(failed, "test_case", None) is test:
                        ET.SubElement(case, kind).text = text
        args.junit.parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(root).write(args.junit, encoding="utf-8", xml_declaration=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
