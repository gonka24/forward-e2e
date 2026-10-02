#!/usr/bin/env python3
"""Reproducible build and fail-closed deployment tooling for Marketplace A9.

The module intentionally uses only the Python standard library. Commands are
passed as argv arrays (never through a shell), and no secret material is read or
written by this tool.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "1.0.0"
BUILD_KIND = "gonka-marketplace-build-manifest"
CONFIG_KIND = "gonka-marketplace-deployment-config"
RECEIPT_KIND = "gonka-marketplace-deployment-receipt"
RELEASE_EVIDENCE_KIND = "gonka-marketplace-release-evidence"
RELEASE_EVIDENCE_CHECK_KIND = "gonka-marketplace-release-evidence-check"
PINNED_GONKA_SHA = "379bebced638aeb5e6077bfd51c986f898443832"
RUST_VERSION = "1.81.0"
OPTIMIZER_IMAGE = "cosmwasm/optimizer:0.16.1"
OPTIMIZER_DIGEST = "sha256:b9c92b2900b7ebaab3499203615c1b8589592bc557355ed3432e48851ffde69e"
OPTIMIZER_PLATFORM = "linux/amd64"
COSMWASM_CHECK_VERSION = "2.2.2"
EXPECTED_FEE_BPS = 150
EXPECTED_CW20_DECIMALS = 6
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SOURCE_PATHS = (
    "Cargo.toml",
    "Cargo.lock",
    "rust-toolchain.toml",
    "contracts",
    "packages",
    "tools/proto-gen",
    "scripts",
    "release",
    ".github/workflows",
    "VERSIONS.md",
    "SECURITY.md",
)
REQUIRED_BUILD_CHECKS = (
    "source_tree_clean",
    "committed_lockfiles",
    "optimizer_build_1",
    "optimizer_build_2",
    "reproducible_wasm",
    "cosmwasm_check",
)
# These are the production dependencies documented as B1, B3, B4, B5,
# and B6 in docs/deployment-tooling.md. B2 is
# explicitly optional, so it is deliberately not a release gate.
REQUIRED_PRODUCTION_RELEASE_CHECKS = (
    "B1_allowlisted_grpc",
    "B3_gas_pruning_bounds",
    "B4_runtime_and_claimed_safety",
    "B5_golden_e2e",
    "B6_production_parameters_and_operations",
)
CONTRACT_FILES = {
    "marketplace-deal": "marketplace_deal.wasm",
    "marketplace-factory": "marketplace_factory.wasm",
}
OPTIMIZER_DNS_RETRY_DELAYS_SECONDS = (5.0, 15.0, 30.0)
OPTIMIZER_RUSTUP_TOOLCHAIN = "1.81.0-x86_64-unknown-linux-musl"


class ToolError(RuntimeError):
    """Expected fail-closed validation or external command failure."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


