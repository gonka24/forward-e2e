"""Unit tests for evidence verification, JUnit XML validation, and fail-closed rules.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forward_e2e.suite.catalog import NATIVE_TASKS, get_task_by_id_or_alias
from forward_e2e.suite.evidence_model import EVIDENCE_MODEL_E2E, EVIDENCE_MODEL_IMMUTABLE
from forward_e2e.suite.models import EvidenceStatus, ExecutionStatus, SourceIdentity, TaskPlan, TOP_LEVEL_SCOPE
from tests.unit.runner.real_fixtures import (
    E1_TARGET_EPOCH,
    GONKA_PREPARED_SHA,
    GONKA_SOURCE_SHA,
    GONKA_TREE_SHA,
    LOCK_EXACT_E_CHECKPOINTS,
    MARKETPLACE_SHA,
    MARKETPLACE_TREE_SHA,
    OVERLAY_MANIFEST_SHA,
    REPO_ROOT,
    R2_SCENARIO,
    funded_claim_release_context,
    go_boundary_evidence,
    legacy_funded_claim_release_phase,
    ordered_vesting_addition_phase,
    real_b3_context,
    real_claim_expiry_positive_context,
    real_claim_expiry_zero_context,
    real_claim_settle_phase,
    real_lock_exact_e_context,
    real_network_unconfirmed_context,
    real_package_a_context,
    real_r1_scenario_context,
    real_terminal_release_repeat_context,
    real_terminal_release_repeat_phase,
    r2_addition,
    r2_releases,
    r2_stage,
    toy_claim_expiry_context,
    unified_funded_claim_release_phase,
    synthetic_late_completed_donation_phase,
    with_native_claimed,
    write_immutable_companion_files,
)
from tests.unit.runner.support.fakes import (
    network_manifest_document,
    source_immutability_document,
    write_suite_output,
)
from forward_e2e.suite.verifier import (
    PACKAGE_C_POLICY_CASES,
    PACKAGE_C_POLICY_TESTS,
    _validate_early_release_rejected,
    _validate_release_receipt,
    _validate_vesting_addition,
    _validate_r2_sequence,
    WASM_ABI_REQUIRED_CASES,
    evaluate_task_evidence,
    extract_and_validate_scenario_predicates,
    validate_evidence_scopes,
    verify_cargo_test_log,
    verify_go_boundary_report,
    verify_junit_xml,
    verify_live_context,
    verify_package_c_policy_log,
    verify_wasm_abi_report,
    verify_and_recalculate_suite,
    check_source_immutability_document,
)

# Composite Package A owns two named producer scenarios; the task id itself is
# never a scenario key, which is exactly why the scopes must be declared.
PACKAGE_A_SCOPES = ["r1-refund-e-plus-5", "r2-vested-gift"]


class SourceImmutabilityEvidenceTests(unittest.TestCase):
    def verify_document(self, record, *, network_manifest=None):
        if network_manifest is None and record.get("schema") == "a8.source-immutability-set/2":
            network_manifest = network_manifest_document()
        document = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
        payload = {"source": {
            "gonka_sha": GONKA_SOURCE_SHA,
            "marketplace_commit_sha": MARKETPLACE_SHA,
            "source_immutability_verdict": "UNCHANGED",
            "source_immutability_sha256": hashlib.sha256(document).hexdigest(),
        }}
        network_bytes = None
        if network_manifest is not None:
            network_bytes = (json.dumps(network_manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
            payload["source"]["network_manifest_sha256"] = hashlib.sha256(network_bytes).hexdigest()
        return check_source_immutability_document(
            document, payload, gonka_sha=GONKA_SOURCE_SHA, marketplace_sha=MARKETPLACE_SHA,
            network_manifest=network_bytes,
        )

    @staticmethod
    def full_network_record():
        manifest = network_manifest_document()
        record = source_immutability_document(
            gonka_sha=GONKA_SOURCE_SHA, marketplace_sha=MARKETPLACE_SHA
        )
        return record, manifest

    def test_a_full_working_copy_proof_matching_the_network_manifest_is_accepted(self):
        record, manifest = self.full_network_record()
        self.assertEqual(self.verify_document(record, network_manifest=manifest)["status"], "MATCHED")

    def test_network_manifest_without_a_live_context_hash_is_incomplete(self):
        record, manifest = self.full_network_record()
        document = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
        payload = {"source": {
            "gonka_sha": GONKA_SOURCE_SHA,
            "marketplace_commit_sha": MARKETPLACE_SHA,
            "source_immutability_verdict": "UNCHANGED",
            "source_immutability_sha256": hashlib.sha256(document).hexdigest(),
        }}
        network_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

        result = check_source_immutability_document(
            document, payload, gonka_sha=GONKA_SOURCE_SHA,
            marketplace_sha=MARKETPLACE_SHA, network_manifest=network_bytes,
        )

        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertIn("live-context.source.network_manifest_sha256", result["missing"])

    def test_a_full_working_copy_manifest_without_its_proof_is_incomplete(self):
        record, manifest = self.full_network_record()
        del record["network_root"]
        result = self.verify_document(record, network_manifest=manifest)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertIn("network_root", result["missing"])

    def test_a_violated_working_copy_cannot_be_hidden_by_unchanged_top_verdict(self):
        record, manifest = self.full_network_record()
        record["network_root"]["verdict"] = "VIOLATED"
        record["network_root"]["violations"] = [{"code": "NETWORK_COPY_MISMATCH", "path": "local-test-net/docker-compose.yml"}]
        manifest["post_run_verification"] = copy.deepcopy(record["network_root"])
        result = self.verify_document(record, network_manifest=manifest)
        self.assertEqual(result["status"], "MISMATCHED")
        self.assertTrue(any(item["field"] == "network_root.verdict" for item in result["mismatches"]))

    def test_a_working_copy_record_disagreeing_with_its_network_manifest_is_rejected(self):
        record, manifest = self.full_network_record()
        record["network_root"]["runtime_additions"] = ["prod-local/genesis/changed.json"]
        result = self.verify_document(record, network_manifest=manifest)
        self.assertEqual(result["status"], "MISMATCHED")
        self.assertTrue(any(item["field"] == "network_root" for item in result["mismatches"]))

    def test_a_record_with_both_pristine_snapshots_is_matched(self):
        record = source_immutability_document(
            gonka_sha=GONKA_SOURCE_SHA, marketplace_sha=MARKETPLACE_SHA
        )
        self.assertEqual(self.verify_document(record)["status"], "MATCHED")

    def test_default_current_fixture_uses_the_full_working_copy_proof(self):
        record = source_immutability_document(
            gonka_sha=GONKA_SOURCE_SHA, marketplace_sha=MARKETPLACE_SHA
        )
        manifest = network_manifest_document()
        self.assertEqual(record["schema"], "a8.source-immutability-set/2")
        self.assertEqual(manifest["copy_mode"], "full_working_tree")
        self.assertEqual(record["network_root"], manifest["post_run_verification"])
        self.assertNotIn("upstream_copy_roots", manifest)

    def test_schema_one_snapshot_only_fixture_is_explicit_compatibility(self):
        record = source_immutability_document(
            gonka_sha=GONKA_SOURCE_SHA,
            marketplace_sha=MARKETPLACE_SHA,
            network_root_verification=None,
        )
        self.assertEqual(record["schema"], "a8.source-immutability-set/1")
        self.assertEqual(self.verify_document(record)["status"], "MATCHED")

    def test_a_record_missing_the_before_snapshot_cannot_claim_unchanged_sources(self):
        record = source_immutability_document(
            gonka_sha=GONKA_SOURCE_SHA, marketplace_sha=MARKETPLACE_SHA
        )
        del record["roots"]["gonka"]["before"]
        result = self.verify_document(record)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertIn("roots.gonka.before", result["missing"])

    def test_a_changed_after_snapshot_is_detected_despite_a_saved_unchanged_verdict(self):
        record = source_immutability_document(
            gonka_sha=GONKA_SOURCE_SHA, marketplace_sha=MARKETPLACE_SHA
        )
        record["roots"]["gonka"]["after"]["tracked_digest"] = "0" * 64
        result = self.verify_document(record)
        self.assertEqual(result["status"], "MISMATCHED")
        self.assertTrue(any(item["field"] == "roots.gonka.fingerprints" for item in result["mismatches"]))


class VerifierTests(unittest.TestCase):
    def test_ordered_vesting_evidence_is_bound_to_the_scope_deal_and_both_schedule_participants(self):
        phase = ordered_vesting_addition_phase(unlock_before=True)
        scope = {"contracts": {"deal": phase["recipient"]}, "phases": [phase]}
        self.assertIn("vesting_addition_verified", _validate_r2_sequence(scope))
        for field in ("before", "after"):
            for replacement in (None, "different-participant"):
                with self.subTest(field=field, replacement=replacement):
                    changed = copy.deepcopy(scope)
                    schedule = changed["phases"][0][field]["vesting_schedule"]
                    if replacement is None:
                        schedule.pop("participant_address")
                    else:
                        schedule["participant_address"] = replacement
                    self.assertNotIn("vesting_addition_verified", _validate_r2_sequence(changed))
        for contracts in ({}, {"deal": "different-deal"}):
            changed = copy.deepcopy(scope)
            changed["contracts"] = contracts
            self.assertNotIn("vesting_addition_verified", _validate_r2_sequence(changed))

    def test_native_unlock_total_must_cover_every_denomination_of_the_released_tranche(self):
        phase = ordered_vesting_addition_phase(unlock_before=True, foreign_tranche=True)
        self.assertTrue(_validate_vesting_addition(phase))
        for total in ("1ngonka,5uatom", "70ngonka", "70ngonka,4uatom", "garbage", "-70ngonka", "0ngonka", "70ngonka,70ngonka", "70ngonka,", "70.0ngonka"):
            with self.subTest(total=total):
                changed = copy.deepcopy(phase)
                changed["vesting_events"][0]["attributes"][0]["value"] = total
                self.assertFalse(_validate_vesting_addition(changed))

    def test_ordered_vesting_addition_accepts_both_gift_unlock_orders_including_one_block(self):
        for unlock_before in (False, True):
            for same_block in (False, True):
                with self.subTest(unlock_before=unlock_before, same_block=same_block):
                    phase = ordered_vesting_addition_phase(unlock_before=unlock_before, same_block=same_block)
                    self.assertTrue(_validate_vesting_addition(phase))

    def test_ordered_vesting_addition_rejects_each_missing_or_changed_mandatory_fact(self):
        mutations = {
            "event model": lambda p: p.pop("vesting_event_model"),
            "unknown model": lambda p: p.__setitem__("vesting_event_model", "ordered/99"),
            "missing events": lambda p: p.pop("vesting_events"),
            "missing gift": lambda p: p["vesting_events"].pop(1),
            "missing unlock": lambda p: p["vesting_events"].pop(0),
            "bank payout": lambda p: p.__setitem__("after_bank_ngonka", 171),
            "schedule": lambda p: p["after"]["vesting_schedule"]["epoch_amounts"][0]["coins"][0].__setitem__("amount", "67"),
            "gift recipient": lambda p: p["vesting_events"][1]["attributes"][0].__setitem__("value", "another-recipient"),
            "gift amount": lambda p: p["vesting_events"][1]["attributes"][1].__setitem__("value", "12ngonka"),
            "gift epochs": lambda p: p["vesting_events"][1]["attributes"][2].__setitem__("value", "3"),
            "gift sender": lambda p: p["vesting_events"][1]["attributes"][3].__setitem__("value", "another-sender"),
            "reward event is not a governance gift": lambda p: p["vesting_events"][1].__setitem__("type", "vest_reward"),
            "duplicate position": lambda p: p["vesting_events"][1].__setitem__("event_index", 0),
            "event outside bracket": lambda p: p["vesting_events"][1].__setitem__("height", 263),
            "reported unlock count": lambda p: p.__setitem__("eligible_prefix_count", 2),
            "reported release count": lambda p: p.__setitem__("released_prefix_count", 2),
            "reported payout": lambda p: p.__setitem__("released_prefix_ngonka", 71),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                phase = ordered_vesting_addition_phase(unlock_before=True, same_block=True)
                mutate(phase)
                self.assertFalse(_validate_vesting_addition(phase))

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-verifier-")
        self.root = Path(self.tmp_dir.name)
        self.evidence_dir = self.root / "evidence"
        self.junit_dir = self.root / "junit"
        self.evidence_dir.mkdir(parents=True)
        self.junit_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_junit_xml_missing_or_empty_rejected(self):
        # Missing
        missing_p = self.junit_dir / "non_existent.xml"
        ok, _, err = verify_junit_xml(missing_p, "funded lock succeeds")
        self.assertFalse(ok)
        self.assertIn("not found", err)

        # Empty testcases
        empty_xml = self.junit_dir / "empty.xml"
        empty_xml.write_text("<testsuite name='empty'></testsuite>", encoding="utf-8")
        ok, _, err = verify_junit_xml(empty_xml, "funded lock succeeds")
        self.assertFalse(ok)
        self.assertIn("0 testcases", err)

    def test_junit_xml_failure_or_skipped_rejected(self):
        failed_xml = self.junit_dir / "failed.xml"
        failed_xml.write_text("""
        <testsuite name='suite'>
            <testcase name='marketplace funded lock succeeds exactly at E' classname='Acceptance'>
                <failure message='assertion failed: epoch was 6, expected 5'/>
            </testcase>
        </testsuite>
        """, encoding="utf-8")
        ok, _, err = verify_junit_xml(failed_xml, "funded lock succeeds exactly at E")
        self.assertFalse(ok)
        self.assertIn("assertion failed", err)

        skipped_xml = self.junit_dir / "skipped.xml"
        skipped_xml.write_text("""
        <testsuite name='suite'>
            <testcase name='marketplace funded lock succeeds exactly at E' classname='Acceptance'>
                <skipped/>
            </testcase>
        </testsuite>
        """, encoding="utf-8")
        ok, _, err = verify_junit_xml(skipped_xml, "funded lock succeeds exactly at E")
        self.assertFalse(ok)
        self.assertIn("skipped", err)

    def test_junit_xml_exact_match_success(self):
        pass_xml = self.junit_dir / "pass.xml"
        pass_xml.write_text("""
        <testsuite name='suite'>
            <testcase name='marketplace funded lock succeeds exactly at E' classname='Acceptance' time='42.5'/>
        </testsuite>
        """, encoding="utf-8")
        ok, obs, err = verify_junit_xml(pass_xml, "funded lock succeeds exactly at E")
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(len(obs), 1)

    def test_native_exit_0_without_junit_fails_verification(self):
        task = get_task_by_id_or_alias("lock-exact-e")
        assert task is not None
        (self.evidence_dir / "live-context.json").write_text("{}", encoding="utf-8")

        # Exit code 0 from process, but junit_dir has no XML
        final_exec, ev_status, _, missing, err = evaluate_task_evidence(
            task=task,
            evidence_dir=self.evidence_dir,
            junit_dir=self.junit_dir,
            raw_status=ExecutionStatus.PASSED,
            exit_code=0,
        )
        self.assertEqual(final_exec, ExecutionStatus.FAILED)
        self.assertEqual(ev_status, EvidenceStatus.INCOMPLETE)
        self.assertIn("No JUnit XML found", err)

    def _package_c_policy_log(self) -> str:
        tests = "".join(f"test {name} ... ok\n" for name in sorted(PACKAGE_C_POLICY_TESTS))
        cases = "".join(f"C_CASE:{name}\n" for name in sorted(PACKAGE_C_POLICY_CASES))
        return (
            "running 18 tests\n" + tests + cases
            + "test result: ok. 18 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
        )

    def test_package_c_policy_requires_exact_18_tests_and_71_markers(self):
        log = self.evidence_dir / "ct-package-c-policy.log"
        log.write_text(self._package_c_policy_log(), encoding="utf-8")
        ok, observed, error = verify_package_c_policy_log(log)
        self.assertTrue(ok, error)
        self.assertEqual(observed, sorted(PACKAGE_C_POLICY_CASES))

    def test_package_c_policy_accepts_real_cargo_nocapture_line_shape(self):
        log = self.evidence_dir / "ct-package-c-policy.log"
        tests = sorted(PACKAGE_C_POLICY_TESTS)
        cases = sorted(PACKAGE_C_POLICY_CASES)
        lines = ["running 18 tests"]
        for index, name in enumerate(tests):
            marker = cases[index]
            lines.extend((f"test {name} ... C_CASE:{marker}", "ok"))
        lines.extend(f"C_CASE:{name}" for name in cases[len(tests):])
        lines.append("test result: ok. 18 passed; 0 failed; 0 ignored; 0 measured")
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        ok, observed, error = verify_package_c_policy_log(log)
        self.assertTrue(ok, error)
        self.assertEqual(observed, cases)

    def test_package_c_policy_rejects_empty_missing_foreign_and_duplicate_evidence(self):
        log = self.evidence_dir / "ct-package-c-policy.log"
        positive = self._package_c_policy_log()
        one_test = sorted(PACKAGE_C_POLICY_TESTS)[0]
        one_case = sorted(PACKAGE_C_POLICY_CASES)[0]
        mutations = {
            "empty": "",
            "missing_test": positive.replace(f"test {one_test} ... ok\n", ""),
            "foreign_test": positive.replace(
                f"test {one_test} ... ok", "test c_foreign_name ... ok"
            ),
            "missing_case": positive.replace(f"C_CASE:{one_case}\n", ""),
            "duplicate_case": positive + f"C_CASE:{one_case}\n",
            "ignored": positive.replace(f"test {one_test} ... ok", f"test {one_test} ... ignored"),
            "ignored_with_reason": positive.replace(f"test {one_test} ... ok", f"test {one_test} ... ignored, unavailable"),
            "failed": positive.replace(f"test {one_test} ... ok", f"test {one_test} ... FAILED"),
            "unfinished": positive.replace(f"test {one_test} ... ok", f"test {one_test} ... stdout without completion"),
            "duplicate_test": positive + f"test {one_test} ... ok\n",
            "failed_suite": positive + "test result: FAILED. 0 passed; 1 failed;\n",
            "unrelated_success_cannot_replace_ignored": positive.replace(
                f"test {one_test} ... ok", f"test {one_test} ... ignored\ntest unrelated ... ok"
            ),
        }
        for name, content in mutations.items():
            with self.subTest(name=name):
                log.write_text(content, encoding="utf-8")
                self.assertFalse(verify_package_c_policy_log(log)[0])

    def test_package_c_policy_requires_multiline_completion_before_next_test_or_summary(self):
        log = self.evidence_dir / "ct-package-c-policy.log"
        tests = sorted(PACKAGE_C_POLICY_TESTS)
        positive = self._package_c_policy_log()
        for target in (tests[0], tests[-1]):
            for completion in ("FAILED", "ignored", "", "ok"):
                with self.subTest(target=target, completion=completion):
                    content = positive.replace(
                        f"test {target} ... ok",
                        f"test {target} ... diagnostic output\n{completion}",
                    )
                    log.write_text(content, encoding="utf-8")
                    self.assertEqual(verify_package_c_policy_log(log)[0], completion == "ok")
        log.write_text(positive + "test unfinished ... diagnostic output\n", encoding="utf-8")
        self.assertFalse(verify_package_c_policy_log(log)[0])

    def test_go_boundary_requires_both_named_tests(self):
        report, raw = go_boundary_evidence()
        rep = self.evidence_dir / "report.json"
        rep.write_text(json.dumps(report), encoding="utf-8")
        raw_path = self.evidence_dir / "raw" / "go-test.json"
        raw_path.parent.mkdir()
        raw_path.write_text(raw, encoding="utf-8")
        required = {
            "TestToQuerierResultClassifiesVMSystemErrors",
            "TestStrictPlanValidation",
        }
        ok, observed, err = verify_go_boundary_report(rep)
        self.assertTrue(ok, err)
        self.assertEqual(set(observed), required)

        for missing_name in sorted(required):
            with self.subTest(missing=missing_name):
                # Remove only the mandatory pass event. Aggregate PASS, output
                # lines, subtests and the other test cannot substitute for it.
                lines = raw.splitlines(keepends=True)
                retained = []
                for line in lines:
                    event = json.loads(line) if line.startswith("{") else {}
                    if event.get("Action") == "pass" and event.get("Test") == missing_name:
                        continue
                    retained.append(line)
                self.assertEqual(len(lines) - len(retained), 1)
                raw_path.write_text("".join(retained), encoding="utf-8")
                # Keep byte-integrity metadata consistent: this case isolates
                # the missing named test rather than a checksum mismatch.
                rep.write_text(json.dumps(dict(
                    report,
                    test_output_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                )), encoding="utf-8")
                ok, observed, err = verify_go_boundary_report(rep)
                self.assertFalse(ok)
                self.assertEqual(err, f"Go boundary missing passed tests: {[missing_name]}")
                self.assertNotIn(missing_name, observed)

    def test_go_boundary_report_cannot_replace_missing_raw_test_events(self):
        report, _ = go_boundary_evidence()
        rep = self.evidence_dir / "report.json"
        rep.write_text(json.dumps(report), encoding="utf-8")

        ok, observed, err = verify_go_boundary_report(rep)
        self.assertFalse(ok)
        self.assertEqual(observed, [])
        self.assertIn("go-test.json", err)

    def test_go_boundary_report_hash_must_bind_the_raw_test_events(self):
        report, raw = go_boundary_evidence()
        rep = self.evidence_dir / "report.json"
        raw_path = self.evidence_dir / "raw" / "go-test.json"
        raw_path.parent.mkdir()
        raw_path.write_text(raw, encoding="utf-8")
        report["test_output_sha256"] = "0" * 64
        rep.write_text(json.dumps(report), encoding="utf-8")

        ok, _, err = verify_go_boundary_report(rep)
        self.assertFalse(ok)
        self.assertIn("test_output_sha256", err)

    def test_go_boundary_rejects_failed_status_or_nonzero_exit_codes_with_passing_tests(self):
        report, raw = go_boundary_evidence()
        rep = self.evidence_dir / "report.json"
        raw_path = self.evidence_dir / "raw" / "go-test.json"
        raw_path.parent.mkdir()
        raw_path.write_text(raw, encoding="utf-8")
        rep.write_text(json.dumps(report), encoding="utf-8")
        self.assertTrue(verify_go_boundary_report(rep)[0])
        for field, value, diagnostic in (
            ("status", "FAIL", "Go boundary status"),
            ("docker_exit", 1, "Go boundary docker_exit"),
            ("go_exit", "1", "Go boundary go_exit"),
        ):
            with self.subTest(field=field):
                rep.write_text(json.dumps(dict(report, **{field: value})), encoding="utf-8")
                ok, _, err = verify_go_boundary_report(rep)
                self.assertFalse(ok)
                self.assertIn(diagnostic, err)

    def test_wasm_abi_requires_exact_9_named_cases(self):
        abi_f = self.evidence_dir / "abi.json"
        wasm_f = self.evidence_dir / "a8_query_boundary.wasm"
        wasm_f.write_bytes(b"\x00asm\x01\x00\x00\x00")
        # 8 cases instead of 9
        abi_f.write_text(json.dumps({
            "status": "PASS",
            "cases": [{"name": c, "status": "PASS"} for c in list(WASM_ABI_REQUIRED_CASES)[:-1]],
        }), encoding="utf-8")
        ok, count, err = verify_wasm_abi_report(abi_f, wasm_f)
        self.assertFalse(ok)
        self.assertIn("expected exactly 9", err)

        # 9 cases with one wrong name
        bad_cases = [{"name": c, "status": "PASS"} for c in list(WASM_ABI_REQUIRED_CASES)[:-1]] + [{"name": "unknown_case_name", "status": "PASS"}]
        abi_f.write_text(json.dumps({
            "status": "PASS",
            "cases": bad_cases,
        }), encoding="utf-8")
        ok, count, err = verify_wasm_abi_report(abi_f, wasm_f)
        self.assertFalse(ok)
        self.assertIn("missing expected case IDs", err)

        # 9 exact cases, all PASS
        abi_f.write_text(json.dumps({
            "status": "PASS",
            "wasm_sha256": hashlib.sha256(wasm_f.read_bytes()).hexdigest(),
            "cases": [{"name": c, "status": "PASS"} for c in WASM_ABI_REQUIRED_CASES],
        }), encoding="utf-8")
        ok, cases, err = verify_wasm_abi_report(abi_f, wasm_f)
        self.assertTrue(ok)
        self.assertEqual(len(cases), 9)

    def test_junit_e_plus_4_never_matches_exact_e(self):
        e4_xml = self.junit_dir / "e4.xml"
        e4_xml.write_text("""
        <testsuite name='suite'>
            <testcase name='marketplace funded lock succeeds exactly at E plus 4()' classname='Acceptance'/>
        </testsuite>
        """, encoding="utf-8")

        # Must fail when verifying exact-E task method!
        ok, obs, err = verify_junit_xml(e4_xml, "marketplace funded lock succeeds exactly at E", exact_match=True)
        self.assertFalse(ok)
        self.assertIn("not found in JUnit XML", err)

        # Must succeed for E plus 4
        ok, obs, err = verify_junit_xml(e4_xml, "marketplace funded lock succeeds exactly at E plus 4", exact_match=True)
        self.assertTrue(ok)

    def test_all_19_native_tasks_have_distinct_exact_methods(self):
        methods = [t.exact_test_method for t in NATIVE_TASKS]
        self.assertEqual(len(methods), 19)
        self.assertEqual(len(set(methods)), 19)  # Every task must be distinct
        for t in NATIVE_TASKS:
            self.assertTrue(bool(t.exact_test_method))

    def test_verify_live_context_rejects_corrupt_or_missing_provenance(self):
        ctx_file = self.evidence_dir / "live-context.json"
        write_immutable_companion_files(self.evidence_dir)
        valid_source = copy.deepcopy(real_lock_exact_e_context()["source"])

        # Not valid JSON
        ctx_file.write_text("not even JSON", encoding="utf-8")
        ok, obs, err = verify_live_context(ctx_file)
        self.assertFalse(ok)
        self.assertIn("Malformed live-context.json", err)

        # Empty JSON object
        ctx_file.write_text("{}", encoding="utf-8")
        ok, obs, err = verify_live_context(ctx_file)
        self.assertFalse(ok)
        self.assertIn("missing non-empty 'source'", err)

        # Missing checkpoints
        ctx_file.write_text(json.dumps({
            "source": copy.deepcopy(valid_source),
            "phases": [{"name": "phase1"}],
        }), encoding="utf-8")
        ok, obs, err = verify_live_context(ctx_file, expected_checkpoints=["phase1", "missing_checkpoint"])
        self.assertFalse(ok)
        self.assertIn("Missing required checkpoints", err)

        # Valid context with provenance and checkpoints
        ctx_file.write_text(json.dumps({
            "source": copy.deepcopy(valid_source),
            "bootstrap": {},
            "phases": [{"name": "Funded->Locked"}],
        }), encoding="utf-8")
        ok, obs, err = verify_live_context(ctx_file, expected_checkpoints=["bootstrap", "Funded->Locked"])
        self.assertTrue(ok)
        self.assertIn("Funded->Locked", obs)

    def test_boundary_missing_artifacts_yields_incomplete_status(self):
        task = get_task_by_id_or_alias("go-boundary")
        assert task is not None
        rep = self.evidence_dir / "report.json"
        rep.write_text(json.dumps({"status": "PASS", "docker_exit": 0, "go_exit": 0}), encoding="utf-8")

        # The PASS summary cannot replace missing raw/go-test.json events.
        exec_status, ev_status, obs, missing, err = evaluate_task_evidence(
            task=task,
            evidence_dir=self.evidence_dir,
            junit_dir=self.junit_dir,
            raw_status=ExecutionStatus.PASSED,
            exit_code=0,
        )
        self.assertEqual(exec_status, ExecutionStatus.FAILED)
        self.assertEqual(ev_status, EvidenceStatus.INCOMPLETE)
        self.assertTrue(len(missing) > 0)

    def test_cargo_test_log_0_tests_is_not_passed(self):
        log_f = self.evidence_dir / "ct.log"
        log_f.write_text("running 0 tests\ntest result: ok. 0 passed; 0 failed; 0 ignored\n", encoding="utf-8")
        ok, count, err = verify_cargo_test_log(log_f)
        self.assertFalse(ok)
        self.assertIn("0 passed", err)

        log_f.write_text("running 1 test\ntest result: ok. 1 passed; 0 failed; 0 ignored\n", encoding="utf-8")
        ok, count, err = verify_cargo_test_log(log_f)
        self.assertTrue(ok)
        self.assertEqual(count, 1)

    def test_lock_exact_e_real_evidence_and_single_fact_deletions(self):
        """The validator must read the producer's real document structure.

        The positive case is the producer-shaped lock-exact-E fixture with its
        scenario nesting; every negative case removes or changes exactly one
        mandatory fact of that same document.
        """
        ctx_file = self.evidence_dir / "live-context.json"
        write_immutable_companion_files(self.evidence_dir)
        expected_cps = [
            "phase:lock_exact_e",
            "state_transition:Funded->Locked",
            "recipient_locked=true",
            "no_deal_bank_transfer",
            "lock_tx_included_in_epoch_e",
            "target_epoch_e_reached",
        ]

        def verify(context):
            ctx_file.write_text(json.dumps(context), encoding="utf-8")
            return verify_live_context(
                ctx_file,
                expected_checkpoints=expected_cps,
                expected_run_id=context.get("run_id"),
                scenario_selector="lock-exact-e",
            )

        base = real_lock_exact_e_context()
        ok, obs, err = verify(base)
        self.assertTrue(ok, f"Producer-shaped lock-exact-E evidence must pass, got: {err}")
        self.assertIn("target_epoch_e_reached", obs)

        # The target epoch comes from the scenario's own terms, so a wrong
        # top-level value must not influence the outcome.
        nested_wins = copy.deepcopy(base)
        nested_wins["terms"]["target_epoch"] = 99
        ok, _, err = verify(nested_wins)
        self.assertTrue(ok, f"Scenario terms must win over top-level terms, got: {err}")

        # ...and a wrong scenario target epoch must fail even when the
        # top-level terms still hold the correct value.
        nested_wrong = copy.deepcopy(base)
        nested_wrong["scenarios"]["lock-exact-e"]["terms"]["target_epoch"] = 6
        ok, obs, _ = verify(nested_wrong)
        self.assertFalse(ok)
        self.assertNotIn("target_epoch_e_reached", obs)

        # Deleted target epoch: the check must not be silently skipped.
        no_target = copy.deepcopy(base)
        del no_target["scenarios"]["lock-exact-e"]["terms"]["target_epoch"]
        ok, obs, _ = verify(no_target)
        self.assertFalse(ok)
        self.assertNotIn("lock_tx_included_in_epoch_e", obs)

        # Emptied epoch bracket must not be tolerated.
        empty_bracket = copy.deepcopy(base)
        empty_bracket["scenarios"]["lock-exact-e"]["phases"][0]["epoch_bracket"] = {}
        ok, obs, _ = verify(empty_bracket)
        self.assertFalse(ok)
        self.assertNotIn("lock_tx_included_in_epoch_e", obs)

        # Bracket not anchored on the transaction height.
        unlinked = copy.deepcopy(base)
        unlinked["scenarios"]["lock-exact-e"]["phases"][0]["epoch_bracket"]["tx_height"] = 9999
        ok, obs, _ = verify(unlinked)
        self.assertFalse(ok)
        self.assertNotIn("lock_tx_included_in_epoch_e", obs)

        # Before/after statuses are compared, not only the locked flags.
        wrong_status = copy.deepcopy(base)
        wrong_status["scenarios"]["lock-exact-e"]["phases"][0]["after"]["state"]["status"] = "funded"
        ok, obs, _ = verify(wrong_status)
        self.assertFalse(ok)
        self.assertNotIn("state_transition:Funded->Locked", obs)

        already_locked = copy.deepcopy(base)
        already_locked["scenarios"]["lock-exact-e"]["phases"][0]["before"]["state"]["recipient_locked"] = True
        ok, obs, _ = verify(already_locked)
        self.assertFalse(ok)
        self.assertNotIn("recipient_locked=true", obs)

        # A Deal bank transfer during lock must be rejected.
        moved_bank = copy.deepcopy(base)
        after_bank = moved_bank["scenarios"]["lock-exact-e"]["phases"][0]["after"]["bank_ngonka"]
        after_bank["deal"] = int(after_bank["deal"]) + 1
        ok, obs, _ = verify(moved_bank)
        self.assertFalse(ok)
        self.assertNotIn("no_deal_bank_transfer", obs)

        # Mutated immutable config must be rejected.
        mutated_config = copy.deepcopy(base)
        phase = mutated_config["scenarios"]["lock-exact-e"]["phases"][0]
        phase["config_after"] = dict(phase["config_after"], a8_mutated=True)
        ok, obs, _ = verify(mutated_config)
        self.assertFalse(ok)
        self.assertNotIn("state_transition:Funded->Locked", obs)

        # Failed lock transaction must be rejected.
        failed_tx = copy.deepcopy(base)
        failed_tx["scenarios"]["lock-exact-e"]["phases"][0]["tx"]["code"] = 5
        ok, obs, _ = verify(failed_tx)
        self.assertFalse(ok)
        self.assertNotIn("lock_tx_included_in_epoch_e", obs)

    def test_evidence_is_not_merged_across_scenarios(self):
        """A neighbouring scenario must not lend its proof to the selected one."""
        base = real_lock_exact_e_context()
        good_phase = base["scenarios"]["lock-exact-e"]["phases"][0]

        # The selected scenario keeps a broken phase; a second scenario holds
        # the intact one. The selected scenario must still be rejected.
        broken = copy.deepcopy(good_phase)
        broken["after"]["state"]["status"] = "funded"
        base["scenarios"]["lock-exact-e"]["phases"] = [broken]
        base["scenarios"]["lock-e-plus-4-neighbour"] = {
            "name": "lock-e-plus-4-neighbour",
            "terms": copy.deepcopy(base["scenarios"]["lock-exact-e"]["terms"]),
            "phases": [copy.deepcopy(good_phase)],
        }

        observed = extract_and_validate_scenario_predicates(base, "lock-exact-e")
        self.assertNotIn("state_transition:Funded->Locked", observed)
        self.assertNotIn("lock-e-plus-4-neighbour", observed)

    def test_b3_accepts_preserved_foreign_balance_and_rejects_regressions(self):
        """Round 4 issue 1: the zero balance applies to GNK, not the foreign denom."""
        base = real_b3_context()
        expected_cps = [
            "phase:b3_foreign_native_successful_release",
            "foreign_native_funding_verified",
            "foreign_native_balance_preserved",
            "release_completed",
        ]

        observed = extract_and_validate_scenario_predicates(base, "b3-foreign-native")
        for checkpoint in expected_cps:
            self.assertIn(checkpoint, observed)

        # A decreased foreign balance on the Deal must be rejected.
        drained = copy.deepcopy(base)
        drained["phases"][0]["release"]["after"]["foreign_native"]["deal"] = 0
        drained["phases"][0]["release"]["actual"]["foreign_native_deal_after"] = 0
        observed = extract_and_validate_scenario_predicates(drained, "b3-foreign-native")
        self.assertNotIn("foreign_native_balance_preserved", observed)
        self.assertNotIn("release_completed", observed)

        # Zero funding must be rejected.
        unfunded = copy.deepcopy(base)
        unfunded["phases"][0]["fixture"]["amount"] = 0
        unfunded["phases"][0]["foreign_native_funding"]["after"]["deal"] = 0
        observed = extract_and_validate_scenario_predicates(unfunded, "b3-foreign-native")
        self.assertNotIn("foreign_native_funding_verified", observed)
        self.assertNotIn("foreign_native_balance_preserved", observed)

        # A failed funding transaction must be rejected.
        bad_funding = copy.deepcopy(base)
        bad_funding["phases"][0]["foreign_native_funding"]["tx"]["code"] = 5
        observed = extract_and_validate_scenario_predicates(bad_funding, "b3-foreign-native")
        self.assertNotIn("foreign_native_funding_verified", observed)

        # GNK must still be fully released: a non-zero Deal ngonka balance fails.
        gnk_left = copy.deepcopy(base)
        gnk_left["phases"][0]["release"]["after"]["ngonka"]["deal"] = 1
        observed = extract_and_validate_scenario_predicates(gnk_left, "b3-foreign-native")
        self.assertNotIn("release_completed", observed)
        self.assertIn("foreign_native_balance_preserved", observed)

        # Broken conservation of the released amount must fail.
        bad_math = copy.deepcopy(base)
        bad_math["phases"][0]["release"]["actual"]["buyer_delta"] += 1
        observed = extract_and_validate_scenario_predicates(bad_math, "b3-foreign-native")
        self.assertNotIn("release_completed", observed)

        # Claimed totals must be derived from the bank snapshots, not merely
        # add up to each other.
        mutations = (
            lambda p: p["release"]["before"]["ngonka"].__setitem__("deal", 1),
            lambda p: p["release"]["after"]["ngonka"].__setitem__("buyer", 1),
            lambda p: p["release"]["after"]["ngonka"].__setitem__("host", 1),
            lambda p: p["release"]["after"]["ngonka"].__setitem__("fee_recipient", 1),
            lambda p: p["release"]["after"]["ngonka"].__setitem__("caller", 1),
            lambda p: p["release"]["expected"].__setitem__("released_total", 1),
            lambda p: p["release"]["expected"].__setitem__("buyer_delta", 1),
            lambda p: p["release"]["expected"].__setitem__("host_released", 1),
            lambda p: p["release"]["state_after"].__setitem__("buyer_released_ngonka", "1"),
            lambda p: p["release"]["state_after"].__setitem__("host_released_ngonka", "1"),
            lambda p: p["release"]["state_before"].__setitem__("released_total_ngonka", "1"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                inconsistent = copy.deepcopy(base)
                mutate(inconsistent["phases"][0])
                observed = extract_and_validate_scenario_predicates(
                    inconsistent, "b3-foreign-native"
                )
                self.assertNotIn("release_completed", observed)

    def test_r1_refund_boundary_binds_to_the_produced_phase_name(self):
        """Round 4 issue 2: the phase is r1_1_refund_e_plus_5_rejected."""
        task = get_task_by_id_or_alias("package-a-r1-r2")
        assert task is not None
        self.assertIn("phase:r1_1_refund_e_plus_5_rejected", task.expected_checkpoints)
        self.assertNotIn("phase:r1_refund_e_plus_5", task.expected_checkpoints)

        base = real_r1_scenario_context()
        observed = extract_and_validate_scenario_predicates(base, "package-a-r1-r2", PACKAGE_A_SCOPES)
        self.assertIn("phase:r1_1_refund_e_plus_5_rejected", observed)
        self.assertIn("r1_refund_boundary_asserted", observed)

        # Simulation-only rejection (no DeliverTx layer) must not count.
        simulated = copy.deepcopy(base)
        simulated["scenarios"]["r1-refund-e-plus-5"]["phases"][0]["attempt"]["layer"] = "simulation"
        observed = extract_and_validate_scenario_predicates(simulated, "package-a-r1-r2", PACKAGE_A_SCOPES)
        self.assertNotIn("r1_refund_boundary_asserted", observed)

        # An accepted refund must not count as a proven boundary.
        accepted = copy.deepcopy(base)
        accepted["scenarios"]["r1-refund-e-plus-5"]["phases"][0]["attempt"]["code"] = 0
        observed = extract_and_validate_scenario_predicates(accepted, "package-a-r1-r2", PACKAGE_A_SCOPES)
        self.assertNotIn("r1_refund_boundary_asserted", observed)

        # A rejection outside the E+5 bracket must not count.
        wrong_epoch = copy.deepcopy(base)
        wrong_epoch["scenarios"]["r1-refund-e-plus-5"]["phases"][0]["epoch_bracket"]["epoch"] = 9
        observed = extract_and_validate_scenario_predicates(wrong_epoch, "package-a-r1-r2", PACKAGE_A_SCOPES)
        self.assertNotIn("r1_refund_boundary_asserted", observed)

        # Changed state or balances after a rejected refund must not count.
        changed_state = copy.deepcopy(base)
        changed_state["scenarios"]["r1-refund-e-plus-5"]["phases"][0]["after"]["state"]["status"] = "refunded"
        observed = extract_and_validate_scenario_predicates(changed_state, "package-a-r1-r2", PACKAGE_A_SCOPES)
        self.assertNotIn("r1_refund_boundary_asserted", observed)

        moved_funds = copy.deepcopy(base)
        after = moved_funds["scenarios"]["r1-refund-e-plus-5"]["phases"][0]["after"]
        after["cw20"]["deal"] = int(after["cw20"]["deal"]) - 1
        observed = extract_and_validate_scenario_predicates(moved_funds, "package-a-r1-r2", PACKAGE_A_SCOPES)
        self.assertNotIn("r1_refund_boundary_asserted", observed)

        # The target epoch of the owning scenario is mandatory.
        no_terms = copy.deepcopy(base)
        no_terms["scenarios"]["r1-refund-e-plus-5"]["terms"] = {}
        observed = extract_and_validate_scenario_predicates(no_terms, "package-a-r1-r2", PACKAGE_A_SCOPES)
        self.assertNotIn("r1_refund_boundary_asserted", observed)

    def test_cw20_fault_rollbacks_reject_empty_or_partial_entries(self):
        """Round 4 issue 4: three empty dicts must never grant the checkpoints."""
        phase = real_claim_settle_phase()
        faults = phase["cw20_fault_rollbacks"]
        accounts = {entry["fault"]["role"]: entry["fault"]["recipient"] for entry in faults}
        deal_event = next(event for event in phase["settle_tx"]["events"]
                          if event["type"] == "wasm-claim_settled")
        deal = next(attr["value"] for attr in deal_event["attributes"]
                    if attr["key"] == "_contract_address")
        context = {
            "terms": {}, "phases": [phase], "accounts": accounts,
            "contracts": {"deal": deal, "cw20": faults[0]["fault"]["contract"]},
        }
        observed = extract_and_validate_scenario_predicates(context, "package-b-r6-1")
        self.assertIn("single_settlement_succeeds", observed)
        self.assertIn("cw20_three_send_rejections_asserted", observed)
        self.assertIn("settlement_atomic_rollback_verified", observed)

        unrelated_wasm = copy.deepcopy(context)
        unrelated_wasm["phases"][0]["withdrawal_txs"]["host"]["events"].append({
            "type": "wasm", "attributes": [
                {"key": "_contract_address", "value": "unrelated-contract"},
                {"key": "action", "value": "unrelated"},
            ],
        })
        observed = extract_and_validate_scenario_predicates(unrelated_wasm, "package-b-r6-1")
        self.assertIn("cw20_three_send_rejections_asserted", observed)

        extra_transfer = copy.deepcopy(context)
        events = extra_transfer["phases"][0]["withdrawal_txs"]["host"]["events"]
        events.append(copy.deepcopy(next(event for event in events
                                         if event["type"] == "wasm")))
        observed = extract_and_validate_scenario_predicates(extra_transfer, "package-b-r6-1")
        self.assertNotIn("cw20_three_send_rejections_asserted", observed)

        missing_withdrawal = copy.deepcopy(context)
        missing_withdrawal["phases"][0]["withdrawal_txs"].pop("buyer")
        observed = extract_and_validate_scenario_predicates(missing_withdrawal, "package-b-r6-1")
        self.assertNotIn("cw20_three_send_rejections_asserted", observed)

        for mutation in ("wrong_paid_recipient", "wrong_paid_amount",
                         "failed_withdrawal", "missing_repeat", "wrong_repeat_reason"):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(context)
                claim = bad["phases"][0]
                if mutation in ("wrong_paid_recipient", "wrong_paid_amount"):
                    tx = claim["withdrawal_txs"]["host"]
                    event = next(event for event in tx["events"]
                                 if event["type"] == "wasm-usdt_paid")
                    key = "recipient" if mutation == "wrong_paid_recipient" else "amount_micro_usdt"
                    next(attr for attr in event["attributes"] if attr["key"] == key)["value"] = "wrong"
                elif mutation == "failed_withdrawal":
                    claim["withdrawal_txs"]["fee"]["code"] = 1
                elif mutation == "missing_repeat":
                    claim.pop("settle_repeat")
                else:
                    claim["settle_repeat"]["proof"]["contract_error"] = "OtherError"
                observed = extract_and_validate_scenario_predicates(bad, "package-b-r6-1")
                self.assertNotIn("cw20_three_send_rejections_asserted", observed)

        swapped_positions = copy.deepcopy(context)
        swapped_faults = swapped_positions["phases"][0]["cw20_fault_rollbacks"]
        swapped_faults[0]["fault"]["outgoing_transfer_index"] = 2
        swapped_faults[1]["fault"]["outgoing_transfer_index"] = 1
        observed = extract_and_validate_scenario_predicates(swapped_positions, "package-b-r6-1")
        self.assertNotIn("cw20_three_send_rejections_asserted", observed)

        for mutation in ("wrong_amount", "wrong_contract", "wrong_role_address",
                         "changed_payment", "reused_setup", "other_settle_deal",
                         "altered_settle_amount", "changed_settled_state"):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(context)
                entries = bad["phases"][0]["cw20_fault_rollbacks"]
                if mutation == "wrong_amount":
                    entries[0]["fault"]["amount"] += 1
                elif mutation == "wrong_contract":
                    entries[1]["fault"]["contract"] = "other-cw20"
                elif mutation == "wrong_role_address":
                    entries[2]["fault"]["recipient"] = "other-buyer"
                elif mutation == "changed_payment":
                    bad["phases"][0]["settlement_payments"]["host"]["pending_micro_usdt"] = "1"
                elif mutation == "reused_setup":
                    entries[1]["setup_tx"]["tx_hash"] = entries[0]["setup_tx"]["tx_hash"]
                elif mutation in ("other_settle_deal", "altered_settle_amount"):
                    event = next(event for event in bad["phases"][0]["settle_tx"]["events"]
                                 if event["type"] == "wasm-claim_settled")
                    key = "_contract_address" if mutation == "other_settle_deal" else "host_net_usdt"
                    attr = next(attr for attr in event["attributes"] if attr["key"] == key)
                    attr["value"] = "other-deal" if mutation == "other_settle_deal" else "1"
                else:
                    bad["phases"][0]["after"]["deal_state"]["fee_usdt"] = "1"
                observed = extract_and_validate_scenario_predicates(bad, "package-b-r6-1")
                self.assertNotIn("cw20_three_send_rejections_asserted", observed)

        empty = copy.deepcopy(context)
        empty["phases"][0]["cw20_fault_rollbacks"] = [{}, {}, {}]
        observed = extract_and_validate_scenario_predicates(empty, "package-b-r6-1")
        self.assertNotIn("single_settlement_succeeds", observed)
        self.assertNotIn("cw20_three_send_rejections_asserted", observed)
        self.assertNotIn("settlement_atomic_rollback_verified", observed)

        # All three faults aimed at the same recipient prove only one target.
        same_target = copy.deepcopy(context)
        host = same_target["phases"][0]["deal_config"]["host"]
        for entry in same_target["phases"][0]["cw20_fault_rollbacks"]:
            entry["fault"]["recipient"] = host
        observed = extract_and_validate_scenario_predicates(same_target, "package-b-r6-1")
        self.assertNotIn("cw20_three_send_rejections_asserted", observed)

        # The same receipt reused for several targets must be rejected.
        same_receipt = copy.deepcopy(context)
        hashes = same_receipt["phases"][0]["cw20_fault_rollbacks"]
        hashes[1]["attempt"]["tx_hash"] = hashes[0]["attempt"]["tx_hash"]
        observed = extract_and_validate_scenario_predicates(same_receipt, "package-b-r6-1")
        self.assertNotIn("cw20_three_send_rejections_asserted", observed)

        # A non-rejected attempt must be rejected.
        accepted = copy.deepcopy(context)
        accepted["phases"][0]["cw20_fault_rollbacks"][2]["attempt"]["code"] = 0
        observed = extract_and_validate_scenario_predicates(accepted, "package-b-r6-1")
        self.assertNotIn("cw20_three_send_rejections_asserted", observed)

        # A non-atomic rollback must be rejected.
        non_atomic = copy.deepcopy(context)
        entry = non_atomic["phases"][0]["cw20_fault_rollbacks"][0]
        entry["after"]["cw20"]["deal"] = int(entry["after"]["cw20"]["deal"]) - 1
        observed = extract_and_validate_scenario_predicates(non_atomic, "package-b-r6-1")
        self.assertNotIn("settlement_atomic_rollback_verified", observed)

        # Lost pending obligations must be rejected.
        lost_pending = copy.deepcopy(context)
        lost_pending["phases"][0]["cw20_fault_rollbacks"][1]["pending_after"] = {"payments": []}
        observed = extract_and_validate_scenario_predicates(lost_pending, "package-b-r6-1")
        self.assertNotIn("settlement_atomic_rollback_verified", observed)

        # Deleted pending snapshots must be rejected.
        no_pending = copy.deepcopy(context)
        del no_pending["phases"][0]["cw20_fault_rollbacks"][0]["pending_before"]
        observed = extract_and_validate_scenario_predicates(no_pending, "package-b-r6-1")
        self.assertNotIn("settlement_atomic_rollback_verified", observed)

    def test_mandatory_runtime_shas_cannot_be_deleted(self):
        """Round 4 issue 7: expected SHA means the field is mandatory."""
        ctx_file = self.evidence_dir / "live-context.json"
        write_immutable_companion_files(self.evidence_dir)
        expected_source_id = SourceIdentity(
            marketplace_commit_sha=MARKETPLACE_SHA,
            gonka_commit_sha=GONKA_SOURCE_SHA,
            runner_version_hash="r" * 64,
            catalog_version_hash="c" * 64,
            marketplace_tree_sha=MARKETPLACE_TREE_SHA,
            gonka_tree_sha=GONKA_TREE_SHA,
        )

        def verify(source):
            ctx_file.write_text(json.dumps({"source": source, "phases": []}), encoding="utf-8")
            return verify_live_context(ctx_file, expected_source_identity=expected_source_id)

        valid_source = copy.deepcopy(real_lock_exact_e_context()["source"])
        ok, _, err = verify(valid_source)
        self.assertTrue(ok, f"Complete provenance must pass, got: {err}")

        # A correct base source SHA must not compensate for a deleted runtime SHA.
        missing_runtime_sha = copy.deepcopy(valid_source)
        del missing_runtime_sha["runtime"]["gonka_source_sha"]
        ok, _, err = verify(missing_runtime_sha)
        self.assertFalse(ok)
        self.assertIn("gonka_source_sha", err)

        empty_runtime_sha = copy.deepcopy(valid_source)
        empty_runtime_sha["runtime"]["gonka_source_sha"] = "   "
        ok, _, err = verify(empty_runtime_sha)
        self.assertFalse(ok)
        self.assertIn("gonka_source_sha", err)

        wrong_runtime_sha = copy.deepcopy(valid_source)
        wrong_runtime_sha["runtime"]["gonka_source_sha"] = "f" * 40
        ok, _, err = verify(wrong_runtime_sha)
        self.assertFalse(ok)
        self.assertIn("Observed runtime commit mismatch in live-context.json", err)

        missing_gonka_tree = copy.deepcopy(valid_source)
        del missing_gonka_tree["gonka_tree_sha"]
        ok, _, err = verify(missing_gonka_tree)
        self.assertFalse(ok)
        self.assertIn("gonka_tree_sha", err)

        wrong_gonka_tree = copy.deepcopy(valid_source)
        wrong_gonka_tree["gonka_tree_sha"] = "a" * 40
        ok, _, err = verify(wrong_gonka_tree)
        self.assertFalse(ok)
        self.assertIn("gonka_tree_sha mismatch", err)

        # Overlay and prepared-commit fields are forbidden in immutable-source evidence.
        with_harness = copy.deepcopy(valid_source)
        with_harness["gonka_test_harness_sha"] = GONKA_PREPARED_SHA
        ok, _, err = verify(with_harness)
        self.assertFalse(ok)
        self.assertIn("gonka_test_harness_sha", err)

        with_overlay = copy.deepcopy(valid_source)
        with_overlay["gonka_overlay_manifest_sha256"] = OVERLAY_MANIFEST_SHA
        ok, _, err = verify(with_overlay)
        self.assertFalse(ok)
        self.assertIn("gonka_overlay_manifest_sha256", err)

    def test_prepared_runtime_sha_mismatch_fails_verification(self):
        """P1-Issue 4: Gonka runtime source SHA mismatch or superseded E2E model must fail verification."""
        ctx_file = self.evidence_dir / "live-context.json"
        write_immutable_companion_files(self.evidence_dir)

        valid_source = copy.deepcopy(real_lock_exact_e_context()["source"])
        valid_source["runtime"]["gonka_source_sha"] = "f" * 40
        ctx_file.write_text(json.dumps({"source": valid_source, "phases": []}), encoding="utf-8")

        expected_source_id = SourceIdentity(
            marketplace_commit_sha=MARKETPLACE_SHA,
            gonka_commit_sha=GONKA_SOURCE_SHA,
            runner_version_hash="r" * 64,
            catalog_version_hash="c" * 64,
            marketplace_tree_sha=MARKETPLACE_TREE_SHA,
            gonka_tree_sha=GONKA_TREE_SHA,
        )

        ok, obs, err = verify_live_context(ctx_file, expected_source_identity=expected_source_id)
        self.assertFalse(ok)
        self.assertIn("Observed runtime commit mismatch in live-context.json", err)


class ScenarioPredicateTestCase(unittest.TestCase):
    """Base test case for scenario predicate extraction and checkpoint assertions."""

    DEFAULT_TASK_ID: str | None = None

    def observed(self, context, task_id: str | None = None):
        resolved_id = task_id or self.DEFAULT_TASK_ID
        assert resolved_id is not None
        task = get_task_by_id_or_alias(resolved_id)
        assert task is not None
        return extract_and_validate_scenario_predicates(
            context, task.scenario_selector, task.evidence_scopes
        )

    def assert_all_expected_checkpoints(self, context, task_id: str | None = None):
        resolved_id = task_id or self.DEFAULT_TASK_ID
        assert resolved_id is not None
        task = get_task_by_id_or_alias(resolved_id)
        assert task is not None
        observed = self.observed(context, resolved_id)
        for checkpoint in task.expected_checkpoints:
            self.assertIn(checkpoint, observed)
        return observed


class UnfundedLockBoundaryPredicateTests(ScenarioPredicateTestCase):
    """Names alone cannot prove Buyer absence or exact native lock boundaries."""

    DEFAULT_TASK_ID = "unfunded-lock-boundaries"

    @staticmethod
    def context():
        e4_before = {"status": "open", "buyer": None, "recipient_locked": False}
        e4_after = {"status": "locked", "buyer": None, "recipient_locked": True}
        e5_snapshot = {
            "state": {"status": "open", "buyer": None, "recipient_locked": False},
            "cw20": {"deal": 0, "buyer": 0},
            "bank_ngonka": {"deal": 0, "host": 0},
        }
        return {"scenarios": {
            "lock-e-plus-4": {
                "terms": {"target_epoch": 100},
                "contracts": {"deal": "deal-e4"},
                "phases": [{
                    "name": "lock",
                    "expected_epoch": 104,
                    "epoch_bracket": {
                        "same_epoch": True, "epoch": 104,
                        "before_height": 200, "tx_height": 201, "after_height": 202,
                    },
                    "tx": {"code": 0, "tx_hash": "e4-tx", "height": 201, "events": []},
                    "recipient_query": {"entries": [{"epoch": 100, "recipient": "deal-e4"}]},
                    "state_before": e4_before,
                    "state_after": e4_after,
                }],
            },
            "lock-e-plus-5": {
                "terms": {"target_epoch": 100},
                "contracts": {"deal": "deal-e5"},
                "phases": [{
                    "name": "lock_rejected",
                    "expected_epoch": 105,
                    "epoch_bracket": {
                        "same_epoch": True, "epoch": 105,
                        "before_height": 300, "tx_height": 301, "after_height": 302,
                    },
                    "routing_expectation": "pruned",
                    "recipient_query": {"entries": []},
                    "attempt": {
                        "code": 5, "layer": "deliver_tx", "codespace": "wasm",
                        "tx_hash": "e5-tx", "height": 301,
                        "raw_log": "lock window is closed",
                    },
                    "before": copy.deepcopy(e5_snapshot),
                    "after": copy.deepcopy(e5_snapshot),
                }],
            },
        }}

    def test_unfunded_lock_boundary_requires_included_transactions_and_absent_buyer(self):
        observed = self.observed(self.context())
        self.assertIn("phase:lock", observed)
        self.assertIn("phase:lock_rejected", observed)
        self.assertIn("unfunded_lock_boundaries_verified", observed)

    def test_native_protobuf_empty_recipient_response_proves_pruning_but_missing_query_does_not(self):
        context = self.context()
        phase = context["scenarios"]["lock-e-plus-5"]["phases"][0]
        # Synthetic response shape emitted by the native CLI for an empty
        # QueryListClaimRecipientsResponse (query.proto repeated entries).
        phase["recipient_query"] = {}
        self.assert_all_expected_checkpoints(context)
        for response in (None, [], {"entries": None}, {"entries": {}},
                         {"error": "query failed"},
                         {"entries": [{"epoch": 100, "recipient": "deal-e5"}]}):
            with self.subTest(response=response):
                bad = copy.deepcopy(context)
                bad["scenarios"]["lock-e-plus-5"]["phases"][0]["recipient_query"] = response
                self.assertNotIn("unfunded_lock_boundaries_verified", self.observed(bad))
        del phase["recipient_query"]
        self.assertNotIn("unfunded_lock_boundaries_verified", self.observed(context))

    def test_phase_names_cannot_substitute_for_unfunded_lock_proof(self):
        mutations = (
            lambda c: c["scenarios"]["lock-e-plus-4"]["phases"][0]["state_before"].pop("buyer"),
            lambda c: c["scenarios"]["lock-e-plus-4"]["phases"][0]["state_after"].__setitem__("buyer", "buyer-address"),
            lambda c: c["scenarios"]["lock-e-plus-4"]["phases"][0].__setitem__("expected_epoch", 103),
            lambda c: c["scenarios"]["lock-e-plus-4"]["phases"][0]["recipient_query"].__setitem__("entries", []),
            lambda c: c["scenarios"]["lock-e-plus-4"]["phases"][0]["tx"].__setitem__("events", [{
                "type": "transfer", "attributes": [
                    {"key": "sender", "value": "deal-e4"},
                    {"key": "recipient", "value": "buyer"},
                    {"key": "amount", "value": "1ngonka"},
                ],
            }]),
            lambda c: c["scenarios"]["lock-e-plus-4"]["phases"][0]["tx"].__setitem__("events", [{
                "type": "transfer", "attributes": [{"key": "amount", "value": "1ngonka"}],
            }]),
            lambda c: c["scenarios"]["lock-e-plus-5"]["phases"][0]["attempt"].__setitem__("code", 0),
            lambda c: c["scenarios"]["lock-e-plus-5"]["phases"][0].__setitem__("routing_expectation", "present"),
            lambda c: c["scenarios"]["lock-e-plus-5"]["phases"][0]["after"]["bank_ngonka"].__setitem__("deal", 1),
            lambda c: c["scenarios"]["lock-e-plus-5"]["phases"][0]["attempt"].pop("height"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                context = self.context()
                mutate(context)
                observed = self.observed(context)
                self.assertNotIn("unfunded_lock_boundaries_verified", observed)


class ClaimedRefundGasSweepPredicateTests(ScenarioPredicateTestCase):
    """The native gas sweep must be proved by its attempts, not its phase name."""

    DEFAULT_TASK_ID = "funded-gas-sweep"

    @staticmethod
    def context():
        limits = [50_000, 100_000, 150_000, 250_000, 500_000, 1_000_000]
        summary = {"epochPerformanceSummary": {
            "claimed": True, "epoch_index": 5, "participant_id": "host",
            "earned_coins": 100, "rewarded_coins": 20,
        }}
        lock_after = {"status": "locked", "buyer": "buyer", "recipient_locked": True}
        baseline = {
            "state": lock_after,
            "cw20": {"deal": 10, "buyer": 0, "host": 0, "fee_recipient": 0},
            "bank_ngonka": {"host": 1000, "buyer": 0, "fee_recipient": 0, "deal": 0},
        }
        phases = [
            {
            "name": "native_auto_claim", "level": "live_network",
            "summary": copy.deepcopy(summary),
                "recipient_query": {"entries": [{"epoch": 5, "recipient": "deal"}]},
                "expected": {"claimed": True, "positive_total": True},
                "actual": {"total_ngonka": 120, "exact_recipient": True},
            },
            {
                "name": "lock", "tx": {"code": 0, "tx_hash": "LOCK", "height": 100, "events": []},
                "epoch_bracket": {"same_epoch": True, "epoch": 7, "before_height": 99, "tx_height": 100, "after_height": 101},
                "recipient_query": {"entries": [{"epoch": 5, "recipient": "deal"}]},
                "state_before": {"status": "funded", "buyer": "buyer", "recipient_locked": False},
                "state_after": lock_after,
            },
        ]
        attempts = []
        observations = []
        serial = 0

        def rejected_tx(code, height, gas, raw_log):
            return {
                "layer": "deliver_tx", "code": code, "codespace": "wasm",
                "tx_hash": f"TX-{height}", "height": str(height),
                "gas_wanted": str(gas), "raw_log": raw_log, "events": [],
            }

        def fee_delta():
            nonlocal serial
            serial += 1
            return {"host": -serial}

        for index, gas in enumerate(limits):
            raw = "out of gas" if index == 0 else "refund rejected"
            tx = rejected_tx(11, 110 + index, gas, raw)
            attempts.append({"mode": "direct", "gas_limit": gas, "tx": tx,
                             "cumulative_fee_payer_deltas_ngonka": fee_delta()})
            if index == 0:
                observations.append({"mode": "direct", "gas_limit": gas, "tx": copy.deepcopy(tx)})

        sufficient = rejected_tx(5, 120, 2_000_000, "native claim for target epoch 5 is already confirmed")
        reason = {"reason": "claimed", "state_status": "locked", "layer": "deliver_tx",
                  "matched_contract_error": "native claim for target epoch"}
        attempts.append({"mode": "direct_sufficient", "gas_limit": 2_000_000, "tx": sufficient,
                         "actual_reason": reason, "cumulative_fee_payer_deltas_ngonka": fee_delta()})
        caller_tx = rejected_tx(5, 121, 2_000_000, "caller forwarded refund failed")
        attempts.append({"mode": "caller_forward", "gas_limit": 2_000_000, "tx": caller_tx,
                         "cumulative_fee_payer_deltas_ngonka": fee_delta()})

        for index, gas in enumerate(limits):
            outer = {"code": 0, "tx_hash": f"OUTER-{index}", "height": str(130 + index),
                     "gas_wanted": "2000000", "events": []}
            reply = {"success": False, "error": "out of gas" if index == 0 else "Refund failed"}
            attempts.append({"mode": "submessage_reply", "gas_limit": gas,
                             "outer_tx": outer, "reply": reply,
                             "cumulative_fee_payer_deltas_ngonka": fee_delta()})
            if index == 0:
                observations.append({"mode": "submessage_reply", "gas_limit": gas,
                                     "outer_tx": copy.deepcopy(outer), "reply": copy.deepcopy(reply)})

        final = copy.deepcopy(baseline)
        final["bank_ngonka"]["host"] -= serial
        sweep = {
            "name": "claimed_refund_gas_sweep", "level": "live_network",
            "preconditions": {
                "target_epoch": 5, "current_epoch": 8, "minimum_epoch": 8,
                "current_epoch_at_least_e_plus_3": True, "summary": copy.deepcopy(summary),
                "claimed": True, "summary_identity_valid": True,
                "work_ngonka": 100, "reward_ngonka": 20, "total_claim_ngonka": 120,
                "positive_claim": True, "fee_payer": "host", "fee_payer_roles": ["host"],
            },
            "gas_limits": limits, "sufficient_gas": 2_000_000, "outer_gas": 2_000_000,
            "baseline": baseline, "attempts": attempts,
            "out_of_gas": {"reached": True, "observations": observations},
            "sufficient_gas_contract_rejection": {
                "reached": True, "attempt": copy.deepcopy(sufficient),
                "actual_reason": copy.deepcopy(reason),
            },
            "final": final,
        }
        phases.append(sweep)
        return {"accounts": {"buyer": "buyer"}, "contracts": {"caller": "caller"},
                "scenarios": {"gas-claimed": {
                    "terms": {"target_epoch": 5},
                    "accounts": {"host": "host", "buyer": "buyer", "fee_recipient": "fee_recipient"},
                    "contracts": {"deal": "deal", "caller": "caller"},
                    "phases": phases,
                }}}

    def test_gas_sweep_requires_claim_lock_and_all_native_attempt_classes(self):
        observed = self.observed(self.context())
        self.assertIn("phase:native_auto_claim", observed)
        self.assertIn("phase:lock", observed)
        self.assertIn("phase:claimed_refund_gas_sweep", observed)
        self.assertIn("claimed_refund_gas_sweep_verified", observed)

    def test_gas_sweep_phase_name_cannot_substitute_for_attempt_evidence(self):
        mutations = (
            lambda c: c["scenarios"]["gas-claimed"]["phases"][2].pop("attempts"),
            lambda c: c["scenarios"]["gas-claimed"]["phases"][2]["attempts"].pop(),
            lambda c: c["scenarios"]["gas-claimed"]["phases"][2]["attempts"][0]["tx"].__setitem__("code", 0),
            lambda c: c["scenarios"]["gas-claimed"]["phases"][2]["attempts"][0]["tx"].__setitem__("gas_wanted", "1"),
            lambda c: c["scenarios"]["gas-claimed"]["phases"][2].__setitem__("gas_limits", [50_000]),
            lambda c: c["scenarios"]["gas-claimed"]["phases"][2]["out_of_gas"].__setitem__("observations", []),
            lambda c: c["scenarios"]["gas-claimed"]["phases"][2]["attempts"][6]["actual_reason"].__setitem__("matched_contract_error", "is already confirmed"),
            lambda c: c["scenarios"]["gas-claimed"]["phases"][2]["attempts"][6]["tx"].__setitem__("raw_log", "native claim for target epoch 6 is already confirmed"),
            lambda c: c["scenarios"]["gas-claimed"]["phases"][2]["attempts"][6]["tx"].__setitem__("raw_log", "native claim for target epoch 5 is already confirmed: out of gas"),
            lambda c: c["scenarios"]["gas-claimed"]["phases"][0]["summary"]["epochPerformanceSummary"].__setitem__("claimed", False),
            lambda c: c["scenarios"]["gas-claimed"]["phases"][1]["state_after"].__setitem__("buyer", "attacker"),
            lambda c: c["scenarios"]["gas-claimed"]["phases"][2]["final"]["cw20"].__setitem__("deal", 99),
            lambda c: c["scenarios"]["gas-claimed"]["phases"][2]["attempts"][0]["tx"].__setitem__("events", [{
                "type": "transfer", "attributes": [
                    {"key": "sender", "value": "deal"},
                    {"key": "recipient", "value": "buyer"},
                ],
            }]),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                context = self.context()
                mutate(context)
                self.assertNotIn("claimed_refund_gas_sweep_verified", self.observed(context))


class NoSaleVestingLifecyclePredicateTests(ScenarioPredicateTestCase):
    """Phase labels cannot stand in for the no-sale asset lifecycle receipts."""

    DEFAULT_TASK_ID = "no-sale-vesting-lifecycle"

    def test_phase_names_do_not_prove_the_no_sale_lifecycle(self):
        names = (
            "native_auto_claim", "lock", "liquid_donation", "liquid_donation",
            "foreign_cw20_contamination", "settle_claim", "vesting_snapshot",
            "vesting_addition", "release", "release",
        )
        context = {
            "accounts": {"buyer": "buyer"},
            "contracts": {"foreign_cw20": "foreign"},
            "scenarios": {"no-sale": {
                "terms": {"target_epoch": 5},
                "accounts": {"host": "host", "buyer": None},
                "contracts": {"deal": "deal"},
                "phases": [{"name": name} for name in names],
            }},
        }
        observed = self.observed(context)
        self.assertIn("phase:liquid_donation", observed)
        self.assertIn("phase:settle_claim", observed)
        self.assertNotIn("no_sale_vesting_lifecycle_verified", observed)

    def test_catalog_requires_cross_phase_semantic_proof(self):
        task = get_task_by_id_or_alias("no-sale-vesting-lifecycle")
        assert task is not None
        self.assertIn("no_sale_vesting_lifecycle_verified", task.expected_checkpoints)
        self.assertIn("early_release_unavailable_verified", task.expected_checkpoints)

    @staticmethod
    def early_release_phase():
        before = {
            "state": {"status": "releasing", "buyer": None},
            "cw20": {"host": 0, "fee_recipient": 0, "buyer": 0, "deal": 0},
            "foreign_cw20": {"deal": 7, "buyer": 0},
            "bank_ngonka": {"host": 100, "buyer": 50, "fee_recipient": 0, "deal": 0},
        }
        after = copy.deepcopy(before)
        after["bank_ngonka"]["host"] -= 3
        attempt = {
            "layer": "deliver_tx", "code": 5, "codespace": "wasm",
            "tx_hash": "EARLY-RELEASE", "height": "99",
            "raw_log": "contract: no additional GNK is currently available for release",
        }
        return {
            "name": "early_release_rejected", "level": "live_network",
            "deal": "deal", "caller": "host",
            "before": before, "after": after, "attempt": attempt,
            "proof": {
                "contract_error": "NothingToRelease", "layer": "deliver_tx",
                "codespace": "wasm", "tx_hash": "EARLY-RELEASE", "height": 99,
            },
            "expected": {
                "contract_error": "NothingToRelease", "deal_balance_ngonka": 0,
                "buyer": None, "state_unchanged": True,
            },
            "fee_payer_deltas_ngonka": {"host": -3},
        }

    @classmethod
    def valid_context(cls):
        summary = {"epochPerformanceSummary": {
            "claimed": True, "epoch_index": 5, "participant_id": "host",
            "earned_coins": 50, "rewarded_coins": 10,
        }}
        native = {
            "name": "native_auto_claim", "level": "live_network",
            "summary": copy.deepcopy(summary),
            "recipient_query": {"entries": [{"epoch": 5, "recipient": "deal"}]},
        }
        lock = {
            "name": "lock", "tx": {"code": 0, "tx_hash": "LOCK", "height": "101", "events": []},
            "epoch_bracket": {"same_epoch": True, "epoch": 6, "before_height": 100,
                              "tx_height": 101, "after_height": 102},
            "state_before": {"status": "open", "buyer": None, "recipient_locked": False},
            "state_after": {"status": "locked", "buyer": None, "recipient_locked": True},
        }

        def donation(label, amount, height, status, balance):
            return {
                "name": "liquid_donation", "label": label, "amount_ngonka": amount,
                "tx": {"code": 0, "tx_hash": f"DONATION-{label}", "height": str(height),
                       "events": [{"type": "transfer", "attributes": [
                           {"key": "sender", "value": "buyer"},
                           {"key": "recipient", "value": "deal"},
                           {"key": "amount", "value": f"{amount}ngonka"},
                       ]}]},
                "before": {"state": {"status": status}, "deal_bank": balance - amount},
                "after": {"state": {"status": status}, "deal_bank": balance},
                "observed_deal_delta_ngonka": amount,
            }

        contamination = {
            "name": "foreign_cw20_contamination", "amount": 7,
            "tx": {"code": 0, "tx_hash": "FOREIGN", "height": "120", "events": [{
                "type": "wasm", "attributes": [
                    {"key": "_contract_address", "value": "foreign"},
                    {"key": "action", "value": "transfer"},
                    {"key": "from", "value": "buyer"},
                    {"key": "to", "value": "deal"},
                    {"key": "amount", "value": "7"},
                ],
            }]},
            "before": {"deal": 0, "buyer": 20, "state": {"status": "locked"}},
            "after": {"deal": 7, "buyer": 13, "state": {"status": "locked"}},
        }
        settle_tx = {"code": 0, "tx_hash": "SETTLE", "height": "130", "events": [{
            "type": "wasm-claim_settled", "attributes": [
                {"key": "_contract_address", "value": "deal"},
                {"key": "deal", "value": "deal"},
                {"key": "host", "value": "host"},
            ],
        }]}
        settlement = {
            "name": "settle_claim", "tx": settle_tx, "summary": copy.deepcopy(summary),
            "before": {"state": {"status": "locked", "buyer": None},
                       "cw20": {"deal": 0}, "foreign_cw20": 7},
            "after": {"state": {"status": "releasing", "buyer": None},
                      "cw20": {"deal": 0}, "foreign_cw20": 7},
            "expected": {
                "status": "releasing", "work_ngonka": 50, "reward_ngonka": 10,
                "total_claim_ngonka": 60, "buyer_entitlement_ngonka": 0,
                "host_entitlement_ngonka": 60, "gross_usdt": 0, "fee_usdt": 0,
                "host_net_usdt": 0, "buyer_refund_usdt": 0, "deal_outflow": 0,
                "cw20_deltas": {"host": 0, "fee_recipient": 0, "buyer": 0},
            },
            "actual": {"cw20_deltas": {"host": 0, "fee_recipient": 0, "buyer": 0},
                       "deal_outflow": 0},
        }
        early = cls.early_release_phase()
        early["attempt"]["height"] = "140"
        early["proof"]["height"] = 140

        def release(label, height, before_deal, before_state, after_state, bank_before, bank_after):
            facts = {
                "deal": "deal", "host": "host", "buyer": None,
                "height": height, "tx_hash": label,
                "frozen_state": {"host": "host", "buyer": None},
                "bank_before": {"deal": before_deal, "host": bank_before},
                "bank_after": {"deal": 0, "host": bank_after},
            }
            return {
                "name": "release", "tx": {}, "facts": facts,
                "state_before": before_state, "state_after": after_state,
                "foreign_cw20_before": 7, "foreign_cw20_after": 7,
            }

        state_mid = {"status": "releasing", "buyer": None, "released_total_ngonka": 10}
        state_final = {"status": "completed", "buyer": None, "released_total_ngonka": 60}
        state_initial = {"status": "releasing", "buyer": None, "released_total_ngonka": 3}
        early["before"]["state"] = copy.deepcopy(state_initial)
        early["after"]["state"] = copy.deepcopy(state_initial)
        phases = [
            native, lock, donation("before_settlement", 3, 110, "locked", 3),
            contamination, settlement,
            release("RELEASE-INITIAL", 135, 3, settlement["after"]["state"], state_initial, 97, 100),
            early,
            {"name": "vesting_snapshot", "label": "before-additional-vesting",
             "required_non_empty": True, "normalized_epoch_amounts": [{"ngonka": 5}]},
            {"name": "vesting_addition"},
            donation("after_settlement", 2, 150, "releasing", 2),
            release("RELEASE-1", 160, 7, state_initial, state_mid, 100, 110),
            release("RELEASE-2", 180, 4, state_mid, state_final, 110, 114),
        ]
        return {
            "accounts": {"buyer": "buyer"}, "contracts": {"foreign_cw20": "foreign"},
            "scenarios": {"no-sale": {
                "terms": {"target_epoch": 5},
                "accounts": {"host": "host", "buyer": None, "fee_recipient": "fee"},
                "contracts": {"deal": "deal"}, "phases": phases,
            }},
        }

    def test_early_release_requires_included_nothing_to_release_and_zero_deal_balance(self):
        scope = {"contracts": {"deal": "deal"}, "accounts": {"host": "host"}}
        good = self.early_release_phase()
        self.assertTrue(_validate_early_release_rejected(good, scope))
        buyer_call = copy.deepcopy(good)
        buyer_call["caller"] = "buyer"
        buyer_call["after"]["bank_ngonka"]["host"] = buyer_call["before"]["bank_ngonka"]["host"]
        buyer_call["after"]["bank_ngonka"]["buyer"] -= 3
        buyer_call["fee_payer_deltas_ngonka"] = {"buyer": -3}
        self.assertTrue(_validate_early_release_rejected(buyer_call, scope, {"buyer": "buyer"}))
        self.assertFalse(_validate_early_release_rejected(buyer_call, scope, {"buyer": "other"}))
        mutations = (
            lambda p: p["attempt"].__setitem__("code", 0),
            lambda p: p["attempt"].__setitem__("raw_log", "out of gas"),
            lambda p: p["before"]["bank_ngonka"].__setitem__("deal", 1),
            lambda p: p["after"]["state"].__setitem__("buyer", "attacker"),
            lambda p: p["after"]["foreign_cw20"].__setitem__("deal", 6),
            lambda p: p["proof"].__setitem__("tx_hash", "OTHER-TX"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                phase = self.early_release_phase()
                mutate(phase)
                self.assertFalse(_validate_early_release_rejected(phase, scope))

    def test_valid_no_sale_lifecycle_binds_every_step_in_one_scope(self):
        def observe(context):
            with (
                patch("forward_e2e.suite.verifier._validate_vesting_addition", return_value=True),
                patch("forward_e2e.suite.verifier._validate_release_receipt",
                      side_effect=lambda phase, strict=False: phase.get("facts")),
            ):
                return self.observed(context)

        context = self.valid_context()
        observed = observe(context)
        self.assertIn("early_release_unavailable_verified", observed)
        self.assertIn("no_sale_vesting_lifecycle_verified", observed)
        mutations = (
            lambda c: c["scenarios"]["no-sale"]["phases"][0]["summary"]["epochPerformanceSummary"].__setitem__("claimed", False),
            lambda c: c["scenarios"]["no-sale"]["phases"][2]["tx"].__setitem__("code", 5),
            lambda c: c["scenarios"]["no-sale"]["phases"][3]["after"].__setitem__("deal", 0),
            lambda c: c["scenarios"]["no-sale"]["phases"][4]["expected"]["cw20_deltas"].__setitem__("buyer", 1),
            lambda c: c["scenarios"]["no-sale"]["phases"][4]["after"].__setitem__("foreign_cw20", 0),
            lambda c: c["scenarios"]["no-sale"]["phases"][5]["facts"]["bank_after"].__setitem__("deal", 3),
            lambda c: c["scenarios"]["no-sale"]["phases"][6]["attempt"].__setitem__("raw_log", "unrelated failure"),
            lambda c: c["scenarios"]["no-sale"]["phases"][7].__setitem__("normalized_epoch_amounts", []),
            lambda c: c["scenarios"]["no-sale"]["phases"][9]["tx"].__setitem__("height", "131"),
            lambda c: c["scenarios"]["no-sale"]["phases"].pop(),
            lambda c: c["scenarios"]["no-sale"]["phases"][11].__setitem__("foreign_cw20_after", 6),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = copy.deepcopy(context)
                mutate(changed)
                self.assertNotIn("no_sale_vesting_lifecycle_verified", observe(changed))


class Round5EvidenceTests(ScenarioPredicateTestCase):
    """Negative fixtures for the round 5 review findings.

    Every negative case removes or changes exactly one mandatory fact of a
    positive fixture that reproduces the real producer layout.
    """

    def assert_d1_missing(self, checkpoint: str, mutate) -> None:
        context = real_claim_expiry_positive_context()
        scenario = context["scenarios"]["claim-expiry-positive"]
        mutate(scenario["phases"], scenario)
        self.assertNotIn(checkpoint, self.observed(context, "claim-expiry-positive"))

    def assert_g3_missing(self, checkpoint: str, mutate) -> None:
        context = real_terminal_release_repeat_context()
        mutate(context["phases"][1])
        self.assertNotIn(checkpoint, self.observed(context, "terminal-release-repeat"))

    def assert_r2_missing(self, checkpoint: str, mutate, *, stage: str | None = None, name: str | None = None) -> None:
        context = real_package_a_context()
        for phase in context["scenarios"][R2_SCENARIO]["phases"]:
            if (stage and phase.get("stage") == stage) or (name and phase.get("name") == name):
                mutate(phase)
        self.assertNotIn(checkpoint, self.observed(context, "package-a-r1-r2"))

    # -- Issue 1: D1 / E1 refund boundaries ---------------------------------

    def test_real_claim_expiry_positive_context_is_accepted(self):
        self.assert_all_expected_checkpoints(real_claim_expiry_positive_context(), "claim-expiry-positive")

    def test_real_claim_expiry_zero_context_is_accepted(self):
        self.assert_all_expected_checkpoints(real_claim_expiry_zero_context(), "claim-expiry-zero")

    def test_zero_branch_rejects_a_non_zero_unclaimed_total(self):
        context = real_claim_expiry_zero_context()
        for phase in context["scenarios"]["claim-expiry-zero"]["phases"]:
            if phase.get("name") == "native_unclaimed_precondition":
                phase["total_ngonka"] = 1
                phase["summary"]["epochPerformanceSummary"]["rewarded_coins"] = 1
        self.assertNotIn("zero_unclaimed_genesis", self.observed(context, "claim-expiry-zero"))

    def test_review_toy_d1_context_proves_nothing(self):
        """Round 5 issue 1: the quoted three-phase context must fail."""
        observed = self.observed(toy_claim_expiry_context(), "claim-expiry-positive")
        for cp in (
            "positive_unclaimed_precondition",
            "early_refund_rejected_at_e_plus_1",
            "claim_expiry_refund_committed_at_e_plus_2",
        ):
            self.assertNotIn(cp, observed)

    def test_missing_refund_attempt_is_not_a_rejection(self):
        self.assert_d1_missing("early_refund_rejected_at_e_plus_1", lambda phases, _: phases[1].pop("attempt"))

    def test_refund_rejection_outside_deliver_tx_is_not_counted(self):
        self.assert_d1_missing("early_refund_rejected_at_e_plus_1", lambda phases, _: phases[1]["attempt"].__setitem__("layer", "simulation"))

    def test_refund_rejected_with_other_contract_error_is_not_counted(self):
        self.assert_d1_missing("early_refund_rejected_at_e_plus_1", lambda phases, _: phases[1]["attempt"].__setitem__("raw_log", "failed to execute message: out of gas"))

    def test_no_buyer_expiry_accepts_an_explicit_null_buyer_at_e_plus_1(self):
        context = real_claim_expiry_positive_context()
        context["scenarios"]["claim-expiry-positive"]["accounts"]["buyer"] = None
        self.assertIn("early_refund_rejected_at_e_plus_1", self.observed(context, "claim-expiry-positive"))

    def test_no_buyer_expiry_rejects_a_missing_buyer_field(self):
        self.assert_d1_missing("early_refund_rejected_at_e_plus_1", lambda _, sc: sc["accounts"].pop("buyer"))

    def test_rejection_one_epoch_late_does_not_prove_e_plus_1(self):
        def mutate(phases, _):
            phases[1]["observed_epoch"] = 7
            phases[1]["epoch_bracket"]["epoch"] = 7
        self.assert_d1_missing("early_refund_rejected_at_e_plus_1", mutate)

    def test_refund_committed_needs_an_included_transaction(self):
        self.assert_d1_missing("claim_expiry_refund_committed_at_e_plus_2", lambda phases, _: phases[3]["tx"].pop("height"))

    def test_refund_committed_rejects_boolean_transaction_code(self):
        """JSON false is not the chain's integer success code. All fixtures are synthetic. No network, Docker, or live chain calls."""
        self.assert_d1_missing(
            "claim_expiry_refund_committed_at_e_plus_2",
            lambda phases, _: phases[3]["tx"].__setitem__("code", False),
        )

    def test_refund_committed_needs_its_epoch_bracket(self):
        self.assert_d1_missing("claim_expiry_refund_committed_at_e_plus_2", lambda phases, _: phases[3].pop("epoch_bracket"))

    def test_late_allowed_refund_does_not_prove_exact_e_plus_2(self):
        """Round 5 issue 1: E+3 is still allowed by the producer but is not E+2."""
        def mutate(phases, _):
            phases[2]["observation"]["epoch"] = 8
            phases[3]["observed_epoch"] = 8
            phases[3]["epoch_bracket"]["epoch"] = 8
        self.assert_d1_missing("claim_expiry_refund_committed_at_e_plus_2", mutate)

    def test_refund_committed_needs_the_full_buyer_payout(self):
        self.assert_d1_missing("claim_expiry_refund_committed_at_e_plus_2", lambda phases, _: phases[3]["after"]["cw20"].__setitem__("buyer", phases[3]["after"]["cw20"]["buyer"] - 1))

    def test_refund_committed_rejects_negative_balance_with_zero_delta(self):
        """Unchanged deltas cannot validate impossible balances. All fixtures are synthetic. No network, Docker, or live chain calls."""
        def mutate(phases, _):
            phases[3]["before"]["cw20"]["host"] = -1
            phases[3]["after"]["cw20"]["host"] = -1
        self.assert_d1_missing("claim_expiry_refund_committed_at_e_plus_2", mutate)

    def test_refund_committed_needs_a_drained_deal(self):
        self.assert_d1_missing("claim_expiry_refund_committed_at_e_plus_2", lambda phases, _: phases[3]["after"]["cw20"].__setitem__("deal", 1))

    def test_refund_committed_needs_a_rejected_terminal_repeat(self):
        self.assert_d1_missing("claim_expiry_refund_committed_at_e_plus_2", lambda phases, _: phases[3]["terminal_repeat"].__setitem__("code", 0))

    def test_refund_committed_rejects_a_check_tx_terminal_repeat(self):
        """A pre-inclusion failure cannot prove terminal behavior. All fixtures are synthetic. No network, Docker, or live chain calls."""
        self.assert_d1_missing(
            "claim_expiry_refund_committed_at_e_plus_2",
            lambda phases, _: phases[3]["terminal_repeat"].__setitem__("layer", "check_tx"),
        )

    def test_refund_committed_rejects_an_unconfirmed_competing_settlement(self):
        """A rejected attempt needs an included transaction receipt. All fixtures are synthetic. No network, Docker, or live chain calls."""
        self.assert_d1_missing(
            "claim_expiry_refund_committed_at_e_plus_2",
            lambda phases, _: phases[3]["competing_settlement"].pop("tx_hash"),
        )

    def test_refund_committed_needs_a_rejected_competing_settlement(self):
        self.assert_d1_missing("claim_expiry_refund_committed_at_e_plus_2", lambda phases, _: phases[3].pop("competing_settlement"))

    def test_claim_expiry_commit_needs_a_same_epoch_unclaimed_proof(self):
        self.assert_d1_missing("claim_expiry_refund_committed_at_e_plus_2", lambda phases, _: phases.pop(2))

    def test_unclaimed_precondition_must_match_its_own_summary(self):
        def mutate(phases, _):
            phases[0]["total_ngonka"] += 1
            phases[2]["total_ngonka"] += 1
        self.assert_d1_missing("positive_unclaimed_precondition", mutate)

    def test_unclaimed_precondition_must_belong_to_this_host(self):
        self.assert_d1_missing(
            "positive_unclaimed_precondition",
            lambda _, sc: sc["accounts"].__setitem__("host", "gonka1someotherhostaddressthatisnottheone"),
        )

    def test_real_network_unconfirmed_context_is_accepted(self):
        self.assert_all_expected_checkpoints(real_network_unconfirmed_context(), "network-unconfirmed")

    def test_bare_not_found_does_not_reach_the_emergency_deadline(self):
        """Round 5 issue 1: only the E+3 observation proves the deadline."""
        context = real_network_unconfirmed_context()
        phases = context["scenarios"]["network-unconfirmed"]["phases"]
        context["scenarios"]["network-unconfirmed"]["phases"] = [{
            "name": "native_summary_absent",
            "native_query": {"grpc_code": "NotFound"},
            "expected": {"summary_absent": True},
        }]
        self.assertNotIn("emergency_deadline_reached", self.observed(context, "network-unconfirmed"))
        self.assertTrue(phases)

    def test_e_plus_2_absence_alone_is_not_the_emergency_deadline(self):
        context = real_network_unconfirmed_context()
        scenario = context["scenarios"]["network-unconfirmed"]
        scenario["phases"] = scenario["phases"][:2]
        observed = self.observed(context, "network-unconfirmed")
        self.assertIn("emergency_refund_rejected_at_e_plus_2", observed)
        self.assertNotIn("emergency_deadline_reached", observed)

    def test_emergency_rejection_is_bound_to_e_plus_2_not_e_plus_1(self):
        """Round 5 issue 1: the producer enforces E+2 for this rejection."""
        context = real_network_unconfirmed_context()
        for phase in context["scenarios"]["network-unconfirmed"]["phases"]:
            if phase.get("name") == "refund_rejected":
                phase["observed_epoch"] = E1_TARGET_EPOCH + 1
                phase["epoch_bracket"]["epoch"] = E1_TARGET_EPOCH + 1
        observed = self.observed(context, "network-unconfirmed")
        self.assertNotIn("emergency_refund_rejected_at_e_plus_2", observed)
        self.assertNotIn("early_refund_rejected_at_e_plus_1", observed)

    def test_emergency_refund_needs_host_only_policy(self):
        context = real_network_unconfirmed_context()
        for phase in context["scenarios"]["network-unconfirmed"]["phases"]:
            if phase.get("name") == "refund_committed":
                phase["after"]["state"]["gnk_release_policy"] = {"proportional": {}}
        self.assertNotIn("emergency_refund_committed", self.observed(context, "network-unconfirmed"))

    # -- Issue 2: G3 terminal release repeat --------------------------------

    def test_real_terminal_release_repeat_is_accepted(self):
        self.assert_all_expected_checkpoints(real_terminal_release_repeat_context(), "terminal-release-repeat")

    def test_deleting_both_terminal_snapshots_proves_nothing(self):
        """Round 5 issue 2: two absent snapshots must not compare equal."""
        context = real_terminal_release_repeat_context()
        phase = context["phases"][1]
        del phase["before"], phase["after"]
        observed = self.observed(context, "terminal-release-repeat")
        for cp in ("state_entitlements_unchanged", "no_deal_bank_transfer", "terminal_rejection_nothing_to_release"):
            self.assertNotIn(cp, observed)

    def test_deleting_both_deal_bank_balances_proves_nothing(self):
        def mutate(phase):
            del phase["before"]["bank"]["deal"], phase["after"]["bank"]["deal"]
        self.assert_g3_missing("no_deal_bank_transfer", mutate)

    def test_changed_terminal_entitlements_are_rejected(self):
        self.assert_g3_missing("state_entitlements_unchanged", lambda p: p["after"]["entitlements"].__setitem__("total_claim_ngonka", "1"))

    def test_changed_terminal_cw20_balance_is_rejected(self):
        self.assert_g3_missing("state_entitlements_unchanged", lambda p: p["after"]["settlement_cw20"].__setitem__("buyer", p["after"]["settlement_cw20"]["buyer"] + 1))

    def test_caller_may_not_gain_gnk_in_a_terminal_repeat(self):
        def mutate(phase):
            phase["after"]["bank"]["caller"]["ngonka"] += 1
            phase["caller_bank_deltas"] = {"ngonka": 1}
        self.assert_g3_missing("terminal_rejection_nothing_to_release", mutate)

    def test_absent_invariant_errors_list_is_not_an_empty_list(self):
        """Round 5 issue 2: the empty list must be explicitly recorded."""
        self.assert_g3_missing("terminal_rejection_nothing_to_release", lambda p: p.pop("invariant_errors"))

    def test_missing_terminal_preconditions_are_rejected(self):
        self.assert_g3_missing("terminal_rejection_nothing_to_release", lambda p: p["preconditions"].pop("scheduled_vesting_ngonka"))

    def test_terminal_rejection_must_match_the_recorded_transaction(self):
        self.assert_g3_missing("terminal_rejection_nothing_to_release", lambda p: p["terminal_rejection"].__setitem__("tx_hash", "F" * 64))

    def test_arbitrary_terminal_error_is_not_nothing_to_release(self):
        self.assert_g3_missing("terminal_rejection_nothing_to_release", lambda p: p["tx"].__setitem__("raw_log", "failed to execute message: out of gas"))

    # -- Issue 3: R2 vested gift and vesting addition -----------------------

    def test_real_package_a_context_is_accepted(self):
        self.assert_all_expected_checkpoints(real_package_a_context(), "package-a-r1-r2")

    def test_name_and_stage_alone_prove_no_r2_boundary(self):
        """Round 5 issue 3: {name, stage} used to be enough."""
        context = real_package_a_context()
        context["scenarios"][R2_SCENARIO]["phases"] = [
            {"name": "r2_gift_checkpoint", "stage": "fully_locked"},
            {"name": "r2_gift_checkpoint", "stage": "first_unlocked"},
            {"name": "r2_gift_checkpoint", "stage": "final"},
            {"name": "vesting_addition"},
        ]
        observed = self.observed(context, "package-a-r1-r2")
        for cp in ("r2_gift_fully_locked", "r2_gift_first_unlocked", "r2_gift_final_released", "vesting_addition_verified"):
            self.assertNotIn(cp, observed)

    def test_r2_stages_require_the_pre_gift_baseline(self):
        context = real_package_a_context()
        phases = context["scenarios"][R2_SCENARIO]["phases"]
        context["scenarios"][R2_SCENARIO]["phases"] = [p for p in phases if p.get("stage") != "pre_gift"]
        observed = self.observed(context, "package-a-r1-r2")
        self.assertNotIn("r2_gift_fully_locked", observed)
        self.assertNotIn("r2_gift_final_released", observed)

    def test_r2_stage_requires_its_snapshot(self):
        self.assert_r2_missing("r2_gift_fully_locked", lambda p: p.pop("snapshot"), stage="fully_locked")

    def test_r2_stage_requires_its_epoch_observation(self):
        self.assert_r2_missing("r2_gift_first_unlocked", lambda p: p.pop("epoch"), stage="first_unlocked")

    def test_partially_locked_gift_is_not_fully_locked(self):
        self.assert_r2_missing("r2_gift_fully_locked", lambda p: p["snapshot"]["native_status"].__setitem__("remaining_vesting_ngonka", "1"), stage="fully_locked")

    def test_single_tranche_gift_is_not_fully_locked(self):
        self.assert_r2_missing(
            "r2_gift_fully_locked",
            lambda p: p["snapshot"]["vesting_schedule"]["vesting_schedule"].__setitem__(
                "epoch_amounts", [{"coins": [{"denom": "ngonka", "amount": str(p["gift_amount_ngonka"])}]}]
            ),
            stage="fully_locked",
        )

    def test_r2_final_counters_must_match_the_independent_oracle(self):
        def mutate(phase):
            state = phase["snapshot"]["state"]
            state["buyer_released_ngonka"] = str(int(state["buyer_released_ngonka"]) + 1)
        self.assert_r2_missing("r2_gift_final_released", mutate, stage="final")

    def test_r2_final_requires_a_drained_vesting_schedule(self):
        self.assert_r2_missing("r2_gift_final_released", lambda p: p["snapshot"]["native_status"].__setitem__("liquid_balance_ngonka", "1"), stage="final")

    def test_r2_gift_may_not_change_frozen_entitlements(self):
        self.assert_r2_missing("r2_gift_final_released", lambda p: p["snapshot"]["entitlements"].__setitem__("total_claim_ngonka", "1"), stage="final")

    def test_vesting_addition_remainder_must_land_in_the_first_tranche(self):
        self.assert_r2_missing("vesting_addition_verified", lambda p: p.__setitem__("addition_by_epoch", list(reversed(p["addition_by_epoch"]))), name="vesting_addition")

    def test_vesting_addition_requires_a_passed_proposal(self):
        self.assert_r2_missing("vesting_addition_verified", lambda p: p["proposal"]["proposal"].__setitem__("status", "PROPOSAL_STATUS_REJECTED"), name="vesting_addition")

    def test_vesting_addition_rejects_a_status_containing_passed(self):
        """A substring cannot prove governance approval. All fixtures are synthetic. No network, Docker, or live chain calls."""
        self.assert_r2_missing(
            "vesting_addition_verified",
            lambda p: p["proposal"]["proposal"].__setitem__("status", "PROPOSAL_STATUS_NOT_PASSED"),
            name="vesting_addition",
        )

    def test_vesting_addition_must_bind_the_proposal_id(self):
        """A neighbouring passed proposal cannot prove this gift. All fixtures are synthetic. No network, Docker, or live chain calls."""
        self.assert_r2_missing(
            "vesting_addition_verified",
            lambda p: p.__setitem__("proposal_id", "2"),
            name="vesting_addition",
        )

    def test_vesting_addition_funding_must_reach_the_proposal_sender(self):
        """An unrelated successful Bank send is not gift funding. All fixtures are synthetic. No network, Docker, or live chain calls."""
        def mutate(phase):
            transfer = next(event for event in phase["funding_tx"]["events"] if event["type"] == "transfer")
            next(attr for attr in transfer["attributes"] if attr["key"] == "recipient")["value"] = "gonka1unrelatedrecipient"
        self.assert_r2_missing("vesting_addition_verified", mutate, name="vesting_addition")

    def test_vesting_addition_funding_must_cover_the_gift(self):
        """A smaller funding transfer cannot justify the gift. All fixtures are synthetic. No network, Docker, or live chain calls."""
        def mutate(phase):
            transfer = next(event for event in phase["funding_tx"]["events"] if event["type"] == "transfer")
            next(attr for attr in transfer["attributes"] if attr["key"] == "amount")["value"] = "1ngonka"
        self.assert_r2_missing("vesting_addition_verified", mutate, name="vesting_addition")

    def test_vesting_addition_proposal_message_must_match_the_gift(self):
        """A passed proposal for another amount cannot fund this gift. All fixtures are synthetic. No network, Docker, or live chain calls."""
        self.assert_r2_missing(
            "vesting_addition_verified",
            lambda p: p["proposal"]["proposal"]["messages"][0]["value"]["amount"][0].__setitem__("amount", "1"),
            name="vesting_addition",
        )

    def test_vesting_addition_must_conserve_the_injected_amount(self):
        self.assert_r2_missing("vesting_addition_verified", lambda p: p.__setitem__("amount_ngonka", p["amount_ngonka"] + 1), name="vesting_addition")

    # -- Issue 4: declared producer scopes ----------------------------------

    def test_every_native_task_declares_its_producer_scopes(self):
        for task in NATIVE_TASKS:
            self.assertTrue(task.evidence_scopes, f"{task.task_id} declares no evidence scopes")

    def test_renamed_scenario_is_not_substituted_by_a_neighbour(self):
        """Round 5 issue 4: renaming the key used to keep every checkpoint."""
        context = real_lock_exact_e_context()
        context["scenarios"]["other"] = context["scenarios"].pop("lock-exact-e")
        task = get_task_by_id_or_alias("lock-exact-e")
        observed = extract_and_validate_scenario_predicates(
            context, task.scenario_selector, task.evidence_scopes
        )
        for checkpoint in LOCK_EXACT_E_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)
        self.assertIsNotNone(validate_evidence_scopes(context, task.evidence_scopes))

    def test_missing_declared_scope_is_reported(self):
        context = real_package_a_context()
        del context["scenarios"]["r1-refund-e-plus-5"]
        task = get_task_by_id_or_alias("package-a-r1-r2")
        error = validate_evidence_scopes(context, task.evidence_scopes)
        self.assertIsNotNone(error)
        self.assertIn("r1-refund-e-plus-5", error)

    def test_routing_refund_requires_recorded_atomic_fault_rollback(self):
        context = real_claim_expiry_positive_context()
        original = context["scenarios"].pop("claim-expiry-positive")
        refund = next(p for p in original["phases"] if p.get("name") == "refund_committed")
        refund["reason"] = "routing_mismatch"
        refund["observed_epoch"] = original["terms"]["target_epoch"]
        refund["epoch_bracket"]["epoch"] = original["terms"]["target_epoch"]
        refund["after"]["state"]["refund_reason"] = "routing_mismatch"
        pristine = copy.deepcopy(refund["before"])
        refund["cw20_fault_rollback"] = {
            "attempt": copy.deepcopy(refund["terminal_repeat"]),
            "setup_tx": copy.deepcopy(refund["tx"]),
            "clear_tx": copy.deepcopy(refund["tx"]),
            "before": pristine,
            "after": copy.deepcopy(pristine),
        }
        original["phases"] = [refund]
        context["scenarios"]["routing-mismatch"] = original

        observed = extract_and_validate_scenario_predicates(
            context, "funded-routing-refunds", ["routing-mismatch"]
        )
        self.assertIn("routing_refund_committed", observed)
        self.assertIn("routing_refund_fault_rollback", observed)

        refund["cw20_fault_rollback"]["after"]["cw20"]["deal"] += 1
        observed = extract_and_validate_scenario_predicates(
            context, "funded-routing-refunds", ["routing-mismatch"]
        )
        self.assertNotIn("routing_refund_fault_rollback", observed)

    def test_routing_refund_fault_needs_included_rejection(self):
        """A simulated fault is no proof of chain rollback. All fixtures are synthetic. No network, Docker, or live chain calls."""
        context = real_claim_expiry_positive_context()
        original = context["scenarios"].pop("claim-expiry-positive")
        refund = next(p for p in original["phases"] if p.get("name") == "refund_committed")
        refund["reason"] = "routing_mismatch"
        refund["observed_epoch"] = original["terms"]["target_epoch"]
        refund["epoch_bracket"]["epoch"] = original["terms"]["target_epoch"]
        refund["after"]["state"]["refund_reason"] = "routing_mismatch"
        pristine = copy.deepcopy(refund["before"])
        refund["cw20_fault_rollback"] = {
            "attempt": copy.deepcopy(refund["terminal_repeat"]),
            "setup_tx": copy.deepcopy(refund["tx"]),
            "clear_tx": copy.deepcopy(refund["tx"]),
            "before": pristine,
            "after": copy.deepcopy(pristine),
        }
        original["phases"] = [refund]
        context["scenarios"]["routing-mismatch"] = original
        refund["cw20_fault_rollback"]["attempt"].pop("height")
        observed = extract_and_validate_scenario_predicates(
            context, "funded-routing-refunds", ["routing-mismatch"]
        )
        self.assertNotIn("routing_refund_fault_rollback", observed)

    def test_routing_task_requires_a_valid_refund_in_each_named_scope(self):
        context = real_claim_expiry_positive_context()
        original = context["scenarios"].pop("claim-expiry-positive")
        refund = next(p for p in original["phases"] if p.get("name") == "refund_committed")
        refund["reason"] = "routing_mismatch"
        refund["observed_epoch"] = original["terms"]["target_epoch"]
        refund["epoch_bracket"]["epoch"] = original["terms"]["target_epoch"]
        refund["after"]["state"]["refund_reason"] = "routing_mismatch"
        pristine = copy.deepcopy(refund["before"])
        refund["cw20_fault_rollback"] = {
            "attempt": copy.deepcopy(refund["terminal_repeat"]),
            "setup_tx": copy.deepcopy(refund["tx"]),
            "clear_tx": copy.deepcopy(refund["tx"]),
            "before": pristine,
            "after": copy.deepcopy(pristine),
        }
        original["phases"] = [{"name": "routing_mutation"}, refund]
        original["accounts"]["host"] = "host-mismatch"
        original["contracts"]["deal"] = "deal-mismatch"
        context["scenarios"]["routing-mismatch"] = original
        missing = copy.deepcopy(original)
        missing["accounts"]["host"] = "host-missing"
        missing["contracts"]["deal"] = "deal-missing"
        missing["phases"] = [{"name": "routing_mutation"}]
        context["scenarios"]["routing-missing"] = missing
        context["phases"] = [{"name": "factory_isolation"}]
        task = get_task_by_id_or_alias("funded-routing-refunds")
        self.assertIsNone(validate_evidence_scopes(context, task.evidence_scopes))
        observed = extract_and_validate_scenario_predicates(
            context, task.scenario_selector, task.evidence_scopes
        )
        self.assertFalse(set(task.expected_checkpoints).issubset(observed))

        missing_refund = copy.deepcopy(refund)
        missing["phases"].append(missing_refund)
        observed = extract_and_validate_scenario_predicates(
            context, task.scenario_selector, task.evidence_scopes
        )
        self.assertFalse(set(task.expected_checkpoints).issubset(observed))

        missing_refund["reason"] = "routing_missing"
        missing_refund["after"]["state"]["refund_reason"] = "routing_missing"
        observed = extract_and_validate_scenario_predicates(
            context, task.scenario_selector, task.evidence_scopes
        )
        self.assertNotIn("factory_isolation_verified", observed)

        self.assertFalse(set(task.expected_checkpoints).issubset(observed))

        context["accounts"] = {"host": "host-primary"}
        context["contracts"] = {"deal": "deal-primary"}
        records = {"primary": context, **context["scenarios"]}
        addresses = {name: row["contracts"]["deal"] for name, row in records.items()}
        indexes = {
            name: {
                "host": row["accounts"]["host"],
                "epoch": row["terms"]["target_epoch"],
                "row": {"address": addresses[name]},
            }
            for name, row in records.items()
        }
        listed = {"deals": [{"address": deal} for deal in addresses.values()]}
        snapshots = {
            name: {"state": {"status": "funded"}, "cw20": 10, "bank_ngonka": 0}
            for name in records
        }
        phase = {
            "name": "factory_isolation", "level": "live_network",
            "indexes": indexes, "unique_addresses": addresses,
            "list_before": listed, "list_after": copy.deepcopy(listed),
            "deal_snapshot_before": snapshots,
            "deal_snapshot_after": copy.deepcopy(snapshots),
            "duplicate_attempt": {
                "layer": "deliver_tx", "code": 1, "codespace": "wasm",
                "tx_hash": "duplicate-hash", "height": "10",
            },
        }
        context["phases"] = [phase]
        observed = extract_and_validate_scenario_predicates(
            context, task.scenario_selector, task.evidence_scopes
        )
        self.assertTrue(set(task.expected_checkpoints).issubset(observed), set(task.expected_checkpoints) - observed)

        phase["indexes"]["routing-missing"]["row"]["address"] = "wrong-deal"
        observed = extract_and_validate_scenario_predicates(
            context, task.scenario_selector, task.evidence_scopes
        )
        self.assertNotIn("factory_isolation_verified", observed)
        phase["indexes"]["routing-missing"]["row"]["address"] = "deal-missing"
        phase["deal_snapshot_after"]["primary"]["cw20"] = 11
        observed = extract_and_validate_scenario_predicates(
            context, task.scenario_selector, task.evidence_scopes
        )
        self.assertNotIn("factory_isolation_verified", observed)

    def test_bank_fault_and_retry_need_included_receipts(self):
        context = {
            "phases": [
                {"name": "native_bank_release_rollback", "attempt": {
                    "code": 1, "raw_log": "user-to-user transfers are restricted",
                }, "before": {"state": {"status": "locked"}},
                   "after": {"state": {"status": "locked"}}},
                {"name": "native_bank_release_retry", "successful_retry": {
                    "tx": {"code": 0}
                }, "restriction_after_fault": {"is_active": False},
                   "expected": {"double_payout": False}},
            ],
            "scenarios": {},
        }
        observed = extract_and_validate_scenario_predicates(
            context, "package-b-r7-1", ["<top-level>"]
        )
        self.assertNotIn("bank_send_rejection_asserted", observed)
        self.assertNotIn("bank_rollback_atomic", observed)
        self.assertNotIn("bank_send_retry_succeeds", observed)

    def test_bank_fault_and_retry_accept_complete_receipts(self):
        release = copy.deepcopy(r2_releases(real_package_a_context())[1])
        release_facts = _validate_release_receipt(release, strict=True)
        self.assertIsNotNone(release_facts)
        snapshot = {
            "state": copy.deepcopy(release["state_before"]),
            "cw20": {"deal": 1, "host": 0, "buyer": 0, "fee_recipient": 0},
            "bank_ngonka": {
                **release["bank_before"], "fee_recipient": 0,
            },
        }
        fault = {"name": "native_bank_release_rollback", "attempt": {
            "layer": "deliver_tx", "code": 1, "tx_hash": "fault-hash",
            "height": "10", "raw_log": "user-to-user transfers are restricted",
        }, "level": "native_keeper_fault",
            "restriction": {"is_active": True, "remaining_blocks": 5},
            "before": snapshot, "after": copy.deepcopy(snapshot)}
        context = {
            "phases": [
                {"name": "native_bank_release_fault_plan"},
                fault,
                {"name": "native_bank_release_retry",
                 "successful_retry": release, "fault": copy.deepcopy(fault),
                 "restriction_after_fault": {"is_active": False},
                 "expected": {"double_payout": False}},
            ],
            "contracts": {"deal": release_facts["deal"]},
            "scenarios": {},
        }
        observed = extract_and_validate_scenario_predicates(
            context, "package-b-r7-1", ["<top-level>"]
        )
        self.assertIn("bank_send_rejection_asserted", observed)
        self.assertIn("bank_rollback_atomic", observed)
        self.assertIn("bank_send_retry_succeeds", observed)
        task = get_task_by_id_or_alias("package-b-r7-1")
        self.assertFalse(set(task.expected_checkpoints).issubset(observed))

        for mutation in ("unrelated_fault", "other_deal", "different_balance"):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(context)
                if mutation == "unrelated_fault":
                    bad["phases"][2]["fault"]["attempt"]["tx_hash"] = "other-hash"
                elif mutation == "other_deal":
                    bad["contracts"]["deal"] = "other-deal"
                else:
                    for key in ("before", "after"):
                        bad["phases"][1][key]["bank_ngonka"]["deal"] += 1
                    bad["phases"][2]["fault"] = copy.deepcopy(bad["phases"][1])
                bad_observed = extract_and_validate_scenario_predicates(
                    bad, "package-b-r7-1", ["<top-level>"]
                )
                self.assertNotIn("bank_send_retry_succeeds", bad_observed)

        deal, buyer, host = (release_facts[key] for key in ("deal", "buyer", "host"))
        context["accounts"] = {"buyer": buyer, "host": host}
        context["phases"][0].update({
            "snapshot": copy.deepcopy(snapshot),
            "plan": {
                "deal": deal, "buyer": buyer, "host": host,
                "buyer_amount": release_facts["buyer_delta"],
                "host_amount": release_facts["host_delta"],
                "failing_send_index": 2,
                "allowed_earlier_recipient": buyer,
                "rejected_recipient": host,
            },
            "vesting_proof": {
                "fully_unlocked": True, "total": {"total_amount": None},
                "schedule": {"vesting_schedule": None},
            },
        })
        fault["restriction"]["current_block_height"] = 9
        fault["expected"] = {
            "failure_category": "bank_send_restriction",
            "outgoing_transfer_index": 1,
            "allowed_earlier_recipient": None,
            "rejected_recipient": buyer,
            "live_exemption": None,
            "state_and_tracked_balances_unchanged": True,
        }
        second = copy.deepcopy(fault)
        second["attempt"]["tx_hash"] = "second-fault-hash"
        second["attempt"]["height"] = "11"
        second["restriction"]["current_block_height"] = 10
        second["expected"].update({
            "outgoing_transfer_index": 2,
            "allowed_earlier_recipient": buyer,
            "rejected_recipient": host,
            "live_exemption": {
                "exemption_id": "buyer-first", "from_address": deal,
                "to_address": buyer, "max_amount": release_facts["buyer_delta"],
                "usage_limit": 1, "expiry_block": 100,
            },
        })
        repeat_height = release_facts["height"] + 1
        repeat_before = {
            "state": copy.deepcopy(release["state_after"]),
            "cw20": copy.deepcopy(snapshot["cw20"]),
            "bank_ngonka": {**release["bank_after"], "fee_recipient": 0},
        }
        repeat = {
            "name": "scenario_release_repeat", "level": "native_keeper_fault",
            "deal": deal, "caller": "independent-caller",
            "caller_ngonka_delta": -1, "before": repeat_before,
            "after": copy.deepcopy(repeat_before),
            "attempt": {
                "layer": "deliver_tx", "code": 1, "codespace": "wasm",
                "tx_hash": "repeat-hash", "height": repeat_height,
                "raw_log": "no additional GNK is currently available for release",
            },
            "terminal_rejection": {
                "contract_error": "NothingToRelease",
                "matched_contract_error": "no additional GNK is currently available for release",
                "layer": "deliver_tx", "codespace": "wasm", "code": 1,
                "tx_hash": "repeat-hash", "height": repeat_height,
            },
            "expected": {
                "selected_scenario_deal": deal, "repeat_payout": 0,
                "terminal_status": release["state_after"]["status"],
                "arbitrary_error_accepted": False,
            },
        }
        retry = context["phases"][2]
        retry.update({
            "level": "native_keeper_fault", "fault": copy.deepcopy(second),
            "repeat": copy.deepcopy(repeat),
            "expected": {
                "outgoing_transfer_index": 2,
                "rejected_recipient": host,
                "fault_off_before_retry": True,
                "exact_release_oracle": True,
                "double_payout": False,
            },
        })
        context["phases"] = [context["phases"][0], fault, second, release, repeat, retry]
        observed = extract_and_validate_scenario_predicates(
            context, task.scenario_selector, task.evidence_scopes
        )
        self.assertTrue(set(task.expected_checkpoints).issubset(observed),
                        set(task.expected_checkpoints) - observed)

        # Synthetic network rewards accrue between rollback and retry, while
        # the Deal and the exact transaction payout deltas remain unchanged.
        for rewarded_roles in (("buyer",), ("host",), ("buyer", "host")):
            with self.subTest(rewarded_roles=rewarded_roles):
                rewarded = copy.deepcopy(context)
                rewarded_release = rewarded["phases"][3]
                rewarded_repeat = rewarded["phases"][4]
                for role in rewarded_roles:
                    for section in ("bank_before", "bank_after"):
                        rewarded_release[section][role] += 17
                    for section in ("before", "after"):
                        rewarded_repeat[section]["bank_ngonka"][role] += 17
                rewarded["phases"][5]["successful_retry"] = copy.deepcopy(rewarded_release)
                rewarded["phases"][5]["repeat"] = copy.deepcopy(rewarded_repeat)
                rewarded_observed = extract_and_validate_scenario_predicates(
                    rewarded, task.scenario_selector, task.evidence_scopes
                )
                self.assertIn("r7_1_two_bank_sends_verified", rewarded_observed)
                # An incorrect payout still fails even with valid background rewards.
                bad_rewarded = copy.deepcopy(rewarded)
                bad_rewarded["phases"][3]["bank_after"]["buyer"] += 1
                bad_rewarded["phases"][5]["successful_retry"] = copy.deepcopy(
                    bad_rewarded["phases"][3]
                )
                self.assertNotIn("r7_1_two_bank_sends_verified",
                    extract_and_validate_scenario_predicates(
                        bad_rewarded, task.scenario_selector, task.evidence_scopes
                    ))

        for mutation in ("first_fault_missing", "both_target_second", "wrong_recipient",
                         "wrong_plan_amount", "broad_exemption", "repeat_changed",
                         "unvested_gnk", "repeat_bank_mismatch", "same_attempt",
                         "retry_deal_balance_changed", "repeat_wrong_deal"):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(context)
                if mutation == "first_fault_missing":
                    bad["phases"].pop(1)
                elif mutation == "both_target_second":
                    bad["phases"][1]["expected"]["outgoing_transfer_index"] = 2
                elif mutation == "wrong_recipient":
                    bad["phases"][2]["expected"]["rejected_recipient"] = buyer
                elif mutation == "wrong_plan_amount":
                    bad["phases"][0]["plan"]["buyer_amount"] += 1
                elif mutation == "broad_exemption":
                    bad["phases"][2]["expected"]["live_exemption"]["to_address"] = "*"
                elif mutation == "repeat_changed":
                    bad["phases"][4]["after"]["bank_ngonka"]["deal"] = 1
                elif mutation == "unvested_gnk":
                    bad["phases"][0]["vesting_proof"]["total"]["total_amount"] = [
                        {"denom": "ngonka", "amount": "1"}
                    ]
                elif mutation == "repeat_bank_mismatch":
                    bad["phases"][4]["before"]["bank_ngonka"]["host"] += 1
                    bad["phases"][4]["after"]["bank_ngonka"]["host"] += 1
                    bad["phases"][5]["repeat"] = copy.deepcopy(bad["phases"][4])
                elif mutation == "same_attempt":
                    bad["phases"][2]["attempt"]["tx_hash"] = "fault-hash"
                    bad["phases"][5]["fault"] = copy.deepcopy(bad["phases"][2])
                elif mutation == "retry_deal_balance_changed":
                    bad["phases"][3]["bank_before"]["deal"] += 1
                    bad["phases"][5]["successful_retry"] = copy.deepcopy(bad["phases"][3])
                else:
                    bad["phases"][4]["deal"] = "other-deal"
                    bad["phases"][5]["repeat"] = copy.deepcopy(bad["phases"][4])
                bad_observed = extract_and_validate_scenario_predicates(
                    bad, task.scenario_selector, task.evidence_scopes
                )
                self.assertNotIn("r7_1_two_bank_sends_verified", bad_observed)

    def test_emptied_declared_scope_is_reported(self):
        context = real_package_a_context()
        context["scenarios"][R2_SCENARIO]["phases"] = []
        task = get_task_by_id_or_alias("package-a-r1-r2")
        self.assertIsNotNone(validate_evidence_scopes(context, task.evidence_scopes))

    def test_top_level_phases_are_ignored_for_a_scenario_scoped_task(self):
        """A scenario-scoped task must not be rescued by harness-level phases."""
        context = real_lock_exact_e_context()
        scenario = context["scenarios"].pop("lock-exact-e")
        context["scenarios"]["other"] = scenario
        context["phases"] = copy.deepcopy(scenario["phases"])
        task = get_task_by_id_or_alias("lock-exact-e")
        observed = extract_and_validate_scenario_predicates(
            context, task.scenario_selector, task.evidence_scopes
        )
        self.assertNotIn("lock_tx_included_in_epoch_e", observed)

    def test_scenario_phases_are_ignored_for_a_top_level_task(self):
        context = real_terminal_release_repeat_context()
        context["scenarios"] = {
            "terminal-release-repeat": {
                "name": "terminal-release-repeat",
                "terms": {"target_epoch": 6},
                "phases": [real_terminal_release_repeat_phase()],
            }
        }
        context["phases"] = []
        self.assertNotIn("terminal_rejection_nothing_to_release", self.observed(context, "terminal-release-repeat"))

    def test_declared_scopes_survive_plan_serialization(self):
        task = get_task_by_id_or_alias("package-a-r1-r2")
        restored = TaskPlan.from_dict(task.to_dict())
        self.assertEqual(restored.evidence_scopes, task.evidence_scopes)

    def test_verify_live_context_rejects_a_missing_declared_scope(self):
        tmp = tempfile.TemporaryDirectory(prefix="a8-round5-scope-")
        self.addCleanup(tmp.cleanup)
        write_immutable_companion_files(Path(tmp.name))
        context = real_claim_expiry_positive_context()
        context.pop("test_fixture_only")
        context["scenarios"]["renamed"] = context["scenarios"].pop("claim-expiry-positive")
        path = Path(tmp.name) / "live-context.json"
        path.write_text(json.dumps(context), encoding="utf-8")
        task = get_task_by_id_or_alias("claim-expiry-positive")
        ok, _, err = verify_live_context(
            path,
            expected_checkpoints=task.expected_checkpoints,
            expected_run_id=context["run_id"],
            scenario_selector=task.scenario_selector,
            evidence_scopes=task.evidence_scopes,
        )
        self.assertFalse(ok)
        self.assertIn("claim-expiry-positive", err)

    def test_verify_live_context_rejects_test_only_fixtures(self):
        tmp = tempfile.TemporaryDirectory(prefix="a8-test-fixture-guard-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "live-context.json"
        for context in (
            real_claim_expiry_positive_context(),
            real_network_unconfirmed_context(),
            real_b3_context(),
        ):
            with self.subTest(run_id=context["run_id"]):
                path.write_text(json.dumps(context), encoding="utf-8")
                ok, _, err = verify_live_context(path)
                self.assertFalse(ok)
                self.assertIn("test-only fixture", err)


class Round6EvidenceTests(ScenarioPredicateTestCase):
    """Negative fixtures for the round 6 review findings.

    Positive fixtures keep the producer's real shape and values; every negative
    variant changes exactly one mandatory fact.
    """

    # -- Issue 1: D1 must read claimed inside the native summary -------------

    D1_CHECKPOINTS = (
        "positive_unclaimed_precondition",
        "claim_expiry_refund_committed_at_e_plus_2",
    )

    def test_omitted_claimed_is_the_accepted_proto3_false(self):
        observed = self.observed(real_claim_expiry_positive_context(), "claim-expiry-positive")
        for checkpoint in self.D1_CHECKPOINTS:
            self.assertIn(checkpoint, observed)

    def test_explicit_false_claimed_is_accepted(self):
        context = with_native_claimed(real_claim_expiry_positive_context(), False)
        observed = self.observed(context, "claim-expiry-positive")
        for checkpoint in self.D1_CHECKPOINTS:
            self.assertIn(checkpoint, observed)

    def test_claimed_true_in_summary_denies_the_unclaimed_precondition(self):
        context = with_native_claimed(real_claim_expiry_positive_context(), True)
        observed = self.observed(context, "claim-expiry-positive")
        for checkpoint in self.D1_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)

    def test_claimed_true_in_summary_denies_the_zero_branch(self):
        context = with_native_claimed(real_claim_expiry_zero_context(), True)
        observed = self.observed(context, "claim-expiry-zero")
        self.assertNotIn("zero_unclaimed_genesis", observed)
        self.assertNotIn("claim_expiry_refund_committed_at_e_plus_2", observed)

    def test_string_true_claimed_is_a_decoding_error(self):
        observed = self.observed(with_native_claimed(real_claim_expiry_positive_context(), "true"), "claim-expiry-positive")
        for checkpoint in self.D1_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)

    def test_string_false_claimed_is_a_decoding_error_not_false(self):
        observed = self.observed(with_native_claimed(real_claim_expiry_positive_context(), "false"), "claim-expiry-positive")
        for checkpoint in self.D1_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)

    def test_numeric_claimed_is_a_decoding_error(self):
        observed = self.observed(with_native_claimed(real_claim_expiry_positive_context(), 0), "claim-expiry-positive")
        for checkpoint in self.D1_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)

    # -- Issue 2: R2 must bind gift, stages and payouts into one proof -------

    R2_CHECKPOINTS = (
        "vesting_addition_verified",
        "r2_gift_fully_locked",
        "r2_gift_first_unlocked",
        "r2_gift_final_released",
    )

    def r2_observed(self, context):
        return self.observed(context, "package-a-r1-r2")

    def test_real_r2_sequence_is_accepted(self):
        observed = self.r2_observed(real_package_a_context())
        for checkpoint in self.R2_CHECKPOINTS:
            self.assertIn(checkpoint, observed)

    @staticmethod
    def rewrite_addition(context, amount, epochs=2):
        """Makes the recorded addition a self-consistent proof of a new amount."""
        addition = r2_addition(context)
        quotient, remainder = divmod(amount, epochs)
        by_epoch = [
            {"ngonka": quotient + (remainder if index == 0 else 0)} for index in range(epochs)
        ]
        address = addition["before"]["vesting_schedule"]["participant_address"]
        addition["amount_ngonka"] = amount
        addition["vesting_epochs"] = epochs
        addition["addition_by_epoch"] = by_epoch
        addition["expected"] = by_epoch
        proposal_amount = addition["proposal"]["proposal"]["messages"][0]["value"]["amount"][0]
        proposal_amount["amount"] = str(amount)
        transfer = next(event for event in addition["funding_tx"]["events"] if event["type"] == "transfer")
        next(attr for attr in transfer["attributes"] if attr["key"] == "amount")["value"] = f"{amount}ngonka"
        addition["after"] = {
            "vesting_schedule": {
                "epoch_amounts": [
                    {"coins": [{"amount": str(entry["ngonka"]), "denom": "ngonka"}]}
                    for entry in by_epoch
                ],
                "participant_address": address,
            }
        }
        return addition

    @staticmethod
    def prepare_released_prefix_addition(prefix_count: int = 1, **overrides):
        context = real_package_a_context()
        phase = next(
            item
            for item in context["scenarios"][R2_SCENARIO]["phases"]
            if item.get("name") == "vesting_addition"
        )
        expected = phase["expected"]
        released = sum(int(item["ngonka"]) for item in expected[:prefix_count])
        phase["after"]["vesting_schedule"]["epoch_amounts"] = [
            {"coins": [{"denom": "ngonka", "amount": str(item["ngonka"])}]}
            for item in expected[prefix_count:]
        ]
        params = dict(
            released_prefix_count=prefix_count,
            released_prefix_ngonka=released,
            before_bank_ngonka=100,
            after_bank_ngonka=100 + released,
            before_epoch=10,
            after_epoch=10,
            before_height=295,
            after_height=305,
            unlock_events=[{
                "height": 305,
                "attributes": [
                    {"key": "unlocked_amount", "value": "283856640332744ngonka"},
                    {"key": "participants_unlocked", "value": "3"},
                    {"key": "participants_processed", "value": "3"},
                    {"key": "mode", "value": "EndBlock"},
                ],
            }],
            eligible_prefix_count=1,
        )
        params.update(overrides)
        phase.update(params)
        return context, phase

    def test_addition_for_a_different_amount_proves_nothing(self):
        context = real_package_a_context()
        addition = self.rewrite_addition(context, 8000000002)
        self.assertTrue(_validate_vesting_addition(addition))
        observed = self.r2_observed(context)
        for checkpoint in self.R2_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)

    def test_stages_describing_a_different_gift_are_rejected(self):
        context = real_package_a_context()
        for stage in ("fully_locked", "first_unlocked", "final"):
            r2_stage(context, stage)["gift_amount_ngonka"] = 12345678901
        observed = self.r2_observed(context)
        for checkpoint in self.R2_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)

    def test_disagreeing_gift_amounts_between_stages_are_rejected(self):
        context = real_package_a_context()
        stage = r2_stage(context, "first_unlocked")
        stage["gift_amount_ngonka"] = int(stage["gift_amount_ngonka"]) + 1
        observed = self.r2_observed(context)
        for checkpoint in self.R2_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)

    def test_unpaid_buyer_and_host_balances_deny_the_final_checkpoint(self):
        context = real_package_a_context()
        previous = r2_stage(context, "first_unlocked")["snapshot"]["bank"]
        final_bank = r2_stage(context, "final")["snapshot"]["bank"]
        for role in ("buyer", "host"):
            final_bank[role] = copy.deepcopy(previous[role])
        observed = self.r2_observed(context)
        self.assertNotIn("r2_gift_final_released", observed)
        self.assertIn("r2_gift_first_unlocked", observed)

    def test_pre_gift_balances_in_the_final_snapshot_deny_the_payout(self):
        context = real_package_a_context()
        baseline = r2_stage(context, "pre_gift")["snapshot"]["bank"]
        final_bank = r2_stage(context, "final")["snapshot"]["bank"]
        for role in ("buyer", "host"):
            final_bank[role] = copy.deepcopy(baseline[role])
        self.assertNotIn("r2_gift_final_released", self.r2_observed(context))

    def test_deal_bank_must_match_the_liquid_balance(self):
        context = real_package_a_context()
        r2_stage(context, "first_unlocked")["snapshot"]["bank"]["deal"] = {"ngonka": 1}
        observed = self.r2_observed(context)
        self.assertNotIn("r2_gift_first_unlocked", observed)
        self.assertNotIn("r2_gift_final_released", observed)

    def test_vesting_total_must_match_the_remaining_vesting(self):
        context = real_package_a_context()
        r2_stage(context, "fully_locked")["snapshot"]["vesting_total"] = {"total_amount": [{"amount": "7", "denom": "ngonka"}]}
        self.assertNotIn("r2_gift_fully_locked", self.r2_observed(context))

    def test_fee_recipient_may_not_be_paid_again(self):
        context = real_package_a_context()
        r2_stage(context, "final")["snapshot"]["bank"]["fee_recipient"] = {"ngonka": 1}
        self.assertNotIn("r2_gift_final_released", self.r2_observed(context))

    def test_shuffled_stage_observations_are_rejected(self):
        context = real_package_a_context()
        first = r2_stage(context, "first_unlocked")
        final = r2_stage(context, "final")
        first["epoch"], final["epoch"] = final["epoch"], first["epoch"]
        observed = self.r2_observed(context)
        for checkpoint in self.R2_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)

    def test_missing_stage_denies_the_whole_sequence(self):
        context = real_package_a_context()
        phases = context["scenarios"][R2_SCENARIO]["phases"]
        context["scenarios"][R2_SCENARIO]["phases"] = [
            phase for phase in phases if not (phase.get("name") == "r2_gift_checkpoint" and phase.get("stage") == "pre_gift")
        ]
        observed = self.r2_observed(context)
        for checkpoint in self.R2_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)

    def test_duplicated_stage_denies_the_whole_sequence(self):
        context = real_package_a_context()
        context["scenarios"][R2_SCENARIO]["phases"].append(copy.deepcopy(r2_stage(context, "final")))
        observed = self.r2_observed(context)
        for checkpoint in self.R2_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)

    def test_locked_schedule_must_be_the_one_the_addition_produced(self):
        context = real_package_a_context()
        r2_stage(context, "fully_locked")["snapshot"]["vesting_schedule"]["vesting_schedule"]["epoch_amounts"].reverse()
        self.assertNotIn("r2_gift_fully_locked", self.r2_observed(context))

    def test_final_counters_must_match_the_release_oracle(self):
        context = real_package_a_context()
        state = r2_stage(context, "final")["snapshot"]["state"]
        status = r2_stage(context, "final")["snapshot"]["release_status"]
        bumped = str(int(state["buyer_released_ngonka"]) + 1)
        state["buyer_released_ngonka"] = bumped
        status["buyer_released_ngonka"] = bumped
        self.assertNotIn("r2_gift_final_released", self.r2_observed(context))

    def test_release_status_must_agree_with_the_state_counters(self):
        context = real_package_a_context()
        status = r2_stage(context, "final")["snapshot"]["release_status"]
        status["released_total_ngonka"] = str(int(status["released_total_ngonka"]) + 1)
        self.assertNotIn("r2_gift_final_released", self.r2_observed(context))

    def test_missing_addition_denies_the_gift_sequence(self):
        context = real_package_a_context()
        context["scenarios"][R2_SCENARIO]["phases"] = [
            phase for phase in context["scenarios"][R2_SCENARIO]["phases"] if phase.get("name") != "vesting_addition"
        ]
        observed = self.r2_observed(context)
        for checkpoint in self.R2_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)

    def test_standalone_addition_without_gift_stages_is_still_verified(self):
        context = real_package_a_context()
        context["scenarios"][R2_SCENARIO]["phases"] = [
            phase for phase in context["scenarios"][R2_SCENARIO]["phases"] if phase.get("name") == "vesting_addition"
        ]
        observed = self.r2_observed(context)
        self.assertIn("vesting_addition_verified", observed)
        self.assertNotIn("r2_gift_final_released", observed)

    def test_vesting_addition_accepts_bank_proven_released_prefix(self):
        context, phase = self.prepare_released_prefix_addition(1)
        self.assertIn("vesting_addition_verified", self.r2_observed(context))

        for field, value in (
            ("unlock_events", []),
            ("unlock_events", phase["unlock_events"] * 2),
            ("before_height", 305),
            ("after_height", 304),
            ("after_epoch", 9),
            ("eligible_prefix_count", 2),
        ):
            with self.subTest(field=field, value=value):
                original = phase[field]
                phase[field] = value
                self.assertNotIn("vesting_addition_verified", self.r2_observed(context))
                phase[field] = original

        phase["after_bank_ngonka"] -= 1
        self.assertNotIn("vesting_addition_verified", self.r2_observed(context))

    def test_vesting_addition_accepts_released_prefix_across_epoch_boundary(self):
        context, _ = self.prepare_released_prefix_addition(1, after_epoch=11)
        self.assertIn("vesting_addition_verified", self.r2_observed(context))

    def test_vesting_addition_rejects_future_prefix_even_when_bank_total_matches(self):
        context, _ = self.prepare_released_prefix_addition(2)
        self.assertNotIn("vesting_addition_verified", self.r2_observed(context))


