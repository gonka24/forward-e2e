"""Synthetic test boundaries, runner stubs, and process fakes.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, List, Optional, Sequence, Union

from tests.unit.runner.support.fixture_artifacts import task_artifacts
from forward_e2e.suite.catalog import ProofLevel, resolve_e2e_selection
from forward_e2e.suite.collector import classify_artifact_kind, update_artifact_index
from forward_e2e.suite import source_snapshot
from forward_e2e.suite.evidence_model import (
    EVIDENCE_MODEL_E2E,
    EVIDENCE_MODEL_IMMUTABLE,
    HISTORICAL_EVIDENCE_MODELS,
)
from forward_e2e.suite.models import (
    AcceptanceStatus,
    ArtifactEntry,
    CleanupStatus,
    EvidenceStatus,
    ExecutionStatus,
    SourceIdentity,
    SuitePlan,
    SuiteResult,
    TaskResult,
    calculate_suite_outcome,
    make_task_run_id,
)
from forward_e2e.execution.compat import (
    WASM_ALLOWLIST_FILE,
    CompatibilityAdapter,
    BuildRecipe,
    BuildStep,
    ExpectationKind,
    RuntimeExpectation,
)
from forward_e2e.execution.runlock import (
    BUILD_MANIFEST_SCHEMA,
    RUN_LOCK_SCHEMA,
    BuildManifest,
    RunLock,
    utc_now_iso,
)
from forward_e2e.execution.sources import (
    AcquiredSource,
    SourceKind,
    SourceSpec,
    bundle_ref,
    github_commit_url,
)
from tests.unit.runner.real_fixtures import (
    GONKA_SOURCE_SHA,
    MARKETPLACE_SHA,
    real_lock_exact_e_context_for_run,
)

POLICY_LIVE_CONTEXT_RELPATH = "live-context.json"
NATIVE_TASK_ID = "lock-exact-e"

BUILD_STARTED_EPOCH = 1_700_000_000.0
#: A commit that is *not* the selected one. Used by negative fixtures that need
#: "some other SHA" (a runtime reporting the wrong commit, a stale historical
#: prepared commit). The immutable-source model has no prepared commit.
PREPARED_SHA = "b" * 40
REQUESTED_SHA = "a" * 40
GONKA_VERSION = "v0.2.9-12-ge86e489"
GONKA_SHA = GONKA_SOURCE_SHA
CONTRACTS_SHA = MARKETPLACE_SHA
# These are the exact trees for GONKA_SHA and CONTRACTS_SHA. Keep shared
# package/live-context fakes internally valid; other synthetic commit fixtures
# may still use arbitrary, explicitly synthetic SHAs.
GONKA_TREE_SHA = "1fde98b66dfcc891ee6d5c75f5263d60428cbd81"
MARKETPLACE_TREE_SHA = "7fc429b5c0db41373fedb4eb5a25d69ebad5f910"
RUNNER_IMAGE_ID = "sha256:" + "a" * 64

A9_MANIFEST_SHA256 = "6" * 64
DEAL_SHA256 = "2" * 64
FACTORY_SHA256 = "3" * 64
CALLER_SHA256 = "4" * 64
CW20_SHA256 = "5" * 64

GO_MOD = """module github.com/product-science/inferenced

go 1.23.6

require (
\tgithub.com/CosmWasm/wasmd v0.53.0
\tgithub.com/CosmWasm/wasmvm/v2 v2.1.2
)
"""

UPSTREAM_LEGACY_GO = """package app

