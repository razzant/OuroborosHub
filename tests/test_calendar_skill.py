"""Hub-level entry point for the calendar skill's offline tests.

The skill declares isolated dependencies (icalendar, cryptography); an
interpreter without them skips this entry point. CI installs them and runs the
skill suite directly in the ``calendar-skill`` job, so a skip here hides nothing.
"""

import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_TESTS = os.path.join(os.path.dirname(HERE), "skills", "calendar", "tests")


def _deps_available() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("icalendar", "cryptography", "dateutil"))


def load_tests(loader, standard_tests, pattern):
    if not _deps_available():
        class Skipped(unittest.TestCase):
            @unittest.skip("calendar skill deps (icalendar, cryptography) not installed in this interpreter")
            def test_skipped(self):
                pass
        standard_tests.addTests(loader.loadTestsFromTestCase(Skipped))
        return standard_tests
    if SKILL_TESTS not in sys.path:
        sys.path.insert(0, SKILL_TESTS)
    # Every module in skills/calendar/tests, so a new regression file cannot be left out of this entry point.
    for name in sorted(n[:-3] for n in os.listdir(SKILL_TESTS) if n.startswith("test_") and n.endswith(".py")):
        standard_tests.addTests(loader.loadTestsFromModule(importlib.import_module(name)))
    return standard_tests
