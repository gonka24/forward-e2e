"""Positive package fixtures must also pass the actual artifact validators.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET

from forward_e2e.suite.catalog import get_task_by_id_or_alias
from tests.unit.runner.real_fixtures import (
    EVIDENCE_DIR,
    GO_BOUNDARY_EVIDENCE_DIR,
    WASM_ABI_EVIDENCE,
    load_recorded_evidence,
)
from tests.unit.runner.support.fakes import write_suite_output
from forward_e2e.suite.verifier import (
    verify_cargo_test_log, verify_go_boundary_report,
    verify_junit_xml, verify_package_c_policy_log, verify_wasm_abi_report,
)


class ArtifactFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="artifact fixture ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def artifact(self, scenario, relative):
        suite = write_suite_output(output_dir=self.root, suite_id=scenario,
                                   profile=None, scenarios=[scenario], e2e_context=None)
        return next((suite / "runs").iterdir()) / relative

    def test_abi_fixture_passes_real_validator_and_missing_case_is_rejected(self):
        path = self.artifact("wasm-abi-boundary", "abi.json")
        wasm_path = path.parent / "a8_query_boundary.wasm"
        self.assertTrue(verify_wasm_abi_report(path, wasm_path)[0])
        data = json.loads(path.read_text())
        recorded = load_recorded_evidence(WASM_ABI_EVIDENCE)
        self.assertEqual(data["cases"], recorded["cases"])
        self.assertEqual(data["wasm_sha256"], hashlib.sha256(
            (path.parent / "a8_query_boundary.wasm").read_bytes()).hexdigest())
        data["wasm_sha256"] = "0" * 64
        path.write_text(json.dumps(data))
        ok, _, error = verify_wasm_abi_report(path, wasm_path)
        self.assertFalse(ok)
        self.assertIn("Wasm hash mismatch", error)
        data["wasm_sha256"] = hashlib.sha256(
            (path.parent / "a8_query_boundary.wasm").read_bytes()).hexdigest()
        data["cases"].pop()
        path.write_text(json.dumps(data))
        ok, _, error = verify_wasm_abi_report(path, wasm_path)
        self.assertFalse(ok)
        self.assertIn("expected exactly 9", error)

    def test_go_fixture_passes_real_validator_and_missing_test_is_rejected(self):
        path = self.artifact("go-boundary", "report.json")
        self.assertTrue(verify_go_boundary_report(path)[0])
        events = path.parent / "raw/go-test.json"
        recorded = EVIDENCE_DIR / GO_BOUNDARY_EVIDENCE_DIR / "raw/go-test.json"
        self.assertEqual(events.read_bytes(), recorded.read_bytes())
        self.assertEqual(json.loads(path.read_text())["test_output_sha256"],
                         hashlib.sha256(events.read_bytes()).hexdigest())
        rows = events.read_text().splitlines()
        # Remove only the mandatory completion, retaining all other raw events.
        retained = []
        for row in rows:
            event = json.loads(row)
            if event.get("Action") == "pass" and event.get("Test") == "TestStrictPlanValidation":
                continue
            retained.append(row)
        events.write_text("\n".join(retained) + "\n")
        report = json.loads(path.read_text(encoding="utf-8"))
        report["test_output_sha256"] = hashlib.sha256(events.read_bytes()).hexdigest()
        path.write_text(json.dumps(report), encoding="utf-8")
        ok, _, error = verify_go_boundary_report(path)
        self.assertFalse(ok)
        self.assertIn("TestStrictPlanValidation", error)

    def test_native_fixture_names_actual_test_and_zero_testcases_is_rejected(self):
        path = self.artifact("lock-exact-e", "junit/TEST-MarketplaceContractAcceptanceTests.xml")
        method = get_task_by_id_or_alias("lock-exact-e").exact_test_method
        self.assertTrue(verify_junit_xml(path, method)[0])
        tree = ET.parse(path)
        root = tree.getroot()
        # Retain suite metadata: a declared count cannot replace a testcase.
        root.remove(root.find("testcase"))
        tree.write(path, encoding="utf-8")
        ok, _, error = verify_junit_xml(path, method)
        self.assertFalse(ok)
        self.assertIn("0 testcases", error)

    def test_cargo_fixtures_prove_the_named_test_and_reject_zero_count_or_wrong_name(self):
        expected_tests = {
            "contract-network-unconfirmed-policy": "network_unconfirmed_refund_rejects_early_and_unexpected_system_failures",
            "contract-claim-expiry-policy": "claim_expiry_checks_pristine_locked_accounting_and_epoch_overflow",
        }
        for scenario, expected_test in expected_tests.items():
            with self.subTest(scenario=scenario):
                path = self.artifact(scenario, scenario + ".log")
                self.assertEqual(verify_cargo_test_log(path, expected_test), (True, 1, None))
                positive = path.read_text()
                # Each negative changes one fact, preserving the other evidence.
                for broken, expected_error in (
                    (positive.replace("1 passed;", "0 passed;", 1), "Zero tests"),
                    (positive.replace(expected_test, "unrelated", 1), "Required Cargo test did not pass"),
                ):
                    with self.subTest(mutation=expected_error):
                        path.write_text(broken)
                        ok, _, error = verify_cargo_test_log(path, expected_test)
                        self.assertFalse(ok)
                        self.assertIn(expected_error, error)

    def test_package_c_fixture_proves_all_cases_across_binaries_and_rejects_one_unfinished_test(self):
        path = self.artifact("contract-query-fault-policy", "contract-query-fault-policy.log")
        ok, cases, error = verify_package_c_policy_log(path)
        self.assertTrue(ok, error)
        self.assertEqual(len(cases), 71)
        positive = path.read_text()
        # A single binary's summary is not the aggregate count of 18 tests.
        self.assertEqual(verify_cargo_test_log(path), (True, 16, None))
        self.assertIn("test result: ok. 2 passed;", positive)
        self.assertIn(
            "test contract::tests::c_policy_handler_error ... C_CASE:r4-handler_error-probe\n",
            positive,
        )
        path.write_text(positive.replace("\nok\n", "\n", 1))
        ok, _, error = verify_package_c_policy_log(path)
        self.assertFalse(ok)
        self.assertIn("did not finish", error)


if __name__ == "__main__":
    unittest.main()