var AcceptedQueries = []string{
\t"/inference.inference.Query/GetCurrentEpoch",
\t"/inference.inference.Query/ListClaimRecipients",
\t"/inference.inference.Query/EpochPerformanceSummaryByParticipant",
\t"/inference.streamvesting.Query/TotalVestingAmount",
}
"""

def write_text_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Snapshot digests and build identities hash exact bytes.  Test fixtures
    # must keep LF bytes on Windows too, otherwise their hashes diverge from
    # the values derived from the supplied text.
    path.write_bytes(content.encode("utf-8"))
    return path


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write_gonka_tree(
    root: Path, *, legacy_go: str = UPSTREAM_LEGACY_GO, go_mod: str = GO_MOD
) -> Path:
    write_text_file(root / WASM_ALLOWLIST_FILE, legacy_go)
    write_text_file(root / "inference-chain/Dockerfile", "FROM golang:1.23\n")
    write_text_file(root / "inference-chain/go.mod", go_mod)
    write_text_file(
        root / "inference-chain/scripts/init-docker-genesis.sh", "#!/bin/sh\n# upstream\n"
    )
    write_text_file(root / "decentralized-api/main.go", "package main\n")
    write_text_file(root / "testermint/gradlew", "#!/bin/sh\nexit 0\n")
    write_text_file(root / "local-test-net/docker-compose.yml", "services: {}\n")
    write_text_file(root / "Makefile", "build-docker:\n\t@true\n")
    # Files the immutable-source recipe reads in place (never writes): the
    # Dockerfiles it builds with, cosmovisor (staged into the inferenced
    # context) and the upstream Gradle wrapper jar it invokes directly.
    write_text_file(root / "cosmovisor/README.md", "pinned cosmovisor\n")
    write_text_file(root / "decentralized-api/Dockerfile", "FROM golang:1.23\n")
    write_text_file(root / "edge-api/Dockerfile", "FROM golang:1.23\n")
    write_text_file(root / "proxy/Dockerfile", "FROM nginx\n")
    write_text_file(root / "testermint/Dockerfile", "FROM eclipse-temurin:21\n")
    write_text_file(
        root / "testermint/mock_server/gradle/wrapper/gradle-wrapper.jar", "PK synthetic jar\n"
    )
    return root


def write_contracts_tree(root: Path, *, with_release_script: bool = True) -> Path:
    write_text_file(root / "Cargo.toml", "[workspace]\nmembers = []\n")
    write_text_file(root / "Cargo.lock", "version = 4\n")
    write_text_file(root / "rust-toolchain.toml", '[toolchain]\nchannel = "1.81.0"\n')
    write_text_file(root / "contracts/marketplace/Cargo.toml", "[package]\n")
    write_text_file(root / "packages/common/Cargo.toml", "[package]\n")
    if with_release_script:
        write_text_file(root / "scripts/a9_release.py", "# reproducible release build\n")
    return root


def docker_timestamp(epoch: float) -> str:
    moment = dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


def completed(argv, *, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


def new_manifest() -> BuildManifest:
    return BuildManifest(
        schema_version=BUILD_MANIFEST_SCHEMA,
        lock_sha256="0" * 64,
        plan_id="plan-offline-test",
        run_id="run-offline-test",
        started_at_utc=utc_now_iso(),
    )


def forbidden_runner(*args, **kwargs):
    """A runner that must never be reached by a pure-computation test."""
    raise AssertionError(
        "This test computes an identity; it must not start a process. "
        f"argv={args!r} kwargs={kwargs!r}"
    )


class FakeGitRunner:
    """Stands in for ``subprocess.run`` inside GitClient."""

    def __init__(self, *, head_sha, prepared_sha, changed_paths, ancestry_ok=True):
        self.head_sha = head_sha
        self.prepared_sha = prepared_sha
        self.changed_paths = list(changed_paths)
        self.ancestry_ok = ancestry_ok
        self.calls = []
        self.invocations = []
        self._rev_parse_calls = 0

    def __call__(self, argv, *, cwd=None, env=None, capture_output=False, text=False, timeout=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.invocations.append(
            {"argv": argv, "cwd": cwd, "env": dict(env or {}), "timeout": timeout}
        )
        if "rev-parse" in argv:
            self._rev_parse_calls += 1
            value = self.head_sha if self._rev_parse_calls == 1 else self.prepared_sha
            return completed(argv, stdout=value + "\n")
        if "merge-base" in argv:
            return completed(argv, returncode=0 if self.ancestry_ok else 1)
        if "diff" in argv:
            return completed(argv, stdout="\n".join(self.changed_paths) + "\n")
        return completed(argv)

    def invocations_with(self, token: str) -> list:
        return [call for call in self.invocations if token in call["argv"]]

    def only_invocation_with(self, token: str) -> dict:
        matches = self.invocations_with(token)
        if len(matches) != 1:
            raise AssertionError(
                f"expected exactly one git {token!r} invocation, got {len(matches)}: "
                f"{[call['argv'] for call in matches]}"
            )
        return matches[0]


class FakeBuildRunner:
    """Stands in for the injected build command runner."""

    def __init__(self, *, image_created_epoch=None, missing_images=(), failing_steps=(),
                 on_command=None):
        self.image_created_epoch = (
            image_created_epoch if image_created_epoch is not None
            else BUILD_STARTED_EPOCH + 60.0
        )
        self.missing_images = set(missing_images)
        self.failing_steps = tuple(failing_steps)
        self.on_command = on_command
        self.calls = []

    def __call__(self, argv, *, cwd=None, env=None, timeout=None):
        argv = [str(a) for a in argv]
        if argv[:3] == ["docker", "image", "inspect"]:
            reference = argv[3]
            if reference in self.missing_images:
                return completed(argv, returncode=1, stderr="No such image")
            payload = [
                {
                    "Id": "sha256:" + hashlib.sha256(reference.encode()).hexdigest(),
                    "RepoDigests": [],
                    "Created": docker_timestamp(self.image_created_epoch),
                }
            ]
            return completed(argv, stdout=json.dumps(payload))
        self.calls.append({"argv": argv, "cwd": cwd, "env": dict(env or {}), "timeout": timeout})
        joined = " ".join(argv)
        if any(marker in joined for marker in self.failing_steps):
            return completed(argv, returncode=2, stderr="compilation failed")
        if self.on_command is not None:
            self.on_command(argv)
        return completed(argv, stdout="ok")

    def image_id_for(self, reference: str) -> str:
        return "sha256:" + hashlib.sha256(reference.encode()).hexdigest()


def make_test_adapter(
    role: str,
    *,
    with_runtime: bool = False,
    with_build: bool = False,
    runtime: Optional[tuple] = None,
) -> CompatibilityAdapter:
    """A real adapter, optionally building an image through the fake process boundary."""
    if runtime is not None:
        rt = runtime
    elif with_runtime:
        rt = (
            RuntimeExpectation(
                component="inferenced",
                field_name="gonka_source_sha",
                kind=ExpectationKind.SELECTED_COMMIT,
                why="the running binary must report the selected commit",
            ),
            RuntimeExpectation(
                component="wasmd",
                field_name="wasmd",
                kind=ExpectationKind.LITERAL_PATTERN,
                pattern=r"^v0\.54\.",
                why="pinned wasmd line",
            ),
        )
    else:
        rt = ()
    return CompatibilityAdapter(
        adapter_id=f"{role}-test-adapter",
        role=role,
        description="synthetic adapter for offline tests",
        verified_commits=frozenset(),
        markers=(),
        build=BuildRecipe(
            recipe_id="synthetic-chain",
            steps=(BuildStep(
                step_id="chain", argv=("make", "build-docker"), cwd_role="gonka",
                timeout_seconds=60, description="Synthetic chain image build",
                produces_images=("gonka/inference-chain:e2e",),
            ),),
            external_images={}, toolchain={}, chain_images=("gonka/inference-chain:e2e",),
        ) if with_build and role == "gonka" else None,
        runtime=rt,
        harness=None,
    )


class StubLayout:
    """Stands in for RunnerLayout so tests never depend on the image contents."""

    def __init__(self, root):
        self.root = Path(root)

    @property
    def harness_script(self) -> Path:
        return self.root / "scripts" / "acceptance_harness.py"

    @property
    def testermint_harness_dir(self) -> Path:
        return self.root / "harness" / "testermint"

    def runner_version(self) -> str:
        return "a8-runner/2.0.0"

    def assert_complete(self) -> None:
        return None


class FakeDockerDaemon:
    """The docker CLI and the build command, as a small in-memory daemon."""

    DEFAULT_CHAIN_IMAGE = "ghcr.io/product-science/inferenced:latest"
    DEFAULT_OLD_IMAGE_EPOCH = BUILD_STARTED_EPOCH - 3600.0

    def __init__(
        self,
        *,
        images,
        honour_removal: bool = True,
        rebuild_restores: bool = True,
        rebuild_epoch: Optional[float] = None,
    ):
        self.images = {reference: dict(spec) for reference, spec in images.items()}
        self.honour_removal = honour_removal
        self.rebuild_restores = rebuild_restores
        self.rebuild_epoch = rebuild_epoch
        self.build_invocations: list = []
        self.removals: list = []
        self.inspections: list = []
        self._removed: dict = {}

    def __call__(self, argv, *, cwd=None, env=None, timeout=None):
        argv = [str(a) for a in argv]
        if argv[:3] == ["docker", "image", "inspect"]:
            return self._inspect(argv)
        if argv[:4] == ["docker", "image", "rm", "-f"]:
            return self._remove(argv)
        return self._build(argv, cwd=cwd, env=env, timeout=timeout)

    def _inspect(self, argv):
        reference = argv[3]
        self.inspections.append(reference)
        spec = self.images.get(reference)
        if spec is None:
            return completed(argv, returncode=1, stderr="Error: No such image")
        payload = [
            {
                "Id": spec["id"],
                "RepoDigests": [],
                "Created": docker_timestamp(spec["created_epoch"]),
            }
        ]
        return completed(argv, stdout=json.dumps(payload))

    def _remove(self, argv):
        reference = argv[4]
        self.removals.append(reference)
        if not self.honour_removal:
            return completed(argv, stdout=f"Untagged: {reference}\n")
        spec = self.images.pop(reference, None)
        if spec is not None:
            self._removed[reference] = spec
        return completed(argv, stdout=f"Deleted: {reference}\n")

    def _build(self, argv, *, cwd, env, timeout):
        attempt = len(self.build_invocations) + 1
        self.build_invocations.append(
            {"argv": argv, "cwd": cwd, "env": dict(env or {}), "timeout": timeout}
        )
        if self.rebuild_restores:
            for reference, spec in list(self._removed.items()):
                restored = dict(spec)
                if self.rebuild_epoch is not None:
                    restored["created_epoch"] = self.rebuild_epoch
                self.images[reference] = restored
                self._removed.pop(reference)
        return completed(argv, stdout=f"build attempt {attempt}\n")

    @staticmethod
    def image_id_for(reference: str) -> str:
        return "sha256:" + hashlib.sha256(reference.encode()).hexdigest()

    @classmethod
    def with_old_image(cls, reference: str = DEFAULT_CHAIN_IMAGE, **kwargs):
        return cls(
            images={
                reference: {
                    "id": cls.image_id_for(reference),
                    "created_epoch": cls.DEFAULT_OLD_IMAGE_EPOCH,
                }
            },
            **kwargs,
        )


class FakeAcquirer:
    """A SourceAcquirer that verifies nothing and materialises empty trees."""

    instances: list = []

    def __init__(self, *args, **kwargs):
        self.verified = []
        self.materialised = []
        self.refetched = []
        FakeAcquirer.instances.append(self)

    def verify_stored_bundle(self, record, *, package_root):
        self.verified.append(str(record.get("bundle_relpath")))
        return Path(package_root) / str(record.get("bundle_relpath"))

    def materialise(self, bundle_path, worktree, ref, sha):
        worktree = Path(worktree)
        worktree.mkdir(parents=True, exist_ok=True)
        self.materialised.append((str(bundle_path), str(worktree), ref, sha))
        return worktree

    def acquire(self, spec, *, worktree_root):
        self.refetched.append(spec)
        raise AssertionError("the stored package was complete; no refetch is allowed")


class RecordingGitRunner:
    """A fake subprocess.run for GitClient that records every argv."""

    def __init__(self, *, commit_sha: str, ref: str):
        self.commit_sha = commit_sha
        self.ref = ref
        self.calls: list = []

    @property
    def flat_calls(self) -> list:
        return [" ".join(argv) for argv in self.calls]

    def __call__(self, argv, *, cwd=None, env=None, capture_output=True, text=True, timeout=None):
        args = [str(a) for a in argv]
        self.calls.append(args)
        if "fetch" in args or ("ls-remote" in args and any(a.startswith("https://") for a in args)):
            return subprocess.CompletedProcess(args, 128, "", "network is unreachable")
        if args[1:3] == ["bundle", "verify"]:
            return subprocess.CompletedProcess(args, 0, "The bundle records a complete history.\n", "")
        if "ls-remote" in args:
            return subprocess.CompletedProcess(args, 0, f"{self.commit_sha}\t{self.ref}\n", "")
        if "clone" in args:
            destination = Path(args[-1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / ".git").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, self.commit_sha + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


def fake_process_runner(argv, **kwargs):
    """Stands in for the build/docker command runner."""
    if list(argv)[:3] == ["docker", "image", "inspect"]:
        return subprocess.CompletedProcess(list(argv), 0, json.dumps([{
            "Id": RUNNER_IMAGE_ID, "RepoDigests": [], "Created": utc_now_iso(),
        }]), "")
    return subprocess.CompletedProcess(list(argv), 0, "synthetic 1.0\n", "")


_UNSET = object()

#: Keys only the retired overlay / prepared-build producer wrote into
#: ``live-context.source``. The immutable-source harness pops exactly these
#: (``scripts/acceptance_harness.py``, run-live step 10) and the verifier rejects
#: their presence in new-model evidence.
RETIRED_SOURCE_FIELDS = (
    "gonka_prepared_sha",
    "gonka_overlay_manifest_sha256",
    "gonka_test_harness_sha",
    "gonka_base_sha",
)


def snapshot_fingerprint(
    head: str,
    tree: str,
    *,
    tracked_files: int = 3,
    status: Sequence[str] = (),
    forbidden: Sequence[str] = (),
    index_matches_head: bool = True,
    missing_tracked: Sequence[str] = (),
    submodules: Sequence[dict] = (),
    tracked_digest: Optional[str] = None,
) -> dict:
    """A fingerprint with exactly the keys ``source_snapshot.capture`` returns.

    Built on the producer's own schema constant, so a renamed key in
    ``forward_e2e/suite/source_snapshot.py`` breaks these fixtures instead of leaving
    them silently valid.
    """
    digest = tracked_digest or hashlib.sha256(
        f"{head}\0{tree}\0{tracked_files}".encode("utf-8")
    ).hexdigest()
    return {
        "schema": source_snapshot.SNAPSHOT_SCHEMA,
        "head": head,
        "tree": tree,
        "index_matches_head": index_matches_head,
        "tracked_files": tracked_files,
        "tracked_digest": digest,
        "missing_tracked": list(missing_tracked),
        "status": list(status),
        "submodules": [dict(item) for item in submodules],
        "forbidden": list(forbidden),
    }


def source_immutability_document(
    *,
    gonka_sha: str = GONKA_SHA,
    marketplace_sha: str = MARKETPLACE_SHA,
    gonka_tree_sha: str = GONKA_TREE_SHA,
    marketplace_tree_sha: str = MARKETPLACE_TREE_SHA,
    gonka_after: Any = _UNSET,
    marketplace_after: Any = _UNSET,
    network_root_verification: Any = _UNSET,
) -> dict:
    """``source-immutability.json`` in either explicit producer schema.

    Mirrors ``write_source_immutability`` in ``scripts/acceptance_harness.py``: one
    ``source_snapshot.immutability_record`` per root, and a set verdict that
    is ``UNCHANGED`` only if every root is. The real record function is used,
    so the per-root structure is the producer's, not a re-invention.
    By default, model the current full-working-copy producer (schema /2). Pass
    ``network_root_verification=None`` only for an explicit historical /1
    snapshot-only fixture.
    Passing ``gonka_after=<fingerprint>`` (or ``None`` for "never measured")
    changes exactly one fact.
    """
    before = {
        "gonka": snapshot_fingerprint(gonka_sha, gonka_tree_sha),
        "marketplace": snapshot_fingerprint(marketplace_sha, marketplace_tree_sha),
    }
    after = {
        "gonka": before["gonka"] if gonka_after is _UNSET else gonka_after,
        "marketplace": before["marketplace"] if marketplace_after is _UNSET else marketplace_after,
    }
    expected = {"gonka": gonka_sha, "marketplace": marketplace_sha}
    roots = {
        label: source_snapshot.immutability_record(
            label=label, expected_sha=expected[label], before=before[label], after=after[label]
        )
        for label in before
    }
    verdicts = {record["verdict"] for record in roots.values()}
    if verdicts == {"UNCHANGED"}:
        verdict = "UNCHANGED"
    elif "VIOLATED" in verdicts:
        verdict = "VIOLATED"
    else:
        verdict = "INCOMPLETE"
    if network_root_verification is _UNSET:
        network_root_verification = network_manifest_document()["post_run_verification"]
    document = {
        "schema": "a8.source-immutability-set/2" if network_root_verification is not None
        else "a8.source-immutability-set/1",
        "roots": roots,
        "verdict": verdict,
    }
    if network_root_verification is not None:
        document["network_root"] = copy.deepcopy(network_root_verification)
        if network_root_verification.get("verdict") != "UNCHANGED":
            document["verdict"] = "VIOLATED" if network_root_verification.get("verdict") == "VIOLATED" else "INCOMPLETE"
    return document


def _json_bytes(value: Any) -> bytes:
    """Bytes exactly as the harness's ``write_json`` writes a document."""
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def harness_inputs_document() -> dict:
    """``external-harness/harness-inputs.json`` in the producer's shape.

    Mirrors ``harness_inputs`` in ``scripts/external_harness.py``.
    """
    harness_files = [
        {"path": "build.gradle.kts", "sha256": "1" * 64},
        {"path": "src/test/kotlin/MarketplaceContractAcceptanceTests.kt", "sha256": "2" * 64},
    ]
    templates = [{"path": "ownership.yml", "sha256": "3" * 64}]

    def tree(entries):
        digest = hashlib.sha256()
        for item in entries:
            digest.update(f"{item['path']}\0{item['sha256']}\n".encode("utf-8"))
        return digest.hexdigest()

    return {
        "schema": "a8.external-harness-inputs/1",
        "harness_dir": "/app/harness/testermint",
        "harness_files": harness_files,
        "harness_tree_sha256": tree(harness_files),
        "network_templates": templates,
        "network_templates_sha256": tree(templates),
        "runner_files": [{"path": "external_harness.py", "sha256": "4" * 64}],
        "upstream_testermint_test": {
            "path": "testermint/src/test/kotlin/TestermintTest.kt",
            "sha256": "5" * 64,
        },
        "classpath_file": {
            "path": "/workspace/a8-work/run/testermint-classpath.txt",
            "sha256": "6" * 64,
            "entries": 42,
        },
    }