class Round7EvidenceTests(ScenarioPredicateTestCase):
    """The R2 gift payout must be proven by its own release receipts.

    A multi-epoch balance trend cannot establish where the coins came from, so
    every negative here breaks one fact of the fixture's release transactions
    while leaving the stage snapshots and the final counters intact.
    """

    R2_PAYOUT_CHECKPOINTS = ("r2_gift_payout_receipts_verified", "r2_gift_final_released")

    def r2_observed(self, context):
        return self.observed(context, "package-a-r1-r2")

    def assert_payout_denied(self, context):
        observed = self.r2_observed(context)
        for checkpoint in self.R2_PAYOUT_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)
        # The stages themselves stay provable; only the payout proof is gone.
        self.assertIn("r2_gift_first_unlocked", observed)

    def assert_release_payout_denied(self, index: int, mutate):
        context = real_package_a_context()
        mutate(r2_releases(context)[index])
        self.assert_payout_denied(context)

    def assert_event_attr_denies_payout(self, event_type: str, key: str, value, index: int = 3):
        def mutate(release):
            for attribute in self.event_attributes(release, event_type):
                if attribute["key"] == key or (isinstance(key, tuple) and attribute["key"] in key):
                    attribute["value"] = value(attribute["value"]) if callable(value) else value
        self.assert_release_payout_denied(index, mutate)

    # -- The positive fixture carries the producer's real receipts -----------

    def test_positive_fixture_contains_the_real_release_phases(self):
        releases = r2_releases(real_package_a_context())
        self.assertEqual(len(releases), 4)
        heights = [int(release["tx"]["height"]) for release in releases]
        self.assertEqual(heights, [212, 237, 285, 309])
        self.assertEqual(len({release["tx"]["tx_hash"] for release in releases}), 4)
        for release in releases:
            self.assertEqual(release["tx"]["code"], 0)
            self.assertIn("epoch_bracket", release)
            self.assertIn("bank_before", release)
            self.assertIn("bank_after", release)

    def test_real_receipts_prove_the_payout(self):
        self.assert_all_expected_checkpoints(real_package_a_context(), "package-a-r1-r2")

    # -- Criterion 1: a missing release phase or receipt breaks the proof ----

    def test_removing_the_last_gift_release_denies_the_payout(self):
        context = real_package_a_context()
        context["scenarios"][R2_SCENARIO]["phases"].remove(r2_releases(context)[3])
        self.assert_payout_denied(context)

    def test_removing_the_first_gift_release_denies_the_payout(self):
        context = real_package_a_context()
        context["scenarios"][R2_SCENARIO]["phases"].remove(r2_releases(context)[2])
        self.assert_payout_denied(context)

    def test_removing_both_gift_releases_denies_the_payout(self):
        # This is the exact gap the review described: stage snapshots and the
        # addition alone must no longer produce a complete R2 proof.
        context = real_package_a_context()
        phases = context["scenarios"][R2_SCENARIO]["phases"]
        for release in r2_releases(context)[2:]:
            phases.remove(release)
        self.assert_payout_denied(context)

    def test_missing_receipt_denies_the_payout(self):
        self.assert_release_payout_denied(3, lambda r: r.pop("tx"))

    def test_failed_receipt_denies_the_payout(self):
        self.assert_release_payout_denied(3, lambda r: r["tx"].__setitem__("code", 5))

    def test_boolean_transaction_code_denies_the_payout(self):
        """A release receipt needs integer code zero, not a boolean. All fixtures are synthetic. No network, Docker, or live chain calls."""
        self.assert_release_payout_denied(3, lambda r: r["tx"].__setitem__("code", False))

    # -- Criterion 2: an underpaid receipt is not healed by the final bank ---

    def test_underpaid_buyer_in_the_receipt_is_not_covered_by_final_balances(self):
        self.assert_release_payout_denied(3, lambda r: r["bank_after"].__setitem__("buyer", int(r["bank_after"]["buyer"]) - 527567))

    def test_underpaid_host_in_the_receipt_is_not_covered_by_final_balances(self):
        self.assert_release_payout_denied(2, lambda r: r["bank_after"].__setitem__("host", int(r["bank_after"]["host"]) - 1))

    def test_recorded_actual_deltas_must_match_the_recomputation(self):
        self.assert_release_payout_denied(3, lambda r: r["actual"].__setitem__("buyer_delta", int(r["actual"]["buyer_delta"]) + 1))

    def test_deal_must_be_fully_drained_by_the_receipt(self):
        self.assert_release_payout_denied(3, lambda r: r["bank_after"].__setitem__("deal", 1))

    # -- Criterion 3: foreign periods and reused receipts do not count -------

    def test_reused_receipt_is_not_a_second_payout(self):
        context = real_package_a_context()
        releases = r2_releases(context)
        phases = context["scenarios"][R2_SCENARIO]["phases"]
        phases[phases.index(releases[3])] = copy.deepcopy(releases[2])
        self.assert_payout_denied(context)

    def test_settlement_releases_cannot_stand_in_for_the_gift(self):
        # The two releases of the original claim are real and included, but
        # they belong to another period and another pair of counters.
        context = real_package_a_context()
        releases = r2_releases(context)
        phases = context["scenarios"][R2_SCENARIO]["phases"]
        for release in releases[2:]:
            phases.remove(release)
        for release, height in zip(releases[:2], (285, 309)):
            release["tx"]["height"] = str(height)
            release["epoch_bracket"] = {
                "epoch": 11 if height == 285 else 12,
                "before_height": height - 1,
                "tx_height": height,
                "after_height": height,
                "same_epoch": True,
            }
        self.assert_payout_denied(context)

    def test_receipt_outside_the_stage_window_is_ignored(self):
        def mutate(release):
            release["tx"]["height"] = "280"
            release["epoch_bracket"] = {
                "epoch": 11,
                "before_height": 279,
                "tx_height": 280,
                "after_height": 280,
                "same_epoch": True,
            }
        self.assert_release_payout_denied(2, mutate)

    def test_bracket_must_describe_its_own_transaction(self):
        self.assert_release_payout_denied(3, lambda r: r["epoch_bracket"].__setitem__("tx_height", 308))

    def test_bracket_must_stay_inside_one_epoch(self):
        self.assert_release_payout_denied(3, lambda r: r["epoch_bracket"].__setitem__("same_epoch", False))

    # -- The chain itself ----------------------------------------------------

    def test_broken_counter_chain_denies_the_payout(self):
        self.assert_release_payout_denied(3, lambda r: r["state_before"].__setitem__("released_total_ngonka", str(int(r["state_before"]["released_total_ngonka"]) + 1)))

    def test_chain_must_start_at_the_unlocked_stage_counters(self):
        context = real_package_a_context()
        snapshot = r2_stage(context, "first_unlocked")["snapshot"]
        # Move the unlocked observation past the first payout so the chain no
        # longer begins where the stage was recorded.
        snapshot["native_status"]["liquid_balance_ngonka"] = "5000000000"
        snapshot["bank"]["deal"] = {"ngonka": 5000000000}
        observed = self.r2_observed(context)
        for checkpoint in self.R2_PAYOUT_CHECKPOINTS:
            self.assertNotIn(checkpoint, observed)

    def test_chain_must_end_on_the_final_stage_counters(self):
        self.assert_release_payout_denied(3, lambda r: r["state_after"].__setitem__("buyer_released_ngonka", "10001055135"))

    def test_counters_must_follow_the_release_oracle(self):
        def mutate(release):
            for document in (release["state_after"], release["expected"]):
                key = "buyer_released_ngonka" if "buyer_released_ngonka" in document else "buyer_released"
                document[key] = str(int(document[key]) + 1)
        self.assert_release_payout_denied(3, mutate)

    def test_release_may_not_move_immutable_state(self):
        self.assert_release_payout_denied(3, lambda r: r["state_after"].__setitem__("total_claim_ngonka", "1"))

    def test_release_rejects_ambiguous_policy_variants(self):
        for extra in ("outer", "proportional"):
            with self.subTest(extra=extra):
                def mutate(release):
                    for state in (release["state_before"], release["state_after"]):
                        policy = state["gnk_release_policy"]
                        if extra == "outer":
                            policy["host_only"] = {}
                        else:
                            policy["proportional"]["unexpected"] = "1"
                self.assert_release_payout_denied(3, mutate)

    def test_release_policy_must_match_the_deal_stage_policy(self):
        def mutate(release):
            # Scaling both terms preserves every payout and counter, so this
            # receipt's own oracle and bank/event checks can all pass.
            for state in (release["state_before"], release["state_after"]):
                policy = state["gnk_release_policy"]["proportional"]
                for key in ("buyer_share_numerator", "share_denominator"):
                    policy[key] = str(int(policy[key]) * 2)

        self.assert_release_payout_denied(3, mutate)

    def test_release_may_not_spend_the_foreign_cw20(self):
        self.assert_release_payout_denied(3, lambda r: r.__setitem__("foreign_cw20_after", 1))

    # -- The chain's own events must corroborate the payout ------------------

    @staticmethod
    def event_attributes(release, event_type):
        for event in release["tx"]["events"]:
            if event["type"] == event_type:
                return event["attributes"]
        raise KeyError(event_type)

    def test_missing_transfer_event_denies_the_payout(self):
        def mutate(release):
            buyer = release["state_after"]["buyer"]
            release["tx"]["events"] = [
                event
                for event in release["tx"]["events"]
                if not (
                    event["type"] == "transfer"
                    and any(
                        attribute["key"] == "recipient" and attribute["value"] == buyer
                        for attribute in event["attributes"]
                    )
                )
            ]
        self.assert_release_payout_denied(3, mutate)

    def test_transfer_to_a_stranger_denies_the_payout(self):
        self.assert_event_attr_denies_payout("transfer", "recipient", "gonka1strangerstrangerstrangerstrangerstrange")

    def test_duplicate_sender_cannot_hide_an_extra_deal_transfer(self):
        """Repeated event keys cannot erase an extra Deal payout. All fixtures are synthetic. No network, Docker, or live chain calls."""
        def mutate(release):
            deal = next(
                attr["value"] for attr in self.event_attributes(release, "transfer")
                if attr["key"] == "sender"
            )
            release["tx"]["events"].append({
                "type": "transfer",
                "attributes": [
                    {"key": "sender", "value": deal},
                    {"key": "recipient", "value": "gonka1strangerstrangerstrangerstrangerstrange"},
                    {"key": "amount", "value": "1ngonka"},
                    {"key": "sender", "value": "gonka1unrelatedunrelatedunrelatedunrelatedun"},
                ],
            })
        self.assert_release_payout_denied(3, mutate)

    def test_transferred_amount_must_equal_the_recomputed_delta(self):
        self.assert_event_attr_denies_payout("transfer", "amount", "1ngonka")

    def test_transfer_cannot_hide_an_extra_denom(self):
        """A Deal release sends only GNK in each BankMsg. All fixtures are synthetic. No network, Docker, or live chain calls."""
        self.assert_event_attr_denies_payout(
            "transfer", "amount", lambda value: f"{value},1uatom"
        )

    def test_transfer_cannot_hide_a_zero_amount_message(self):
        """A release emits no zero-value BankMsg. All fixtures are synthetic. No network, Docker, or live chain calls."""
        def mutate(release):
            attrs = copy.deepcopy(self.event_attributes(release, "transfer"))
            next(attr for attr in attrs if attr["key"] == "amount")["value"] = "0ngonka"
            release["tx"]["events"].append({"type": "transfer", "attributes": attrs})
        self.assert_release_payout_denied(3, mutate)

    def test_release_event_must_agree_with_the_oracle(self):
        self.assert_event_attr_denies_payout("wasm-gnk_released", "host_delta_ngonka", lambda v: str(int(v) + 1))

    def test_release_event_must_name_this_scenarios_deal(self):
        self.assert_event_attr_denies_payout("wasm-gnk_released", ("deal", "_contract_address"), "gonka1otherdealotherdealotherdealotherdealother")

    def test_release_event_must_name_this_scenarios_host(self):
        self.assert_event_attr_denies_payout("wasm-gnk_released", "host", "gonka1otherhostotherhostotherhostotherhostothe")

    def test_wrong_entry_point_denies_the_payout(self):
        self.assert_event_attr_denies_payout("wasm-gnk_released", "entry_point", "emergency_refund")

    # -- release_completed is no longer granted by a phase name --------------

    def test_every_recorded_release_phase_reproduces(self):
        # Includes the settlement release that closes the Deal, so the legal
        # "releasing" -> "completed" transition must not be rejected.
        releases = r2_releases(real_package_a_context())
        statuses = [
            (release["state_before"]["status"], release["state_after"]["status"])
            for release in releases
        ]
        self.assertIn(("releasing", "completed"), statuses)
        for release in releases:
            self.assertIsNotNone(_validate_release_receipt(release))

    def test_release_completed_requires_a_reproducible_receipt(self):
        context = real_package_a_context()
        phases = context["scenarios"][R2_SCENARIO]["phases"]
        releases = r2_releases(context)
        for release in releases[1:]:
            phases.remove(release)
        self.assertIn("release_completed", self.r2_observed(context))

        releases[0]["bank_after"]["host"] = int(releases[0]["bank_after"]["host"]) + 1
        self.assertNotIn("release_completed", self.r2_observed(context))

    def test_release_may_not_rewind_a_completed_deal(self):
        self.assert_release_payout_denied(3, lambda r: r["state_after"].__setitem__("status", "releasing"))

    def test_payouts_must_add_up_to_the_whole_gift(self):
        context = real_package_a_context()
        phases = context["scenarios"][R2_SCENARIO]["phases"]
        extra = copy.deepcopy(r2_releases(context)[3])
        extra["tx"]["tx_hash"] = "F" * 64
        extra["tx"]["height"] = "307"
        extra["epoch_bracket"] = {
            "epoch": 12,
            "before_height": 306,
            "tx_height": 307,
            "after_height": 307,
            "same_epoch": True,
        }
        phases.append(extra)
        self.assert_payout_denied(context)


