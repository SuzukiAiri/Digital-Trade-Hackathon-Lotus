from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleasePackageTest(unittest.TestCase):
    def test_cli_help_and_demo_help(self):
        help_result = subprocess.run(
            [sys.executable, "-m", "rdtii_tool", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("map-rdtii", help_result.stdout)
        demo_result = subprocess.run(
            [sys.executable, "-m", "rdtii_tool", "demo", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("--mode", demo_result.stdout)

    def test_offline_demo_exports_schema_valid_csv_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                [sys.executable, "-m", "rdtii_tool", "demo", "--mode", "offline", "--output-dir", tmp],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            csv_path = Path(tmp) / "rdtii_demo.csv"
            json_path = Path(tmp) / "rdtii_demo.json"
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            json_rows = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(len(csv_rows), len(json_rows))
            self.assertEqual(len(csv_rows), 3)
            self.assertTrue(all(row["Article / Section"] for row in csv_rows))
            self.assertTrue(all(row["Verbatim Snippet"] for row in csv_rows))
            self.assertTrue(all(row["Source URL"] for row in csv_rows))

    def test_final_submit_csv_json_parity_and_tags(self):
        for economy in ("singapore", "australia", "malaysia"):
            directory = ROOT / "outputs" / "corpus" / economy / "final_submit"
            csv_path = next(directory.glob("*.csv"))
            json_path = next(directory.glob("*.json"))
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            data = json.loads(json_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(len(rows), len(data))
            self.assertTrue({row["Discovery Tag"] for row in rows} <= {"NEW", "KNOWN"})
            self.assertTrue(all(row["Article / Section"] for row in rows))
            self.assertTrue(all(row["Verbatim Snippet"] for row in rows))
            self.assertTrue(all(row["Source URL"] for row in rows))

    def test_release_verifier_passes(self):
        result = subprocess.run(
            [sys.executable, "scripts/verify_release.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("Release verification: PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