def network_manifest_document() -> dict:
    """``network/network-manifest.json`` in the producer's shape.

    Mirrors ``prepare_network_root`` in ``scripts/external_harness.py``:
    upstream copies carry equal ``sha256`` and ``source_sha256``.
    """
    return {
        "schema": "a8.network-manifest/1",
        "gonka_dir": "/workspace/src/gonka",
        "network_root": "/workspace/a8-work/run/network-root",
        "copy_mode": "full_working_tree",
        "b3": False,
        "upstream_copies": [
            {"path": "local-test-net/docker-compose.yml", "sha256": "7" * 64,
             "source_sha256": "7" * 64, "mode": "0o644"},
        ],
        "generated": [
            {"path": "local-test-net/ownership.yml",
             "template": "harness/network/ownership.yml",
             "sha256": "8" * 64, "template_sha256": "8" * 64, "mode": "0o644"},
        ],
        "directories": [{"path": "prod-local", "mode": "0o777"}],
        "compose_files_by_pair": {
            pair: ["local-test-net/ownership.yml", "local-test-net/nats.yml"]
            for pair in ("genesis", "join1", "join2")
        },
        "ownership_label": "io.gonka.a8.run-id",
        "genesis_provisioner": None,
        "post_run_verification": {
            "verdict": "UNCHANGED",
            "checked_upstream_files": 1,
            "checked_generated_files": 1,
            "violations": [],
            "runtime_additions": [],
        },
    }


