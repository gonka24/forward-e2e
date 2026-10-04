"""Offline A9 release tooling tests.

All fixtures are synthetic. No external network, Docker, or live chain calls; HTTP is loopback-only.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("a9_release", ROOT / "vendor" / "contract_release" / "release.py")
a9 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = a9
SPEC.loader.exec_module(a9)
FIXTURES = Path(__file__).parent / "fixtures"
DEAL_ADDRESS = "gonka1deal0000000000000000000000000000000000000"
HOST_ADDRESS = "gonka1host0000000000000000000000000000000000000"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def deal_request() -> dict:
    return {
        "deal_address": DEAL_ADDRESS,
        "host": HOST_ADDRESS,
        "target_epoch": 123,
        "price_micro_usdt_per_gnk": "1000000",
        "buyer_budget_micro_usdt": "100000000",
    }


def make_inputs(root: Path):
    release = root / "release"
    (release / "wasm").mkdir(parents=True)
    (release / "schemas").mkdir()
    deal_bytes = b"deal-wasm"
    factory_bytes = b"factory-wasm"
    schema_bytes = b'{"title":"schema"}\n'
    (release / "wasm" / "marketplace_deal.wasm").write_bytes(deal_bytes)
    (release / "wasm" / "marketplace_factory.wasm").write_bytes(factory_bytes)
    (release / "schemas" / "deal.json").write_bytes(schema_bytes)
    checks = {name: {"status": "pass"} for name in a9.REQUIRED_BUILD_CHECKS}
    manifest = {
        "schema_version": a9.SCHEMA_VERSION,
        "kind": a9.BUILD_KIND,
        "generated_at_utc": "2026-09-07T00:00:00Z",
        "marketplace_commit_sha": "a" * 40,
        "gonka": {
            "pinned_source_sha": a9.PINNED_GONKA_SHA,
            "runtime": {"source_sha": None, "binary_version": None, "binary_sha256": None, "evidence": None},
        },
        "toolchain": {
            "rust": a9.RUST_VERSION,
            "optimizer": {"image": a9.OPTIMIZER_IMAGE, "digest": a9.OPTIMIZER_DIGEST, "platform": a9.OPTIMIZER_PLATFORM},
            "cosmwasm_check": a9.COSMWASM_CHECK_VERSION,
        },
        "inputs": {"cargo_lock_sha256": "1" * 64, "proto_generator_lock_sha256": "2" * 64},
        "contracts": [
            {"name": "marketplace-deal", "path": "wasm/marketplace_deal.wasm", "sha256": a9.sha256_bytes(deal_bytes)},
            {"name": "marketplace-factory", "path": "wasm/marketplace_factory.wasm", "sha256": a9.sha256_bytes(factory_bytes)},
        ],
        "schemas": [{"path": "schemas/deal.json", "sha256": a9.sha256_bytes(schema_bytes)}],
        "checks": checks,
    }
    manifest_path = release / "build-manifest.json"
    a9.write_json_atomic(manifest_path, manifest)
    config = {
        "schema_version": a9.SCHEMA_VERSION,
        "kind": a9.CONFIG_KIND,
        "network_class": "local",
        "chain_id": "gonka-local-a9",
        "rpc_endpoint": "http://127.0.0.1:26657",
        "expected_gonka_source_sha": a9.PINNED_GONKA_SHA,
        "build_manifest_sha256": a9.sha256_file(manifest_path),
        "settlement_cw20": "gonka1token00000000000000000000000000000000000",
        "settlement_cw20_decimals": 6,
        "fee_recipient": "gonka1fee000000000000000000000000000000000000",
        "fee_bps": 150,
        "factory_label": "marketplace-a9-test",
        "tx": {"from": "deployer", "gas": "auto", "gas_adjustment": "1.3"},
        "runtime_evidence": None,
    }
    config_path = root / "deployment-config.json"
    a9.write_json_atomic(config_path, config)
    receipt = {
        "schema_version": a9.SCHEMA_VERSION,
        "kind": a9.RECEIPT_KIND,
        "status": "complete",
        "created_at_utc": "2026-09-07T00:00:00Z",
        "chain_id": config["chain_id"],
        "build_manifest_sha256": config["build_manifest_sha256"],
        "deployment_config_sha256": a9.sha256_file(config_path),
        "transactions": [],
        "code_ids": {"deal": 17, "factory": 18},
        "contracts": {"factory": "gonka1factory000000000000000000000000000000000"},
        "verification": {},
    }
    return manifest, manifest_path, config, config_path, receipt


def make_production_evidence(root: Path, manifest: dict, manifest_path: Path, config: dict) -> dict:
    """Create synthetic reports only for validator tests; never a real release receipt."""
    hashes = a9.expected_contract_hashes(manifest)
    runtime_binary = "b" * 64
    runtime = {"source_sha": "d" * 40, "binary_version": "inferenced v0.54.3", "binary_sha256": runtime_binary}
    target = {
        "network_class": "production",
        "chain_id": config["chain_id"],
        "rpc_endpoint": config["rpc_endpoint"],
        "network_fingerprint_sha256": "c" * 64,
        "pinned_interface_source_sha": config["expected_gonka_source_sha"],
        "build_manifest_sha256": config["build_manifest_sha256"],
        "runtime_source_sha": runtime["source_sha"],
        "runtime_binary_version": runtime["binary_version"],
        "runtime_binary_sha256": runtime_binary,
        "deal_wasm_sha256": hashes["marketplace-deal"],
        "factory_wasm_sha256": hashes["marketplace-factory"],
        "settlement_cw20": config["settlement_cw20"],
        "settlement_cw20_decimals": config["settlement_cw20_decimals"],
        "fee_recipient": config["fee_recipient"],
        "fee_bps": config["fee_bps"],
        "factory_label": config["factory_label"],
        "tx_from": config["tx"]["from"],
        "tx_gas": config["tx"]["gas"],
        "tx_gas_adjustment": config["tx"]["gas_adjustment"],
    }
    evidence_dir = manifest_path.parent / "evidence"
    evidence_dir.mkdir()
    reports = {}
    for check in a9.REQUIRED_PRODUCTION_RELEASE_CHECKS:
        raw_path = evidence_dir / f"{check}.raw.txt"
        raw_path.write_text(f"synthetic source evidence for {check}\n", encoding="utf-8")
        path = evidence_dir / f"{check}.json"
        a9.write_json_atomic(path, {
            "schema_version": a9.SCHEMA_VERSION,
            "kind": a9.RELEASE_EVIDENCE_CHECK_KIND,
            "check": check,
            "status": "PASS",
            "target": target,
            "attestation": {"operator": "synthetic-test", "issued_at_utc": "2026-09-09T00:00:00Z", "method": "unit-fixture"},
            "result": {"assertions": [f"synthetic {check} assertion"]},
            "artifacts": [{"path": str(raw_path.relative_to(manifest_path.parent)).replace("\\", "/"), "sha256": a9.sha256_file(raw_path)}],
        })
        reports[check] = {"path": str(path.relative_to(manifest_path.parent)).replace("\\", "/"), "sha256": a9.sha256_file(path)}
    return {
        "schema_version": a9.SCHEMA_VERSION,
        "kind": a9.RELEASE_EVIDENCE_KIND,
        "runtime": runtime,
        "target": target,
        "check_reports": reports,
    }


def preflight_with_fake_network(config: dict, manifest: dict, manifest_path: Path, runner: "FakeChainRunner") -> dict:
    return a9.preflight(
        config, manifest, manifest_path, "inferenced", runner,
        network_fingerprint_reader=lambda _rpc: runner.network_fingerprint,
    )


class ArchiveExtractionTests(unittest.TestCase):
    def test_internal_symlink_chains_are_preserved_but_an_escaping_target_is_rejected_in_either_order(self):
        for reverse in (False, True):
            for escaping in (False, True):
                with self.subTest(reverse=reverse, escaping=escaping), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    destination = root / "source"
                    destination.mkdir()
                    # Git archives store symlinks as SYMTYPE members. The negative
                    # differs only in the target of the second link.
                    links = [("d/b", ".."), ("escape", "d/b/.." if escaping else "d/b/scripts")]
                    if reverse:
                        links.reverse()
                    archive = io.BytesIO()
                    with tarfile.open(fileobj=archive, mode="w") as output:
                        member = tarfile.TarInfo("scripts/run.sh")
                        payload = b"#!/bin/sh\necho safe\n"
                        member.mode = 0o755
                        member.size = len(payload)
                        output.addfile(member, io.BytesIO(payload))
                        for name, target in links:
                            member = tarfile.TarInfo(name)
                            member.type = tarfile.SYMTYPE
                            member.mode = 0o777
                            member.linkname = target
                            output.addfile(member)
                    archive.seek(0)
                    with tarfile.open(fileobj=archive) as source:
                        if escaping:
                            with self.assertRaisesRegex(a9.ToolError, "unsafe symlink target"):
                                a9.safe_extract_tar(source, destination)
                            self.assertFalse((destination / "escape").is_symlink())
                            self.assertFalse((destination / "d/b").is_symlink())
                        else:
                            a9.safe_extract_tar(source, destination)
                            self.assertEqual((destination / "escape/run.sh").read_bytes(), payload)

    def test_safe_extraction_is_compatible_with_python_311_and_preserves_executable_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar"
            payload = b"#!/bin/sh\necho safe\n"
            with tarfile.open(archive, "w") as output:
                member = tarfile.TarInfo("scripts/run.sh")
                member.size = len(payload)
                member.mode = 0o755
                output.addfile(member, io.BytesIO(payload))

            destination = root / "destination"
            destination.mkdir()
            with tarfile.open(archive) as source:
                a9.safe_extract_tar(source, destination)

            extracted = destination / "scripts" / "run.sh"
            self.assertEqual(extracted.read_bytes(), payload)
            if os.name == "posix":
                self.assertEqual(extracted.stat().st_mode & 0o777, 0o755)

    def test_safe_extraction_rejects_a_symlink_that_escapes_the_archive_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "unsafe.tar"
            with tarfile.open(archive, "w") as output:
                member = tarfile.TarInfo("links/outside")
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
                output.addfile(member)

            destination = root / "destination"
            destination.mkdir()
            with tarfile.open(archive) as source:
                with self.assertRaisesRegex(a9.ToolError, "unsafe symlink target"):
                    a9.safe_extract_tar(source, destination)


class FakeChainRunner:
    def __init__(self, manifest, config, receipt):
        self.manifest = manifest
        self.config = config
        self.receipt = receipt
        self.calls = []
        self.chain_id = config["chain_id"]
        self.network_fingerprint = "c" * 64
        self.decimals = 6
        self.factory_admin = ""
        self.deal_admin = ""
        self.factory_code_id = receipt["code_ids"]["factory"]
        self.deal_code_id = receipt["code_ids"]["deal"]
        self.factory_fee_bps = 150
        self.checksum_override = None
        self.factory_checksum_override = None
        self.deal_checksum_override = None
        self.factory_config_overrides = {}
        self.deal_overrides = {}
        self.factory_index_address = DEAL_ADDRESS
        self.broadcast_index = 0

    def run(self, argv, timeout=None):
        self.calls.append(list(argv))
        if argv[1] == "status":
            value = fixture("status.json")
            value["NodeInfo"]["network"] = self.chain_id
            return a9.CommandResult(0, json.dumps(value))
        if argv[1:3] == ["tx", "wasm"]:
            if argv[1:4] == ["tx", "wasm", "instantiate"]:
                has_admin = "--admin" in argv
                has_no_admin = "--no-admin" in argv
                if has_admin == has_no_admin:
                    return a9.CommandResult(1, "", "must set exactly one of --admin or --no-admin")
            hashes = ["D" * 64, "E" * 64, "F" * 64]
            hash_value = hashes[self.broadcast_index]
            self.broadcast_index += 1
            value = fixture("broadcast-sync.json")
            value["txhash"] = hash_value
            return a9.CommandResult(0, json.dumps(value))
        if argv[1:3] == ["query", "tx"]:
            hash_value = argv[3]
            if hash_value == "F" * 64:
                value = fixture("instantiate-confirmed.json")
            else:
                value = fixture("store-confirmed.json")
                value["tx_response"]["logs"][0]["events"][0]["attributes"][0]["value"] = "17" if hash_value == "D" * 64 else "18"
            value["tx_response"]["txhash"] = hash_value
            return a9.CommandResult(0, json.dumps(value))
        if argv[1:4] == ["query", "wasm", "code-info"]:
            code_id = int(argv[4])
            hashes = a9.expected_contract_hashes(self.manifest)
            if code_id == self.receipt["code_ids"]["deal"]:
                digest = self.deal_checksum_override or hashes["marketplace-deal"]
            else:
                digest = self.factory_checksum_override or hashes["marketplace-factory"]
            if self.checksum_override:
                digest = self.checksum_override
            value = {"code_info": {"code_id": str(code_id), "data_hash": base64.b64encode(bytes.fromhex(digest)).decode()}}
            return a9.CommandResult(0, json.dumps(value))
        if argv[1:4] == ["query", "wasm", "contract"]:
            address = argv[4]
            is_factory = address == self.receipt["contracts"]["factory"]
            code_id = self.factory_code_id if is_factory else self.deal_code_id
            admin = self.factory_admin if is_factory else self.deal_admin
            value = {"address": address, "contract_info": {"code_id": str(code_id), "admin": admin}}
            return a9.CommandResult(0, json.dumps(value))
        if argv[1:5] == ["query", "wasm", "contract-state", "smart"]:
            address = argv[5]
            message = json.loads(argv[6])
            if "token_info" in message:
                return a9.CommandResult(0, json.dumps({"name": "Local USDT", "symbol": "USDT", "decimals": self.decimals, "total_supply": "1"}))
            if address == self.receipt["contracts"]["factory"] and "config" in message:
                value = {"deal_code_id": 17, "settlement_cw20": self.config["settlement_cw20"], "fee_recipient": self.config["fee_recipient"], "fee_bps": self.factory_fee_bps}
                value.update(self.factory_config_overrides)
                return a9.CommandResult(0, json.dumps(value))
            if address == self.receipt["contracts"]["factory"] and "deal_by_host_epoch" in message:
                return a9.CommandResult(0, json.dumps({"address": self.factory_index_address}))
            value = {
                "factory": self.receipt["contracts"]["factory"],
                "host": HOST_ADDRESS,
                "deal_address": address,
                "target_epoch": 123,
                "price_micro_usdt_per_gnk": "1000000",
                "buyer_budget_micro_usdt": "100000000",
                "settlement_cw20": self.config["settlement_cw20"],
                "fee_recipient": self.config["fee_recipient"],
                "fee_bps": 150,
                "pinned_gonka_sha": a9.PINNED_GONKA_SHA,
            }
            value.update(self.deal_overrides)
            return a9.CommandResult(0, json.dumps(value))
        raise AssertionError(f"unexpected command: {argv}")


class QueueRunner:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def run(self, argv, timeout=None):
        self.calls.append(list(argv))
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class OptimizerDnsRetryTests(unittest.TestCase):
    def test_optimizer_selects_the_toolchain_already_pinned_in_the_image(self):
        command = a9.optimizer_docker_command(
            Path("/release/build-1/source"), "optimizer@sha256:digest"
        )
        self.assertIn(
            "RUSTUP_TOOLCHAIN=1.81.0-x86_64-unknown-linux-musl", command
        )
        self.assertEqual(command.count("-e"), 1)
        self.assertEqual(command[-1], "optimizer@sha256:digest")

    def test_transient_rustup_dns_failures_are_bounded_and_then_succeed(self):
        failures = [
            a9.CommandResult(
                1,
                "",
                "could not download https://static.rust-lang.org/dist/channel: "
                "dns error: failed to lookup address information",
            ),
            a9.CommandResult(
                1,
                "",
                "could not download https://static.rust-lang.org/dist/channel: "
                "failed to lookup address information",
            ),
        ]
        runner = QueueRunner([*failures, a9.CommandResult(0, "built")])
        delays = []

        result, attempts = a9.run_optimizer_checked(
            runner, ["docker", "run", "optimizer"], sleep=delays.append
        )

        self.assertEqual(result.stdout, "built")
        self.assertEqual(attempts, 3)
        self.assertEqual(delays, [5.0, 15.0])

    def test_crates_index_dns_failure_uses_the_same_bounded_retry(self):
        runner = QueueRunner(
            [
                a9.CommandResult(
                    1,
                    "",
                    "failed to download https://index.crates.io/config.json: "
                    "Couldn't resolve host: index.crates.io",
                ),
                a9.CommandResult(0, "built"),
            ]
        )
        delays = []

        _result, attempts = a9.run_optimizer_checked(
            runner, ["docker", "run", "optimizer"], sleep=delays.append
        )

        self.assertEqual(attempts, 2)
        self.assertEqual(delays, [5.0])

    def test_a_non_dns_optimizer_failure_is_never_retried(self):
        runner = QueueRunner([a9.CommandResult(1, "", "error: could not compile crate")])
        with self.assertRaisesRegex(a9.ToolError, "could not compile crate"):
            a9.run_optimizer_checked(
                runner, ["docker", "run", "optimizer"], sleep=self.fail
            )
        self.assertEqual(len(runner.calls), 1)

    def test_repeated_dns_failure_still_fails_closed_after_four_attempts(self):
        failure = a9.CommandResult(
            1,
            "",
            "https://static.rust-lang.org: dns error: failed to lookup address",
        )
        runner = QueueRunner([failure, failure, failure, failure])
        delays = []
        with self.assertRaisesRegex(a9.ToolError, "static.rust-lang.org"):
            a9.run_optimizer_checked(
                runner, ["docker", "run", "optimizer"], sleep=delays.append
            )
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(delays, [5.0, 15.0, 30.0])


class ContractReleaseTests(unittest.TestCase):
    def run_verify_deal_cli(self, root: Path, manifest_path: Path, config_path: Path, receipt: dict, runner: FakeChainRunner, request: dict | None = None):
        receipt_path = root / "deployment.receipt.json"
        request_path = root / "deal-verification.json"
        a9.write_json_atomic(receipt_path, receipt)
        a9.write_json_atomic(request_path, request or deal_request())
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = a9.main(
                [
                    "verify-deal",
                    "--config", str(config_path),
                    "--manifest", str(manifest_path),
                    "--receipt", str(receipt_path),
                    "--request", str(request_path),
                    "--cli", "inferenced",
                ],
                runner,
            )
        tx_calls = [call for call in runner.calls if call[1:3] == ["tx", "wasm"]]
        return exit_code, stdout.getvalue(), stderr.getvalue(), tx_calls

    def assert_verify_deal_cli_rejected(self, result):
        exit_code, stdout, stderr, tx_calls = result
        self.assertNotEqual(exit_code, 0)
        self.assertNotIn('"factory_indexed": true', stdout)
        self.assertIn("ERROR:", stderr)
        self.assertEqual(tx_calls, [])

    def test_verified_cli_response_fixtures_parse(self):
        self.assertEqual(a9.tx_hash(fixture("broadcast-sync.json")), "A" * 64)
        self.assertEqual(a9.one_event_value(fixture("store-confirmed.json"), ("code_id",), "store"), "17")
        self.assertTrue(a9.one_event_value(fixture("instantiate-confirmed.json"), ("_contract_address",), "instantiate").startswith("gonka1"))
        self.assertEqual(a9.code_checksum(fixture("code-info.json")), "0" * 64)
        self.assertEqual(a9.contract_info(fixture("contract-info.json"))["admin"], "")

    def test_network_fingerprint_uses_real_cometbft_block_rpc_shape(self):
        fingerprint = "a" * 64
        seen_paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen_paths.append(self.path)
                body = json.dumps({"jsonrpc": "2.0", "id": -1, "result": {"block_id": {"hash": fingerprint}, "block": {"header": {"height": "1"}}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            rpc = f"http://127.0.0.1:{server.server_port}"
            self.assertEqual(a9.node_network_fingerprint(rpc), fingerprint)
            self.assertEqual(seen_paths, ["/block?height=1"])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_manifest_rejects_missing_field_and_missing_release_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, _, _, _, _ = make_inputs(Path(directory))
            missing = copy.deepcopy(manifest)
            del missing["inputs"]
            with self.assertRaisesRegex(a9.ToolError, "missing required"):
                a9.validate_manifest(missing)
            not_run = copy.deepcopy(manifest)
            not_run["checks"]["optimizer_build_2"]["status"] = "not_run"
            with self.assertRaisesRegex(a9.ToolError, "release evidence"):
                a9.validate_manifest(not_run)

    def test_tampered_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, manifest_path, _, _, _ = make_inputs(Path(directory))
            (manifest_path.parent / "wasm" / "marketplace_deal.wasm").write_bytes(b"tampered")
            with self.assertRaisesRegex(a9.ToolError, "checksum mismatch"):
                a9.verify_manifest_artifacts(manifest, manifest_path)

    def test_independent_build_mismatch_is_rejected(self):
        with self.assertRaisesRegex(a9.ToolError, "independent optimizer builds differ"):
            a9.ensure_reproducible_builds({"deal": "a" * 64}, {"deal": "b" * 64})

    def test_wrong_chain_id_fails_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, manifest_path, config, _, receipt = make_inputs(Path(directory))
            runner = FakeChainRunner(manifest, config, receipt)
            runner.chain_id = "wrong-chain"
            with self.assertRaisesRegex(a9.ToolError, "chain ID mismatch"):
                a9.preflight(config, manifest, manifest_path, "inferenced", runner)

    def test_production_rejects_old_empty_and_malformed_runtime_evidence(self):
        cases = (
            ("empty", {"source_sha": "", "binary_version": "", "binary_sha256": "0" * 64, "evidence": {}}),
            ("not_run", {"source_sha": "d" * 40, "binary_version": "v", "binary_sha256": "b" * 64, "evidence": {"status": "not_run"}}),
            ("wrong_type", "not-an-object"),
        )
        for name, runtime in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                manifest, manifest_path, config, _, _ = make_inputs(Path(directory))
                config.update({"network_class": "production", "chain_id": "gonka-mainnet", "rpc_endpoint": "https://rpc.example.invalid", "runtime_evidence": runtime})
                with self.assertRaisesRegex(a9.ToolError, "runtime_evidence|release-evidence"):
                    a9.validate_config(config, manifest, manifest_path)

    def test_production_valid_synthetic_evidence_sets_shared_complete_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, manifest_path, config, _, receipt = make_inputs(root)
            config.update({"network_class": "production", "chain_id": "gonka-mainnet", "rpc_endpoint": "https://rpc.example.invalid"})
            config["runtime_evidence"] = make_production_evidence(root, manifest, manifest_path, config)
            runner = FakeChainRunner(manifest, config, receipt)
            result = preflight_with_fake_network(config, manifest, manifest_path, runner)
            self.assertTrue(result["runtime_evidence_complete"])

    def test_local_and_testnet_keep_documented_null_evidence_compatibility(self):
        for network_class in ("local", "testnet"):
            with self.subTest(network_class=network_class), tempfile.TemporaryDirectory() as directory:
                manifest, manifest_path, config, _, receipt = make_inputs(Path(directory))
                config["network_class"] = network_class
                config["runtime_evidence"] = None
                runner = FakeChainRunner(manifest, config, receipt)
                result = a9.preflight(config, manifest, manifest_path, "inferenced", runner)
                self.assertFalse(result["runtime_evidence_complete"])

    def test_production_rejects_missing_failed_not_run_or_partial_check(self):
        for name, mutate in (
            ("missing", lambda evidence: evidence["check_reports"].pop(a9.REQUIRED_PRODUCTION_RELEASE_CHECKS[0])),
            ("failed", lambda evidence: evidence["check_reports"][a9.REQUIRED_PRODUCTION_RELEASE_CHECKS[0]].update({"sha256": "f" * 64})),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest, manifest_path, config, _, receipt = make_inputs(root)
                config.update({"network_class": "production", "chain_id": "gonka-mainnet", "rpc_endpoint": "https://rpc.example.invalid"})
                evidence = make_production_evidence(root, manifest, manifest_path, config)
                if name == "failed":
                    check = a9.REQUIRED_PRODUCTION_RELEASE_CHECKS[0]
                    report_path = manifest_path.parent / evidence["check_reports"][check]["path"]
                    report = a9.load_json(report_path)
                    report["status"] = "FAIL"
                    a9.write_json_atomic(report_path, report)
                    evidence["check_reports"][check]["sha256"] = a9.sha256_file(report_path)
                else:
                    mutate(evidence)
                config["runtime_evidence"] = evidence
                with self.assertRaisesRegex(a9.ToolError, "check_reports|required production release check"):
                    preflight_with_fake_network(config, manifest, manifest_path, FakeChainRunner(manifest, config, receipt))
        for status in ("NOT_RUN", "PARTIAL"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest, manifest_path, config, _, receipt = make_inputs(root)
                config.update({"network_class": "production", "chain_id": "gonka-mainnet", "rpc_endpoint": "https://rpc.example.invalid"})
                evidence = make_production_evidence(root, manifest, manifest_path, config)
                check = a9.REQUIRED_PRODUCTION_RELEASE_CHECKS[0]
                report_path = manifest_path.parent / evidence["check_reports"][check]["path"]
                report = a9.load_json(report_path)
                report["status"] = status
                a9.write_json_atomic(report_path, report)
                evidence["check_reports"][check]["sha256"] = a9.sha256_file(report_path)
                config["runtime_evidence"] = evidence
                with self.assertRaisesRegex(a9.ToolError, "not PASS"):
                    preflight_with_fake_network(config, manifest, manifest_path, FakeChainRunner(manifest, config, receipt))

    def test_production_rejects_wrong_target_and_changed_or_missing_report(self):
        cases = ("network", "runtime", "runtime_source", "runtime_version", "wasm", "settlement_cw20", "fee_recipient", "zero_source", "wrong_type", "missing_file", "changed_file")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest, manifest_path, config, _, receipt = make_inputs(root)
                config.update({"network_class": "production", "chain_id": "gonka-mainnet", "rpc_endpoint": "https://rpc.example.invalid"})
                evidence = make_production_evidence(root, manifest, manifest_path, config)
                check = a9.REQUIRED_PRODUCTION_RELEASE_CHECKS[0]
                report_path = manifest_path.parent / evidence["check_reports"][check]["path"]
                if case == "network":
                    evidence["target"]["chain_id"] = "gonka-local"
                elif case == "runtime":
                    evidence["target"]["runtime_binary_sha256"] = "e" * 64
                elif case == "runtime_source":
                    evidence["runtime"]["source_sha"] = "e" * 40
                elif case == "runtime_version":
                    evidence["runtime"]["binary_version"] = "different version"
                elif case == "wasm":
                    evidence["target"]["deal_wasm_sha256"] = "e" * 64
                elif case == "settlement_cw20":
                    config["settlement_cw20"] = "gonka1differenttoken00000000000000000000000000000"
                elif case == "fee_recipient":
                    config["fee_recipient"] = "gonka1differentfee000000000000000000000000000000"
                elif case == "zero_source":
                    evidence["runtime"]["source_sha"] = "0" * 40
                elif case == "wrong_type":
                    evidence["runtime"]["binary_version"] = 1
                elif case == "missing_file":
                    report_path.unlink()
                else:
                    report_path.write_text("{}", encoding="utf-8")
                config["runtime_evidence"] = evidence
                with self.assertRaises(a9.ToolError):
                    preflight_with_fake_network(config, manifest, manifest_path, FakeChainRunner(manifest, config, receipt))

    def test_production_rejects_empty_report_content_artifact_and_connected_network_mismatch(self):
        for case in ("empty_result", "missing_artifact", "changed_artifact", "network_fingerprint"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest, manifest_path, config, _, receipt = make_inputs(root)
                config.update({"network_class": "production", "chain_id": "gonka-mainnet", "rpc_endpoint": "https://rpc.example.invalid"})
                evidence = make_production_evidence(root, manifest, manifest_path, config)
                check = a9.REQUIRED_PRODUCTION_RELEASE_CHECKS[0]
                report_path = manifest_path.parent / evidence["check_reports"][check]["path"]
                report = a9.load_json(report_path)
                raw_path = manifest_path.parent / report["artifacts"][0]["path"]
                if case == "empty_result":
                    report["result"] = {"assertions": []}
                    a9.write_json_atomic(report_path, report)
                    evidence["check_reports"][check]["sha256"] = a9.sha256_file(report_path)
                elif case == "missing_artifact":
                    raw_path.unlink()
                elif case == "changed_artifact":
                    raw_path.write_text("tampered", encoding="utf-8")
                config["runtime_evidence"] = evidence
                runner = FakeChainRunner(manifest, config, receipt)
                if case == "network_fingerprint":
                    runner.network_fingerprint = "e" * 64
                with self.assertRaises(a9.ToolError):
                    preflight_with_fake_network(config, manifest, manifest_path, runner)

    def test_invalid_production_evidence_stops_deploy_before_transactions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, manifest_path, config, config_path, receipt = make_inputs(root)
            config.update({"network_class": "production", "chain_id": "gonka-mainnet", "rpc_endpoint": "https://rpc.example.invalid", "runtime_evidence": {}})
            a9.write_json_atomic(config_path, config)
            runner = FakeChainRunner(manifest, config, receipt)
            receipt_path = root / "deployment.receipt.json"
            with self.assertRaises(a9.ToolError):
                a9.deploy(config, config_path, manifest, manifest_path, receipt_path, "inferenced", runner)
            self.assertFalse(any(call[1:3] == ["tx", "wasm"] for call in runner.calls))

    def test_failed_confirmed_tx_is_not_success(self):
        sync = a9.CommandResult(0, json.dumps(fixture("broadcast-sync.json")))
        failed = fixture("store-confirmed.json")
        failed["tx_response"]["code"] = 12
        failed["tx_response"]["raw_log"] = "store rejected"
        runner = QueueRunner([sync, a9.CommandResult(0, json.dumps(failed))])
        with self.assertRaisesRegex(a9.ToolError, "failed with code 12"):
            a9.broadcast_and_confirm(runner, ["inferenced", "tx"], "inferenced", "http://rpc", poll_seconds=0)
        self.assertEqual(len([call for call in runner.calls if "tx" in call]), 2)

    def test_ambiguous_broadcast_timeout_never_resends(self):
        timeout = TimeoutError("timeout")
        timeout.stdout = ""
        runner = QueueRunner([timeout])
        with self.assertRaisesRegex(a9.ToolError, "must not be resent"):
            a9.broadcast_and_confirm(runner, ["inferenced", "tx", "wasm", "store"], "inferenced", "http://rpc", poll_seconds=0)
        self.assertEqual(len(runner.calls), 1)

    def test_prepare_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, manifest_path, config, _, receipt = make_inputs(Path(directory))
            runner = FakeChainRunner(manifest, config, receipt)
            a9.preflight(config, manifest, manifest_path, "inferenced", runner)
            commands = a9.deployment_commands(config, manifest, manifest_path, "inferenced")
            self.assertEqual(len(commands), 3)
            self.assertFalse(any(call[1:3] == ["tx", "wasm"] for call in runner.calls))
            self.assertEqual(commands[2].count("--no-admin"), 1)
            self.assertNotIn("--admin", commands[2])

    def test_fake_cli_rejects_missing_or_conflicting_admin_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, manifest_path, config, _, receipt = make_inputs(Path(directory))
            command = a9.deployment_commands(config, manifest, manifest_path, "inferenced")[2]
            without_choice = [argument for argument in command if argument != "--no-admin"]
            with_both = [*command, "--admin", "gonka1admin"]
            for name, invalid in (("missing", without_choice), ("both", with_both)):
                with self.subTest(name=name):
                    runner = FakeChainRunner(manifest, config, receipt)
                    result = runner.run(invalid)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("exactly one", result.stderr)

    def test_existing_receipt_blocks_repeat_before_any_chain_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, manifest_path, config, config_path, receipt = make_inputs(root)
            receipt_path = root / "receipt.json"
            a9.write_json_atomic(receipt_path, receipt)
            runner = FakeChainRunner(manifest, config, receipt)
            with self.assertRaisesRegex(a9.ToolError, "refusing to create duplicate"):
                a9.deploy(config, config_path, manifest, manifest_path, receipt_path, "inferenced", runner)
            self.assertEqual(runner.calls, [])

    def test_deploy_stores_both_codes_instantiates_without_admin_and_writes_verified_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, manifest_path, config, config_path, receipt_template = make_inputs(root)
            receipt_path = root / "deployment.receipt.json"
            runner = FakeChainRunner(manifest, config, receipt_template)
            receipt = a9.deploy(
                config,
                config_path,
                manifest,
                manifest_path,
                receipt_path,
                "inferenced",
                runner,
                timeout_seconds=1,
                poll_seconds=0,
            )
            self.assertEqual(receipt["status"], "complete")
            self.assertEqual(receipt["code_ids"], {"deal": 17, "factory": 18})
            self.assertEqual(receipt["contracts"]["factory"], receipt_template["contracts"]["factory"])
            broadcasts = [call for call in runner.calls if call[1:3] == ["tx", "wasm"]]
            self.assertEqual(len(broadcasts), 3)
            self.assertEqual(broadcasts[2].count("--no-admin"), 1)
            self.assertNotIn("--admin", broadcasts[2])
            self.assertEqual(a9.load_json(receipt_path)["status"], "complete")

    def test_verifier_detects_checksum_code_admin_and_config_mismatches(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, manifest_path, config, config_path, receipt = make_inputs(Path(directory))
            cases = [
                ("checksum_override", "f" * 64, "checksum"),
                ("factory_code_id", 999, "code_id"),
                ("factory_admin", "gonka1admin", "must have no admin"),
                ("factory_fee_bps", 151, "Factory config fee_bps"),
                ("decimals", 18, "CW20 decimals"),
            ]
            for attribute, value, message in cases:
                with self.subTest(attribute=attribute):
                    runner = FakeChainRunner(manifest, config, receipt)
                    setattr(runner, attribute, value)
                    with self.assertRaisesRegex(a9.ToolError, message):
                        a9.verify_deployment(config, config_path, manifest, manifest_path, receipt, "inferenced", runner)

    def test_existing_deal_verifies_code_admin_terms_factory_and_gonka_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, manifest_path, config, config_path, receipt = make_inputs(Path(directory))
            runner = FakeChainRunner(manifest, config, receipt)
            result = a9.verify_deal(deal_request(), config, config_path, manifest, manifest_path, receipt, "inferenced", runner)
            self.assertTrue(result["factory_indexed"])
            self.assertEqual(result["immutable_config"]["pinned_gonka_sha"], a9.PINNED_GONKA_SHA)
            self.assertEqual(result["deployment"]["chain_id"], config["chain_id"])

    def test_verify_deal_cli_success_runs_common_checks_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, manifest_path, config, config_path, receipt = make_inputs(root)
            runner = FakeChainRunner(manifest, config, receipt)
            exit_code, stdout, stderr, tx_calls = self.run_verify_deal_cli(root, manifest_path, config_path, receipt, runner)
            self.assertEqual(exit_code, 0, stderr)
            self.assertIn('"factory_indexed": true', stdout)
            self.assertIn('"deployment"', stdout)
            self.assertTrue(any(call[1] == "status" for call in runner.calls))
            self.assertEqual(tx_calls, [])

    def test_verify_deal_cli_rejects_wrong_node_chain_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, manifest_path, config, config_path, receipt = make_inputs(root)
            runner = FakeChainRunner(manifest, config, receipt)
            runner.chain_id = "wrong-chain"
            self.assert_verify_deal_cli_rejected(self.run_verify_deal_cli(root, manifest_path, config_path, receipt, runner))

    def test_verify_deal_cli_rejects_tampered_local_wasm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, manifest_path, config, config_path, receipt = make_inputs(root)
            (manifest_path.parent / "wasm" / "marketplace_deal.wasm").write_bytes(b"tampered")
            runner = FakeChainRunner(manifest, config, receipt)
            self.assert_verify_deal_cli_rejected(self.run_verify_deal_cli(root, manifest_path, config_path, receipt, runner))

    def test_verify_deal_cli_rejects_stale_manifest_or_config_hash_binding(self):
        for changed in ("manifest", "config"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest, manifest_path, config, config_path, receipt = make_inputs(root)
                if changed == "manifest":
                    manifest["generated_at_utc"] = "2026-09-07T00:00:01Z"
                    a9.write_json_atomic(manifest_path, manifest)
                else:
                    config["factory_label"] = "changed-after-receipt"
                    a9.write_json_atomic(config_path, config)
                runner = FakeChainRunner(manifest, config, receipt)
                self.assert_verify_deal_cli_rejected(self.run_verify_deal_cli(root, manifest_path, config_path, receipt, runner))

    def test_verify_deal_cli_rejects_receipt_from_other_deployment_config_or_manifest(self):
        cases = {
            "deployment": ("chain_id", "another-chain"),
            "config": ("deployment_config_sha256", "c" * 64),
            "manifest": ("build_manifest_sha256", "d" * 64),
        }
        for name, (field, value) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest, manifest_path, config, config_path, receipt = make_inputs(root)
                receipt[field] = value
                runner = FakeChainRunner(manifest, config, receipt)
                self.assert_verify_deal_cli_rejected(self.run_verify_deal_cli(root, manifest_path, config_path, receipt, runner))

    def test_verify_deal_cli_rejects_wrong_factory_checksum_code_admin_or_config(self):
        cases = (
            ("factory_checksum_override", "f" * 64),
            ("factory_code_id", 999),
            ("factory_admin", "gonka1admin"),
            ("factory_fee_bps", 151),
        )
        for attribute, value in cases:
            with self.subTest(attribute=attribute), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest, manifest_path, config, config_path, receipt = make_inputs(root)
                runner = FakeChainRunner(manifest, config, receipt)
                setattr(runner, attribute, value)
                self.assert_verify_deal_cli_rejected(self.run_verify_deal_cli(root, manifest_path, config_path, receipt, runner))

    def test_verify_deal_cli_rejects_wrong_cw20_decimals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, manifest_path, config, config_path, receipt = make_inputs(root)
            runner = FakeChainRunner(manifest, config, receipt)
            runner.decimals = 18
            self.assert_verify_deal_cli_rejected(self.run_verify_deal_cli(root, manifest_path, config_path, receipt, runner))

    def test_verify_deal_cli_rejects_wrong_deal_code_admin_terms_or_factory_index(self):
        cases = (
            ("deal_code_id", 999),
            ("deal_admin", "gonka1admin"),
            ("deal_overrides", {"target_epoch": 124}),
            ("factory_index_address", "gonka1wrongdeal"),
        )
        for attribute, value in cases:
            with self.subTest(attribute=attribute), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest, manifest_path, config, config_path, receipt = make_inputs(root)
                runner = FakeChainRunner(manifest, config, receipt)
                setattr(runner, attribute, value)
                self.assert_verify_deal_cli_rejected(self.run_verify_deal_cli(root, manifest_path, config_path, receipt, runner))


if __name__ == "__main__":
    unittest.main()
