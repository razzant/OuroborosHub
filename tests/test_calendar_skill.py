"""Hub-level entry point for the calendar skill's offline tests.

The skill declares isolated dependencies (icalendar, cryptography); the hub CI
image may not have them, so the suite is skipped there and runs wherever the
interpreter does (e.g. a venv with the skill's dependencies installed).
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
    import test_calendar  # noqa: E402  (skills/calendar/tests/test_calendar.py)
    import test_calendar_review  # noqa: E402  (review-round regression tests)
    import test_calendar_round2  # noqa: E402
    import test_calendar_batch3  # noqa: E402
    import test_calendar_round3  # noqa: E402
    import test_calendar_round4  # noqa: E402
    import test_calendar_batch5  # noqa: E402
    import test_calendar_round5  # noqa: E402
    standard_tests.addTests(loader.loadTestsFromModule(test_calendar))
    standard_tests.addTests(loader.loadTestsFromModule(test_calendar_review))
    standard_tests.addTests(loader.loadTestsFromModule(test_calendar_round2))
    standard_tests.addTests(loader.loadTestsFromModule(test_calendar_batch3))
    standard_tests.addTests(loader.loadTestsFromModule(test_calendar_round3))
    standard_tests.addTests(loader.loadTestsFromModule(test_calendar_round4))
    standard_tests.addTests(loader.loadTestsFromModule(test_calendar_batch5))
    standard_tests.addTests(loader.loadTestsFromModule(test_calendar_round5))
    return standard_tests