HARNESS_INPUTS_BYTES = _json_bytes(harness_inputs_document())
NETWORK_MANIFEST_BYTES = _json_bytes(network_manifest_document())
EXTERNAL_HARNESS_INPUTS_SHA256 = hashlib.sha256(HARNESS_INPUTS_BYTES).hexdigest()
NETWORK_MANIFEST_SHA256 = hashlib.sha256(NETWORK_MANIFEST_BYTES).hexdigest()

#: Evidence documents run-live writes next to ``live-context.json`` whose
#: SHA-256 the live context declares, keyed by relative path.
IMMUTABLE_EVIDENCE_FILES = {
    "external-harness/harness-inputs.json": HARNESS_INPUTS_BYTES,
    "network/network-manifest.json": NETWORK_MANIFEST_BYTES,
}


def source_immutability_bytes(document: Optional[dict] = None, **kwargs: Any) -> bytes:
    """The current producer-shaped bytes whose SHA-256 the context declares."""
    doc = document if document is not None else source_immutability_document(**kwargs)
    return _json_bytes(doc)


def apply_immutable_source_fields(
    context: dict,
    *,
    gonka_sha: str,
    marketplace_sha: str,
    gonka_tree_sha: str = GONKA_TREE_SHA,
    marketplace_tree_sha: str = MARKETPLACE_TREE_SHA,
    immutability_bytes: Optional[bytes] = None,
    verdict: str = "UNCHANGED",
) -> dict:
    """Rewrite ``context["source"]`` the way run-live step 10 does.

    Follows ``scripts/acceptance_harness.py`` (run-live, "live-context.json source
    fields for the immutable-source model"): pop the retired fields, then set
    the model, both selected SHAs, both tree SHAs, the immutability verdict
    and the digests of the documents the run wrote. Used to turn a recorded
    historical context into the shape the current producer emits without
    re-inventing the rest of the document.
    """
    source = context.setdefault("source", {})
    for stale in RETIRED_SOURCE_FIELDS:
        source.pop(stale, None)
    document = (
        immutability_bytes
        if immutability_bytes is not None
        else source_immutability_bytes(
            gonka_sha=gonka_sha,
            marketplace_sha=marketplace_sha,
            gonka_tree_sha=gonka_tree_sha,
            marketplace_tree_sha=marketplace_tree_sha,
        )
    )
    source["evidence_model"] = EVIDENCE_MODEL_IMMUTABLE
    source["gonka_sha"] = gonka_sha
    source["gonka_tree_sha"] = gonka_tree_sha
    source["marketplace_commit_sha"] = marketplace_sha
    source["marketplace_tree_sha"] = marketplace_tree_sha
    source["source_immutability_verdict"] = verdict
    source["source_immutability_sha256"] = hashlib.sha256(document).hexdigest()
    source["external_harness_inputs_sha256"] = EXTERNAL_HARNESS_INPUTS_SHA256
    source["network_manifest_sha256"] = NETWORK_MANIFEST_SHA256
    return context


