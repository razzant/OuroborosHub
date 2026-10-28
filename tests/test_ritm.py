"""Ritm's packaged, model-visible trigger stays coupled to its no-effect design."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "ritm" / "SKILL.md"


class RitmPackageTests(unittest.TestCase):
    def test_catalog_delivers_the_manifest_and_no_executable_files(self):
        catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
        entries = [entry for entry in catalog["skills"] if entry["slug"] == "ritm"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["type"], "instruction")
        self.assertEqual([file["path"] for file in entry["files"]], ["SKILL.md"])
        self.assertEqual(entry["files"][0]["size"], SKILL.stat().st_size)
        self.assertEqual(entry["files"][0]["sha256"], hashlib.sha256(SKILL.read_bytes()).hexdigest())

    def test_manifest_exposes_on_request_and_conditional_integration(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        manifest = yaml.safe_load(text.split("---\n", 2)[1])
        self.assertEqual(manifest["type"], "instruction")
        self.assertEqual(manifest["permissions"], [])
        for forbidden in ("entry", "scheduled_tasks", "env_from_settings", "requested_keys"):
            self.assertNotIn(forbidden, manifest)
        trigger = manifest["when_to_use"]
        for fragment in ("сна", "шаг", "трениров", "самочувств", "истори", "whoop-health"):
            self.assertIn(fragment, trigger.lower())
        self.assertIn("сам упомянул WHOOP", trigger)
        self.assertIn("не хватает сна/тренировок", trigger)
        self.assertIn("не читает ранее сохранённые записи и аккаунты", trigger)
        self.assertIn("no tools or background work", manifest["model_experience"]["what_model_sees"])


if __name__ == "__main__":
    unittest.main()
