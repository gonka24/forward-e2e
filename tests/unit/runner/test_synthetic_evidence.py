"""The committed evidence fixtures are synthetic, and provably so.

``tests/fixtures/evidence/`` replaced the recorded development receipts that
used to be committed with the review history. The replacement is only honest
if two things hold and keep holding:

* no document contains a chain identity that is not a derivation of the
  labelled seed in ``support/synthetic_evidence.py`` (an address, hash, node
  id or timestamp copied from a real run would be a dependency on that run,
  however the file is labelled), and
* the two boundary fixtures are exactly what the generator in that module
  produces, so they can be regenerated instead of edited.

The in-memory contexts of ``real_fixtures.py`` get the same treatment: every
address must be a role derivation (or the one product constant, the B3
genesis account) and every transaction hash must be either one the committed
documents already carry or one that ``fixture_tx_hash`` registered.

The last test checks the repository itself: nothing under ``tests/`` may
mention the removed review artefacts, so the suites cannot depend on them.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from forward_e2e.suite.verifier import verify_go_boundary_report, verify_wasm_abi_report
from tests.unit.runner import real_fixtures
from tests.unit.runner.real_fixtures import (
    B3_GENESIS_ADDRESS,
    CLAIM_EXPIRY_DOCUMENT,
    EVIDENCE_DIR,
    FOREIGN_NATIVE_DOCUMENT,
    GO_BOUNDARY_EVIDENCE_DIR,
    IN_MEMORY_TX_LABELS,
    NETWORK_UNCONFIRMED_DOCUMENT,
    REQUIRED_SYNTHETIC_EVIDENCE,
    WASM_ABI_EVIDENCE,
    load_synthetic_evidence,
)
from tests.unit.runner.support import synthetic_evidence as synth
from tests.unit.runner.support.synthetic_evidence import (
    ADDRESS_RE,
    CLOCK_ORIGIN,
    FORBIDDEN_SUBSTRINGS,
    HEX64_UPPER_RE,
    SCHEMA,
    TIMESTAMP_RE,
    bech32_encode,
    boundary_fixture_files,
    identity_findings,
    iter_strings,
    json_document_bytes,
    synthetic_address,
    synthetic_hex,
    synthetic_tx_hash,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The committed JSON documents (``raw/go-test.json`` is JSON lines and has no
#: identities of its own; its bytes are pinned through ``test_output_sha256``).
COMMITTED_DOCUMENTS = tuple(
    name for name in REQUIRED_SYNTHETIC_EVIDENCE
    if name.endswith(".json") and not name.endswith("go-test.json")
)

#: Role vocabulary of the in-memory documents. A superset is harmless: every
#: entry is a derivation, and the check only asks whether an address found in
#: a context is one of them.
IN_MEMORY_ROLES = ("host", "buyer", "fee_recipient", "caller", "deal", "settlement_cw20", "foreign_cw20")
IN_MEMORY_DOCUMENTS = (CLAIM_EXPIRY_DOCUMENT, NETWORK_UNCONFIRMED_DOCUMENT, FOREIGN_NATIVE_DOCUMENT)


def committed(name: str) -> dict:
    return load_synthetic_evidence(name)


def document_label(document: dict) -> str:
    return document["synthetic_fixture"]["document"]


class CommittedFixtureIdentityTests(unittest.TestCase):
    """Every committed document carries only derived identities and says so."""

    def test_every_committed_json_fixture_contains_only_synthetic_identities(self):
        for name in COMMITTED_DOCUMENTS:
            with self.subTest(fixture=name):
                document = committed(name)
                self.assertEqual(identity_findings(document, document_label(document)), [])

    def test_every_committed_json_fixture_names_its_producer_schema_and_derivation_rules(self):
        producers = {
            "scripts/acceptance_harness.py",
            "scripts/run_go_boundary.py",
            "scripts/test_wasm_query_boundary.mjs",
        }
        for name in COMMITTED_DOCUMENTS:
            with self.subTest(fixture=name):
                block = committed(name)["synthetic_fixture"]
                self.assertEqual(block["schema"], SCHEMA)
                self.assertIn(block["producer"], producers)
                self.assertTrue(block["producer_functions"])
                self.assertRegex(block["producer_revision"], r"^[0-9a-f]{40}$")
                self.assertEqual(block["identity_rules"]["checked_by"], "tests/unit/runner/test_synthetic_evidence.py")
                self.assertIn("not evidence of any executed chain", block["notice"])

    def test_every_committed_json_fixture_is_stored_in_the_canonical_encoding(self):
        """Sorted keys, two-space indent, trailing newline: the order the identity numbering relies on."""
        for name in COMMITTED_DOCUMENTS:
            with self.subTest(fixture=name):
                raw = (EVIDENCE_DIR / name).read_bytes()
                self.assertEqual(raw, json_document_bytes(json.loads(raw.decode("utf-8"))))

    def test_the_address_map_of_each_document_covers_every_address_the_document_uses(self):
        """The map is the complete cast list, not a sample of it."""
        for name in COMMITTED_DOCUMENTS:
            with self.subTest(fixture=name):
                document = committed(name)
                listed = set(document["synthetic_fixture"]["identities"]["addresses"].values())
                used = {
                    address
                    for _, text in iter_strings({k: v for k, v in document.items() if k != "synthetic_fixture"})
                    for address in ADDRESS_RE.findall(text)
                }
                self.assertEqual(used - listed, set())


class IdentityCheckSensitivityTests(unittest.TestCase):
    """The check is only worth having if a single foreign identity trips it."""

    def setUp(self):
        self.document = committed("terminal-release-repeat.json")
        self.label = document_label(self.document)
        self.assertEqual(identity_findings(self.document, self.label), [])

    def test_one_address_from_another_document_is_reported_as_unlisted(self):
        mutated = copy.deepcopy(self.document)
        mutated["roles"]["caller"] = synthetic_address("some-other-document", "caller")
        findings = identity_findings(mutated, self.label)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("unlisted address", findings[0])

    def test_one_transaction_hash_that_is_not_the_next_derivation_is_reported(self):
        mutated = copy.deepcopy(self.document)
        mutated["terminal_transaction"]["tx_hash"] = synthetic_tx_hash("some-other-document", 1)
        findings = identity_findings(mutated, self.label)
        self.assertTrue(findings)
        self.assertTrue(all("is not derivation" in finding for finding in findings), findings)

    def test_one_timestamp_off_the_synthetic_clock_is_reported(self):
        document = committed("lock-exact-epoch-legacy-context.json")
        label = document_label(document)
        self.assertEqual(identity_findings(document, label), [])
        stamp = document["created_at_utc"]
        self.assertTrue(TIMESTAMP_RE.fullmatch(stamp) and stamp.startswith(CLOCK_ORIGIN.strftime("%Y-%m-%dT")))
        mutated = copy.deepcopy(document)
        mutated["created_at_utc"] = stamp.replace("2026-", "2025-", 1)
        findings = identity_findings(mutated, label)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("not on the synthetic clock", findings[0])

    def test_one_runner_path_or_review_folder_substring_is_reported(self):
        for forbidden in FORBIDDEN_SUBSTRINGS:
            with self.subTest(substring=forbidden):
                mutated = copy.deepcopy(self.document)
                mutated["terminal_preconditions"]["note"] = f"copied from {forbidden}x"
                findings = identity_findings(mutated, self.label)
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("forbidden substring", findings[0])

    def test_a_document_without_the_test_only_marker_is_reported(self):
        mutated = copy.deepcopy(self.document)
        mutated["test_fixture_only"] = False
        findings = identity_findings(mutated, self.label)
        self.assertEqual(findings, [f"{self.label}: test_fixture_only is not true"])

    def test_a_document_without_a_provenance_block_is_reported_before_anything_else(self):
        mutated = copy.deepcopy(self.document)
        del mutated["synthetic_fixture"]
        findings = identity_findings(mutated, self.label)
        self.assertEqual(len(findings), 1)
        self.assertIn("missing synthetic_fixture block", findings[0])


class DerivationTests(unittest.TestCase):
    """The derivations are deterministic and produce chain-valid shapes."""

    def test_bech32_encoding_matches_the_cosmos_sdk_reference_vector(self):
        # The Cosmos SDK address documentation vector: HRP "cosmos", payload 0x01..0x14.
        self.assertEqual(
            bech32_encode("cosmos", bytes(range(1, 21))),
            "cosmos1qypqxpq9qcrsszg2pvxq6rs0zqg3yyc5lzv7xu",
        )

    def test_account_and_contract_addresses_have_the_chain_string_lengths(self):
        account = synthetic_address("doc", "host")
        contract = synthetic_address("doc", "deal", contract=True)
        self.assertEqual(len(account), 44)
        self.assertEqual(len(contract), 64)
        self.assertTrue(account.startswith("gonka1") and contract.startswith("gonka1"))
        self.assertEqual(account, synthetic_address("doc", "host"))
        self.assertNotEqual(account, synthetic_address("doc", "buyer"))
        self.assertNotEqual(account, synthetic_address("other", "host"))

    def test_hash_derivations_have_their_declared_case_and_length(self):
        self.assertRegex(synthetic_tx_hash("doc", 1), r"^[0-9A-F]{64}$")
        self.assertRegex(synth.synthetic_sha256("doc", 1), r"^[0-9a-f]{64}$")
        self.assertRegex(synth.synthetic_hex40("doc", 1), r"^[0-9a-f]{40}$")
        self.assertNotEqual(synthetic_tx_hash("doc", 1), synthetic_tx_hash("doc", 2))
        self.assertEqual(synthetic_tx_hash("doc", 1), synthetic_hex("doc:tx:1", upper=True))


class GeneratedBoundaryFixtureTests(unittest.TestCase):
    """The Go and Wasm fixtures are regenerated, not edited, and still satisfy the verifier."""

    def test_the_committed_boundary_files_are_byte_identical_to_the_generator_output(self):
        for relative, expected in boundary_fixture_files().items():
            with self.subTest(file=relative):
                self.assertEqual((EVIDENCE_DIR / relative).read_bytes(), expected)

    def test_the_go_report_binds_the_committed_raw_events_by_digest(self):
        report = committed(f"{GO_BOUNDARY_EVIDENCE_DIR}/report.json")
        raw = (EVIDENCE_DIR / GO_BOUNDARY_EVIDENCE_DIR / "raw" / "go-test.json").read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(report["test_output_sha256"], digest)
        self.assertEqual(report["synthetic_fixture"]["bound_digests"], [digest])
        self.assertEqual((EVIDENCE_DIR / GO_BOUNDARY_EVIDENCE_DIR / "raw" / "exit-code").read_bytes(), b"0\n")

    def test_the_wasm_report_binds_the_placeholder_module_by_digest(self):
        report = committed(WASM_ABI_EVIDENCE)
        digest = hashlib.sha256(synth.WASM_PLACEHOLDER).hexdigest()
        self.assertEqual(report["wasm_sha256"], digest)
        self.assertEqual(report["synthetic_fixture"]["bound_digests"], [digest])
        self.assertEqual([case["name"] for case in report["cases"]], [name for name, _, _ in synth.WASM_CASES])

    def test_the_generated_go_fixture_passes_the_real_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative, content in boundary_fixture_files().items():
                if relative.startswith(GO_BOUNDARY_EVIDENCE_DIR):
                    path = root / Path(relative).relative_to(GO_BOUNDARY_EVIDENCE_DIR)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
            ok, checkpoints, error = verify_go_boundary_report(root / "report.json", root / "raw" / "go-test.json")
        self.assertTrue(ok, error)
        self.assertTrue(checkpoints)

    def test_the_generated_wasm_fixture_passes_the_real_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "abi.json").write_bytes(boundary_fixture_files()[WASM_ABI_EVIDENCE])
            (root / "probe.wasm").write_bytes(synth.WASM_PLACEHOLDER)
            ok, checkpoints, error = verify_wasm_abi_report(root / "abi.json", root / "probe.wasm")
        self.assertTrue(ok, error)
        self.assertTrue(checkpoints)

    def test_the_go_raw_events_report_a_pass_for_exactly_the_required_tests(self):
        events = [
            json.loads(line)
            for line in (EVIDENCE_DIR / GO_BOUNDARY_EVIDENCE_DIR / "raw" / "go-test.json").read_text().splitlines()
        ]
        passed = {event["Test"] for event in events if event["Action"] == "pass" and "Test" in event}
        self.assertTrue(set(synth.GO_REQUIRED_TESTS) <= passed)
        self.assertTrue(all(event["Package"] == synth.GO_PACKAGE for event in events))
        self.assertFalse(any(event["Action"] == "fail" for event in events))


class InMemoryFixtureIdentityTests(unittest.TestCase):
    """The programmatic contexts of real_fixtures.py are as synthetic as the files."""

    @staticmethod
    def contexts():
        return {
            "lock_exact_e": real_fixtures.real_lock_exact_e_context(),
            "claim_settle_phase": real_fixtures.real_claim_settle_phase(),
            "r1": real_fixtures.real_r1_scenario_context(),
            "package_a": real_fixtures.real_package_a_context(),
            "b3": real_fixtures.real_b3_context(),
            "claim_expiry_positive": real_fixtures.real_claim_expiry_positive_context(),
            "claim_expiry_zero": real_fixtures.real_claim_expiry_zero_context(),
            "network_unconfirmed": real_fixtures.real_network_unconfirmed_context(),
            "terminal_release_repeat": real_fixtures.real_terminal_release_repeat_context(),
            "funded_claim_legacy": real_fixtures.funded_claim_release_context(
                real_fixtures.legacy_funded_claim_release_phase()
            ),
            "funded_claim_unified": real_fixtures.funded_claim_release_context(
                real_fixtures.unified_funded_claim_release_phase()
            ),
            "late_donation": real_fixtures.synthetic_late_completed_donation_phase(),
            "ordered_vesting_addition": real_fixtures.ordered_vesting_addition_phase(unlock_before=True),
        }

    @classmethod
    def admissible_addresses(cls):
        addresses = {B3_GENESIS_ADDRESS}
        for name in COMMITTED_DOCUMENTS:
            addresses |= set(committed(name)["synthetic_fixture"]["identities"]["addresses"].values())
        for document in IN_MEMORY_DOCUMENTS:
            for role in IN_MEMORY_ROLES:
                addresses.add(synthetic_address(document, role))
                addresses.add(synthetic_address(document, role, contract=True))
        return addresses

    @classmethod
    def admissible_tx_hashes(cls):
        hashes = set()
        for name in COMMITTED_DOCUMENTS:
            for _, text in iter_strings(committed(name)):
                hashes.update(HEX64_UPPER_RE.findall(text))
        # The registry is filled as the factories run, so it is read after them.
        hashes |= {synthetic_hex(f"{document}:tx:{label}", upper=True) for document, label in IN_MEMORY_TX_LABELS}
        return hashes

    def test_every_address_in_an_in_memory_context_is_a_role_derivation_or_the_b3_genesis_account(self):
        contexts = self.contexts()
        admissible = self.admissible_addresses()
        for name, context in contexts.items():
            with self.subTest(context=name):
                found = {
                    address for _, text in iter_strings(context) for address in ADDRESS_RE.findall(text)
                }
                self.assertEqual(found - admissible, set())

    def test_every_transaction_hash_in_an_in_memory_context_is_derived_or_comes_from_a_committed_document(self):
        contexts = self.contexts()
        admissible = self.admissible_tx_hashes()
        for name, context in contexts.items():
            with self.subTest(context=name):
                found = {
                    value for _, text in iter_strings(context) for value in HEX64_UPPER_RE.findall(text)
                }
                self.assertEqual(found - admissible, set())

    def test_no_in_memory_context_carries_a_wall_clock_time_or_a_runner_path(self):
        for name, context in self.contexts().items():
            with self.subTest(context=name):
                for path, text in iter_strings(context):
                    for stamp in TIMESTAMP_RE.findall(text):
                        self.assertTrue(stamp.startswith(CLOCK_ORIGIN.strftime("%Y-%m-%dT")), f"{path}: {stamp}")
                    for forbidden in FORBIDDEN_SUBSTRINGS:
                        self.assertNotIn(forbidden, text, path)

    def test_every_in_memory_live_context_has_a_synthetic_run_id(self):
        for name, context in self.contexts().items():
            if "run_id" in context:
                with self.subTest(context=name):
                    self.assertTrue(str(context["run_id"]).startswith("synthetic-"), context["run_id"])


class NoDevelopmentArtifactDependencyTests(unittest.TestCase):
    """The suites depend on nothing that was removed with the review history."""

    # The path form, with its slash: the bare folder name is also the rule
    # token in FORBIDDEN_SUBSTRINGS and may legitimately appear there. It is
    # assembled at runtime so this module does not contain the path itself.
    REMOVED_PATH_PREFIX = "/".join(("docs", "reviews", ""))

    def test_no_test_fixture_or_support_file_mentions_the_removed_review_evidence(self):
        offenders = []
        for path in sorted((REPO_ROOT / "tests").rglob("*")):
            if path.is_file() and path.suffix in {".py", ".json", ".md", ".log", ".txt"}:
                text = path.read_text(encoding="utf-8", errors="replace")
                if self.REMOVED_PATH_PREFIX in text:
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [])
        self.assertFalse((REPO_ROOT / "docs" / "reviews").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