def live_context_payload(
    *,
    run_id: str = "20260101000000",
    task_id: Optional[str] = None,
    scenario: Optional[str] = None,
    gonka_sha: Optional[str] = None,
    requested_sha: Optional[str] = None,
    prepared_sha: Optional[str] = None,
    observed_sha: Any = _UNSET,
    runtime_sha: Any = _UNSET,
    harness_sha: Any = _UNSET,
    marketplace_sha: str = MARKETPLACE_SHA,
    a9_manifest_sha256: str = A9_MANIFEST_SHA256,
    a9_contract_sha256: Optional[dict] = None,
    test_contract_sha256: Optional[dict] = None,
    with_runtime: bool = True,
    evidence_model: Optional[str] = EVIDENCE_MODEL_IMMUTABLE,
    gonka_tree_sha: str = GONKA_TREE_SHA,
    marketplace_tree_sha: str = MARKETPLACE_TREE_SHA,
    source_immutability_verdict: str = "UNCHANGED",
    immutability_bytes: Optional[bytes] = None,
) -> dict:
    """A live context in the shape the harness writes.

    Default: the immutable-source model (``EVIDENCE_MODEL_IMMUTABLE``), whose
    ``source`` carries the selected SHAs, their tree SHAs and the
    source-immutability verdict and digest, and none of the retired
    prepared-build fields.

    ``evidence_model=EVIDENCE_MODEL_E2E`` (or the legacy id, or ``None``)
    yields the historical prepared-build shape, including
    ``gonka_prepared_sha`` and ``gonka_test_harness_sha``. Such packages stay
    readable but can never be graded as an immutable-source pass.
    """
    req_sha = (
        requested_sha
        if requested_sha is not None
        else (gonka_sha if gonka_sha is not None else GONKA_SHA)
    )
    historical = evidence_model is None or evidence_model in HISTORICAL_EVIDENCE_MODELS
    prep_sha = prepared_sha if prepared_sha is not None else req_sha
    if observed_sha is not _UNSET:
        obs_sha = observed_sha
    elif runtime_sha is not _UNSET:
        obs_sha = runtime_sha
    else:
        # Nothing is patched in the immutable model, so the running binary
        # reports the selected commit itself.
        obs_sha = prep_sha if historical else req_sha

    source: dict = {}
    if evidence_model is not None:
        source["evidence_model"] = evidence_model
    source.update(
        {
            "gonka_source_sha": req_sha,
            "gonka_sha": req_sha,
            "protobuf_sha": "379bebced638aeb5e6077bfd51c986f898443832",
            "marketplace_commit_sha": marketplace_sha,
            "a9_manifest_sha256": a9_manifest_sha256,
            "a9_contract_sha256": (
                dict(a9_contract_sha256)
                if a9_contract_sha256 is not None
                else {"deal": DEAL_SHA256, "factory": FACTORY_SHA256}
            ),
            "test_contract_sha256": (
                dict(test_contract_sha256)
                if test_contract_sha256 is not None
                else {"caller": CALLER_SHA256, "cw20": CW20_SHA256}
            ),
        }
    )
    if historical:
        source["gonka_prepared_sha"] = prep_sha
    if with_runtime:
        runtime = {
            "wasmd": "v0.54.2",
            "wasmvm": "v2.2.4",
            "go": "go1.23.5",
            "cosmos_sdk": "v0.53.0",
        }
        if obs_sha is not None:
            runtime["gonka_source_sha"] = obs_sha
        source["runtime"] = runtime
    if historical:
        if harness_sha is not _UNSET:
            if harness_sha is not None:
                source["gonka_test_harness_sha"] = harness_sha
        else:
            source["gonka_test_harness_sha"] = prep_sha

    payload = {
        "schema_version": "1.0.0",
        "kind": "gonka-marketplace-a8-live-context",
        "created_at_utc": "2026-01-01T00:00:00Z",
        "run_id": run_id,
        "level": "live_network",
        "source": source,
        "chain": {
            "chain_id": "gonka-mainnet",
            "status": {"node_info": {"network": "gonka-mainnet"}},
            "transfer_restrictions": {"is_active": False},
        },
        "terms": {
            "target_epoch": 5,
            "budget_micro_usdt": "100000000",
            "price_micro_usdt_per_gnk": "1000000",
            "fee_bps": 150,
        },
        "accounts": {"host": "gonka1host", "buyer": "gonka1buyer"},
        "contracts": {"factory": "gonka1factory", "deal": "gonka1deal"},
        "bootstrap": {},
        "phases": [],
        "command": {
            "entrypoint": "python scripts/acceptance_harness.py run-live",
            "scenario": (
                scenario
                if scenario is not None
                else (task_id if task_id is not None else "lock-exact-e")
            ),
            "test": (
                "MarketplaceContractAcceptanceTests"
                if (task_id is not None or scenario is not None)
                else "MarketplaceContractAcceptanceTests.marketplace funded lock succeeds exactly at E"
            ),
        },
    }
    if not historical:
        apply_immutable_source_fields(
            payload,
            gonka_sha=req_sha,
            marketplace_sha=marketplace_sha,
            gonka_tree_sha=gonka_tree_sha,
            marketplace_tree_sha=marketplace_tree_sha,
            immutability_bytes=immutability_bytes,
            verdict=source_immutability_verdict,
        )
        # A caller may deliberately declare a different model id (a negative
        # fixture for an unknown model); keep it rather than overwrite it.
        source["evidence_model"] = evidence_model
    return payload


def ownership_payload(*, run_id: str, image_id: str) -> dict:
    return {
        "run_id": run_id,
        "cleanup_started_utc": "2026-01-01T00:05:00+00:00",
        "keep_resources": False,
        "ownership_verified": True,
        "owned_containers": [
            {
                "id": "0123456789ab",
                "name": "genesis-node",
                "service": "node",
                "status": "running",
                "image": image_id,
                "image_reference": "gonka/inference-chain:e2e",
            }
        ],
        "removed_containers": ["genesis-node"],
        "removed_volumes": [],
        "removed_networks": [],
        "container_logs_saved": ["genesis-node.log"],
        "status": "CLEANED",
    }