class CommandRunner:
    def run(self, argv: Sequence[str], timeout: float | None = None) -> CommandResult:
        try:
            completed = subprocess.run(
                list(argv),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _text(exc.stdout)
            stderr = _text(exc.stderr)
            error = TimeoutError(f"command timed out: {display_command(argv)}")
            error.stdout = stdout  # type: ignore[attr-defined]
            error.stderr = stderr  # type: ignore[attr-defined]
            raise error from exc
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ToolError(f"JSON root must be an object: {path}")
    return value


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stream.write(payload)
        temporary = Path(stream.name)
    os.replace(temporary, path)


def display_command(argv: Sequence[str]) -> str:
    return " ".join(json.dumps(item) if re.search(r"\s", item) else item for item in argv)


def require_fields(value: Mapping[str, Any], fields: Iterable[str], context: str) -> None:
    missing = sorted(set(fields) - set(value))
    if missing:
        raise ToolError(f"{context} missing required field(s): {', '.join(missing)}")


def require_sha(value: Any, context: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ToolError(f"{context} must be a lowercase 64-character SHA-256")
    return value


def require_git_sha(value: Any, context: str) -> str:
    if not isinstance(value, str) or not GIT_SHA_RE.fullmatch(value):
        raise ToolError(f"{context} must be a lowercase full 40-character Git SHA")
    return value


def require_nonempty(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{context} must be a non-empty string")
    if any(marker in value for marker in ("<", ">", "REPLACE_ME")):
        raise ToolError(f"{context} still contains a placeholder")
    return value


def validate_manifest(manifest: Mapping[str, Any], require_evidence: bool = True) -> None:
    require_fields(
        manifest,
        ("schema_version", "kind", "generated_at_utc", "marketplace_commit_sha", "gonka", "toolchain", "inputs", "contracts", "schemas", "checks"),
        "build manifest",
    )
    if manifest["schema_version"] != SCHEMA_VERSION or manifest["kind"] != BUILD_KIND:
        raise ToolError("unsupported build manifest kind or schema_version")
    require_git_sha(manifest["marketplace_commit_sha"], "marketplace_commit_sha")
    gonka = manifest["gonka"]
    if not isinstance(gonka, dict):
        raise ToolError("gonka must be an object")
    require_fields(gonka, ("pinned_source_sha", "runtime"), "gonka")
    if gonka["pinned_source_sha"] != PINNED_GONKA_SHA:
        raise ToolError("build manifest pinned Gonka SHA differs from VERSIONS.md")
    runtime = gonka["runtime"]
    if not isinstance(runtime, dict):
        raise ToolError("gonka.runtime must be an object")
    require_fields(runtime, ("source_sha", "binary_version", "binary_sha256", "evidence"), "gonka.runtime")
    if runtime["source_sha"] is not None:
        require_git_sha(runtime["source_sha"], "gonka.runtime.source_sha")
    if runtime["binary_sha256"] is not None:
        require_sha(runtime["binary_sha256"], "gonka.runtime.binary_sha256")

    toolchain = manifest["toolchain"]
    if not isinstance(toolchain, dict):
        raise ToolError("toolchain must be an object")
    require_fields(toolchain, ("rust", "optimizer", "cosmwasm_check"), "toolchain")
    optimizer = toolchain["optimizer"]
    expected = {
        "rust": RUST_VERSION,
        "cosmwasm_check": COSMWASM_CHECK_VERSION,
    }
    for key, expected_value in expected.items():
        if toolchain[key] != expected_value:
            raise ToolError(f"toolchain.{key} must equal pinned value {expected_value}")
    if not isinstance(optimizer, dict) or optimizer != {
        "image": OPTIMIZER_IMAGE,
        "digest": OPTIMIZER_DIGEST,
        "platform": OPTIMIZER_PLATFORM,
    }:
        raise ToolError("optimizer identity does not match VERSIONS.md")

    inputs = manifest["inputs"]
    if not isinstance(inputs, dict):
        raise ToolError("inputs must be an object")
    require_fields(inputs, ("cargo_lock_sha256", "proto_generator_lock_sha256"), "inputs")
    require_sha(inputs["cargo_lock_sha256"], "inputs.cargo_lock_sha256")
    require_sha(inputs["proto_generator_lock_sha256"], "inputs.proto_generator_lock_sha256")

    contracts = manifest["contracts"]
    if not isinstance(contracts, list) or len(contracts) != 2:
        raise ToolError("contracts must contain exactly Marketplace Deal and Factory")
    names = set()
    for index, contract in enumerate(contracts):
        if not isinstance(contract, dict):
            raise ToolError(f"contracts[{index}] must be an object")
        require_fields(contract, ("name", "path", "sha256"), f"contracts[{index}]")
        names.add(contract["name"])
        require_nonempty(contract["path"], f"contracts[{index}].path")
        require_sha(contract["sha256"], f"contracts[{index}].sha256")
    if names != set(CONTRACT_FILES):
        raise ToolError("contracts must name marketplace-deal and marketplace-factory exactly once")

    schemas = manifest["schemas"]
    if not isinstance(schemas, list) or not schemas:
        raise ToolError("schemas must contain committed generated contract schemas")
    for index, schema in enumerate(schemas):
        if not isinstance(schema, dict):
            raise ToolError(f"schemas[{index}] must be an object")
        require_fields(schema, ("path", "sha256"), f"schemas[{index}]")
        require_nonempty(schema["path"], f"schemas[{index}].path")
        require_sha(schema["sha256"], f"schemas[{index}].sha256")

    checks = manifest["checks"]
    if not isinstance(checks, dict):
        raise ToolError("checks must be an object")
    require_fields(checks, REQUIRED_BUILD_CHECKS, "checks")
    if require_evidence:
        absent = [name for name in REQUIRED_BUILD_CHECKS if not isinstance(checks[name], dict) or checks[name].get("status") != "pass"]
        if absent:
            raise ToolError("required release evidence is not passing: " + ", ".join(absent))


def _safe_artifact(root: Path, relative: Any, context: str) -> Path:
    value = require_nonempty(relative, context)
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ToolError(f"{context} escapes the manifest directory") from exc
    return candidate


def verify_manifest_artifacts(manifest: Mapping[str, Any], manifest_path: Path) -> None:
    validate_manifest(manifest)
    root = manifest_path.resolve().parent
    for group in ("contracts", "schemas"):
        for item in manifest[group]:
            artifact = _safe_artifact(root, item["path"], f"{group} path")
            if not artifact.is_file():
                raise ToolError(f"missing artifact: {artifact}")
            actual = sha256_file(artifact)
            if actual != item["sha256"]:
                raise ToolError(f"checksum mismatch for {artifact}: expected {item['sha256']}, got {actual}")


def _require_exact_keys(value: Mapping[str, Any], keys: Iterable[str], context: str) -> None:
    expected = set(keys)
    actual = set(value)
    if actual != expected:
        raise ToolError(f"{context} must contain exactly: {', '.join(sorted(expected))}")


def _require_production_target(target: Any, config: Mapping[str, Any], manifest: Mapping[str, Any], runtime: Mapping[str, Any], context: str) -> dict[str, Any]:
    if not isinstance(target, dict):
        raise ToolError(f"{context} must be an object")
    keys = ("network_class", "chain_id", "rpc_endpoint", "network_fingerprint_sha256", "pinned_interface_source_sha", "build_manifest_sha256", "runtime_source_sha", "runtime_binary_version", "runtime_binary_sha256", "deal_wasm_sha256", "factory_wasm_sha256", "settlement_cw20", "settlement_cw20_decimals", "fee_recipient", "fee_bps", "factory_label", "tx_from", "tx_gas", "tx_gas_adjustment")
    _require_exact_keys(target, keys, context)
    if target["network_class"] != "production":
        raise ToolError(f"{context}.network_class must be production")
    for key in ("chain_id", "rpc_endpoint", "settlement_cw20", "fee_recipient", "factory_label", "tx_from", "tx_gas", "tx_gas_adjustment", "runtime_binary_version"):
        require_nonempty(target[key], f"{context}.{key}")
    for key in ("network_fingerprint_sha256", "build_manifest_sha256", "runtime_binary_sha256", "deal_wasm_sha256", "factory_wasm_sha256"):
        require_sha(target[key], f"{context}.{key}")
        if target[key] == "0" * 64:
            raise ToolError(f"{context}.{key} must not be an all-zero placeholder")
    if target["runtime_source_sha"] != runtime["source_sha"] or target["runtime_binary_version"] != runtime["binary_version"] or target["runtime_binary_sha256"] != runtime["binary_sha256"]:
        raise ToolError("release evidence target does not match runtime identity")
    if target["pinned_interface_source_sha"] != config["expected_gonka_source_sha"] or target["build_manifest_sha256"] != config["build_manifest_sha256"]:
        raise ToolError("release evidence target does not match pinned source or build manifest")
    expected_config = {
        "chain_id": config["chain_id"], "rpc_endpoint": config["rpc_endpoint"], "settlement_cw20": config["settlement_cw20"],
        "settlement_cw20_decimals": config["settlement_cw20_decimals"], "fee_recipient": config["fee_recipient"], "fee_bps": config["fee_bps"],
        "factory_label": config["factory_label"], "tx_from": config["tx"]["from"], "tx_gas": config["tx"]["gas"], "tx_gas_adjustment": config["tx"]["gas_adjustment"],
    }
    if any(target[key] != value for key, value in expected_config.items()):
        raise ToolError("release evidence target does not match deployment network")
    hashes = expected_contract_hashes(manifest)
    if target["deal_wasm_sha256"] != hashes["marketplace-deal"] or target["factory_wasm_sha256"] != hashes["marketplace-factory"]:
        raise ToolError("release evidence target does not match deployable Deal/Factory Wasm")
    return target


def _validate_report_content(check: str, report: Mapping[str, Any], root: Path) -> None:
    attestation = report["attestation"]
    if not isinstance(attestation, dict):
        raise ToolError(f"release evidence report {check}.attestation must be an object")
    _require_exact_keys(attestation, ("operator", "issued_at_utc", "method"), f"release evidence report {check}.attestation")
    for field in ("operator", "issued_at_utc", "method"):
        require_nonempty(attestation[field], f"release evidence report {check}.attestation.{field}")
    result = report["result"]
    if not isinstance(result, dict) or not result:
        raise ToolError(f"release evidence report {check}.result must be a non-empty object")
    assertions = result.get("assertions")
    if not isinstance(assertions, list) or not assertions or any(not isinstance(item, str) or not item.strip() for item in assertions):
        raise ToolError(f"release evidence report {check}.result.assertions must be a non-empty string list")
    artifacts = report["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise ToolError(f"release evidence report {check}.artifacts must be a non-empty list")
    for index, artifact in enumerate(artifacts):
        context = f"release evidence report {check}.artifacts[{index}]"
        if not isinstance(artifact, dict):
            raise ToolError(f"{context} must be an object")
        _require_exact_keys(artifact, ("path", "sha256"), context)
        path = _safe_artifact(root, artifact["path"], f"{context}.path")
        require_sha(artifact["sha256"], f"{context}.sha256")
        if not path.is_file() or sha256_file(path) != artifact["sha256"]:
            raise ToolError(f"{context} is missing or its checksum differs")


def validate_runtime_evidence(runtime: Any, config: Mapping[str, Any], manifest: Mapping[str, Any], manifest_path: Path, actual_network_fingerprint: str) -> bool:
    """Validate production release evidence and return one shared completeness result.

    Hashes and a consistent report establish artifact/report integrity only. They
    cannot prove that a remote validator runs the reported binary; that remains
    an operational trust boundary documented with the evidence format.
    """
    if config["network_class"] != "production":
        return False
    if not isinstance(runtime, dict):
        raise ToolError("production config requires release-evidence object")
    fields = ("schema_version", "kind", "runtime", "target", "check_reports")
    _require_exact_keys(runtime, fields, "runtime_evidence")
    if runtime["schema_version"] != SCHEMA_VERSION or runtime["kind"] != RELEASE_EVIDENCE_KIND:
        raise ToolError("unsupported release-evidence kind or schema_version")
    if not config["rpc_endpoint"].startswith("https://"):
        raise ToolError("production deployment requires an https RPC endpoint")
    runtime_identity = runtime["runtime"]
    if not isinstance(runtime_identity, dict):
        raise ToolError("runtime_evidence.runtime must be an object")
    _require_exact_keys(runtime_identity, ("source_sha", "binary_version", "binary_sha256"), "runtime_evidence.runtime")
    require_git_sha(runtime_identity["source_sha"], "runtime_evidence.runtime.source_sha")
    if runtime_identity["source_sha"] == "0" * 40:
        raise ToolError("runtime_evidence.runtime.source_sha must not be an all-zero placeholder")
    require_nonempty(runtime_identity["binary_version"], "runtime_evidence.runtime.binary_version")
    require_sha(runtime_identity["binary_sha256"], "runtime_evidence.runtime.binary_sha256")
    if runtime_identity["binary_sha256"] == "0" * 64:
        raise ToolError("runtime_evidence.runtime.binary_sha256 must not be an all-zero placeholder")
    target = _require_production_target(runtime["target"], config, manifest, runtime_identity, "runtime_evidence.target")
    if target["network_fingerprint_sha256"] != actual_network_fingerprint:
        raise ToolError("release evidence network fingerprint does not match the connected node")
    reports = runtime["check_reports"]
    if not isinstance(reports, dict):
        raise ToolError("runtime_evidence.check_reports must be an object")
    _require_exact_keys(reports, REQUIRED_PRODUCTION_RELEASE_CHECKS, "runtime_evidence.check_reports")
    root = manifest_path.resolve().parent
    for check in REQUIRED_PRODUCTION_RELEASE_CHECKS:
        reference = reports[check]
        if not isinstance(reference, dict):
            raise ToolError(f"runtime_evidence.check_reports.{check} must be an object")
        _require_exact_keys(reference, ("path", "sha256"), f"runtime_evidence.check_reports.{check}")
        report_path = _safe_artifact(root, reference["path"], f"runtime_evidence.check_reports.{check}.path")
        require_sha(reference["sha256"], f"runtime_evidence.check_reports.{check}.sha256")
        if reference["sha256"] == "0" * 64:
            raise ToolError(f"runtime_evidence.check_reports.{check}.sha256 must not be an all-zero placeholder")
        if not report_path.is_file():
            raise ToolError(f"missing release evidence report: {report_path}")
        if sha256_file(report_path) != reference["sha256"]:
            raise ToolError(f"release evidence report checksum mismatch: {report_path}")
        report = load_json(report_path)
        if not isinstance(report, dict):
            raise ToolError(f"release evidence report must be an object: {report_path}")
        _require_exact_keys(report, ("schema_version", "kind", "check", "status", "target", "attestation", "result", "artifacts"), f"release evidence report {check}")
        if report["schema_version"] != SCHEMA_VERSION or report["kind"] != RELEASE_EVIDENCE_CHECK_KIND:
            raise ToolError(f"unsupported release evidence report format: {report_path}")
        if report["check"] != check or report["status"] != "PASS":
            raise ToolError(f"required production release check is not PASS: {check}")
        if _require_production_target(report["target"], config, manifest, runtime_identity, f"release evidence report {check}.target") != target:
            raise ToolError(f"release evidence report target differs: {check}")
        _validate_report_content(check, report, root)
    return True


def validate_config(config: Mapping[str, Any], manifest: Mapping[str, Any], manifest_path: Path) -> None:
    require_fields(
        config,
        ("schema_version", "kind", "network_class", "chain_id", "rpc_endpoint", "expected_gonka_source_sha", "build_manifest_sha256", "settlement_cw20", "settlement_cw20_decimals", "fee_recipient", "fee_bps", "factory_label", "tx", "runtime_evidence"),
        "deployment config",
    )
    if config["schema_version"] != SCHEMA_VERSION or config["kind"] != CONFIG_KIND:
        raise ToolError("unsupported deployment config kind or schema_version")
    if config["network_class"] not in ("local", "testnet", "production"):
        raise ToolError("network_class must be local, testnet, or production")
    require_nonempty(config["chain_id"], "chain_id")
    rpc = require_nonempty(config["rpc_endpoint"], "rpc_endpoint")
    if not rpc.startswith(("http://", "https://")):
        raise ToolError("rpc_endpoint must use http:// or https://")
    if config["expected_gonka_source_sha"] != PINNED_GONKA_SHA or config["expected_gonka_source_sha"] != manifest["gonka"]["pinned_source_sha"]:
        raise ToolError("deployment config and build manifest pinned Gonka SHA differ")
    require_sha(config["build_manifest_sha256"], "build_manifest_sha256")
    if sha256_file(manifest_path) != config["build_manifest_sha256"]:
        raise ToolError("deployment config build_manifest_sha256 does not match manifest bytes")
    require_nonempty(config["settlement_cw20"], "settlement_cw20")
    require_nonempty(config["fee_recipient"], "fee_recipient")
    require_nonempty(config["factory_label"], "factory_label")
    if config["settlement_cw20_decimals"] != EXPECTED_CW20_DECIMALS:
        raise ToolError("settlement_cw20_decimals must be 6")
    if config["fee_bps"] != EXPECTED_FEE_BPS:
        raise ToolError("fee_bps must be 150")
    tx = config["tx"]
    if not isinstance(tx, dict):
        raise ToolError("tx must be an object")
    require_fields(tx, ("from", "gas", "gas_adjustment"), "tx")
    require_nonempty(tx["from"], "tx.from")
    require_nonempty(tx["gas"], "tx.gas")
    require_nonempty(tx["gas_adjustment"], "tx.gas_adjustment")
    runtime = config["runtime_evidence"]
    if config["network_class"] == "production":
        if not isinstance(runtime, dict):
            raise ToolError("production config requires release-evidence object")
        _require_exact_keys(runtime, ("schema_version", "kind", "runtime", "target", "check_reports"), "runtime_evidence")
        if runtime["schema_version"] != SCHEMA_VERSION or runtime["kind"] != RELEASE_EVIDENCE_KIND:
            raise ToolError("unsupported release-evidence kind or schema_version")
    elif runtime is not None:
        raise ToolError("runtime_evidence must be null outside production")
    return None


def validate_receipt(receipt: Mapping[str, Any]) -> None:
    require_fields(receipt, ("schema_version", "kind", "status", "created_at_utc", "chain_id", "build_manifest_sha256", "deployment_config_sha256", "transactions", "code_ids", "contracts", "verification"), "deployment receipt")
    if receipt["schema_version"] != SCHEMA_VERSION or receipt["kind"] != RECEIPT_KIND:
        raise ToolError("unsupported deployment receipt kind or schema_version")
    if receipt["status"] not in ("in_progress", "ambiguous", "failed", "complete"):
        raise ToolError("invalid deployment receipt status")
    require_nonempty(receipt["chain_id"], "receipt.chain_id")
    require_sha(receipt["build_manifest_sha256"], "receipt.build_manifest_sha256")
    require_sha(receipt["deployment_config_sha256"], "receipt.deployment_config_sha256")


def run_checked(runner: CommandRunner, argv: Sequence[str], timeout: float | None = None) -> CommandResult:
    result = runner.run(argv, timeout=timeout)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise ToolError(f"command failed: {display_command(argv)}: {detail}")
    return result


def run_optimizer_checked(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    sleep: Any = time.sleep,
) -> tuple[CommandResult, int]:
    """Retry only the observed rustup DNS failure, preserving fail-closed builds."""
    for attempt in range(1, len(OPTIMIZER_DNS_RETRY_DELAYS_SECONDS) + 2):
        result = runner.run(argv)
        if result.returncode == 0:
            return result, attempt
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        normalized = detail.lower()
        transient_dns = (
            any(
                host in normalized
                for host in ("static.rust-lang.org", "index.crates.io", "static.crates.io")
            )
            and any(
                marker in normalized
                for marker in (
                    "dns error",
                    "failed to lookup address",
                    "couldn't resolve host",
                    "could not resolve host",
                )
            )
        )
        if not transient_dns or attempt > len(OPTIMIZER_DNS_RETRY_DELAYS_SECONDS):
            raise ToolError(f"command failed: {display_command(argv)}: {detail}")
        delay = OPTIMIZER_DNS_RETRY_DELAYS_SECONDS[attempt - 1]
        print(
            f"optimizer transient DNS failure on attempt {attempt}; retrying in {delay:g}s: {detail}",
            file=sys.stderr,
            flush=True,
        )
        sleep(delay)
    raise AssertionError("optimizer retry loop exhausted without returning")


def parse_json_output(result: CommandResult, context: str) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError(f"{context} did not return JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ToolError(f"{context} JSON root must be an object")
    return value


def git_output(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise ToolError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def ensure_clean_sources(repo: Path) -> None:
    output = git_output(repo, "status", "--porcelain=v1", "--untracked-files=all", "--", *SOURCE_PATHS)
    if output.strip():
        raise ToolError("release build refuses uncommitted source/input changes:\n" + output.rstrip())


def git_blob(repo: Path, commit: str, path: str) -> bytes:
    result = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=repo, check=False, capture_output=True)
    if result.returncode != 0:
        raise ToolError(f"committed input is missing at {commit}:{path}")
    return result.stdout


def safe_extract_tar(bundle: tarfile.TarFile, destination: Path) -> None:
    """Extract source safely even on Python versions without tar filters."""
    root = destination.resolve()
    directories: list[tuple[tarfile.TarInfo, Path]] = []
    files: list[tuple[tarfile.TarInfo, Path]] = []
    links: list[tuple[tarfile.TarInfo, Path]] = []

    for member in bundle.getmembers():
        target = (destination / member.name).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ToolError(f"unsafe path in git archive: {member.name}") from exc

        if member.isdir():
            directories.append((member, target))
        elif member.isreg():
            files.append((member, target))
        elif member.issym():
            link_name = member.linkname
            if Path(link_name).is_absolute() or re.match(r"^[A-Za-z]:[\\\\/]", link_name):
                raise ToolError(f"unsafe symlink target in git archive: {member.name}")
            link_target = (target.parent / link_name).resolve()
            try:
                link_target.relative_to(root)
            except ValueError as exc:
                raise ToolError(f"unsafe symlink target in git archive: {member.name}") from exc
            links.append((member, target))
        else:
            raise ToolError(f"unsupported archive member type: {member.name}")

    for _, target in directories:
        target.mkdir(parents=True, exist_ok=True)
    for member, target in files:
        target.parent.mkdir(parents=True, exist_ok=True)
        content = bundle.extractfile(member)
        if content is None:
            raise ToolError(f"archive member has no readable content: {member.name}")
        with content, target.open("xb") as extracted:
            shutil.copyfileobj(content, extracted)
        target.chmod(member.mode & 0o777)
    # Links are created last so a later file cannot traverse a symlink that
    # appeared earlier in the archive.
    created_links: list[Path] = []
    try:
        for member, target in links:
            # An earlier link may have changed the meaning of this parent.
            parent = target.parent.resolve()
            if not parent.is_relative_to(root):
                raise ToolError(f"unsafe symlink parent in git archive: {member.name}")
            parent.mkdir(parents=True, exist_ok=True)
            target = parent / target.name
            os.symlink(member.linkname, target)
            created_links.append(target)
        # Forward references must be checked after *all* links exist: individually
        # safe targets can form an escaping chain (d/b -> .., escape -> d/b/..).
        for target in created_links:
            try:
                target.resolve(strict=False).relative_to(root)
            except (ValueError, OSError, RuntimeError) as exc:
                raise ToolError(f"unsafe symlink target in git archive: {target}") from exc
    except Exception:
        for target in reversed(created_links):
            target.unlink()
        raise


def extract_git_archive(repo: Path, commit: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    archive = destination.parent / "source.tar"
    result = subprocess.run(["git", "archive", "--format=tar", f"--output={archive}", commit], cwd=repo, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise ToolError(result.stderr.strip() or "git archive failed")
    try:
        with tarfile.open(archive) as bundle:
            safe_extract_tar(bundle, destination)
    finally:
        archive.unlink(missing_ok=True)


def ensure_reproducible_builds(first: Mapping[str, str], second: Mapping[str, str]) -> None:
    if dict(first) != dict(second):
        raise ToolError(f"independent optimizer builds differ: {dict(first)} != {dict(second)}")


def optimizer_docker_command(source: Path | str, image: str) -> list[str]:
    return [
        "docker", "run", "--rm", "--platform", OPTIMIZER_PLATFORM,
        "-e", f"RUSTUP_TOOLCHAIN={OPTIMIZER_RUSTUP_TOOLCHAIN}",
        "--mount", f"type=bind,source={source},target=/code", image,
    ]


def build_release(repo: Path, output: Path, commit_ref: str, runner: CommandRunner) -> Path:
    repo = repo.resolve()
    output = output.resolve()
    if output.exists():
        raise ToolError(f"output directory already exists: {output}")
    ensure_clean_sources(repo)
    commit = git_output(repo, "rev-parse", f"{commit_ref}^{{commit}}").strip()
    head = git_output(repo, "rev-parse", "HEAD").strip()
    if commit != head:
        raise ToolError(f"release commit {commit} is not current HEAD {head}")
    require_git_sha(commit, "resolved commit")
    cargo_lock = git_blob(repo, commit, "Cargo.lock")
    proto_lock = git_blob(repo, commit, "tools/proto-gen/Cargo.lock")

    image = f"{OPTIMIZER_IMAGE}@{OPTIMIZER_DIGEST}"
    build_hashes: list[dict[str, str]] = []
    command_records: list[list[str]] = []
    optimizer_attempts: list[int] = []
    for number in (1, 2):
        source = output / f"build-{number}" / "source"
        extract_git_archive(repo, commit, source)
        docker_argv = optimizer_docker_command(source, image)
        recorded = optimizer_docker_command(f"<build-{number}/source>", image)
        command_records.append(recorded)
        _result, attempts = run_optimizer_checked(runner, docker_argv)
        optimizer_attempts.append(attempts)
        artifacts = source / "artifacts"
        hashes: dict[str, str] = {}
        for filename in CONTRACT_FILES.values():
            wasm = artifacts / filename
            if not wasm.is_file():
                raise ToolError(f"optimizer did not produce {filename} in build {number}")
            hashes[filename] = sha256_file(wasm)
        build_hashes.append(hashes)
    ensure_reproducible_builds(build_hashes[0], build_hashes[1])

    wasm_dir = output / "wasm"
    wasm_dir.mkdir(parents=True)
    for filename in CONTRACT_FILES.values():
        shutil.copyfile(output / "build-1" / "source" / "artifacts" / filename, wasm_dir / filename)

    version = run_checked(runner, ["cosmwasm-check", "--version"])
    if COSMWASM_CHECK_VERSION not in version.stdout:
        raise ToolError(f"cosmwasm-check must be {COSMWASM_CHECK_VERSION}, got: {version.stdout.strip()}")
    check_argv = ["cosmwasm-check", *(str(wasm_dir / filename) for filename in CONTRACT_FILES.values())]
    check_result = run_checked(runner, check_argv)

    schema_paths = git_output(repo, "ls-tree", "-r", "--name-only", commit, "contracts/marketplace-deal/schema", "contracts/marketplace-factory/schema").splitlines()
    schema_dir = output / "schemas"
    schemas: list[dict[str, str]] = []
    for path in sorted(schema_paths):
        data = git_blob(repo, commit, path)
        destination = schema_dir / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        relative = destination.relative_to(output).as_posix()
        schemas.append({"path": relative, "sha256": sha256_bytes(data)})

    contracts = [
        {"name": name, "path": f"wasm/{filename}", "sha256": build_hashes[0][filename]}
        for name, filename in CONTRACT_FILES.items()
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": BUILD_KIND,
        "generated_at_utc": utc_now(),
        "marketplace_commit_sha": commit,
        "gonka": {
            "pinned_source_sha": PINNED_GONKA_SHA,
            "runtime": {"source_sha": None, "binary_version": None, "binary_sha256": None, "evidence": None},
        },
        "toolchain": {
            "rust": RUST_VERSION,
            "optimizer": {"image": OPTIMIZER_IMAGE, "digest": OPTIMIZER_DIGEST, "platform": OPTIMIZER_PLATFORM},
            "cosmwasm_check": COSMWASM_CHECK_VERSION,
        },
        "inputs": {
            "cargo_lock_sha256": sha256_bytes(cargo_lock),
            "proto_generator_lock_sha256": sha256_bytes(proto_lock),
        },
        "contracts": contracts,
        "schemas": schemas,
        "checks": {
            "source_tree_clean": {"status": "pass", "command": ["git", "status", "--porcelain=v1", "--", *SOURCE_PATHS]},
            "committed_lockfiles": {"status": "pass", "paths": ["Cargo.lock", "tools/proto-gen/Cargo.lock"]},
            "optimizer_build_1": {"status": "pass", "command": command_records[0], "attempts": optimizer_attempts[0], "sha256": build_hashes[0]},
            "optimizer_build_2": {"status": "pass", "command": command_records[1], "attempts": optimizer_attempts[1], "sha256": build_hashes[1]},
            "reproducible_wasm": {"status": "pass", "details": "both independent optimizer outputs are byte-identical"},
            "cosmwasm_check": {"status": "pass", "command": ["cosmwasm-check", *[f"wasm/{name}" for name in CONTRACT_FILES.values()]], "output": check_result.stdout.strip()},
        },
    }
    manifest_path = output / "build-manifest.json"
    write_json_atomic(manifest_path, manifest)
    verify_manifest_artifacts(manifest, manifest_path)
    return manifest_path


def unwrap_tx(value: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = value.get("tx_response")
    return nested if isinstance(nested, dict) else value


def tx_code(value: Mapping[str, Any]) -> int:
    raw = unwrap_tx(value).get("code", 0)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"transaction code is not an integer: {raw!r}") from exc


def tx_hash(value: Mapping[str, Any]) -> str | None:
    raw = unwrap_tx(value).get("txhash") or unwrap_tx(value).get("tx_hash")
    return raw if isinstance(raw, str) and raw else None


def tx_events(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tx = unwrap_tx(value)
    result: list[Mapping[str, Any]] = []
    for event in tx.get("events", []) if isinstance(tx.get("events"), list) else []:
        if isinstance(event, dict):
            result.append(event)
    logs = tx.get("logs", [])
    if isinstance(logs, list):
        for log in logs:
            if isinstance(log, dict) and isinstance(log.get("events"), list):
                result.extend(event for event in log["events"] if isinstance(event, dict))
    return result


def event_values(value: Mapping[str, Any], keys: Iterable[str]) -> list[str]:
    wanted = set(keys)
    found: list[str] = []
    for event in tx_events(value):
        attributes = event.get("attributes", [])
        if not isinstance(attributes, list):
            continue
        for attribute in attributes:
            if isinstance(attribute, dict) and attribute.get("key") in wanted and isinstance(attribute.get("value"), str):
                found.append(attribute["value"])
    return sorted(set(found))


def one_event_value(value: Mapping[str, Any], keys: Iterable[str], context: str) -> str:
    values = event_values(value, keys)
    if len(values) != 1:
        raise ToolError(f"{context} expected one structured event value, got {values}")
    return values[0]


def cli_json(runner: CommandRunner, argv: Sequence[str], context: str, timeout: float = 30) -> dict[str, Any]:
    return parse_json_output(run_checked(runner, argv, timeout=timeout), context)


def node_chain_id(runner: CommandRunner, cli: str, rpc: str) -> str:
    value = cli_json(runner, [cli, "status", "--node", rpc, "--output", "json"], "node status")
    candidates = (
        value.get("NodeInfo", {}).get("network") if isinstance(value.get("NodeInfo"), dict) else None,
        value.get("node_info", {}).get("network") if isinstance(value.get("node_info"), dict) else None,
        value.get("result", {}).get("node_info", {}).get("network") if isinstance(value.get("result"), dict) and isinstance(value["result"].get("node_info"), dict) else None,
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
    raise ToolError("node status does not contain a recognizable chain ID")


def node_network_fingerprint(rpc: str) -> str:
    """Read the immutable genesis-block ID from the connected CometBFT node.

    A chain ID can be reused by a local A8/Testermint instance. The block at
    height 1 identifies the concrete chain history; unavailable/pruned genesis
    data is a production fail-closed condition. This intentionally uses the
    CometBFT RPC response: the pinned `inferenced query block` CLI defaults to
    hash lookup and its protobuf output does not contain top-level `block_id`.
    """
    parts = urllib.parse.urlsplit(rpc)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ToolError("RPC endpoint is not a valid HTTP(S) URL")
    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/block", "height=1", ""))
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError(f"CometBFT genesis block query failed: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
        raise ToolError("CometBFT genesis block response has no result object")
    block_id = value["result"].get("block_id")
    fingerprint = block_id.get("hash") if isinstance(block_id, dict) else None
    if not isinstance(fingerprint, str):
        raise ToolError("genesis block query does not contain block_id.hash")
    normalized = fingerprint.lower().removeprefix("0x")
    require_sha(normalized, "genesis block fingerprint")
    if normalized == "0" * 64:
        raise ToolError("genesis block fingerprint must not be an all-zero placeholder")
    return normalized


def query_wasm(runner: CommandRunner, cli: str, rpc: str, *args: str) -> dict[str, Any]:
    return cli_json(runner, [cli, "query", "wasm", *args, "--node", rpc, "--output", "json"], "wasm query")


def smart_query(runner: CommandRunner, cli: str, rpc: str, address: str, message: Mapping[str, Any]) -> dict[str, Any]:
    value = query_wasm(runner, cli, rpc, "contract-state", "smart", address, json.dumps(message, separators=(",", ":"), sort_keys=True))
    return value["data"] if isinstance(value.get("data"), dict) else value


def code_checksum(value: Mapping[str, Any]) -> str:
    info = value.get("code_info") if isinstance(value.get("code_info"), dict) else value
    raw = info.get("data_hash") or info.get("checksum")
    if not isinstance(raw, str):
        raise ToolError("code-info response has no data_hash/checksum")
    normalized = raw.lower().removeprefix("0x")
    if SHA256_RE.fullmatch(normalized):
        return normalized
    try:
        decoded = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise ToolError("code-info checksum is neither hex nor base64") from exc
    if len(decoded) != 32:
        raise ToolError("code-info checksum must decode to 32 bytes")
    return decoded.hex()


def contract_info(value: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = value.get("contract_info")
    return nested if isinstance(nested, dict) else value


def preflight(config: Mapping[str, Any], manifest: Mapping[str, Any], manifest_path: Path, cli: str, runner: CommandRunner, gonka_checkout: Path | None = None, network_fingerprint_reader: Any = None) -> dict[str, Any]:
    validate_manifest(manifest)
    verify_manifest_artifacts(manifest, manifest_path)
    validate_config(config, manifest, manifest_path)
    if gonka_checkout is not None:
        actual = git_output(gonka_checkout.resolve(), "rev-parse", "HEAD").strip()
        if actual != config["expected_gonka_source_sha"]:
            raise ToolError(f"Gonka checkout SHA mismatch: expected {config['expected_gonka_source_sha']}, got {actual}")
    actual_chain = node_chain_id(runner, cli, config["rpc_endpoint"])
    if actual_chain != config["chain_id"]:
        raise ToolError(f"chain ID mismatch: expected {config['chain_id']}, got {actual_chain}")
    token = smart_query(runner, cli, config["rpc_endpoint"], config["settlement_cw20"], {"token_info": {}})
    if token.get("decimals") != EXPECTED_CW20_DECIMALS:
        raise ToolError(f"settlement CW20 decimals mismatch: expected 6, got {token.get('decimals')!r}")
    actual_network_fingerprint = None
    runtime_evidence_complete = False
    if config["network_class"] == "production":
        reader = network_fingerprint_reader or node_network_fingerprint
        actual_network_fingerprint = reader(config["rpc_endpoint"])
        runtime_evidence_complete = validate_runtime_evidence(config["runtime_evidence"], config, manifest, manifest_path, actual_network_fingerprint)
    return {
        "chain_id": actual_chain,
        "network_fingerprint_sha256": actual_network_fingerprint,
        "settlement_cw20_decimals": token["decimals"],
        "gonka_checkout_sha": config["expected_gonka_source_sha"] if gonka_checkout else None,
        "runtime_evidence_complete": runtime_evidence_complete,
    }


def contract_artifacts(manifest: Mapping[str, Any], manifest_path: Path) -> dict[str, Path]:
    return {item["name"]: _safe_artifact(manifest_path.parent, item["path"], f"contract {item['name']} path") for item in manifest["contracts"]}


def tx_common(config: Mapping[str, Any]) -> list[str]:
    tx = config["tx"]
    return [
        "--from", tx["from"], "--gas", str(tx["gas"]), "--gas-adjustment", str(tx["gas_adjustment"]),
        "--broadcast-mode", "sync", "--node", config["rpc_endpoint"], "--chain-id", config["chain_id"],
        "--output", "json", "--yes",
    ]


def require_no_admin_instantiate(argv: Sequence[str]) -> None:
    has_admin = any(argument == "--admin" or argument.startswith("--admin=") for argument in argv)
    no_admin_count = argv.count("--no-admin")
    if has_admin:
        raise ToolError("internal invariant violated: Factory instantiate must not set --admin")
    if no_admin_count != 1:
        raise ToolError("internal invariant violated: Factory instantiate must explicitly set --no-admin exactly once")


def deployment_commands(config: Mapping[str, Any], manifest: Mapping[str, Any], manifest_path: Path, cli: str, deal_code_id: str = "<DEAL_CODE_ID>", factory_code_id: str = "<FACTORY_CODE_ID>") -> list[list[str]]:
    artifacts = contract_artifacts(manifest, manifest_path)
    instantiate = {
        "deal_code_id": int(deal_code_id) if deal_code_id.isdigit() else deal_code_id,
        "settlement_cw20": config["settlement_cw20"],
        "fee_recipient": config["fee_recipient"],
        "fee_bps": config["fee_bps"],
    }
    commands = [
        [cli, "tx", "wasm", "store", str(artifacts["marketplace-deal"]), *tx_common(config)],
        [cli, "tx", "wasm", "store", str(artifacts["marketplace-factory"]), *tx_common(config)],
        [cli, "tx", "wasm", "instantiate", factory_code_id, json.dumps(instantiate, separators=(",", ":"), sort_keys=True), "--label", config["factory_label"], "--no-admin", *tx_common(config)],
    ]
    require_no_admin_instantiate(commands[2])
    return commands


def query_confirmed_tx(runner: CommandRunner, cli: str, rpc: str, hash_value: str, timeout_seconds: float = 60, poll_seconds: float = 2) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    argv = [cli, "query", "tx", hash_value, "--node", rpc, "--output", "json"]
    while True:
        try:
            result = runner.run(argv, timeout=min(15, max(1, timeout_seconds)))
        except TimeoutError:
            result = CommandResult(1, "", "query timeout")
        if result.returncode == 0:
            value = parse_json_output(result, "confirmed tx")
            if tx_code(value) != 0:
                tx = unwrap_tx(value)
                raise ToolError(f"confirmed transaction {hash_value} failed with code {tx_code(value)}: {tx.get('raw_log', '')}")
            return value
        if time.monotonic() >= deadline:
            raise ToolError(f"transaction {hash_value} was broadcast but confirmation is ambiguous; do not resend automatically")
        time.sleep(poll_seconds)


def broadcast_and_confirm(runner: CommandRunner, argv: Sequence[str], cli: str, rpc: str, timeout_seconds: float = 60, poll_seconds: float = 2) -> dict[str, Any]:
    try:
        result = runner.run(argv, timeout=timeout_seconds)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ToolError(f"broadcast command failed before confirmation: {detail}")
        response = parse_json_output(result, "broadcast")
    except TimeoutError as exc:
        raw = getattr(exc, "stdout", "")
        try:
            response = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            response = {}
        hash_value = tx_hash(response) if isinstance(response, dict) else None
        if not hash_value:
            raise ToolError("broadcast timed out without a transaction hash; outcome is ambiguous and the command must not be resent automatically") from exc
        return query_confirmed_tx(runner, cli, rpc, hash_value, timeout_seconds, poll_seconds)
    if tx_code(response) != 0:
        tx = unwrap_tx(response)
        raise ToolError(f"CheckTx failed with code {tx_code(response)}: {tx.get('raw_log', '')}")
    hash_value = tx_hash(response)
    if not hash_value:
        raise ToolError("broadcast response has no transaction hash; outcome is ambiguous")
    return query_confirmed_tx(runner, cli, rpc, hash_value, timeout_seconds, poll_seconds)


def new_receipt(config: Mapping[str, Any], config_path: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "status": "in_progress",
        "created_at_utc": utc_now(),
        "chain_id": config["chain_id"],
        "build_manifest_sha256": config["build_manifest_sha256"],
        "deployment_config_sha256": sha256_file(config_path),
        "transactions": [],
        "code_ids": {},
        "contracts": {},
        "verification": None,
    }


def tx_record(stage: str, value: Mapping[str, Any]) -> dict[str, Any]:
    tx = unwrap_tx(value)
    return {"stage": stage, "tx_hash": tx_hash(value), "height": str(tx.get("height", "")), "code": tx_code(value)}


def deploy(config: Mapping[str, Any], config_path: Path, manifest: Mapping[str, Any], manifest_path: Path, receipt_path: Path, cli: str, runner: CommandRunner, gonka_checkout: Path | None = None, timeout_seconds: float = 60, poll_seconds: float = 2) -> dict[str, Any]:
    if receipt_path.exists():
        raise ToolError(f"receipt already exists; refusing to create duplicate deployment: {receipt_path}")
    preflight(config, manifest, manifest_path, cli, runner, gonka_checkout)
    receipt = new_receipt(config, config_path)
    write_json_atomic(receipt_path, receipt)
    try:
        commands = deployment_commands(config, manifest, manifest_path, cli)
        deal_tx = broadcast_and_confirm(runner, commands[0], cli, config["rpc_endpoint"], timeout_seconds, poll_seconds)
        deal_code_id = one_event_value(deal_tx, ("code_id",), "Deal store")
        if not deal_code_id.isdigit():
            raise ToolError("Deal code_id is not numeric")
        receipt["transactions"].append(tx_record("store_deal", deal_tx))
        receipt["code_ids"]["deal"] = int(deal_code_id)
        write_json_atomic(receipt_path, receipt)

        factory_tx = broadcast_and_confirm(runner, commands[1], cli, config["rpc_endpoint"], timeout_seconds, poll_seconds)
        factory_code_id = one_event_value(factory_tx, ("code_id",), "Factory store")
        if not factory_code_id.isdigit():
            raise ToolError("Factory code_id is not numeric")
        receipt["transactions"].append(tx_record("store_factory", factory_tx))
        receipt["code_ids"]["factory"] = int(factory_code_id)
        write_json_atomic(receipt_path, receipt)

        instantiate = deployment_commands(config, manifest, manifest_path, cli, deal_code_id, factory_code_id)[2]
        require_no_admin_instantiate(instantiate)
        factory_tx = broadcast_and_confirm(runner, instantiate, cli, config["rpc_endpoint"], timeout_seconds, poll_seconds)
        address = one_event_value(factory_tx, ("_contract_address", "contract_address"), "Factory instantiate")
        receipt["transactions"].append(tx_record("instantiate_factory", factory_tx))
        receipt["contracts"]["factory"] = address
        write_json_atomic(receipt_path, receipt)

        verification = verify_deployment(config, config_path, manifest, manifest_path, receipt, cli, runner)
        receipt["verification"] = verification
        receipt["status"] = "complete"
        write_json_atomic(receipt_path, receipt)
        return receipt
    except ToolError as exc:
        receipt["status"] = "ambiguous" if "ambiguous" in str(exc).lower() else "failed"
        receipt["error"] = str(exc)
        write_json_atomic(receipt_path, receipt)
        raise


def expected_contract_hashes(manifest: Mapping[str, Any]) -> dict[str, str]:
    return {item["name"]: item["sha256"] for item in manifest["contracts"]}


def _require_equal(actual: Any, expected: Any, context: str) -> None:
    if actual != expected:
        raise ToolError(f"{context} mismatch: expected {expected!r}, got {actual!r}")


def verify_code(runner: CommandRunner, cli: str, rpc: str, code_id: int, expected_hash: str, context: str) -> None:
    info = query_wasm(runner, cli, rpc, "code-info", str(code_id))
    _require_equal(code_checksum(info), expected_hash, f"{context} on-chain checksum")


def verify_contract_instance(runner: CommandRunner, cli: str, rpc: str, address: str, code_id: int, context: str) -> None:
    info = contract_info(query_wasm(runner, cli, rpc, "contract", address))
    try:
        actual_code_id = int(info.get("code_id"))
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{context} contract-info has invalid code_id") from exc
    _require_equal(actual_code_id, code_id, f"{context} code_id")
    if info.get("admin") not in (None, ""):
        raise ToolError(f"{context} must have no admin, got {info.get('admin')!r}")


def verify_deployment(config: Mapping[str, Any], config_path: Path, manifest: Mapping[str, Any], manifest_path: Path, receipt: Mapping[str, Any], cli: str, runner: CommandRunner) -> dict[str, Any]:
    preflight_result = preflight(config, manifest, manifest_path, cli, runner)
    validate_receipt(receipt)
    _require_equal(receipt["chain_id"], config["chain_id"], "receipt chain_id")
    _require_equal(receipt["build_manifest_sha256"], config["build_manifest_sha256"], "receipt manifest hash")
    _require_equal(receipt["deployment_config_sha256"], sha256_file(config_path), "receipt config hash")
    actual_chain = preflight_result["chain_id"]
    _require_equal(actual_chain, config["chain_id"], "node chain_id")
    require_fields(receipt["code_ids"], ("deal", "factory"), "receipt.code_ids")
    require_fields(receipt["contracts"], ("factory",), "receipt.contracts")
    hashes = expected_contract_hashes(manifest)
    verify_code(runner, cli, config["rpc_endpoint"], int(receipt["code_ids"]["deal"]), hashes["marketplace-deal"], "Deal")
    verify_code(runner, cli, config["rpc_endpoint"], int(receipt["code_ids"]["factory"]), hashes["marketplace-factory"], "Factory")
    factory_address = receipt["contracts"]["factory"]
    verify_contract_instance(runner, cli, config["rpc_endpoint"], factory_address, int(receipt["code_ids"]["factory"]), "Factory")
    factory = smart_query(runner, cli, config["rpc_endpoint"], factory_address, {"config": {}})
    expected_factory = {
        "deal_code_id": int(receipt["code_ids"]["deal"]),
        "settlement_cw20": config["settlement_cw20"],
        "fee_recipient": config["fee_recipient"],
        "fee_bps": EXPECTED_FEE_BPS,
    }
    for key, expected in expected_factory.items():
        _require_equal(factory.get(key), expected, f"Factory config {key}")
    token = smart_query(runner, cli, config["rpc_endpoint"], config["settlement_cw20"], {"token_info": {}})
    _require_equal(token.get("decimals"), EXPECTED_CW20_DECIMALS, "settlement CW20 decimals")
    return {
        "verified_at_utc": utc_now(),
        "chain_id": actual_chain,
        "network_fingerprint_sha256": preflight_result["network_fingerprint_sha256"],
        "deal_code_checksum": hashes["marketplace-deal"],
        "factory_code_checksum": hashes["marketplace-factory"],
        "factory_address": factory_address,
        "factory_admin": None,
        "factory_config": expected_factory,
        "settlement_cw20_decimals": EXPECTED_CW20_DECIMALS,
        "runtime_evidence": config["runtime_evidence"],
    }


def _verify_deal_instance(request: Mapping[str, Any], config: Mapping[str, Any], manifest: Mapping[str, Any], receipt: Mapping[str, Any], cli: str, runner: CommandRunner) -> dict[str, Any]:
    require_fields(request, ("deal_address", "host", "target_epoch", "price_micro_usdt_per_gnk", "buyer_budget_micro_usdt"), "deal verification request")
    address = require_nonempty(request["deal_address"], "deal_address")
    deal_code_id = int(receipt["code_ids"]["deal"])
    verify_code(runner, cli, config["rpc_endpoint"], deal_code_id, expected_contract_hashes(manifest)["marketplace-deal"], "Deal")
    verify_contract_instance(runner, cli, config["rpc_endpoint"], address, deal_code_id, "Deal")
    factory_address = receipt["contracts"]["factory"]
    deal = smart_query(runner, cli, config["rpc_endpoint"], address, {"config": {}})
    expected = {
        "factory": factory_address,
        "host": request["host"],
        "deal_address": address,
        "target_epoch": request["target_epoch"],
        "price_micro_usdt_per_gnk": str(request["price_micro_usdt_per_gnk"]),
        "buyer_budget_micro_usdt": str(request["buyer_budget_micro_usdt"]),
        "settlement_cw20": config["settlement_cw20"],
        "fee_recipient": config["fee_recipient"],
        "fee_bps": EXPECTED_FEE_BPS,
        "pinned_gonka_sha": PINNED_GONKA_SHA,
    }
    for key, expected_value in expected.items():
        _require_equal(deal.get(key), expected_value, f"Deal immutable config {key}")
    indexed = smart_query(runner, cli, config["rpc_endpoint"], factory_address, {"deal_by_host_epoch": {"host": request["host"], "epoch": request["target_epoch"]}})
    _require_equal(indexed.get("address"), address, "Factory Deal index")
    return {"verified_at_utc": utc_now(), "deal_address": address, "deal_code_id": deal_code_id, "admin": None, "immutable_config": expected, "factory_indexed": True}


def verify_deal(request: Mapping[str, Any], config: Mapping[str, Any], config_path: Path, manifest: Mapping[str, Any], manifest_path: Path, receipt: Mapping[str, Any], cli: str, runner: CommandRunner) -> dict[str, Any]:
    deployment = verify_deployment(config, config_path, manifest, manifest_path, receipt, cli, runner)
    deal = _verify_deal_instance(request, config, manifest, receipt, cli, runner)
    return {**deal, "deployment": deployment}


def load_inputs(config_path: Path, manifest_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    return load_json(config_path), load_json(manifest_path)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="run two pinned optimizer builds and write a manifest")
    build.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--commit", default="HEAD")
    validate = commands.add_parser("verify-artifacts", help="validate manifest and all artifact hashes")
    validate.add_argument("--manifest", type=Path, required=True)
    for name in ("check-environment", "prepare-deployment", "deploy", "verify-deployment"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--cli", required=True, help="path to the built Gonka inferenced binary")
        if name in ("check-environment", "prepare-deployment", "deploy"):
            command.add_argument("--gonka-checkout", type=Path)
        if name in ("deploy", "verify-deployment"):
            command.add_argument("--receipt", type=Path, required=True)
        if name == "deploy":
            command.add_argument("--approve-broadcast", action="store_true", help="required acknowledgement that transactions will be sent")
    deal = commands.add_parser("verify-deal")
    deal.add_argument("--config", type=Path, required=True)
    deal.add_argument("--manifest", type=Path, required=True)
    deal.add_argument("--receipt", type=Path, required=True)
    deal.add_argument("--request", type=Path, required=True)
    deal.add_argument("--cli", required=True)
    return root


def main(argv: Sequence[str] | None = None, runner: CommandRunner | None = None) -> int:
    args = parser().parse_args(argv)
    runner = runner or CommandRunner()
    try:
        if args.command == "build":
            path = build_release(args.repo, args.output, args.commit, runner)
            print(path)
        elif args.command == "verify-artifacts":
            manifest = load_json(args.manifest)
            verify_manifest_artifacts(manifest, args.manifest)
            print("build manifest and artifact hashes verified")
        elif args.command in ("check-environment", "prepare-deployment"):
            config, manifest = load_inputs(args.config, args.manifest)
            evidence = preflight(config, manifest, args.manifest, args.cli, runner, args.gonka_checkout)
            print(json.dumps(evidence, indent=2, sort_keys=True))
            if args.command == "prepare-deployment":
                for command in deployment_commands(config, manifest, args.manifest, args.cli):
                    print(display_command(command))
                print("read-only preparation complete; no transaction was sent")
        elif args.command == "deploy":
            if not args.approve_broadcast:
                raise ToolError("deploy requires --approve-broadcast; use prepare-deployment for a read-only dry run")
            config, manifest = load_inputs(args.config, args.manifest)
            receipt = deploy(config, args.config, manifest, args.manifest, args.receipt, args.cli, runner, args.gonka_checkout)
            print(json.dumps(receipt, indent=2, sort_keys=True))
        elif args.command == "verify-deployment":
            config, manifest = load_inputs(args.config, args.manifest)
            receipt = load_json(args.receipt)
            result = verify_deployment(config, args.config, manifest, args.manifest, receipt, args.cli, runner)
            print(json.dumps(result, indent=2, sort_keys=True))
        elif args.command == "verify-deal":
            config, manifest = load_inputs(args.config, args.manifest)
            receipt = load_json(args.receipt)
            request = load_json(args.request)
            result = verify_deal(request, config, args.config, manifest, args.manifest, receipt, args.cli, runner)
            print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ToolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
