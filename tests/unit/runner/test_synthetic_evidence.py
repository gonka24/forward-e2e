"""The committed evidence fixtures are synthetic, and provably so.

``tests/fixtures/evidence/`` replaced the recorded development receipts that
used to be committed with the review history. The replacement is only honest
if two things hold and keep holding:

* no document contains a chain identity that is not a derivation of the
  labelled seed in ``support/synthetic_evidence.py`` (an address of any
  Bech32 prefix, a hash, a validator key, a node id or a wall-clock timestamp
  copied from a real run would be a dependency on that run, however the file
  is labelled), and
* the two boundary fixtures are exactly what the generator in that module
  produces, so they can be regenerated instead of edited.

The four live-context documents are identity-scrubbed derivations of former
development receipts and say so in their ``synthetic_fixture.notice``; the
tests here pin that wording, the producer functions the blocks name (each
must exist in the named producer) and the derivation rules, so the documents
cannot drift into claiming more -- or less -- than they are.

The in-memory contexts of ``real_fixtures.py`` get the same treatment: every
address must be a role derivation (or the one product constant, the B3
genesis account), every transaction hash must be either one the committed
documents already carry or one that ``fixture_tx_hash`` registered, and every
timestamp must sit on the synthetic clock.

The last test checks the repository itself: nothing under ``tests/`` may
mention the removed review artefacts, so the suites cannot depend on them.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
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
    CLOCK_ORIGIN,
    FORBIDDEN_SUBSTRINGS,
    GENERATED_NOTICE,
    GO_PRODUCER_REVISION,
    HARNESS_PRODUCER_REVISION,
    HEX64_UPPER_RE,
    IDENTITY_RULES,
    PUBKEY_RE,
    SCHEMA,
    SCRUBBED_NOTICE,
    TIMESTAMP_RE,
    WASM_PRODUCER_REVISION,
    bech32_encode,
    bech32_verify,
    boundary_fixture_files,
    find_addresses,
    identity_findings,
    iter_strings,
    json_document_bytes,
    on_synthetic_clock,
    synthetic_address,
    synthetic_hex,
    synthetic_pubkey,
    synthetic_tx_hash,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
GO_REPORT = f"{GO_BOUNDARY_EVIDENCE_DIR}/report.json"

#: The committed JSON documents (``raw/go-test.json`` is JSON lines and has no
#: identities of its own; its bytes are pinned through ``test_output_sha256``).
COMMITTED_DOCUMENTS = tuple(
    name for name in REQUIRED_SYNTHETIC_EVIDENCE
    if name.endswith(".json") and not name.endswith("go-test.json")
)

#: What each committed document must say about itself: the notice and the
#: producer revision are pinned exactly, because the whole point of the
#: wording is that it cannot quietly become a stronger claim.
EXPECTED_PROVENANCE = {
    WASM_ABI_EVIDENCE: (GENERATED_NOTICE, WASM_PRODUCER_REVISION),
    GO_REPORT: (GENERATED_NOTICE, GO_PRODUCER_REVISION),
}
for _name in COMMITTED_DOCUMENTS:
    EXPECTED_PROVENANCE.setdefault(_name, (SCRUBBED_NOTICE, HARNESS_PRODUCER_REVISION))

#: Role vocabulary of the in-memory documents. A superset is harmless: every
#: entry is a derivation, and the check only asks whether an address found in
#: a context is one of them.
IN_MEMORY_ROLES = ("host", "buyer", "fee_recipient", "caller", "deal", "settlement_cw20", "foreign_cw20")
IN_MEMORY_DOCUMENTS = (CLAIM_EXPIRY_DOCUMENT, NETWORK_UNCONFIRMED_DOCUMENT, FOREIGN_NATIVE_DOCUMENT)

#: The host-path prefixes the check must at least refuse. Pinned here so the
#: list in the generator cannot shrink back to ``/home/`` alone.
REQUIRED_FORBIDDEN_PATHS = ("/home/", "/root/", "/Users/", "/private/", "/tmp/", "/var/folders/", "/workspace/", "/out/")

#: A foreign address from outside the seed: the Cosmos SDK documentation
#: vector (HRP ``cosmos``, payload 0x01..0x14). Checksum-valid, so it is the
#: shape a recorded address of another chain would have.
COSMOS_REFERENCE_ADDRESS = "cosmos1qypqxpq9qcrsszg2pvxq6rs0zqg3yyc5lzv7xu"


def committed(name: str) -> dict:
    return load_synthetic_evidence(name)


def document_label(document: dict) -> str:
    return document["synthetic_fixture"]["document"]


def recomputed_bound_digests(name: str) -> frozenset:
    """The SHA-256 values this test recomputes from the bytes a report claims to bind.

    ``identity_findings`` accepts a declared bound digest only if the caller
    recomputed it; the declaration alone would let a recorded digest be
    laundered as "bound". The Go report binds the committed raw Go events,
    the Wasm report binds the placeholder module; nothing else binds anything.
    """
    if name == GO_REPORT:
        raw = (EVIDENCE_DIR / GO_BOUNDARY_EVIDENCE_DIR / "raw" / "go-test.json").read_bytes()
        return frozenset({hashlib.sha256(raw).hexdigest()})
    if name == WASM_ABI_EVIDENCE:
        return frozenset({hashlib.sha256(synth.WASM_PLACEHOLDER).hexdigest()})
    return frozenset()


def findings_for(name: str, document: dict | None = None) -> list:
    document = committed(name) if document is None else document
    return identity_findings(document, document_label(document), recomputed_digests=recomputed_bound_digests(name))


def producer_defines(producer: str, function: str) -> bool:
    """Whether ``producer`` (a repository path) defines ``function`` at top level."""
    source = (REPO_ROOT / producer).read_text(encoding="utf-8")
    if producer.endswith(".py"):
        tree = ast.parse(source)
        return any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function
            for node in tree.body
        )
    # The Wasm probe is JavaScript: a top-level binding or function of that name.
    return re.search(rf"^(?:const|let|var|function|async function)\s+{re.escape(function)}\b", source, re.M) is not None


class CommittedFixtureIdentityTests(unittest.TestCase):
    """Every committed document carries only derived identities and says so."""

    def test_every_committed_json_fixture_contains_only_synthetic_identities(self):
        for name in COMMITTED_DOCUMENTS:
            with self.subTest(fixture=name):
                self.assertEqual(findings_for(name), [])

    def test_every_committed_json_fixture_pins_its_notice_producer_revision_and_derivation_rules(self):
        """The provenance block is checked word for word, not for shape.

        The scrubbed documents must say they derive from former development
        receipts and keep recorded arithmetic; the generated ones must say no
        value was recorded. A looser check (``assertIn("synthetic", notice)``)
        would let either claim drift into the other.
        """
        producers = {
            "scripts/acceptance_harness.py",
            "scripts/run_go_boundary.py",
            "scripts/test_wasm_query_boundary.mjs",
        }
        for name in COMMITTED_DOCUMENTS:
            with self.subTest(fixture=name):
                block = committed(name)["synthetic_fixture"]
                notice, revision = EXPECTED_PROVENANCE[name]
                self.assertEqual(block["schema"], SCHEMA)
                self.assertIn(block["producer"], producers)
                self.assertTrue(block["producer_functions"])
                self.assertEqual(block["notice"], notice)
                self.assertEqual(block["producer_revision"], revision)
                self.assertTrue(block["producer_shape_note"])
                self.assertEqual(block["identity_rules"], IDENTITY_RULES)
                self.assertIn("not evidence of any executed chain", block["notice"])

    def test_the_scrubbed_documents_declare_what_was_replaced_and_what_was_kept(self):
        """A reader of the file alone must learn it is a scrubbed receipt, not a generated one."""
        for name in COMMITTED_DOCUMENTS:
            notice, _ = EXPECTED_PROVENANCE[name]
            if notice is not SCRUBBED_NOTICE:
                continue
            with self.subTest(fixture=name):
                block = committed(name)["synthetic_fixture"]
                provenance = block["provenance"]
                self.assertEqual(provenance["kind"], "identity-scrubbed development receipt")
                self.assertTrue(provenance["method"].startswith(
                    "renumber_identities() in tests/unit/runner/support/synthetic_evidence.py"))
                self.assertIs(provenance["live_receipt"], False)
                for identity in ("Bech32 address", "transaction and block hash", "validator public key",
                                 "timestamp", "host path"):
                    self.assertIn(identity, provenance["replaced"])
                for fact in ("amounts", "heights", "epochs", "balances"):
                    self.assertIn(fact, provenance["retained"])
                self.assertIn("no regression on recorded identities", provenance["regression_value"])
                self.assertTrue(block["producer_phases"])

    def test_every_producer_function_a_fixture_names_is_defined_by_the_producer_it_names(self):
        """``producer_functions`` is attribution, so a name that does not exist is a false attribution."""
        for name in COMMITTED_DOCUMENTS:
            block = committed(name)["synthetic_fixture"]
            for function in block["producer_functions"]:
                with self.subTest(fixture=name, function=function):
                    self.assertTrue(producer_defines(block["producer"], function),
                                    f"{block['producer']} does not define {function}")

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
                    for address in find_addresses(text)
                }
                self.assertEqual(used - listed, set())

    def test_the_public_key_map_of_each_document_covers_every_validator_key_the_document_uses(self):
        """Same rule for the one identity that is neither hex nor Bech32: the Ed25519 validator key."""
        for name in COMMITTED_DOCUMENTS:
            with self.subTest(fixture=name):
                document = committed(name)
                listed = set((document["synthetic_fixture"]["identities"].get("pubkeys") or {}).values())
                used = {
                    value
                    for _, text in iter_strings({k: v for k, v in document.items() if k != "synthetic_fixture"})
                    for value in PUBKEY_RE.findall(text)
                }
                self.assertEqual(used - listed, set())

    def test_the_pinned_forbidden_path_list_covers_developer_and_runner_directories(self):
        for required in REQUIRED_FORBIDDEN_PATHS:
            with self.subTest(prefix=required):
                self.assertIn(required, FORBIDDEN_SUBSTRINGS)


class IdentityCheckSensitivityTests(unittest.TestCase):
    """The check is only worth having if a single foreign identity trips it.

    Every mutation below changes exactly one fact of a committed document
    that passes, and expects exactly the finding for that fact.
    """

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

    def test_one_address_of_a_foreign_bech32_prefix_is_reported_as_unlisted(self):
        """A recorded ``cosmos1``/``wasm1``/``gonkavaloper1`` address is as much a run identity as a ``gonka1`` one."""
        foreign = {
            "cosmos (reference vector)": COSMOS_REFERENCE_ADDRESS,
            "wasm (contract-sized)": bech32_encode("wasm", bytes(range(32))),
            "gonkavaloper": bech32_encode("gonkavaloper", bytes(range(20, 40))),
        }
        for description, address in foreign.items():
            with self.subTest(address=description):
                self.assertTrue(bech32_verify(address))
                mutated = copy.deepcopy(self.document)
                mutated["roles"]["caller"] = address
                findings = identity_findings(mutated, self.label)
                self.assertEqual(len(findings), 1, findings)
                self.assertIn(f"unlisted address {address}", findings[0])

    def test_a_lowercase_hex_digest_is_not_misread_as_an_address(self):
        """The Bech32 pattern overlaps the hex alphabet; only a checksum-valid string counts."""
        digest = synth.synthetic_sha256("doc", 1)
        self.assertEqual(find_addresses(f"sha256 {digest} inline"), [])
        self.assertEqual(find_addresses(f"to {COSMOS_REFERENCE_ADDRESS}."), [COSMOS_REFERENCE_ADDRESS])

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
        self.assertTrue(TIMESTAMP_RE.fullmatch(stamp) and on_synthetic_clock(stamp))
        mutated = copy.deepcopy(document)
        mutated["created_at_utc"] = stamp.replace("2026-", "2025-", 1)
        findings = identity_findings(mutated, label)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("not on the synthetic clock", findings[0])

    def test_one_wall_clock_timestamp_with_a_numeric_offset_is_reported(self):
        """A ``-05:00`` spelling is parsed, not pattern-matched: the instant is what is judged.

        The control is the clock origin itself written as ``2025-12-31T19:00:00-05:00``,
        which an implementation comparing date prefixes would reject and an
        implementation applying the offset accepts.
        """
        document = committed("lock-exact-epoch-legacy-context.json")
        label = document_label(document)
        control = copy.deepcopy(document)
        control["created_at_utc"] = "2025-12-31T19:00:00-05:00"
        self.assertEqual(identity_findings(control, label), [])
        mutated = copy.deepcopy(document)
        mutated["created_at_utc"] = "2026-03-15T12:00:00-05:00"
        findings = identity_findings(mutated, label)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("2026-03-15T12:00:00-05:00", findings[0])
        self.assertIn("not on the synthetic clock", findings[0])

    def test_a_timestamp_with_seconds_or_a_nonzero_fraction_is_off_the_synthetic_clock(self):
        """The clock ticks in whole minutes; a real instant almost never lands on one."""
        self.assertTrue(on_synthetic_clock("2026-01-01T00:07:00Z"))
        self.assertTrue(on_synthetic_clock("2026-01-01T00:07:00.000000000Z"))
        self.assertTrue(on_synthetic_clock("2026-01-01T00:07:00+00:00"))
        self.assertFalse(on_synthetic_clock("2026-01-01T00:07:13Z"))
        self.assertFalse(on_synthetic_clock("2026-01-01T00:07:00.5Z"))
        self.assertFalse(on_synthetic_clock("2026-01-02T00:00:00Z"))  # a day past the origin
        self.assertFalse(on_synthetic_clock("2025-12-31T23:59:00Z"))

    def test_one_foreign_validator_public_key_is_reported_as_unlisted(self):
        document = committed("lock-exact-epoch-legacy-context.json")
        label = document_label(document)
        self.assertEqual(identity_findings(document, label), [])
        key = document["chain"]["status"]["validator_info"]["pub_key"]
        self.assertEqual(key["value"], synthetic_pubkey(label, "validator"))
        mutated = copy.deepcopy(document)
        mutated["chain"]["status"]["validator_info"]["pub_key"]["value"] = synthetic_pubkey("some-other-document", "validator")
        findings = identity_findings(mutated, label)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("unlisted 32-byte public key", findings[0])

    def test_a_declared_public_key_that_is_not_its_derivation_is_reported(self):
        """Listing a key under ``identities.pubkeys`` does not launder it; the listing itself is checked."""
        document = committed("lock-exact-epoch-legacy-context.json")
        label = document_label(document)
        mutated = copy.deepcopy(document)
        foreign = synthetic_pubkey("some-other-document", "validator")
        mutated["synthetic_fixture"]["identities"]["pubkeys"]["validator"] = foreign
        mutated["chain"]["status"]["validator_info"]["pub_key"]["value"] = foreign
        findings = identity_findings(mutated, label)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("public key 'validator' is not its derivation", findings[0])

    def test_one_pasted_sha256_declared_as_bound_is_reported_unless_recomputed(self):
        """``bound_digests`` is a claim the caller must prove, not an exemption the document grants itself."""
        report = committed(GO_REPORT)
        label = document_label(report)
        self.assertEqual(findings_for(GO_REPORT, report), [])
        pasted = hashlib.sha256(b"a go-test.json recorded on some developer's machine").hexdigest()
        mutated = copy.deepcopy(report)
        mutated["synthetic_fixture"]["bound_digests"].append(pasted)
        findings = findings_for(GO_REPORT, mutated)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn(f"bound digest {pasted[:12]}... is declared but was not recomputed", findings[0])

    def test_the_genuine_bound_digest_is_not_accepted_on_the_documents_say_so_alone(self):
        """Without the recomputation the committed Go report itself is reported, and only for that."""
        report = committed(GO_REPORT)
        label = document_label(report)
        findings = identity_findings(report, label)
        self.assertEqual(len(findings), 2, findings)
        self.assertIn("is declared but was not recomputed", findings[0])
        self.assertIn("test_output_sha256 is not derivation", findings[1])

    def test_a_laundered_test_output_digest_is_reported_even_when_declared_consistently(self):
        """Replacing the binding digest and its declaration together still trips the check, because the bytes it binds did not change."""
        report = committed(GO_REPORT)
        pasted = hashlib.sha256(b"different raw events").hexdigest()
        mutated = copy.deepcopy(report)
        mutated["test_output_sha256"] = pasted
        mutated["synthetic_fixture"]["bound_digests"] = [pasted]
        findings = findings_for(GO_REPORT, mutated)
        self.assertTrue(any("not recomputed" in finding for finding in findings), findings)
        self.assertTrue(any("test_output_sha256 is not derivation" in finding for finding in findings), findings)

    def test_one_runner_path_or_review_folder_substring_is_reported(self):
        for forbidden in FORBIDDEN_SUBSTRINGS:
            with self.subTest(substring=forbidden):
                mutated = copy.deepcopy(self.document)
                mutated["terminal_preconditions"]["note"] = f"copied from {forbidden}x"
                findings = identity_findings(mutated, self.label)
                self.assertEqual(len(findings), 1, findings)
                self.assertIn("forbidden substring", findings[0])

    def test_one_macos_home_directory_path_is_reported(self):
        """The developer machines this repository is edited on are Macs; ``/home/`` alone would miss them."""
        mutated = copy.deepcopy(self.document)
        mutated["terminal_preconditions"]["note"] = "see /Users/someone/forward-e2e/evidence/live-context.json"
        findings = identity_findings(mutated, self.label)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("forbidden substring '/Users/'", findings[0])

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
        self.assertEqual(bech32_encode("cosmos", bytes(range(1, 21))), COSMOS_REFERENCE_ADDRESS)
        self.assertTrue(bech32_verify(COSMOS_REFERENCE_ADDRESS))
        self.assertFalse(bech32_verify(COSMOS_REFERENCE_ADDRESS[:-1] + "v"))

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

    def test_public_key_derivations_are_32_bytes_of_base64_and_match_the_detector(self):
        key = synthetic_pubkey("doc", "validator")
        self.assertEqual(len(key), 44)
        self.assertTrue(PUBKEY_RE.fullmatch(key), key)
        self.assertNotEqual(key, synthetic_pubkey("doc", "other"))
        self.assertEqual(key, synthetic_pubkey("doc", "validator"))


class GeneratedBoundaryFixtureTests(unittest.TestCase):
    """The Go and Wasm fixtures are regenerated, not edited, and still satisfy the verifier's rules."""

    def test_the_committed_boundary_files_are_byte_identical_to_the_generator_output(self):
        for relative, expected in boundary_fixture_files().items():
            with self.subTest(file=relative):
                self.assertEqual((EVIDENCE_DIR / relative).read_bytes(), expected)

    def test_the_go_report_binds_the_committed_raw_events_by_digest(self):
        report = committed(GO_REPORT)
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

    def test_the_go_fixture_is_attributed_to_the_post_relocation_producer_and_not_to_the_harness_commit(self):
        """The Go report reproduces ``scripts/run_go_boundary.py`` on this branch, whose predecessor wrote a different shape."""
        report = committed(GO_REPORT)
        block = report["synthetic_fixture"]
        self.assertNotRegex(block["producer_revision"], r"^[0-9a-f]{40}$")
        self.assertIn("scripts/run_go_boundary.py", block["producer_revision"])
        self.assertIn("not the scripts/run_a8_go_boundary.py shape", block["producer_revision"])
        self.assertIn("/app/harness/go_boundary", block["producer_shape_note"])
        self.assertEqual(report["harness_module"]["dir"], "/app/harness/go_boundary")
        self.assertIn("-module=github.com/gonka24/forward-e2e-go-boundary", report["go_mod_edit_flags"])

    def _write_go_fixture(self, root: Path) -> None:
        for relative, content in boundary_fixture_files().items():
            if relative.startswith(GO_BOUNDARY_EVIDENCE_DIR):
                path = root / Path(relative).relative_to(GO_BOUNDARY_EVIDENCE_DIR)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

    def test_the_committed_go_fixture_presented_verbatim_is_refused_as_a_test_only_fixture(self):
        """The marker is the fixture's protection against being graded as a run's evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_go_fixture(root)
            ok, checkpoints, error = verify_go_boundary_report(root / "report.json", root / "raw" / "go-test.json")
        self.assertFalse(ok)
        self.assertEqual(checkpoints, [])
        self.assertEqual(error, "test-only fixture cannot be verified as live evidence")

    def test_the_generated_go_fixture_passes_the_real_verifier_once_the_marker_is_stripped_in_memory(self):
        """With the marker gone and nothing else changed, every inner rule of the verifier is met."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_go_fixture(root)
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            del report["test_fixture_only"]
            (root / "report.json").write_bytes(json_document_bytes(report))
            ok, checkpoints, error = verify_go_boundary_report(root / "report.json", root / "raw" / "go-test.json")
        self.assertTrue(ok, error)
        self.assertTrue(checkpoints)

    def test_the_committed_wasm_fixture_presented_verbatim_is_refused_as_a_test_only_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "abi.json").write_bytes(boundary_fixture_files()[WASM_ABI_EVIDENCE])
            (root / "probe.wasm").write_bytes(synth.WASM_PLACEHOLDER)
            ok, checkpoints, error = verify_wasm_abi_report(root / "abi.json", root / "probe.wasm")
        self.assertFalse(ok)
        self.assertEqual(checkpoints, [])
        self.assertEqual(error, "test-only fixture cannot be verified as live evidence")

    def test_the_generated_wasm_fixture_passes_the_real_verifier_once_the_marker_is_stripped_in_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = json.loads(boundary_fixture_files()[WASM_ABI_EVIDENCE].decode("utf-8"))
            del report["test_fixture_only"]
            (root / "abi.json").write_bytes(json_document_bytes(report))
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
            "go_boundary_report": real_fixtures.go_boundary_evidence()[0],
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

    @classmethod
    def admissible_pubkeys(cls):
        keys = set()
        for name in COMMITTED_DOCUMENTS:
            keys |= set((committed(name)["synthetic_fixture"]["identities"].get("pubkeys") or {}).values())
        return keys

    def test_every_address_in_an_in_memory_context_is_a_role_derivation_or_the_b3_genesis_account(self):
        contexts = self.contexts()
        admissible = self.admissible_addresses()
        for name, context in contexts.items():
            with self.subTest(context=name):
                found = {address for _, text in iter_strings(context) for address in find_addresses(text)}
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

    def test_every_validator_key_in_an_in_memory_context_comes_from_a_committed_document(self):
        contexts = self.contexts()
        admissible = self.admissible_pubkeys()
        for name, context in contexts.items():
            with self.subTest(context=name):
                found = {value for _, text in iter_strings(context) for value in PUBKEY_RE.findall(text)}
                self.assertEqual(found - admissible, set())

    def test_no_in_memory_context_carries_a_wall_clock_time_or_a_runner_path(self):
        for name, context in self.contexts().items():
            with self.subTest(context=name):
                for path, text in iter_strings(context):
                    for stamp in TIMESTAMP_RE.findall(text):
                        self.assertTrue(on_synthetic_clock(stamp), f"{path}: {stamp}")
                    for forbidden in FORBIDDEN_SUBSTRINGS:
                        self.assertNotIn(forbidden, text, path)

    def test_every_in_memory_live_context_has_a_synthetic_run_id(self):
        for name, context in self.contexts().items():
            if "run_id" in context:
                with self.subTest(context=name):
                    self.assertTrue(str(context["run_id"]).startswith("synthetic-"), context["run_id"])

    def test_the_go_boundary_evidence_helper_strips_only_the_marker(self):
        """The helper exists for the verifier's refusal; it must not also simplify the shape it hands over."""
        report, raw = real_fixtures.go_boundary_evidence()
        on_disk = committed(GO_REPORT)
        self.assertNotIn("test_fixture_only", report)
        self.assertIs(on_disk["test_fixture_only"], True)
        del on_disk["test_fixture_only"]
        self.assertEqual(report, on_disk)
        self.assertEqual(hashlib.sha256(raw.encode("utf-8")).hexdigest(), report["test_output_sha256"])


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