def write_suite_output(
    *,
    output_dir=None,
    suite_dir: Optional[Path] = None,
    suite_id: str,
    profile=None,
    scenarios=None,
    tasks=None,
    e2e_context=None,
    live_context_relpath: Union[str, Callable[[str], str]] = POLICY_LIVE_CONTEXT_RELPATH,
    write_live_context: bool = True,
    execution_status=ExecutionStatus.PASSED,
    evidence_status=EvidenceStatus.COMPLETE,
    live_context_payload_for=None,
    ownership_image: Optional[str] = None,
    gonka_overlay_manifest_sha256: Optional[str] = None,
    gonka_prepared_sha: Optional[str] = None,
    evidence_model: Optional[str] = None,
) -> Path:
    """Write a complete synthetic suite directory, as the orchestrator would.

    By default every native task gets immutable-source evidence: a live
    context in the current producer's shape and, next to it, the
    ``source-immutability.json`` whose SHA-256 that context declares (run-live
    writes both into the same evidence directory).

    Passing ``gonka_prepared_sha`` / ``gonka_overlay_manifest_sha256`` or a
    historical ``evidence_model`` writes a *historical* prepared-build suite
    instead. Those are only for tests proving that old packages stay
    readable and are never graded as an immutable-source pass.
    """
    if tasks is not None:
        resolved_tasks = list(tasks)
        resolved_profile = profile
        resolved_scenarios = list(scenarios) if scenarios else ([t.task_id for t in resolved_tasks] if not profile else None)
    else:
        resolved_tasks, resolved_profile, resolved_scenarios = resolve_e2e_selection(
            profile=profile, scenarios=list(scenarios) if scenarios else None
        )
    tasks = resolved_tasks
    suite_dir = Path(suite_dir) if suite_dir is not None else (Path(output_dir) / suite_id)
    suite_dir.mkdir(parents=True, exist_ok=True)

    prep_sha = (
        gonka_prepared_sha
        if gonka_prepared_sha is not None
        else getattr(e2e_context, "gonka_prepared_sha", None)
    )
    if evidence_model is None:
        evidence_model = (
            EVIDENCE_MODEL_E2E
            if (prep_sha is not None or gonka_overlay_manifest_sha256 is not None)
            else EVIDENCE_MODEL_IMMUTABLE
        )
    historical = evidence_model in HISTORICAL_EVIDENCE_MODELS
    source_identity = SourceIdentity(
        marketplace_commit_sha=getattr(e2e_context, "contracts_requested_sha", None)
        or CONTRACTS_SHA,
        gonka_commit_sha=getattr(e2e_context, "gonka_requested_sha", None) or GONKA_SHA,
        runner_version_hash="7" * 64,
        catalog_version_hash="8" * 64,
        gonka_tree_sha=None if historical else GONKA_TREE_SHA,
        marketplace_tree_sha=None if historical else MARKETPLACE_TREE_SHA,
        source_immutability_verdict=None if historical else "UNCHANGED",
        gonka_overlay_manifest_sha256=gonka_overlay_manifest_sha256,
        gonka_prepared_sha=prep_sha,
    )
    plan = SuitePlan(
        schema_version="1.0.0",
        suite_id=suite_id,
        created_at_utc="2026-01-01T00:00:00+00:00",
        profile=resolved_profile,
        requested_scenarios=resolved_scenarios,
        source_identity=source_identity,
        tasks=tasks,
    )
    (suite_dir / "suite-plan.json").write_text(
        json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    if e2e_context is not None and hasattr(e2e_context, "to_dict"):
        (suite_dir / "e2e-context.json").write_text(
            json.dumps(e2e_context.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    results: List[TaskResult] = []
    indexed_files = []
    for task in tasks:
        task_run_id = make_task_run_id(suite_id, task.ordinal, task.task_id)
        run_dir = suite_dir / "runs" / task_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        if historical:
            # The shape the retired prepared-build runtime wrote.
            ident_data = {
                "run_id": task_run_id,
                "task_id": task.task_id,
                "marketplace_commit_sha": source_identity.marketplace_commit_sha,
                "marketplace_source_sha": source_identity.marketplace_commit_sha,
                "gonka_commit_sha": source_identity.gonka_commit_sha,
                "gonka_source_sha": source_identity.gonka_commit_sha,
                "gonka_prepared_sha": source_identity.gonka_prepared_sha or source_identity.gonka_commit_sha,
                "evidence_model": evidence_model,
            }
        else:
            # ``prepare_runtime_snapshot`` (forward_e2e/suite/runtime.py): the snapshot
            # HEAD is the selected commit, recorded with its tree object id;
            # nothing about a prepared commit or an overlay.
            ident_data = {
                "run_id": task_run_id,
                "task_id": task.task_id,
                "created_at_utc": "2026-01-01T00:00:00+00:00",
                "marketplace_source_sha": source_identity.marketplace_commit_sha,
                "marketplace_commit_sha": source_identity.marketplace_commit_sha,
                "marketplace_tree_sha": source_identity.marketplace_tree_sha,
                "gonka_source_sha": source_identity.gonka_commit_sha,
                "gonka_commit_sha": source_identity.gonka_commit_sha,
                "gonka_checkout_sha": source_identity.gonka_commit_sha,
                "gonka_tree_sha": source_identity.gonka_tree_sha,
                "evidence_model": evidence_model,
                "source_policy": "immutable",
                "marketplace_bundle_sha256": hashlib.sha256(b"marketplace-bundle").hexdigest(),
                "gonka_bundle_sha256": hashlib.sha256(b"gonka-bundle").hexdigest(),
                "run_dir": str(run_dir),
            }
        ident_file = run_dir / "identity.json"
        ident_file.write_text(json.dumps(ident_data, indent=2) + "\n", encoding="utf-8")
        indexed_files.append((ident_file, task_run_id, task.task_id, "METADATA_JSON"))

        if write_live_context and task.proof_level == ProofLevel.NATIVE:
            immutability = None if historical else source_immutability_bytes(
                gonka_sha=source_identity.gonka_commit_sha,
                marketplace_sha=source_identity.marketplace_commit_sha,
            )
            if live_context_payload_for is not None:
                ctx = live_context_payload_for(task_run_id)
            elif task.task_id == NATIVE_TASK_ID:
                ctx = real_lock_exact_e_context_for_run(task_run_id)
                ctx["source"]["marketplace_commit_sha"] = source_identity.marketplace_commit_sha
                ctx["source"]["gonka_source_sha"] = source_identity.gonka_commit_sha
                ctx["source"]["gonka_sha"] = source_identity.gonka_commit_sha
                if historical:
                    ctx["source"]["gonka_prepared_sha"] = source_identity.gonka_prepared_sha or source_identity.gonka_commit_sha
                    ctx["source"]["gonka_test_harness_sha"] = source_identity.gonka_prepared_sha or source_identity.gonka_commit_sha
                    ctx["source"]["runtime"]["gonka_source_sha"] = source_identity.gonka_prepared_sha or source_identity.gonka_commit_sha
                else:
                    # Nothing is patched: the binary reports the selected commit.
                    ctx["source"]["runtime"]["gonka_source_sha"] = source_identity.gonka_commit_sha
                    apply_immutable_source_fields(
                        ctx,
                        gonka_sha=source_identity.gonka_commit_sha,
                        marketplace_sha=source_identity.marketplace_commit_sha,
                        immutability_bytes=immutability,
                    )
            else:
                ctx = live_context_payload(
                    run_id=task_run_id,
                    gonka_sha=source_identity.gonka_commit_sha,
                    marketplace_sha=source_identity.marketplace_commit_sha,
                    task_id=task.task_id,
                    prepared_sha=source_identity.gonka_prepared_sha,
                    evidence_model=evidence_model,
                    immutability_bytes=immutability,
                )
            relpath = (
                live_context_relpath(task_run_id)
                if callable(live_context_relpath)
                else live_context_relpath
            )
            live_path = run_dir / relpath
            live_path.parent.mkdir(parents=True, exist_ok=True)
            live_path.write_text(json.dumps(ctx, indent=2) + "\n", encoding="utf-8")
            indexed_files.append((live_path, task_run_id, task.task_id, classify_artifact_kind(relpath)))
            if immutability is not None:
                # run-live writes both documents into the same evidence dir.
                immutability_relpath = (Path(relpath).parent / "source-immutability.json").as_posix()
                immutability_path = run_dir / immutability_relpath
                immutability_path.parent.mkdir(parents=True, exist_ok=True)
                immutability_path.write_bytes(immutability)
                indexed_files.append((
                    immutability_path, task_run_id, task.task_id,
                    classify_artifact_kind(immutability_relpath),
                ))
                for extra_rel, extra_bytes in IMMUTABLE_EVIDENCE_FILES.items():
                    extra_relpath = (Path(relpath).parent / extra_rel).as_posix()
                    extra_path = run_dir / extra_relpath
                    extra_path.parent.mkdir(parents=True, exist_ok=True)
                    extra_path.write_bytes(extra_bytes)
                    indexed_files.append((
                        extra_path, task_run_id, task.task_id,
                        classify_artifact_kind(extra_relpath),
                    ))

        for relative, contents in task_artifacts(task, source_identity.gonka_commit_sha).items():
            artifact_path = run_dir / relative
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(contents)
            indexed_files.append((artifact_path, task_run_id, task.task_id, classify_artifact_kind(relative)))

        if ownership_image is not None:
            cleanup_dir = run_dir / "cleanup-evidence"
            cleanup_dir.mkdir(parents=True, exist_ok=True)
            ownership_file = cleanup_dir / "ownership.json"
            ownership_file.write_text(
                json.dumps(ownership_payload(run_id=task_run_id, image_id=ownership_image), indent=2) + "\n",
                encoding="utf-8",
            )
            indexed_files.append((
                ownership_file, task_run_id, task.task_id,
                classify_artifact_kind("cleanup-evidence/ownership.json"),
            ))

        task_result = TaskResult(
            task_id=task.task_id,
            ordinal=task.ordinal,
            run_id=task_run_id,
            proof_level=task.proof_level,
            execution_status=execution_status,
            evidence_status=evidence_status,
            cleanup_status=CleanupStatus.CLEANED,
            acceptance_status=AcceptanceStatus.NOT_REVIEWED,
            start_time_utc="2026-01-01T00:00:00+00:00",
            end_time_utc="2026-01-01T00:01:00+00:00",
            duration_seconds=60.0,
            phase="COMPLETED",
            exit_code=0 if execution_status == ExecutionStatus.PASSED else 1,
            primary_failure=None if execution_status == ExecutionStatus.PASSED else "synthetic failure",
            secondary_errors=[],
            expected_cases=list(task.expected_checkpoints),
            observed_passed_cases=(list(task.expected_checkpoints)
                                   if execution_status == ExecutionStatus.PASSED
                                   and evidence_status == EvidenceStatus.COMPLETE else []),
            missing_cases=([] if execution_status == ExecutionStatus.PASSED
                           and evidence_status == EvidenceStatus.COMPLETE
                           else list(task.expected_checkpoints)),
            missing_artifacts=[],
            raw_evidence_dir=None,
            exported_evidence_dir=None,
        )
        res_file = run_dir / "result.json"
        res_file.write_text(json.dumps(task_result.to_dict(), indent=2) + "\n", encoding="utf-8")
        indexed_files.append((res_file, task_run_id, task.task_id, classify_artifact_kind("result.json")))
        results.append(task_result)

    outcome = calculate_suite_outcome(
        results,
    )
    suite_result = SuiteResult(
        schema_version="1.0.0",
        suite_id=suite_id,
        created_at_utc="2026-01-01T00:00:00+00:00",
        completed_at_utc="2026-01-01T00:02:00+00:00",
        source_identity=source_identity,
        overall_status=outcome[0],
        tasks=results,
        summary_message="synthetic suite output",
    )
    (suite_dir / "suite-result.json").write_text(
        json.dumps(suite_result.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    indexed_files.append((suite_dir / "suite-result.json", suite_id, "suite", "SUITE_RESULT_JSON"))
    update_artifact_index(
        suite_dir,
        [
            ArtifactEntry(
                relative_path=path.relative_to(suite_dir).as_posix(),
                size_bytes=path.stat().st_size,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                run_id=run_id,
                task_id=task_id,
                artifact_kind=artifact_kind,
            )
            for path, run_id, task_id, artifact_kind in indexed_files
        ],
    )
    return suite_dir


def _make_source_section(role, sha, *, repo_url=None, local_path=None, origin=None, tree_sha=None):
    spec = SourceSpec(
        role=role,
        kind=SourceKind.REMOTE if repo_url else SourceKind.LOCAL,
        commit_sha=sha,
        repo_url=repo_url,
        local_path=local_path,
    )
    acquired = AcquiredSource(
        spec=spec,
        worktree=Path("/workspace/src") / role,
        bundle_path=Path("/workspace/package/bundles") / f"{role}.bundle",
        bundle_relpath=f"bundles/{role}.bundle",
        bundle_sha256=hashlib.sha256(f"{role}-bundle".encode("utf-8")).hexdigest(),
        bundle_ref=bundle_ref(role),
        commit_url=github_commit_url(origin, sha),
        resolved_origin_url=origin,
    )
    record = acquired.to_lock_record()
    # ``planner._product_record`` adds the commit's tree object id.
    if tree_sha is not None:
        record["tree_sha"] = tree_sha
    return record


def make_test_lock(**overrides) -> RunLock:
    """Build a synthetic ``e2e/run-lock/2`` RunLock for offline tests.

    The sections follow ``planner.build_plan``: product records carry
    ``tree_sha``, ``source_policy`` is the planner's own section and
    ``external_tests`` has the shape ``_external_tests_section`` writes. There
    is no ``overlay`` section; a v1 lock is built by passing
    ``schema_version=RUN_LOCK_SCHEMA_V1`` together with ``overlay=...``.
    """
    from forward_e2e.execution.planner import _source_policy_section

    trees = {
        label: {"relpath": rel, "sha256": hashlib.sha256(rel.encode()).hexdigest(), "file_count": 1}
        for label, rel in (
            ("testermint_harness", "harness/testermint"),
            ("network_templates", "harness/network"),
            ("go_boundary", "harness/go_boundary"),
            ("wasm_probe", "harness/wasm_query_allowlist"),
        )
    }
    payload = {
        "schema_version": RUN_LOCK_SCHEMA,
        "plan_id": "plan-test",
        "created_at_utc": "2026-01-01T00:00:00+00:00",
        "gonka": _make_source_section(
            "gonka", GONKA_SHA, repo_url="https://github.com/gonka-ai/gonka.git",
            origin="https://github.com/gonka-ai/gonka.git", tree_sha=GONKA_TREE_SHA,
        ),
        "contracts": _make_source_section(
            "contracts", CONTRACTS_SHA, local_path="/mnt/contracts", origin=None,
            tree_sha=MARKETPLACE_TREE_SHA,
        ),
        "runner": {
            "image_id": RUNNER_IMAGE_ID,
            "image_tag": "a8-e2e:local",
            "runner_version": "a8-runner/2.0.0",
        },
        "platform": {"os": "linux", "arch": "amd64"},
        "compatibility": {
            "gonka": {"adapter_id": "gonka-v1", "match_mode": "exact"},
            "contracts": {"adapter_id": "contracts-v1", "match_mode": "exact"},
        },
        "selection": {"profile": "native", "scenarios": ["lock-exact-e", "funded-claim"]},
        "limits": {"per_task_timeout_seconds": 900},
        "network": {"chain_id": "gonka-e2e", "fresh_state": True},
        "build": {"gonka": {"targets": ["inferenced"]}, "contracts": {"targets": ["marketplace"]}},
        "source_policy": _source_policy_section(),
        "external_tests": {
            "trees": trees,
            "tests_hash": hashlib.sha256(
                json.dumps({k: v["sha256"] for k, v in trees.items()}, sort_keys=True).encode()
            ).hexdigest(),
        },
        "semantic_inputs": {"A8_PROOF_LEVEL": "native"},
        "source_package": {"bundles": ["bundles/gonka.bundle", "bundles/contracts.bundle"]},
        "notes": ["synthetic fixture"],
    }
    payload.update(overrides)
    return RunLock.from_dict(payload)


def run_failing_suite_orchestrator(
    *,
    marketplace_dir: Path,
    gonka_dir: Path,
    output_dir: Path,
    runtime_root: Path,
    suite_id: str,
    e2e_context,
    clean_heads: Sequence[str],
) -> tuple[dict, dict]:
    """Drive the real ``SuiteOrchestrator.run_suite`` until the first task fails.

    Returns ``(captured_kwargs_dict, suite_plan_json_dict)`` so callers can
    inspect either the arguments forwarded to ``prepare_runtime_snapshot`` /
    ``evaluate_task_evidence`` or the written ``suite-plan.json``.
    """
    from unittest.mock import MagicMock, patch
    from forward_e2e.suite.orchestrator import SuiteOrchestrator

    orchestrator = SuiteOrchestrator(
        marketplace_dir=marketplace_dir,
        gonka_dir=gonka_dir,
        output_dir=output_dir,
        runtime_root=runtime_root,
        e2e_context=e2e_context,
    )
    captured: dict = {}

    def make_fake_snap(**kwargs):
        captured["prepare"] = kwargs
        rid = kwargs.get("run_id", "test-run")
        snap = MagicMock()
        snap.run_id = rid
        snap.run_dir = runtime_root / rid
        snap.evidence_dir = snap.run_dir / "evidence"
        snap.junit_dir = snap.run_dir / "junit"
        snap.evidence_dir.mkdir(parents=True, exist_ok=True)
        snap.junit_dir.mkdir(parents=True, exist_ok=True)
        return snap

    def fake_evaluate(**kwargs):
        captured["evaluate"] = kwargs
        return (
            ExecutionStatus.FAILED,
            EvidenceStatus.INCOMPLETE,
            [],
            ["live-context.json"],
            "stopped before any real execution",
        )

    with patch("forward_e2e.suite.orchestrator.create_git_bundle"), patch(
        "forward_e2e.suite.orchestrator.get_git_clean_head",
        side_effect=list(clean_heads),
    ), patch(
        "forward_e2e.suite.orchestrator.git_tree_sha",
        side_effect=[GONKA_TREE_SHA, MARKETPLACE_TREE_SHA],
    ), patch(
        "forward_e2e.suite.orchestrator.prepare_runtime_snapshot", side_effect=make_fake_snap
    ), patch(
        "forward_e2e.suite.orchestrator.evaluate_task_evidence", side_effect=fake_evaluate
    ), patch(
        "forward_e2e.suite.orchestrator.NativeTaskAdapter"
    ) as adapter_cls, patch(
        "forward_e2e.suite.orchestrator.perform_runtime_cleanup", return_value={"status": "CLEANED"}
    ):
        adapter = MagicMock()
        adapter.execute.return_value = (
            ExecutionStatus.FAILED,
            1,
            "stopped before any real execution",
        )
        adapter_cls.return_value = adapter
        tasks, _, _ = resolve_e2e_selection(scenarios=["lock-exact-e"])
        orchestrator.run_suite(
            tasks=tasks, requested_scenarios=["lock-exact-e"], suite_id=suite_id
        )

    plan_file = output_dir / suite_id / "suite-plan.json"
    plan_data = json.loads(plan_file.read_text(encoding="utf-8")) if plan_file.is_file() else {}
    return captured, plan_data
