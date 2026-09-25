import json
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("site_generator", ROOT / "pipeline" / "site.py")
site_generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site_generator)


class VirtualSourceFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = ROOT / "tests" / "fixtures" / "virtual_source.json"
        cls.snapshot = json.loads(fixture.read_text(encoding="utf-8"))

    def test_unknown_source_is_discovered_and_normalized(self):
        payload = site_generator.snapshot_payload(self.snapshot)

        self.assertEqual(site_generator.source_keys(self.snapshot), ["github", "podcast"])
        self.assertIn("podcast", payload["sources"])
        self.assertEqual(payload["source_meta"][1]["label"], "Podcast")
        item = payload["sources"]["podcast"][0]
        self.assertEqual(item["title"], "Knowledge pipelines in production")
        self.assertEqual(item["summary"], "A conversation about reliable knowledge pipelines.")
        self.assertEqual(item["published_at"], "2026-09-24T10:00:00Z")
        self.assertEqual(item["section"], "podcast")
        self.assertIn(item, payload["sections"]["podcast"])

    def test_unknown_source_participates_in_cross_source_matching(self):
        cross = site_generator.build_cross(self.snapshot)

        self.assertEqual(len(cross), 1)
        self.assertEqual(cross[0]["sources"], ["github", "podcast"])


if __name__ == "__main__":
    unittest.main()