class Round8ReleaseSchemaTests(ScenarioPredicateTestCase):
    """Both release producers must write evidence the validator accepts.

    scripts/acceptance_harness.py exposes two commands that record a GNK release:
    `release` (funded-claim, top-level scope) and `release-scenario` (R2 and
    friends). Round 7 checked only the second shape, which broke the first.
    """

    @staticmethod
    def producer_tree():
        source = (REPO_ROOT / "scripts" / "acceptance_harness.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        return {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }

    def funded_claim_observed(self, context):
        return self.observed(context, "funded-claim")

    def assert_both_funded_claim_releases_reject(self, mutate):
        for build in (legacy_funded_claim_release_phase, unified_funded_claim_release_phase):
            with self.subTest(build=build.__name__):
                phase = build()
                mutate(phase)
                context = funded_claim_release_context(phase)
                self.assertNotIn("release_completed", self.funded_claim_observed(context))

    @staticmethod
    def no_buyer_release_phase():
        phase = unified_funded_claim_release_phase()
        before = phase["state_before"]
        after = phase["state_after"]
        available = phase["bank_before"]["deal"]
        previous = int(before["released_total_ngonka"])
        released = previous + available
        denominator = int(before["total_claim_ngonka"])
        policy = {
            "proportional": {
                "buyer_share_numerator": "0",
                "share_denominator": str(denominator),
            }
        }
        for state, total in ((before, previous), (after, released)):
            state["buyer"] = None
            state["buyer_entitlement_ngonka"] = "0"
            state["buyer_released_ngonka"] = "0"
            state["host_released_ngonka"] = str(total)
            state["released_total_ngonka"] = str(total)
            state["gnk_release_policy"] = copy.deepcopy(policy)

        phase["bank_after"]["deal"] = 0
        phase["bank_after"]["buyer"] = phase["bank_before"]["buyer"]
        phase["bank_after"]["host"] = phase["bank_before"]["host"] + available
        phase["expected"] = {
            "released_total": released,
            "buyer_released": 0,
            "host_released": released,
            "buyer_delta": 0,
            "host_delta": available,
        }
        phase["actual"].update(
            buyer_delta=0,
            host_delta=available,
            address_delta=available,
            released_total=released,
            buyer_released=0,
            host_released=released,
        )

        release_event = next(
            event for event in phase["tx"]["events"]
            if event["type"] == "wasm-gnk_released"
        )
        attrs = {
            item["key"]: item for item in release_event["attributes"]
        }
        attrs.pop("buyer", None)
        for key, value in (
            ("released_total_ngonka", released),
            ("buyer_released_ngonka", 0),
            ("host_released_ngonka", released),
            ("buyer_delta_ngonka", 0),
            ("host_delta_ngonka", available),
            ("buyer_share_numerator", 0),
            ("share_denominator", denominator),
            ("available_balance_ngonka", available),
        ):
            attrs[key]["value"] = str(value)
        release_event["attributes"] = list(attrs.values())
        deal = attrs["deal"]["value"]
        host = attrs["host"]["value"]
        phase["tx"]["events"] = [
            event for event in phase["tx"]["events"]
            if event["type"] != "transfer"
        ] + [{
            "type": "transfer",
            "attributes": [
                {"key": "recipient", "value": host},
                {"key": "sender", "value": deal},
                {"key": "amount", "value": f"{available}ngonka"},
            ],
        }]
        return phase

    def test_release_accepts_an_explicit_no_buyer_host_only_receipt(self):
        phase = self.no_buyer_release_phase()
        self.assertIsNotNone(_validate_release_receipt(phase))

    def test_release_does_not_treat_a_missing_buyer_event_as_buyer_absence(self):
        phase = self.no_buyer_release_phase()
        phase["state_before"]["buyer"] = "gonka1unexpectedbuyer"
        phase["state_after"]["buyer"] = "gonka1unexpectedbuyer"
        self.assertIsNone(_validate_release_receipt(phase))

    # -- Producer and fixture agree on one schema ---------------------------

    def test_both_release_commands_write_the_shared_receipt(self):
        functions = self.producer_tree()
        for name in ("release", "release_scenario"):
            calls = {
                node.func.id
                for node in ast.walk(functions[name])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            self.assertIn("close_gnk_release", calls, name)
            self.assertIn("open_gnk_release", calls, name)

    def test_unified_fixture_matches_the_producer_record(self):
        """Criterion 1: the positive fixture is what the producer writes today."""
        functions = self.producer_tree()
        returned = next(
            node.value
            for node in ast.walk(functions["close_gnk_release"])
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
        )
        keys = {key.value for key in returned.keys}
        actual_keys = {
            key.value
            for key, value in zip(returned.keys, returned.values)
            if key.value == "actual"
            for key in value.keys
        }
        phase = unified_funded_claim_release_phase()
        self.assertEqual(set(phase), keys)
        self.assertEqual(set(phase["actual"]), actual_keys)

    # -- Criterion 1: the funded-claim release proves release_completed ------

    def test_unified_funded_claim_release_is_accepted(self):
        context = funded_claim_release_context(unified_funded_claim_release_phase())
        self.assertIn("release_completed", self.funded_claim_observed(context))

    def test_legacy_funded_claim_release_is_accepted(self):
        # Contexts recorded before the shapes merged carry no epoch bracket,
        # caller or address_delta; their own evidence still has to be enough.
        phase = legacy_funded_claim_release_phase()
        self.assertNotIn("epoch_bracket", phase)
        self.assertNotIn("caller", phase)
        self.assertNotIn("address_delta", phase["actual"])
        context = funded_claim_release_context(phase)
        self.assertIn("release_completed", self.funded_claim_observed(context))

    # -- Criterion 3: both producers are covered, positively and negatively --

    def test_both_producer_shapes_reproduce(self):
        receipts = [
            *r2_releases(real_package_a_context()),
            legacy_funded_claim_release_phase(),
            unified_funded_claim_release_phase(),
        ]
        for receipt in receipts:
            self.assertIsNotNone(_validate_release_receipt(receipt))

    def test_funded_claim_release_still_needs_a_real_receipt(self):
        self.assert_both_funded_claim_releases_reject(lambda p: p["tx"].__setitem__("code", 5))

    def test_funded_claim_release_still_needs_the_oracle(self):
        self.assert_both_funded_claim_releases_reject(lambda p: p["state_after"].__setitem__("buyer_released_ngonka", str(int(p["state_after"]["buyer_released_ngonka"]) + 1)))

    def test_funded_claim_release_still_needs_the_local_bank_delta(self):
        self.assert_both_funded_claim_releases_reject(lambda p: p["bank_after"].__setitem__("buyer", int(p["bank_after"]["buyer"]) - 1))

    def test_funded_claim_release_still_needs_the_transfer_events(self):
        self.assert_both_funded_claim_releases_reject(lambda p: p["tx"].__setitem__("events", [e for e in p["tx"]["events"] if e["type"] != "transfer"]))

    def test_funded_claim_release_rejects_a_broken_recorded_actual(self):
        self.assert_both_funded_claim_releases_reject(lambda p: p["actual"].__setitem__("released_total", int(p["actual"]["released_total"]) + 1))

    # -- Criterion 2: R2 keeps the strict receipt requirement ----------------

    def test_reduced_receipt_cannot_join_the_r2_gift_chain(self):
        context = real_package_a_context()
        r2_releases(context)[3].pop("epoch_bracket")
        observed = self.observed(context, "package-a-r1-r2")
        self.assertNotIn("r2_gift_payout_receipts_verified", observed)
        self.assertNotIn("r2_gift_final_released", observed)

    def test_gift_chain_still_requires_caller_and_fee(self):
        for field in ("caller", "caller_native_fee_delta"):
            with self.subTest(field=field):
                context = real_package_a_context()
                r2_releases(context)[3].pop(field)
                self.assertNotIn("r2_gift_final_released", self.observed(context, "package-a-r1-r2"))

    def test_gift_chain_still_requires_the_recorded_address_delta(self):
        context = real_package_a_context()
        r2_releases(context)[3]["actual"].pop("address_delta")
        self.assertNotIn("r2_gift_final_released", self.observed(context, "package-a-r1-r2"))

    def test_r2_positive_evidence_is_unaffected(self):
        self.assert_all_expected_checkpoints(real_package_a_context(), "package-a-r1-r2")

    def test_historical_gonka_shas_are_not_exempt_when_plan_identity_differs(self):
        import forward_e2e.suite.verifier as verifier_mod

        self.assertFalse(hasattr(verifier_mod, "APPROVED_GONKA_BASE_SHAS"))
        task = get_task_by_id_or_alias("lock-exact-e")
        assert task is not None
        context = real_lock_exact_e_context()
        # Context has gonka_sha = 29a58fcf64b87967cb874b6169ce2c61e1f269b1;
        # when the plan identity specifies a different commit, verify_live_context must reject it.
        different_identity = SourceIdentity(
            marketplace_commit_sha=MARKETPLACE_SHA,
            gonka_commit_sha="1" * 40,
            runner_version_hash="r" * 64,
            catalog_version_hash="c" * 64,
            marketplace_tree_sha=MARKETPLACE_TREE_SHA,
            gonka_tree_sha=GONKA_TREE_SHA,
        )
        with tempfile.TemporaryDirectory(prefix="a8-test-hist-sha-") as tmp:
            write_immutable_companion_files(Path(tmp))
            ctx_path = Path(tmp) / "live-context.json"
            ctx_path.write_text(json.dumps(context), encoding="utf-8")
            ok, _obs, err = verify_live_context(
                ctx_path,
                task,
                expected_run_id=context["run_id"],
                expected_source_identity=different_identity,
            )
            self.assertFalse(ok)
            self.assertIn("Gonka source commit SHA mismatch", err or "")


class IndexedTaskEvidenceTests(unittest.TestCase):
    def test_resealed_wasm_report_must_match_the_indexed_wasm_bytes(self):
        with tempfile.TemporaryDirectory(prefix="a8-test-wasm-hash-") as tmp:
            suite_dir = write_suite_output(
                suite_dir=Path(tmp) / "suite-wasm",
                suite_id="suite-wasm",
                scenarios=["wasm-abi-boundary"],
            )
            self.assertEqual(
                verify_and_recalculate_suite(suite_dir).result.overall_status,
                ExecutionStatus.PASSED,
            )
            report_path = next(suite_dir.rglob("abi.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["wasm_sha256"] = "0" * 64
            report_path.write_text(json.dumps(report), encoding="utf-8")
            index_path = suite_dir / "artifact-index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            relative = report_path.relative_to(suite_dir).as_posix()
            entry = next(item for item in index["artifacts"] if item["relative_path"] == relative)
            entry["sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
            entry["size_bytes"] = report_path.stat().st_size
            index_path.write_text(json.dumps(index), encoding="utf-8")

            verification = verify_and_recalculate_suite(suite_dir)

            self.assertEqual(verification.result.overall_status, ExecutionStatus.FAILED)
            self.assertIn("Wasm hash mismatch", " ".join(verification.integrity_errors))

    def test_default_verifier_cannot_parse_transient_passing_junit(self):
        """Direct reporter/orchestrator callers must bind parsed bytes to the index."""
        from xml.etree import ElementTree as ET

        with tempfile.TemporaryDirectory(prefix="a8-test-default-indexed-native-") as tmp:
            suite_dir = write_suite_output(
                suite_dir=Path(tmp) / "suite-native",
                suite_id="suite-native",
                scenarios=["lock-exact-e"],
            )
            junit_path = next(suite_dir.rglob("TEST-MarketplaceContractAcceptanceTests.xml"))
            passing_bytes = junit_path.read_bytes()
            junit_path.write_bytes(b"invalid indexed JUnit\n")
            index_path = suite_dir / "artifact-index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            rel = junit_path.relative_to(suite_dir).as_posix()
            entry = next(item for item in index["artifacts"] if item["relative_path"] == rel)
            entry["sha256"] = hashlib.sha256(junit_path.read_bytes()).hexdigest()
            entry["size_bytes"] = junit_path.stat().st_size
            index_path.write_text(json.dumps(index), encoding="utf-8")

            with patch("forward_e2e.suite.verifier.ET.parse", return_value=ET.ElementTree(ET.fromstring(passing_bytes))):
                verification = verify_and_recalculate_suite(suite_dir)

            self.assertEqual(verification.result.overall_status, ExecutionStatus.FAILED)
            self.assertIn("native evidence invalid", " ".join(verification.integrity_errors))

    def test_native_junit_cannot_be_parsed_from_transient_passing_bytes(self):
        """A passing XML substitution cannot override indexed failed JUnit bytes."""
        from xml.etree import ElementTree as ET

        with tempfile.TemporaryDirectory(prefix="a8-test-indexed-native-") as tmp:
            suite_dir = write_suite_output(
                suite_dir=Path(tmp) / "suite-native",
                suite_id="suite-native",
                scenarios=["lock-exact-e"],
            )
            junit_path = next(suite_dir.rglob("TEST-MarketplaceContractAcceptanceTests.xml"))
            passing_bytes = junit_path.read_bytes()
            junit_path.write_bytes(b"invalid indexed JUnit\n")
            index_path = suite_dir / "artifact-index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            rel = junit_path.relative_to(suite_dir).as_posix()
            entry = next(item for item in index["artifacts"] if item["relative_path"] == rel)
            entry["sha256"] = hashlib.sha256(junit_path.read_bytes()).hexdigest()
            entry["size_bytes"] = junit_path.stat().st_size
            index_path.write_text(json.dumps(index), encoding="utf-8")

            baseline = verify_and_recalculate_suite(
                suite_dir,
                artifact_index_bytes=index_path.read_bytes(),
                artifact_index_checked=True,
                artifact_reader=lambda path: path.read_bytes(),
            )
            self.assertEqual(baseline.result.overall_status, ExecutionStatus.FAILED)
            self.assertIn("native evidence invalid", " ".join(baseline.integrity_errors))

            def transient_reader(path):
                if path == junit_path:
                    return passing_bytes
                return path.read_bytes()

            with patch("forward_e2e.suite.verifier.ET.parse", return_value=ET.ElementTree(ET.fromstring(passing_bytes))):
                verification = verify_and_recalculate_suite(
                    suite_dir,
                    artifact_index_bytes=index_path.read_bytes(),
                    artifact_index_checked=True,
                    artifact_reader=transient_reader,
                )
            self.assertEqual(verification.result.overall_status, ExecutionStatus.FAILED)
            self.assertIn("differs from its indexed bytes", " ".join(verification.integrity_errors))

    def test_other_boundary_artifacts_cannot_be_parsed_from_transient_bytes(self):
        cases = (
            ("go-query-error-classification", "go-test.json"),
            ("wasm-abi-boundary", "abi.json"),
            ("contract-network-unconfirmed-policy", "contract-network-unconfirmed-policy.log"),
            ("contract-query-fault-policy", "contract-query-fault-policy.log"),
        )
        for scenario, filename in cases:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory(
                prefix="a8-test-indexed-boundary-",
            ) as tmp:
                suite_dir = write_suite_output(
                    suite_dir=Path(tmp) / "suite-boundary",
                    suite_id="suite-boundary",
                    scenarios=[scenario],
                )
                evidence_path = next(suite_dir.rglob(filename))
                passing_bytes = evidence_path.read_bytes()
                evidence_path.write_bytes(b"invalid indexed evidence\n")
                index_path = suite_dir / "artifact-index.json"
                index = json.loads(index_path.read_text(encoding="utf-8"))
                rel = evidence_path.relative_to(suite_dir).as_posix()
                entry = next(item for item in index["artifacts"] if item["relative_path"] == rel)
                entry["sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
                entry["size_bytes"] = evidence_path.stat().st_size
                index_path.write_text(json.dumps(index), encoding="utf-8")

                original_read_text = Path.read_text

                def substituted_text(path, *args, **kwargs):
                    if path == evidence_path:
                        return passing_bytes.decode("utf-8", errors="replace")
                    return original_read_text(path, *args, **kwargs)

                def transient_reader(path):
                    if path == evidence_path:
                        return passing_bytes
                    return path.read_bytes()

                with patch.object(Path, "read_text", new=substituted_text):
                    verification = verify_and_recalculate_suite(
                        suite_dir,
                        artifact_index_bytes=index_path.read_bytes(),
                        artifact_index_checked=True,
                        artifact_reader=transient_reader,
                    )
                self.assertEqual(verification.result.overall_status, ExecutionStatus.FAILED)
                self.assertIn("differs from its indexed bytes", " ".join(verification.integrity_errors))

    def test_transient_passing_go_report_cannot_override_indexed_failed_report(self):
        """Parsed boundary bytes must be the bytes whose indexed hash passed."""
        with tempfile.TemporaryDirectory(prefix="a8-test-indexed-boundary-") as tmp:
            suite_dir = write_suite_output(
                suite_dir=Path(tmp) / "suite-go-boundary",
                suite_id="suite-go-boundary",
                scenarios=["go-boundary"],
            )
            report_path = next(suite_dir.rglob("report.json"))
            passing_bytes = report_path.read_bytes()
            failed_report = json.loads(passing_bytes)
            failed_report["status"] = "FAIL"
            report_path.write_text(json.dumps(failed_report), encoding="utf-8")
            index_path = suite_dir / "artifact-index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            rel = report_path.relative_to(suite_dir).as_posix()
            entry = next(item for item in index["artifacts"] if item["relative_path"] == rel)
            entry["sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
            entry["size_bytes"] = report_path.stat().st_size
            index_path.write_text(json.dumps(index), encoding="utf-8")

            original_read_text = Path.read_text

            def substituted_text(path, *args, **kwargs):
                if path == report_path:
                    return passing_bytes.decode("utf-8")
                return original_read_text(path, *args, **kwargs)

            def transient_reader(path):
                if path == report_path:
                    return passing_bytes
                return path.read_bytes()

            with patch.object(Path, "read_text", new=substituted_text):
                verification = verify_and_recalculate_suite(
                    suite_dir,
                    artifact_index_bytes=index_path.read_bytes(),
                    artifact_index_checked=True,
                    artifact_reader=transient_reader,
                )
            self.assertEqual(verification.result.overall_status, ExecutionStatus.FAILED)
            self.assertIn("differs from its indexed bytes", " ".join(verification.integrity_errors))


class FundedClaimPathEvidenceTests(unittest.TestCase):
    """The split funded producer must prove one resumed claim and fee fault."""

    @staticmethod
    def context():
        # No post-split live run is recorded. The settlement is the existing
        # producer-shaped synthetic fixture; the surrounding records use the
        # fields written by claim_settle at scripts/acceptance_harness.py:5280-5415.
        settled = real_claim_settle_phase()
        fee_fault = copy.deepcopy(settled["cw20_fault_rollbacks"][1])
        settled["cw20_fault_rollbacks"] = [fee_fault]
        config = settled["deal_config"]
        accounts = {
            "host": config["host"],
            "fee_recipient": config["fee_recipient"],
            "buyer": settled["settlement_payments"]["buyer"]["recipient"],
        }
        prepared = {
            "name": "claim_prepared", "claim_tx": copy.deepcopy(settled["claim_tx"]),
            "summary": copy.deepcopy(settled["summary"]),
            "deal_config": copy.deepcopy(config), "before": copy.deepcopy(settled["before"]),
        }
        payments = copy.deepcopy(settled["settlement_payments"])
        for payment in payments.values():
            payment["paid_micro_usdt"] = payment["accrued_micro_usdt"]
            payment["pending_micro_usdt"] = "0"
        delivery = {
            "name": "settlement_delivery_verified", "deal": config["deal_address"],
            "settle_tx": copy.deepcopy(settled["settle_tx"]),
            "expected": copy.deepcopy(settled["expected"]),
            "actual": {
                "cw20_deltas": copy.deepcopy(settled["actual"]["cw20_deltas"]),
                "deal_outflow": settled["actual"]["deal_outflow"],
            },
            "payments": payments,
            "state": copy.deepcopy(settled["after"]["deal_state"]),
        }
        return {
            "accounts": accounts,
            "contracts": {"deal": config["deal_address"], "cw20": config["settlement_cw20"]},
            "terms": {"target_epoch": config["target_epoch"],
                      "budget_micro_usdt": config["buyer_budget_micro_usdt"]},
            "phases": [prepared, {"name": "usdt_fault_rollback", "evidence": copy.deepcopy(fee_fault)},
                       delivery, settled],
        }

    @staticmethod
    def observed(context):
        return extract_and_validate_scenario_predicates(
            context, "funded-claim", [TOP_LEVEL_SCOPE]
        )

    def test_producer_shaped_resumed_claim_fault_delivery_and_repeat_are_verified(self):
        self.assertIn(
            "funded_claim_path_verified",
            get_task_by_id_or_alias("funded-claim").expected_checkpoints,
        )
        self.assertIn("funded_claim_path_verified", self.observed(self.context()))

    def test_named_phases_without_receipts_cannot_complete_funded_claim(self):
        context = self.context()
        context["phases"][0] = {"name": "claim_prepared"}
        observed = self.observed(context)
        self.assertIn("phase:claim_prepared", observed)
        self.assertNotIn("funded_claim_path_verified", observed)

    def test_missing_or_changed_funded_claim_facts_reject_path(self):
        cases = {
            "native_claim_receipt": lambda c: c["phases"][0].pop("claim_tx"),
            "summary_identity": lambda c: c["phases"][0]["summary"]["epochPerformanceSummary"].update(participant_id="foreign"),
            "prepared_balance": lambda c: c["phases"][0]["before"]["cw20"].update(deal="1"),
            "locked_buyer": lambda c: c["phases"][3]["before"]["deal_state"].update(buyer="foreign"),
            "fault_receipt": lambda c: c["phases"][1]["evidence"]["attempt"].update(layer="simulation"),
            "fault_phase_binding": lambda c: c["phases"][1]["evidence"]["fault"].update(recipient="foreign"),
            "fault_snapshot": lambda c: c["phases"][3]["cw20_fault_rollbacks"][0]["after"]["cw20"].update(deal="1"),
            "delivery_amount": lambda c: c["phases"][2]["payments"]["fee"].update(paid_micro_usdt="1"),
            "delivery_receipt": lambda c: c["phases"][2].pop("settle_tx"),
            "buyer_withdrawal": lambda c: c["phases"][3]["withdrawal_txs"].pop("buyer"),
            "terminal_repeat": lambda c: c["phases"][3]["settle_repeat"].pop("attempt"),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                context = self.context()
                mutate(context)
                self.assertNotIn("funded_claim_path_verified", self.observed(context))


class FundedReleaseLifecycleEvidenceTests(unittest.TestCase):
    """The funded Deal requires its second GNK release attempt, in either form."""

    @staticmethod
    def set_event(event, key, value):
        for attribute in event["attributes"]:
            if attribute["key"] == key:
                attribute["value"] = str(value)
                return
        raise AssertionError(f"fixture event lacks {key}")

    @classmethod
    def fill_release(cls, phase, *, available, buyer_delta, host_delta, completed):
        before = phase["state_before"]
        after = copy.deepcopy(before)
        after["status"] = "completed" if completed else "releasing"
        released_total = int(before["released_total_ngonka"]) + available
        buyer_released = int(before["buyer_released_ngonka"]) + buyer_delta
        host_released = int(before["host_released_ngonka"]) + host_delta
        after.update(released_total_ngonka=str(released_total),
                     buyer_released_ngonka=str(buyer_released),
                     host_released_ngonka=str(host_released))
        phase["state_after"] = after
        phase["bank_before"]["deal"] = available
        phase["bank_after"] = copy.deepcopy(phase["bank_before"])
        phase["bank_after"]["deal"] = 0
        phase["bank_after"]["buyer"] += buyer_delta
        phase["bank_after"]["host"] += host_delta
        phase["expected"] = {
            "released_total": released_total, "buyer_released": buyer_released,
            "host_released": host_released, "buyer_delta": buyer_delta,
            "host_delta": host_delta,
        }
        phase["actual"] = {
            **phase["expected"], "address_delta": available,
            "coincident_recipients": False,
        }
        wasm = next(e for e in phase["tx"]["events"] if e["type"] == "wasm-gnk_released")
        for key, value in (
            ("available_balance_ngonka", available),
            ("released_total_ngonka", released_total),
            ("buyer_released_ngonka", buyer_released),
            ("host_released_ngonka", host_released),
            ("buyer_delta_ngonka", buyer_delta),
            ("host_delta_ngonka", host_delta),
        ):
            cls.set_event(wasm, key, value)
        buyer = before["buyer"]
        for event in phase["tx"]["events"]:
            if event["type"] == "transfer":
                recipient = next(a["value"] for a in event["attributes"] if a["key"] == "recipient")
                cls.set_event(event, "amount", f"{buyer_delta if recipient == buyer else host_delta}ngonka")
        return phase

    @classmethod
    def second_payout(cls, first):
        second = copy.deepcopy(first)
        second["state_before"] = copy.deepcopy(first["state_after"])
        second["bank_before"] = copy.deepcopy(first["bank_after"])
        buyer_delta = (int(second["state_before"]["buyer_entitlement_ngonka"])
                       - int(second["state_before"]["buyer_released_ngonka"]))
        host_delta = (int(second["state_before"]["host_entitlement_ngonka"])
                      - int(second["state_before"]["host_released_ngonka"]))
        second = cls.fill_release(second, available=buyer_delta + host_delta,
                                  buyer_delta=buyer_delta, host_delta=host_delta,
                                  completed=True)
        height = int(first["tx"]["height"]) + 100
        second["tx"].update(tx_hash="B" * 64, height=str(height))
        second["epoch_bracket"].update(
            epoch=first["epoch_bracket"]["epoch"] + 1,
            before_height=height - 1, tx_height=height, after_height=height,
        )
        return second

    @classmethod
    def completed_first(cls):
        first = unified_funded_claim_release_phase()
        state = first["state_before"]
        buyer = int(state["buyer_entitlement_ngonka"])
        host = int(state["host_entitlement_ngonka"])
        return cls.fill_release(first, available=buyer + host,
                                buyer_delta=buyer, host_delta=host, completed=True)

    @staticmethod
    def repeat_after(first):
        height = int(first["tx"]["height"]) + 100
        marker = "no additional GNK is currently available for release"
        attempt = {
            "layer": "deliver_tx", "code": 5, "codespace": "wasm",
            "tx_hash": "C" * 64, "height": str(height),
            "raw_log": marker, "events": [],
        }
        return {
            "name": "release_repeat_rejected", "level": "live_network",
            "deal": next(a["value"] for e in first["tx"]["events"]
                         if e["type"] == "wasm-gnk_released"
                         for a in e["attributes"] if a["key"] == "deal"),
            "tx": attempt,
            "terminal_rejection": {
                "contract_error": "NothingToRelease", "matched_contract_error": marker,
                "layer": "deliver_tx", "codespace": "wasm", "code": 5,
                "tx_hash": attempt["tx_hash"], "height": height,
            },
            "caller": first["caller"], "caller_native_fee_delta": 0,
            "epoch_bracket": {"same_epoch": True, "epoch": first["epoch_bracket"]["epoch"] + 1,
                              "before_height": height - 1, "tx_height": height,
                              "after_height": height},
            "before_state": copy.deepcopy(first["state_after"]),
            "after_state": copy.deepcopy(first["state_after"]),
            "before": copy.deepcopy(first["bank_after"]),
            "after": copy.deepcopy(first["bank_after"]),
            "foreign_cw20_before": first["foreign_cw20_after"],
            "foreign_cw20_after": first["foreign_cw20_after"],
            "expected": {"contract_error": "NothingToRelease", "native_transfers": 0,
                         "state_unchanged": True},
        }

    @staticmethod
    def context(first, second):
        released = next(e for e in first["tx"]["events"] if e["type"] == "wasm-gnk_released")
        attributes = {a["key"]: a["value"] for a in released["attributes"]}
        return {
            "accounts": {"host": attributes["host"], "buyer": attributes["buyer"]},
            "contracts": {"deal": attributes["deal"]},
            "phases": [
                {"name": "claim_settle", "settle_tx": {"height": "160"},
                 "after": {"deal_state": copy.deepcopy(first["state_before"]) }},
                first, second,
            ],
        }

    @staticmethod
    def observed(context):
        return extract_and_validate_scenario_predicates(context, "funded-claim", [TOP_LEVEL_SCOPE])

    def test_two_successful_release_receipts_complete_the_funded_deal(self):
        first = unified_funded_claim_release_phase()
        context = self.context(first, self.second_payout(first))
        self.assertIn("funded_release_lifecycle_verified",
                      get_task_by_id_or_alias("funded-claim").expected_checkpoints)
        self.assertIn("funded_release_lifecycle_verified", self.observed(context))
        context["phases"].pop()
        self.assertNotIn("funded_release_lifecycle_verified", self.observed(context))

    def test_unrelated_balance_increase_between_epochs_does_not_hide_release(self):
        first = unified_funded_claim_release_phase()
        second = self.second_payout(first)
        for role, credit in (("buyer", 118_434_547_948_213), ("host", 17)):
            second["bank_before"][role] += credit
            second["bank_after"][role] += credit
        self.assertIn(
            "funded_release_lifecycle_verified",
            self.observed(self.context(first, second)),
        )

    def test_completed_first_release_requires_included_unchanged_terminal_repeat(self):
        first = self.completed_first()
        context = self.context(first, self.repeat_after(first))
        self.assertIn("funded_release_lifecycle_verified", self.observed(context))
        for label, mutate in {
            "simulation": lambda c: c["phases"][2]["tx"].update(layer="simulation"),
            "other_error": lambda c: c["phases"][2]["tx"].update(raw_log="other failure"),
            "changed_balance": lambda c: c["phases"][2]["after"].update(deal=1),
            "changed_state": lambda c: c["phases"][2]["after_state"].update(status="releasing"),
            "foreign_deal": lambda c: c["phases"][2].update(deal="foreign"),
        }.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(context)
                mutate(changed)
                self.assertNotIn("funded_release_lifecycle_verified", self.observed(changed))

    def test_second_payout_must_follow_first_for_same_deal_and_complete_it(self):
        first = unified_funded_claim_release_phase()
        context = self.context(first, self.second_payout(first))
        for label, mutate in {
            "other_deal": lambda c: c["phases"][2]["tx"]["events"][1]["attributes"][0].update(value="foreign"),
            "older_receipt": lambda c: c["phases"][2]["tx"].update(height="100"),
            "still_releasing": lambda c: c["phases"][2]["state_after"].update(status="releasing"),
            "broken_state_chain": lambda c: c["phases"][2]["state_before"].update(buyer="foreign"),
            "broken_balance_chain": lambda c: c["phases"][2]["bank_before"].update(host=1),
        }.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(context)
                mutate(changed)
                self.assertNotIn("funded_release_lifecycle_verified", self.observed(changed))


class LateCompletedDonationEvidenceTests(unittest.TestCase):
    def test_empty_zero_balance_maps_and_recorded_deltas_prove_cumulative_donations(self):
        phase = synthetic_late_completed_donation_phase()
        observed = extract_and_validate_scenario_predicates(
            {"phases": [phase]}, "late-donation-after-completed", [TOP_LEVEL_SCOPE]
        )
        self.assertIn("two_liquid_donations_verified", observed)
        self.assertIn("cumulative_rounding_asserted", observed)
        for label, mutate in {
            "missing_actual": lambda p: p["releases"][0].pop("actual"),
            "missing_bank_map": lambda p: p["releases"][0]["before"]["bank"].pop("deal"),
            "wrong_delta": lambda p: p["releases"][1]["actual"].update(buyer_delta=1),
            "malformed_balance": lambda p: p["releases"][0]["before"]["bank"]["deal"].update(ngonka="invalid"),
            "nonzero_start": lambda p: p["releases"][0]["before"]["bank"]["deal"].update(ngonka=1),
        }.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(phase)
                mutate(changed)
                rejected = extract_and_validate_scenario_predicates(
                    {"phases": [changed]}, "late-donation-after-completed", [TOP_LEVEL_SCOPE]
                )
                self.assertNotIn("two_liquid_donations_verified", rejected)
                self.assertNotIn("cumulative_rounding_asserted", rejected)

    def test_phase_name_and_success_codes_do_not_prove_donations_or_rounding(self):
        forged = {
            "name": "late_completed_donation_release",
            "rounding": {"independent_buyer_per_donation": 1},
            "releases": [
                {"amount_ngonka": 2, "donation_tx": {"code": 0}, "release_tx": {"code": 0}},
                {"amount_ngonka": 2, "donation_tx": {"code": 0}, "release_tx": {"code": 0}},
            ],
        }
        observed = extract_and_validate_scenario_predicates(
            {"phases": [forged]}, "late-donation-after-completed", [TOP_LEVEL_SCOPE]
        )
        self.assertNotIn("two_liquid_donations_verified", observed)
        self.assertNotIn("cumulative_rounding_asserted", observed)


if __name__ == "__main__":
    unittest.main()
