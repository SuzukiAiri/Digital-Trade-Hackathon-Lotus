import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from rdtii_tool.mapping.discovery_tags import (
    DiscoveryTagBaselineError,
    load_legal_inventory_registry,
    normalize_law_number_identities,
)
from rdtii_tool.mapping.final_audit import FinalAuditActionBatch, _apply_actions, _build_canonical_rows, _deduplicate_rows, _human_review_cell, _load_final_audit_review_rows, _load_prefinal_rows
from rdtii_tool.mapping.models import AtomicEvidenceRecord
from rdtii_tool.mapping.offline_export import export_completed_submissions
from rdtii_tool.mapping.submission_exporter import SUBMISSION_COLUMNS, _submission_row
from rdtii_tool.mapping.submission_rationale import render_submission_rationale, validate_submission_rationale


class ReleaseReadinessTest(unittest.TestCase):
    def _project_with_inventory(self) -> Path:
        root = Path(tempfile.mkdtemp())
        inventory = root / "Singapore, Malaysia, Australia, Legal Inventory.csv"
        inventory.write_text(
            "\n".join(
                [
                    "country,Act.and.or.practice,Coverage,Timeframe,References,cluster,Region,Cov.Name,name,policy.description",
                    "Singapore,Personal Data Protection Act 2012,,,,,South-East Asia,Horizontal,Data protection,Privacy framework",
                    "Singapore,Cybersecurity Act 2018,,,,,South-East Asia,Horizontal,Cybersecurity,Cybersecurity framework",
                    "Australia,Example Records Act 2000,,,,,Pacific,Sectoral,Data retention,Record retention requirement",
                    "Malaysia,Services Tax Act (Act 807) 2018,,,,,South-East Asia,Horizontal,Data retention,Minimum period of data retention requirements",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.addCleanup(shutil.rmtree, root)
        return root

    def test_legal_inventory_csv_is_readable(self):
        registry = load_legal_inventory_registry(self._project_with_inventory())
        self.assertEqual(registry.header[0], "country")
        self.assertEqual(registry.summary()["recognized_rows"], 4)

    def test_baseline_missing_fails_export(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        with self.assertRaises(DiscoveryTagBaselineError):
            load_legal_inventory_registry(root)

    def test_same_country_indicator_law_matches_known(self):
        registry = load_legal_inventory_registry(self._project_with_inventory())
        match = registry.match(economy="Singapore", indicator_id="P7-I1", law_name="Personal Data Protection Act 2012")
        self.assertEqual(match.discovery_tag, "KNOWN")

    def test_same_law_different_indicator_is_new(self):
        registry = load_legal_inventory_registry(self._project_with_inventory())
        match = registry.match(economy="Singapore", indicator_id="P7-I2", law_name="Personal Data Protection Act 2012")
        self.assertEqual(match.discovery_tag, "NEW")

    def test_baseline_outside_law_is_new(self):
        registry = load_legal_inventory_registry(self._project_with_inventory())
        match = registry.match(economy="Australia", indicator_id="P7-I3", law_name="Unlisted Act 2024")
        self.assertEqual(match.discovery_tag, "NEW")

    def test_malaysia_act_number_variants_match_known(self):
        registry = load_legal_inventory_registry(self._project_with_inventory())
        for law_name in ("807", "Act 807", "Services Tax Act (Act 807) 2018"):
            match = registry.match(economy="Malaysia", indicator_id="P7-I3", law_name=law_name)
            self.assertEqual(match.discovery_tag, "KNOWN", law_name)
            self.assertIn("act:807", match.baseline_match_key)

    def test_law_number_identity_normalizes_malaysia_tokens(self):
        self.assertIn("act:807", normalize_law_number_identities("ACT NO. 807"))
        self.assertIn("pua:66/2023", normalize_law_number_identities("P.U. (A) 66/2023"))
        self.assertIn("amendment:a1728", normalize_law_number_identities("A1728"))

    def test_exporter_uses_record_discovery_tag(self):
        record = AtomicEvidenceRecord(
            evidence_id="e1",
            economy="Singapore",
            indicator_id="P7-I1",
            document_id="doc",
            law_name="Personal Data Protection Act 2012",
            article="s. 1",
            location_reference="s. 1",
            focal_quote="personal data protection rule",
            mapping_rationale="accepted",
            source_url="https://example.invalid",
            coverage="horizontal",
            sector="",
            discovery_tag="KNOWN",
            mapper_task_id="task",
            citation_status="verified",
            decision="accepted",
        )
        source_index = SimpleNamespace(document_meta=lambda document_id: None)
        row = _submission_row(record, source_index)
        self.assertEqual(row["Discovery Tag"], "KNOWN")

    def test_long_internal_rationale_is_rendered_publicly(self):
        rationale = (
            "Reviewer accepted because E1 and E2 satisfy each candidate element; "
            "the focal clause and supporting evidence show no exclusion is triggered. "
            "This model schema explanation is intentionally too long. "
        ) * 3
        rendered = render_submission_rationale(
            "P7-I3",
            "Example Act",
            "s. 1",
            "The operator must keep records for at least 5 years after the transaction.",
            {"retention_periods": [{"value": "5", "unit": "years", "trigger_event": "the transaction"}]},
            "retention_periods=5 years transaction",
            rationale,
        )
        self.assertLessEqual(len(rendered), 300)
        self.assertEqual(validate_submission_rationale(rendered), [])
        self.assertIn("5 years", rendered)
        self.assertNotRegex(rendered, r"\bE\d+\b|Reviewer|focal clause|supporting evidence|schema")

    def test_short_public_rationale_is_stable(self):
        rationale = "The provision requires operators to retain transaction records for at least five years."
        rendered = render_submission_rationale("P7-I3", "Example Act", "s. 1", "text", {}, "", rationale)
        self.assertEqual(rendered, rationale)

    def test_renderer_does_not_change_indicator_or_decision(self):
        rendered = render_submission_rationale("P6-I2", "Example Act", "s. 1", "Records must be kept in Australia.", {}, "", "")
        self.assertEqual(validate_submission_rationale(rendered), [])
        self.assertIn("domestic storage", rendered)

    def test_validator_rejects_bad_public_rationale(self):
        self.assertIn("mapping_rationale_over_300_chars", validate_submission_rationale("A" * 301 + "."))
        self.assertTrue(any(reason.startswith("mapping_rationale_internal_term") for reason in validate_submission_rationale("Reviewer accepted E1.")))
        self.assertTrue(validate_submission_rationale("Accept: the clause is valid."))
        self.assertTrue(validate_submission_rationale("The provision requires year of assessment to retain records."))
        self.assertTrue(validate_submission_rationale("The provision requires records for at least not less than seven years."))

    def test_submission_columns_are_fixed_13_columns(self):
        self.assertEqual(
            SUBMISSION_COLUMNS,
            [
                "Economy",
                "Law Name",
                "Law Number / Ref",
                "Last Amended",
                "Indicator ID",
                "Article / Section",
                "Discovery Tag",
                "Location Reference",
                "Verbatim Snippet",
                "Mapping Rationale",
                "Source URL",
                "Confidence",
                "Notes",
            ],
        )

    def test_human_reject_is_removed_before_final_audit_rows(self):
        human = {"rk1": {"review_key": "rk1", "decision": "accept"}}
        rows = _build_canonical_rows(
            "singapore",
            [{"review_key": "rk2", "Economy": "Singapore", "Indicator ID": "P7-I1", "Law Name": "X", "Article / Section": "s. 1", "Verbatim Snippet": "text", "Source URL": "https://example.invalid"}],
            {"rk2": {"review_key": "rk2", "decision": "reject"}},
        )
        self.assertEqual(rows, [])
        rows = _build_canonical_rows(
            "singapore",
            [{"review_key": "rk1", "Economy": "Singapore", "Indicator ID": "P7-I1", "Law Name": "X", "Article / Section": "s. 1", "Verbatim Snippet": "text", "Source URL": "https://example.invalid"}],
            human,
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["human_protected"])

    def test_prefinal_rows_ignore_existing_final_rows(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        submission = root / "outputs" / "corpus" / "singapore" / "submission"
        submission.mkdir(parents=True)
        stale_row = {column: "" for column in SUBMISSION_COLUMNS}
        stale_row.update({"Economy": "Singapore", "Law Name": "Stale Act", "Indicator ID": "P7-I1"})
        current_row = {column: "" for column in SUBMISSION_COLUMNS}
        current_row.update({"Economy": "Singapore", "Law Name": "Current Act", "Indicator ID": "P7-I1"})
        (submission / "final_rows.jsonl").write_text(json.dumps({"row": stale_row}, ensure_ascii=False) + "\n", encoding="utf-8")
        (submission / "singapore_p6_p7.json").write_text(json.dumps([current_row], ensure_ascii=False), encoding="utf-8")

        source_rows, canonical_rows = _load_prefinal_rows(submission, "singapore", {}, scope_slug="p6_p7")

        self.assertEqual(source_rows[0]["Law Name"], "Current Act")
        self.assertEqual(canonical_rows[0]["row"]["Law Name"], "Current Act")
        self.assertTrue((submission / "final_audit_input_p6_p7.json").exists())

    def test_prefinal_rows_prefer_stable_input_snapshot(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        submission = root / "outputs" / "corpus" / "singapore" / "submission"
        submission.mkdir(parents=True)
        snapshot_row = {column: "" for column in SUBMISSION_COLUMNS}
        snapshot_row.update({"Economy": "Singapore", "Law Name": "Snapshot Act", "Indicator ID": "P7-I1"})
        current_row = {column: "" for column in SUBMISSION_COLUMNS}
        current_row.update({"Economy": "Singapore", "Law Name": "Current Act", "Indicator ID": "P7-I1"})
        (submission / "final_audit_input_p6_p7.json").write_text(json.dumps([snapshot_row], ensure_ascii=False), encoding="utf-8")
        (submission / "singapore_p6_p7.json").write_text(json.dumps([current_row], ensure_ascii=False), encoding="utf-8")

        source_rows, canonical_rows = _load_prefinal_rows(submission, "singapore", {}, scope_slug="p6_p7")

        self.assertEqual(source_rows[0]["Law Name"], "Snapshot Act")
        self.assertEqual(canonical_rows[0]["row"]["Law Name"], "Snapshot Act")

    def test_final_audit_review_rows_prefer_stable_snapshot(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        submission = root / "outputs" / "corpus" / "singapore" / "submission"
        submission.mkdir(parents=True)
        (submission / "human_review.jsonl").write_text(json.dumps({"reason": "current"}, ensure_ascii=False) + "\n", encoding="utf-8")
        (submission / "final_audit_human_review_input_p6_p7.jsonl").write_text(
            json.dumps({"reason": "snapshot"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        rows = _load_final_audit_review_rows(submission, scope_slug="p6_p7")

        self.assertEqual(rows, [{"reason": "snapshot"}])

    def test_final_audit_action_schema_has_no_free_objects(self):
        schema = FinalAuditActionBatch.model_json_schema()

        def walk(node, path="root"):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, path)
                for key, value in node.items():
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")

        walk(schema)

    def test_actions_are_applied_to_canonical_rows(self):
        row = {column: "" for column in SUBMISSION_COLUMNS}
        row.update(
            {
                "Economy": "Singapore",
                "Law Name": "Personal Data Protection Act 2012",
                "Indicator ID": "P7-I1",
                "Article / Section": "s. 1",
                "Verbatim Snippet": "personal data protection rule",
                "Source URL": "https://example.invalid",
            }
        )
        canonical = _build_canonical_rows("singapore", [row], {})
        final_rows, human_review = _apply_actions(canonical, [], [{"target_audit_key": canonical[0]["audit_key"], "action": "reject", "reason": "bad"}])
        self.assertEqual(final_rows, [])
        self.assertEqual(human_review, [])

    def test_repeated_action_is_already_applied(self):
        row = {column: "" for column in SUBMISSION_COLUMNS}
        row.update({"Economy": "Singapore", "Law Name": "X", "Indicator ID": "P7-I1", "Article / Section": "s. 1", "Verbatim Snippet": "text", "Source URL": "https://example.invalid"})
        canonical = _build_canonical_rows("singapore", [row], {})
        action = {"target_audit_key": canonical[0]["audit_key"], "action": "repair_fields", "reason": "fix", "corrected_fields": [{"name": "Notes", "value": "coverage=horizontal"}]}
        final_rows, human_review, report = _apply_actions(canonical, [], [action, action], return_report=True)
        self.assertEqual(len(final_rows), 1)
        self.assertEqual(human_review, [])
        self.assertEqual([item["status"] for item in report], ["applied", "already_applied"])

    def test_action_after_reject_is_already_resolved(self):
        row = {column: "" for column in SUBMISSION_COLUMNS}
        row.update({"Economy": "Singapore", "Law Name": "X", "Indicator ID": "P7-I1", "Article / Section": "s. 1", "Verbatim Snippet": "text", "Source URL": "https://example.invalid"})
        canonical = _build_canonical_rows("singapore", [row], {})
        target = canonical[0]["audit_key"]
        actions = [
            {"target_audit_key": target, "action": "reject", "reason": "bad"},
            {"target_audit_key": target, "action": "human_review", "reason": "late"},
        ]
        final_rows, human_review, report = _apply_actions(canonical, [], actions, return_report=True)
        self.assertEqual(final_rows, [])
        self.assertEqual(human_review, [])
        self.assertEqual([item["status"] for item in report], ["applied", "already_resolved"])

    def test_merge_source_later_action_does_not_create_review(self):
        row1 = {column: "" for column in SUBMISSION_COLUMNS}
        row1.update({"Economy": "Singapore", "Law Name": "X", "Indicator ID": "P7-I1", "Article / Section": "s. 1", "Verbatim Snippet": "text one", "Source URL": "https://example.invalid"})
        row2 = dict(row1)
        row2["Verbatim Snippet"] = "text two"
        canonical = _build_canonical_rows("singapore", [row1, row2], {})
        source, dest = canonical[1]["audit_key"], canonical[0]["audit_key"]
        actions = [
            {"target_audit_key": source, "action": "merge", "merge_into_audit_key": dest, "reason": "dup"},
            {"target_audit_key": source, "action": "human_review", "reason": "late"},
        ]
        final_rows, human_review, report = _apply_actions(canonical, [], actions, return_report=True)
        self.assertEqual(len(final_rows), 1)
        self.assertEqual(human_review, [])
        self.assertEqual(report[1]["status"], "already_resolved")

    def test_target_not_found_is_warning_not_human_review(self):
        final_rows, human_review, report = _apply_actions([], [], [{"target_audit_key": "missing", "action": "reject", "reason": "bad"}], return_report=True)
        self.assertEqual(final_rows, [])
        self.assertEqual(human_review, [])
        self.assertEqual(report[0]["status"], "warning")

    def test_metadata_only_human_review_action_keeps_row(self):
        row = {column: "" for column in SUBMISSION_COLUMNS}
        row.update({"Economy": "Malaysia", "Law Name": "807", "Indicator ID": "P7-I3", "Article / Section": "s. 1", "Verbatim Snippet": "records must be kept for seven years", "Source URL": "https://example.invalid"})
        canonical = _build_canonical_rows("malaysia", [row], {})
        action = {"target_audit_key": canonical[0]["audit_key"], "action": "human_review", "reason": "The Law Name field contains only the Act number and duplicates Law Number / Ref. The exact statutory title must be verified."}
        final_rows, human_review, report = _apply_actions(canonical, [], [action], return_report=True)
        self.assertEqual(len(final_rows), 1)
        self.assertEqual(human_review, [])
        self.assertEqual(report[0]["detail"], "metadata_completion_required")

    def test_empty_human_review_object_is_not_created(self):
        final_rows, human_review, _ = _apply_actions([], [{"reason": "needs review"}], [], return_report=True)
        self.assertEqual(final_rows, [])
        self.assertEqual(human_review, [])

    def test_human_review_cell_serializes_nested_json(self):
        cell = _human_review_cell({"row": {"Law Name": "X"}})
        self.assertEqual(json.loads(cell), {"row": {"Law Name": "X"}})

    def test_dedup_happens_before_global_audit(self):
        row = {column: "" for column in SUBMISSION_COLUMNS}
        row.update(
            {
                "Economy": "Singapore",
                "Law Name": "Personal Data Protection Act 2012",
                "Indicator ID": "P7-I1",
                "Article / Section": "s. 1",
                "Verbatim Snippet": "personal data protection rule",
                "Source URL": "https://example.invalid",
            }
        )
        canonical = _build_canonical_rows("singapore", [row, row], {})
        deduped, groups = _deduplicate_rows(canonical)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(len(groups), 1)

    def test_dedup_keeps_same_article_different_indicator(self):
        row = {column: "" for column in SUBMISSION_COLUMNS}
        row.update({"Economy": "Singapore", "Law Name": "X", "Indicator ID": "P6-I2", "Article / Section": "Reg. 30", "Verbatim Snippet": "records kept in Singapore for 5 years", "Source URL": "https://example.invalid"})
        other = dict(row)
        other["Indicator ID"] = "P7-I3"
        canonical = _build_canonical_rows("singapore", [row, other], {})
        deduped, groups = _deduplicate_rows(canonical)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(groups, [])

    def test_export_submission_does_not_require_api_key(self):
        root = self._project_with_inventory()
        submission = root / "outputs" / "corpus" / "singapore" / "submission"
        submission.mkdir(parents=True)
        row = {column: "" for column in SUBMISSION_COLUMNS}
        row.update(
            {
                "Economy": "Singapore",
                "Law Name": "Personal Data Protection Act 2012",
                "Indicator ID": "P7-I1",
                "Discovery Tag": "NEW",
                "Source URL": "https://example.invalid",
            }
        )
        (submission / "final_rows.jsonl").write_text(json.dumps({"row": row}, ensure_ascii=False) + "\n", encoding="utf-8")
        previous_key = os.environ.pop("OPENAI_API_KEY", None)
        try:
            summary = export_completed_submissions(root, ["singapore"], {6, 7})
        finally:
            if previous_key:
                os.environ["OPENAI_API_KEY"] = previous_key
        self.assertEqual(summary["economies"]["singapore"]["known"], 1)

    def test_cli_help_imports(self):
        result = subprocess.run(
            [sys.executable, "-m", "rdtii_tool", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("export-submission", result.stdout)


if __name__ == "__main__":
    unittest.main()
