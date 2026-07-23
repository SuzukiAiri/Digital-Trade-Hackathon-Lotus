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

    def test_export_submission_preflights_before_writing(self):
        from rdtii_tool.mapping.offline_export import export_completed_submissions

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "final_rows\\.jsonl missing"):
                export_completed_submissions(project_root, ["singapore"], {6, 7})
            self.assertFalse((project_root / "outputs" / "final_submission").exists())

    def test_final_audit_missing_prefinal_input_fails_closed(self):
        from rdtii_tool.mapping.final_audit import run_final_audit

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "prefinal submission input missing"):
                run_final_audit(project_root, "singapore", {6, 7})
            self.assertFalse((project_root / "outputs" / "corpus" / "singapore" / "submission").exists())

    def test_final_audit_empty_source_rows_fails_closed(self):
        from rdtii_tool.mapping.final_audit import run_final_audit

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            submission_dir = project_root / "outputs" / "corpus" / "singapore" / "submission"
            submission_dir.mkdir(parents=True)
            (submission_dir / "singapore_p6_p7.json").write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "has no rows"):
                run_final_audit(project_root, "singapore", {6, 7})
            self.assertFalse((submission_dir / "final_audit_summary.json").exists())
            self.assertFalse((submission_dir / "final_audit_actions.jsonl").exists())

    def test_final_audit_empty_final_rows_fails_closed(self):
        from rdtii_tool.mapping.final_audit import run_final_audit

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            submission_dir = project_root / "outputs" / "corpus" / "singapore" / "submission"
            submission_dir.mkdir(parents=True)
            (submission_dir / "final_rows.jsonl").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "has no rows"):
                run_final_audit(project_root, "singapore", {6, 7})
            self.assertFalse((submission_dir / "final_audit_summary.json").exists())
            self.assertFalse((submission_dir / "final_audit_actions.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
