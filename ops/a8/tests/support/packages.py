"""Current baseline package builders and modifier helpers.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from ops.a8.catalog import resolve_e2e_selection
from ops.a8.e2e.context import E2ERunContext
from ops.a8.e2e.deployment import A9_MANIFEST_ROLE, A9_RELEASE_ROLE_PREFIX
from ops.a8.e2e.file_safety import sha256_file
from ops.a8.e2e.planner import _limits_section, _selection_section, _source_policy_section
from ops.a8.e2e.runlock import (
    BUILD_MANIFEST_FILENAME,
    DELIVERY_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    RUN_LOCK_FILENAME,
    RUN_LOCK_SCHEMA,
    BUILD_MANIFEST_SCHEMA,
    EXECUTION_MANIFEST_SCHEMA,
    BuildManifest,
    DeliveryAttempt,
    DeliveryManifest,
    DeliveryStatus,
    ExecutionManifest,
    ManifestStatus,
    RunLock,
    content_sha256,
    write_delivery_manifest,
    write_run_lock,
)
from ops.a8.models import EvidenceStatus, ExecutionStatus
from ops.a8.tests.real_fixtures import GONKA_PREPARED_SHA
from ops.a8.tests.support.fakes import (
    A9_MANIFEST_SHA256,
    CALLER_SHA256,
    CONTRACTS_SHA,
    CW20_SHA256,
    DEAL_SHA256,
    FACTORY_SHA256,
    GONKA_SHA,
    GONKA_TREE_SHA,
    MARKETPLACE_TREE_SHA,
    RUNNER_IMAGE_ID,
    snapshot_fingerprint,
    write_suite_output,
)

DEAL_ROLE = A9_RELEASE_ROLE_PREFIX + "marketplace_deal.wasm"
FACTORY_ROLE = A9_RELEASE_ROLE_PREFIX + "marketplace_factory.wasm"
CALLER_ROLE = "test-wasm-target/wasm32-unknown-unknown/release/a8_caller.wasm"
CW20_ROLE = "test-wasm-target/wasm32-unknown-unknown/release/a8_cw20.wasm"


def default_manifest_source_immutability(
    *,
    gonka_sha: str = GONKA_SHA,
    contracts_sha: str = CONTRACTS_SHA,
    gonka_tree_sha: str = GONKA_TREE_SHA,
    contracts_tree_sha: str = MARKETPLACE_TREE_SHA,
) -> dict:
    """Default build-manifest source_immutability record for a clean immutable run."""
    gonka_fp = snapshot_fingerprint(gonka_sha, gonka_tree_sha)
    contracts_fp = snapshot_fingerprint(contracts_sha, contracts_tree_sha)
    return {
        "model": "immutable-source",
        "pristine_before_build": True,
        "after_build_verdict": "UNCHANGED",
        "after_execution_verdict": "UNCHANGED",
        "verdict": "UNCHANGED",
        "roles": {
            "gonka": {
                "expected_sha": gonka_sha,
                "expected_tree": gonka_tree_sha,
                "before_build": dict(gonka_fp),
                "after_build": dict(gonka_fp),
                "after_execution": dict(gonka_fp),
                "verdict": "UNCHANGED",
            },
            "contracts": {
                "expected_sha": contracts_sha,
                "expected_tree": contracts_tree_sha,
                "before_build": dict(contracts_fp),
                "after_build": dict(contracts_fp),
                "after_execution": dict(contracts_fp),
                "verdict": "UNCHANGED",
            },
        },
    }


def make_baseline_lock(
    scenarios: Optional[Sequence[str]] = None,
    *,
    selection: Optional[dict] = None,
    plan_id: str = "plan-baseline",
    gonka_sha: str = GONKA_SHA,
    contracts_sha: str = CONTRACTS_SHA,
    gonka_tree_sha: str = GONKA_TREE_SHA,
    contracts_tree_sha: str = MARKETPLACE_TREE_SHA,
    gonka_bundle_sha256: str = "1" * 64,
    contracts_bundle_sha256: str = "2" * 64,
    gonka_bundle_ref: str = "refs/e2e/source/gonka",
    contracts_bundle_ref: str = "refs/e2e/source/contracts",
    runner_image_id: str = RUNNER_IMAGE_ID,
    harness_hash: str = "f" * 64,
    verifier_hash: str = "0" * 64,
    catalog_hash: str = "1" * 64,
    adapter_content_hashes: Tuple[str, str] = ("2" * 64, "3" * 64),
    limits: Optional[dict] = None,
    created_at_utc: str = "2026-01-01T00:00:00+00:00",
) -> RunLock:
    """Create a fully-specified current RunLock."""
    if selection is not None:
        sel = dict(selection)
        lim = dict(limits) if limits is not None else {}
    else:
        actual_scenarios = list(scenarios) if scenarios is not None else ["lock-exact-e"]
        tasks, profile, normalised = resolve_e2e_selection(scenarios=actual_scenarios)
        sel = _selection_section(profile, normalised, tasks)
        lim = dict(limits) if limits is not None else _limits_section(tasks)

    return RunLock.from_dict(
        {
            "schema_version": RUN_LOCK_SCHEMA,
            "plan_id": plan_id,
            "created_at_utc": created_at_utc,
            "gonka": {
                "role": "gonka",
                "source_kind": "remote",
                "repo_url": "https://github.com/example/gonka",
                "local_path_hint": None,
                "resolved_origin_url": "https://github.com/example/gonka",
                "commit_sha": gonka_sha,
                "tree_sha": gonka_tree_sha,
                "commit_url": f"https://github.com/example/gonka/commit/{gonka_sha}",
                "bundle_relpath": "bundles/gonka.bundle",
                "bundle_sha256": gonka_bundle_sha256,
                "bundle_ref": gonka_bundle_ref,
                "fetch_strategy": "object",
                "submodules": [],
            },
            "contracts": {
                "role": "contracts",
                "source_kind": "remote",
                "repo_url": "https://github.com/example/contracts",
                "local_path_hint": None,
                "resolved_origin_url": "https://github.com/example/contracts",
                "commit_sha": contracts_sha,
                "tree_sha": contracts_tree_sha,
                "commit_url": None,
                "bundle_relpath": "bundles/contracts.bundle",
                "bundle_sha256": contracts_bundle_sha256,
                "bundle_ref": contracts_bundle_ref,
                "fetch_strategy": "object",
                "submodules": [],
            },
            "runner": {
                "locator": "a8-runner:local",
                "image_id": runner_image_id,
                "repo_digest": None,
                "runner_version": "a8-runner/2.0.0",
                "harness_hash": harness_hash,
                "verifier_hash": verifier_hash,
                "catalog_hash": catalog_hash,
            },
            "platform": {"docker_platform": "linux/amd64"},
            "compatibility": {
                "gonka": {
                    "adapter_id": "gonka-test-adapter",
                    "adapter_content_hash": adapter_content_hashes[0],
                },
                "contracts": {
                    "adapter_id": "contracts-test-adapter",
                    "adapter_content_hash": adapter_content_hashes[1],
                },
            },
            "selection": sel,
            "limits": lim,
            "network": {},
            "build": {},
            "source_policy": _source_policy_section(),
            "external_tests": {"trees": {}, "tests_hash": "5" * 64},
            "semantic_inputs": {"semantic_environment": {}},
            "source_package": {"relpath": "."},
            "notes": [],
        }
    )


make_test_lock = make_baseline_lock


def make_build_manifest(
    *,
    lock_sha256: str,
    plan_id: str = "plan-baseline",
    run_id: str = "e2e-baseline-run",
    status: str = ManifestStatus.COMPLETE,
    source_immutability: Optional[dict] = None,
) -> BuildManifest:
    return BuildManifest(
        schema_version=BUILD_MANIFEST_SCHEMA,
        lock_sha256=lock_sha256,
        plan_id=plan_id,
        run_id=run_id,
        started_at_utc="2026-01-01T00:01:00+00:00",
        status=status,
        source_immutability=(
            dict(source_immutability)
            if source_immutability is not None
            else default_manifest_source_immutability()
        ),
    )


def make_a9_build_manifest(
    *,
    lock: Optional[RunLock] = None,
    lock_sha256: str = "a" * 64,
    plan_id: str = "plan-r10",
    run_id: str = "run-r10",
    deal_sha: str = DEAL_SHA256,
    factory_sha: str = FACTORY_SHA256,
    caller_sha: str = CALLER_SHA256,
    cw20_sha: Optional[str] = CW20_SHA256,
    manifest_sha: str = A9_MANIFEST_SHA256,
    build_out: str = "/build",
    production_wasm: bool = True,
) -> BuildManifest:
    """A build manifest populated with the A9 release binary and wasm artifacts."""
    if lock is not None:
        manifest = BuildManifest.start(lock=lock, run_id=run_id)
        manifest.source_immutability = default_manifest_source_immutability(
            gonka_sha=lock.gonka.get("commit_sha", GONKA_SHA),
            contracts_sha=lock.contracts.get("commit_sha", CONTRACTS_SHA),
            gonka_tree_sha=lock.gonka.get("tree_sha", GONKA_TREE_SHA),
            contracts_tree_sha=lock.contracts.get("tree_sha", MARKETPLACE_TREE_SHA),
        )
    else:
        manifest = BuildManifest(
            schema_version=BUILD_MANIFEST_SCHEMA,
            lock_sha256=lock_sha256,
            plan_id=plan_id,
            run_id=run_id,
            started_at_utc="2026-01-01T00:00:00+00:00",
        )
        manifest.source_immutability = default_manifest_source_immutability()
    manifest.binaries = [
        {
            "role": A9_MANIFEST_ROLE,
            "step_id": "a9-release",
            "path": f"{build_out}/{A9_MANIFEST_ROLE}",
            "sha256": manifest_sha,
        }
    ]
    wasm_entries = []
    if production_wasm:
        wasm_entries.extend(
            [
                {
                    "role": DEAL_ROLE,
                    "step_id": "a9-release",
                    "path": f"{build_out}/{DEAL_ROLE}",
                    "sha256": deal_sha,
                },
                {
                    "role": FACTORY_ROLE,
                    "step_id": "a9-release",
                    "path": f"{build_out}/{FACTORY_ROLE}",
                    "sha256": factory_sha,
                },
            ]
        )
    wasm_entries.append(
        {
            "role": CALLER_ROLE,
            "step_id": "test-contracts",
            "path": f"{build_out}/{CALLER_ROLE}",
            "sha256": caller_sha,
        }
    )
    if cw20_sha is not None:
        wasm_entries.append(
            {
                "role": CW20_ROLE,
                "step_id": "test-contracts",
                "path": f"{build_out}/{CW20_ROLE}",
                "sha256": cw20_sha,
            }
        )
    manifest.wasm = wasm_entries
    return manifest


def setup_baseline_package(
    pkg_dir: Path,
    *,
    scenarios: List[str],
    run_id: str = "e2e-baseline-run",
    suite_relpath: str = "suite",
    execution_status: ExecutionStatus = ExecutionStatus.PASSED,
    evidence_status: EvidenceStatus = EvidenceStatus.COMPLETE,
    delivery_status: str = DeliveryStatus.COMPLETED,
    export_error: Optional[str] = None,
    delivery_manifest_relpath: Optional[str] = DELIVERY_MANIFEST_FILENAME,
    include_delivery_file: bool = True,
    make_bundles: bool = True,
    ownership_image: str = RUNNER_IMAGE_ID,
    e2e_context: Optional[E2ERunContext] = None,
) -> Tuple[Path, RunLock, BuildManifest, ExecutionManifest]:
    """Create a complete, synthetically valid current run package in pkg_dir."""
    pkg_dir.mkdir(parents=True, exist_ok=True)
    gonka_bytes = b"SYNTHETIC-GONKA-BUNDLE-PAYLOAD-BASELINE"
    contracts_bytes = b"SYNTHETIC-CONTRACTS-BUNDLE-PAYLOAD-BASELINE"
    g_hash = hashlib.sha256(gonka_bytes).hexdigest()
    c_hash = hashlib.sha256(contracts_bytes).hexdigest()

    lock = make_baseline_lock(
        scenarios,
        gonka_bundle_sha256=g_hash,
        contracts_bundle_sha256=c_hash,
    )
    write_run_lock(lock, pkg_dir / RUN_LOCK_FILENAME)
    lock_hash = content_sha256(lock.to_dict())

    if make_bundles:
        bundles_dir = pkg_dir / "bundles"
        bundles_dir.mkdir(parents=True, exist_ok=True)
        (bundles_dir / "gonka.bundle").write_bytes(gonka_bytes)
        (bundles_dir / "contracts.bundle").write_bytes(contracts_bytes)

    build_m = BuildManifest(
        schema_version=BUILD_MANIFEST_SCHEMA,
        lock_sha256=lock_hash,
        plan_id=lock.plan_id,
        run_id=run_id,
        started_at_utc="2026-01-01T00:01:00+00:00",
        status=ManifestStatus.COMPLETE,
        source_immutability=default_manifest_source_immutability(
            gonka_sha=lock.gonka["commit_sha"],
            contracts_sha=lock.contracts["commit_sha"],
            gonka_tree_sha=lock.gonka["tree_sha"],
            contracts_tree_sha=lock.contracts["tree_sha"],
        ),
    )
    build_m.images.append({
        "role": "gonka/inference-chain:e2e", "reference": "gonka/inference-chain:e2e",
        "image_id": RUNNER_IMAGE_ID, "repo_digests": [],
        "created_epoch": 1.0, "step_id": "chain-images",
    })
    b_file = pkg_dir / BUILD_MANIFEST_FILENAME
    b_file.write_text(json.dumps(build_m.to_dict(), indent=2) + "\n", encoding="utf-8")
    b_hash = content_sha256(build_m.to_dict())

    exec_m = ExecutionManifest(
        schema_version=EXECUTION_MANIFEST_SCHEMA,
        lock_sha256=lock_hash,
        plan_id=lock.plan_id,
        run_id=run_id,
        suite_id=run_id,
        created_at_utc="2026-01-01T00:02:00+00:00",
        command="run",
        build_manifest_sha256=b_hash,
        suite_export_relpath=f"{suite_relpath}/{run_id}",
        runner_image_id=RUNNER_IMAGE_ID,
        export_status=None,
        export_error=None,
        delivery_manifest_relpath=delivery_manifest_relpath,
        source_immutability={
            "verdict": "UNCHANGED",
            "after_execution": {
                role: dict(build_m.source_immutability["roles"][role]["after_execution"])
                for role in ("gonka", "contracts")
            },
        },
    )
    e_file = pkg_dir / EXECUTION_MANIFEST_FILENAME
    e_file.write_text(json.dumps(exec_m.to_dict(), indent=2) + "\n", encoding="utf-8")

    if include_delivery_file:
        attempts = [
            DeliveryAttempt(
                attempt_number=1,
                command="run",
                started_at_utc="2026-01-01T00:02:05+00:00",
                completed_at_utc=(
                    "2026-01-01T00:02:10+00:00"
                    if delivery_status in (DeliveryStatus.COMPLETED, DeliveryStatus.FAILED)
                    else None
                ),
                destination=str(pkg_dir),
                status=delivery_status,
                error=export_error,
            )
        ]
        delivery = DeliveryManifest(
            run_id=run_id,
            status=delivery_status,
            attempts=attempts,
        )
        write_delivery_manifest(delivery, pkg_dir / DELIVERY_MANIFEST_FILENAME)

    suite_dest = pkg_dir / suite_relpath
    suite_dest.mkdir(parents=True, exist_ok=True)

    if e2e_context is None:
        e2e_context = E2ERunContext(
            gonka_requested_sha=GONKA_SHA,
            contracts_requested_sha=CONTRACTS_SHA,
            expected_gonka_sha=GONKA_SHA,
            expected_marketplace_sha=CONTRACTS_SHA,
            runner_image_id=RUNNER_IMAGE_ID,
        )

    write_suite_output(
        output_dir=suite_dest,
        suite_id=run_id,
        profile=None,
        scenarios=scenarios,
        e2e_context=e2e_context,
        execution_status=execution_status,
        evidence_status=evidence_status,
        ownership_image=ownership_image,
    )

    return pkg_dir, lock, build_m, exec_m


def resync_artifact_index(suite_dir: Path) -> None:
    """Update artifact-index.json checksums to reflect modifications made to task files."""
    index_file = suite_dir / "artifact-index.json"
    if not index_file.is_file():
        return
    data = json.loads(index_file.read_text(encoding="utf-8"))
    for item in data.get("artifacts", []):
        fpath = suite_dir / item["relative_path"]
        if fpath.is_file():
            item["sha256"] = sha256_file(fpath)
            item["size_bytes"] = fpath.stat().st_size
    index_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
