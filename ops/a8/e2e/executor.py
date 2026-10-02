"""Execution: turn a lock into real images, real binaries and real evidence.

``run`` and ``run --from`` and ``rerun --from`` all end up here, with exactly
one difference between them: a replay is told that it is a replay, so that it
records ``replay_of_plan`` and its parent run id. Everything else -- fresh run
id, fresh network, fresh artefacts -- is identical, because "replay" must never
degrade into "resume".

The order of operations is chosen so that every cheap check happens before any
expensive or destructive one:

1. the runner image must be the one the lock was created with;
2. the runner's own harness/verifier/catalog must still hash to the recorded
   values, and the compatibility adapter recorded in the lock must still exist
   in this runner with the same content hash;
3. the stored source package must verify (hash, bundle contents, commit object)
   before a single byte of it is trusted;
4. both materialised snapshots must be *pristine* -- exactly the locked commit
   and tree, nothing added, changed or deleted, submodules at their pins
   (:mod:`ops.a8.source_snapshot`);
5. only then are the images built, and is the inner network allowed to start.
   The snapshots are measured again after the build and after execution; any
   difference is a blocking finding, so such a run can never be a PASS.

A plan written for the retired overlay / prepared-commit runner is refused
before anything is written (``PLAN_SCHEMA_SUPERSEDED``).

Nothing here ever writes into the original lock. Facts that can only be known
after building are recorded in ``build-manifest.json`` and
``execution-manifest.json``, both bound to the lock by its content hash.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, MutableMapping, Optional, Sequence

from .. import source_snapshot
from .builder import (
    Builder,
    image_ledger_path,
    record_tool_versions,
    source_fingerprint,
    verify_observed_runtime,
)
from .cancel import Cancellation, make_build_runner
from .compat import CompatibilityAdapter, contracts_adapters, gonka_adapters
from .context import E2ERunContext
from .deployment import (
    A9_MANIFEST_ROLE as _A9_MANIFEST_ROLE,
    A9_RELEASE_ROLE_PREFIX as _A9_RELEASE_ROLE_PREFIX,
    INCOMPLETE as _DEPLOYMENT_INCOMPLETE,
    MISMATCHED as _DEPLOYMENT_MISMATCHED,
    SOURCE_IMMUTABILITY_FILENAME,
    verify_deployed_artifacts,
    verify_source_immutability_evidence,
)
from .errors import (
    BuildProvenanceError,
    IntegrityError,
    LockIntegrityError,
    OutputCollisionError,
    SourceAcquisitionError,
    SourceNotPristineError,
    SourceSnapshotMutatedError,
    SuiteExecutionError,
    UnsupportedCompatibilityError,
)
from ..evidence_model import EVIDENCE_MODEL_IMMUTABLE
from .evidence import (
    EvidencePolicyError,
    EvidenceRequirement,
    load_suite_plan,
    requirements_from_documents,
    tasks_from_lock,
)
from .gitio import CredentialProvider, GitClient
from .ownership import verify_ownership_evidence
from .outcome import RUN_RESULT_FILENAME, Finding, RunOutcome, evaluate_run
from .planner import RunnerLayout, default_runner_root, hash_runner_files, hash_runner_tree
from .planner import EXTERNAL_TEST_DIRS, HARNESS_FILES, NETWORK_FILES, VERIFIER_FILES
from .file_safety import atomic_write_bytes
from .runlock import (
    BUILD_MANIFEST_FILENAME,
    DELIVERY_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    BuildManifest,
    DeliveryAttempt,
    DeliveryManifest,
    DeliveryStatus,
    ExecutionManifest,
    ManifestStatus,
    RunLock,
    EXECUTION_MANIFEST_SCHEMA,
    assert_lock_executable,
    content_sha256,
    copy_lock_into_run,
    preserve_source_bundles,
    utc_now_iso,
)
from .runner_image import assert_runner_image_matches, resolve_runner_image
from .runner_source import read_runner_source
from .runpackage import (
    BUILD_LOG_DIRNAME,
    BUILD_OUTPUT_DIRNAME,
    SUITE_EXPORT_DIRNAME,
    LoadedRunPackage,
    run_stage_dir,
    verify_run_package,
)
from .sources import (
    SourceAcquirer,
    SourceKind,
    SourceSpec,
    bundle_ref,
    normalize_full_sha,
    resolve_within,
    sha256_path,
)

RUN_INPUTS_FILENAME = "run.json"
RUN_STATUS_FILENAME = "status.json"
RUN_RESULT_SHORT_FILENAME = "result.json"


def generate_run_id() -> str:
    return f"e2e-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:6]}"


@dataclass
class ExecutionRequest:
    """Operational parameters of one execution of an existing lock."""

    lock: RunLock
    lock_path: Path
    package_dir: Path
    output_dir: Path
    workspace_dir: Path
    run_id: Optional[str] = None
    command: str = "run"
    replay: bool = False
    parent_run_id: Optional[str] = None
    runtime_root: Optional[Path] = None
    keep_resources: bool = False
    credential: Optional[CredentialProvider] = None
    allow_remote_refetch: bool = True
    notes: List[str] = field(default_factory=list)
    #: Shared stop switch. The CLI installs the signal handlers and passes the
    #: token in, so a SIGINT during a two-hour build stops the build itself
    #: instead of being noticed only once it finishes.
    cancellation: Optional[Cancellation] = None
    #: Optional already-materialised sources from an in-process ``run`` that
    #: planned immediately before executing, avoiding redundant bundle restore.
    pre_acquired_sources: Optional[Mapping[str, Any]] = None


@dataclass
class RestoredSource:
    role: str
    worktree: Path
    commit_sha: str
    bundle_path: Path
    origin: str  # "package", "refetched", or "direct"


# ---------------------------------------------------------------------------
# preconditions
# ---------------------------------------------------------------------------
def assert_runner_matches_lock(lock: RunLock, layout: RunnerLayout) -> None:
    """The judge must be the judge the plan was made with.

    A plan is only meaningful together with the harness, the catalog and the
    verifier that produced it. If this runner image carries different ones, the
    honest answer is to refuse and ask for a new plan, not to quietly grade the
    run with different rules.
    """
    locked = lock.runner
    actual_source = read_runner_source(layout.root)
    for key, actual in actual_source.items():
        if locked.get(key) != actual:
            raise IntegrityError("Runner Git identity differs from the plan; create a new plan",
                                 {"field": key, "locked": locked.get(key), "actual": actual})
    checks = (
        ("harness_hash", hash_runner_files(layout.root, HARNESS_FILES)),
        ("verifier_hash", hash_runner_files(layout.root, VERIFIER_FILES)),
    )
    drift = {
        name: {"locked": locked.get(name), "actual": actual}
        for name, actual in checks
        if locked.get(name) != actual
    }
    from ..catalog import compute_catalog_hash

    actual_catalog = compute_catalog_hash()
    if locked.get("catalog_hash") != actual_catalog:
        drift["catalog_hash"] = {"locked": locked.get("catalog_hash"), "actual": actual_catalog}
    # The runner version, the external tests and the network templates judge
    # as much as the verifier does: a different Kotlin scenario or compose
    # template is a different plan.
    actual_version = layout.runner_version()
    if locked.get("runner_version") != actual_version:
        drift["runner_version"] = {"locked": locked.get("runner_version"), "actual": actual_version}
    locked_trees = (lock.external_tests or {}).get("trees") or {}
    for label, rel in EXTERNAL_TEST_DIRS:
        actual_tree = hash_runner_tree(layout.root, rel)["sha256"]
        locked_tree = (locked_trees.get(label) or {}).get("sha256")
        if locked_tree != actual_tree:
            drift[f"external_tests.{label}"] = {"locked": locked_tree, "actual": actual_tree}
    locked_network = (lock.network or {}).get("config_hashes") or {}
    for rel in NETWORK_FILES:
        actual_file = sha256_path(layout.root / rel)
        if locked_network.get(rel) != actual_file:
            drift[f"network.{rel}"] = {"locked": locked_network.get(rel), "actual": actual_file}
    if drift:
        raise IntegrityError(
            "This runner image no longer matches the one the lock was created with. Replaying "
            "the plan here would grade it with a different harness, catalog, verifier, "
            "external test or network template. "
            "Create a new plan with this runner instead.",
            {"drift": drift},
        )


def rebuild_adapter(role: str, lock: RunLock, layout: RunnerLayout) -> CompatibilityAdapter:
    """Recover the compatibility adapter the lock was planned with.

    Selection is *not* re-run here: the lock already decided. What is enforced
    is that this runner still provides an adapter with that id and that its
    definition has not drifted, because the build recipe, the allowed patches
    and the runtime expectations all come from it.
    """
    locked = lock.compatibility.get(role)
    if not isinstance(locked, Mapping):
        raise UnsupportedCompatibilityError(
            "The lock has no compatibility record for this role", {"role": role}
        )
    adapter_id = str(locked.get("adapter_id") or "")
    candidates = gonka_adapters() if role == "gonka" else contracts_adapters()
    for adapter in candidates:
        if adapter.adapter_id != adapter_id:
            continue
        expected_hash = str(locked.get("adapter_content_hash") or "")
        actual_hash = adapter.content_hash()
        if expected_hash != actual_hash:
            raise UnsupportedCompatibilityError(
                "The compatibility adapter recorded in the lock has changed in this runner "
                "image. Its build recipe or its runtime expectations would differ from the plan.",
                {"role": role, "adapter_id": adapter_id,
                 "locked_content_hash": expected_hash, "actual_content_hash": actual_hash},
            )
        return adapter
    raise UnsupportedCompatibilityError(
        "This runner image does not provide the compatibility adapter recorded in the lock",
        {
            "role": role,
            "adapter_id": adapter_id,
            "available": [a.adapter_id for a in candidates],
        },
    )


# ---------------------------------------------------------------------------
# source restoration
# ---------------------------------------------------------------------------
def restore_source(
    role: str,
    lock: RunLock,
    *,
    acquirer: SourceAcquirer,
    package_dir: Path,
    worktree_root: Path,
    allow_remote_refetch: bool,
    emit: Callable[[str], None],
    pre_acquired: Optional[Mapping[str, Any]] = None,
) -> RestoredSource:
    """Recover one pinned checkout from the stored package.

    The package is authoritative. The recorded remote is only a *fallback for a
    missing file*, never a fallback for a failing check: a bundle whose hash
    does not match is tampering, and tampering is never repaired by downloading
    something new.
    """
    record = lock.source_record(role)
    sha = normalize_full_sha(record.get("commit_sha"), flag=f"lock.{role}.commit_sha")
    ref = record.get("bundle_ref")
    if ref != bundle_ref(role):
        raise IntegrityError(
            "The source bundle_ref does not match its pinned role",
            {"role": role, "expected": bundle_ref(role), "actual": ref},
        )
    if pre_acquired and role in pre_acquired and pre_acquired[role] is not None:
        pre = pre_acquired[role]
        pre_worktree = getattr(pre, "worktree", None)
        pre_sha = getattr(pre, "commit_sha", None) or getattr(
            getattr(pre, "spec", None), "commit_sha", None
        )
        if (
            pre_worktree is not None
            and Path(pre_worktree).is_dir()
            and normalize_full_sha(pre_sha, flag=f"pre_acquired.{role}.commit_sha") == sha
        ):
            return RestoredSource(
                role=role,
                worktree=Path(pre_worktree),
                commit_sha=sha,
                bundle_path=Path(getattr(pre, "bundle_path", pre_worktree)),
                origin="direct",
            )
    rel = str(record.get("bundle_relpath") or "")
    bundle_path = (
        resolve_within(package_dir, rel, what=f"lock.{role}.bundle_relpath")
        if rel else None
    )
    origin = "package"

    if bundle_path is None or not bundle_path.is_file():
        repo_url = record.get("repo_url") or record.get("resolved_origin_url")
        if not allow_remote_refetch or not repo_url:
            raise IntegrityError(
                "The source package is incomplete and the lock records no remote that could "
                "supply this exact commit. The plan cannot be replayed.",
                {"role": role, "bundle_relpath": rel, "commit_sha": sha},
            )
        emit(
            f"Bundle for {role} is missing from the package; re-fetching commit {sha} "
            f"from the recorded repository."
        )
        spec = SourceSpec(
            role=role, kind=SourceKind.REMOTE, repo_url=str(repo_url),
            local_path=None, commit_sha=sha,
        )
        try:
            acquire_options = {} if rel else {"create_bundle": False}
            refetched = acquirer.acquire(
                spec, worktree_root=worktree_root, **acquire_options
            )
        except Exception as exc:
            raise SourceAcquisitionError(
                "The stored bundle is missing and the recorded repository could not supply "
                "the exact commit. A different version is never substituted.",
                {
                    "role": role,
                    "commit_sha": sha,
                    "cause_code": getattr(exc, "code", type(exc).__name__),
                },
            ) from exc
        expected_hash = str(record.get("bundle_sha256") or "")
        if expected_hash and refetched.bundle_sha256 != expected_hash:
            emit(
                f"Re-fetched {role} bundle has a different byte layout than the planned one "
                "(Git bundles are not byte-reproducible). The commit object itself was verified."
            )
        return RestoredSource(
            role=role,
            worktree=refetched.worktree,
            commit_sha=sha,
            bundle_path=refetched.bundle_path,
            origin="refetched",
        )

    acquirer.verify_stored_bundle(record, package_root=package_dir)
    worktree = acquirer.materialise(bundle_path, Path(worktree_root) / role, ref, sha)
    _restore_submodules(record, acquirer=acquirer, package_dir=package_dir, worktree=worktree)
    return RestoredSource(
        role=role, worktree=worktree, commit_sha=sha, bundle_path=bundle_path, origin=origin
    )


def _restore_submodules(
    record: Mapping[str, Any],
    *,
    acquirer: SourceAcquirer,
    package_dir: Path,
    worktree: Path,
) -> None:
    for entry in record.get("submodules") or []:
        rel = str(entry.get("bundle_relpath") or "")
        bundle = resolve_within(package_dir, rel, what="submodule bundle_relpath")
        if not bundle.is_file():
            raise IntegrityError(
                "The source package is missing a submodule bundle. An incomplete source "
                "snapshot is never accepted.",
                {"submodule": entry.get("path"), "bundle_relpath": rel},
            )
        expected = str(entry.get("bundle_sha256") or "")
        actual = sha256_path(bundle)
        if expected and expected != actual:
            raise IntegrityError(
                "A stored submodule bundle does not match the lock",
                {"submodule": entry.get("path"), "expected": expected, "actual": actual},
            )
        sub_sha = normalize_full_sha(entry.get("commit_sha"), flag="lock.submodule.commit_sha")
        target = resolve_within(worktree, str(entry.get("path") or ""), what="submodule path")
        if target.exists() and any(target.iterdir()):
            continue
        if target.exists():
            target.rmdir()
        acquirer.materialise(bundle, target, "refs/e2e/submodule", sub_sha)


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
def apply_semantic_environment(
    lock: RunLock,
    *,
    environ: Optional[MutableMapping[str, str]] = None,
    emit: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Rebuild the process environment from the plan.

    The planner records every semantic variable in ``lock.semantic_inputs`` and
    the lock says they are frozen. That promise is only real if execution
    actually enforces it, so this function:

    * restores each saved semantic value, overwriting whatever the current shell
      had;
    * **removes** ambient semantic variables the lock does not carry, because a
      variable that did not exist at plan time must not appear at replay time
      and silently change what is proven (``A8_EXPECTED_PROTO_SHA`` is the
      dangerous example: it moves the recorded ABI provenance);
    * leaves operational variables alone, since they only say where things live.

    Returns a record of what it changed, which goes into the build manifest
    so a reviewer can see that the replay ran in the planned environment.
    """
    from .planner import OPERATIONAL_ENV_NAMES, SEMANTIC_ENV_PREFIXES

    say = emit or (lambda message: None)
    env = environ if environ is not None else os.environ
    saved = lock.semantic_inputs.get("semantic_environment", {})
    if not isinstance(saved, Mapping):
        raise LockIntegrityError(
            "The lock's semantic_environment is not a mapping, so the planned execution "
            "environment cannot be reconstructed.",
            {"type": type(saved).__name__},
        )

    # Reject malformed values before changing any environment variable or
    # reporting a restoration; a rejected lock must not leave partial state.
    for name, value in saved.items():
        if not isinstance(name, str) or not name or "=" in name or "\0" in name:
            raise LockIntegrityError(
                "A semantic environment variable name in the lock is invalid",
                {"name": name, "type": type(name).__name__},
            )
        if not isinstance(value, str):
            # Stringifying it would put something like "{'a': 1}" into a child's
            # environment and call that "the planned value".
            raise LockIntegrityError(
                "A semantic environment value in the lock is not a string, so the planned "
                "execution environment cannot be reconstructed faithfully.",
                {"name": name, "type": type(value).__name__},
            )
        if "\0" in value:
            raise LockIntegrityError(
                "A semantic environment value in the lock contains a NUL character",
                {"name": name},
            )

    restored: Dict[str, str] = {}
    overridden: List[str] = []
    for name in sorted(saved):
        value = saved[name]
        if env.get(name) != value:
            if name in env:
                overridden.append(name)
            say(f"Restoring semantic variable {name} from the plan.")
        env[name] = value
        restored[name] = value

    cleared: List[str] = []
    for name in sorted(list(env)):
        if not name.startswith(SEMANTIC_ENV_PREFIXES):
            continue
        if name in OPERATIONAL_ENV_NAMES or name in saved:
            continue
        cleared.append(name)
        del env[name]
        say(
            f"Clearing ambient semantic variable {name}: it was not part of this plan, so "
            "letting it through would change what the replay proves."
        )

    return {
        "restored": sorted(restored),
        "overridden_from_shell": overridden,
        "cleared_ambient": cleared,
        "note": (
            "Semantic variables come from the lock. Ambient ones absent from the lock are "
            "removed so the surrounding shell cannot re-parameterise a replay. Operational "
            "variables are untouched."
        ),
    }


def export_and_record_delivery(
    stage_dir: Path,
    run_dir: Path,
    delivery: DeliveryManifest,
    *,
    say: Optional[Callable[[str], None]] = None,
    exporter: Optional[Callable[..., Any]] = None,
) -> tuple[Path, Optional[BaseException], Optional[BaseException]]:
    """Export the staged run package into run_dir and record the delivery outcome.

    Delegates to DeliveryService.deliver_initial to ensure a single owner for delivery
    lifecycle, per-file atomic publication, status transitions, and error handling.
    """
    from .delivery import DeliveryService

    result = DeliveryService.deliver_initial(
        stage_dir, run_dir, delivery, emit=say, exporter=exporter
    )
    return result.graded_root, result.export_failure, result.ledger_failure


def _write_run_inputs(
    path: Path,
    *,
    request: ExecutionRequest,
    lock: RunLock,
    run_id: str,
    created_at_utc: str,
) -> None:
    payload: Dict[str, Any] = {
        "schema_version": "e2e/run-inputs/1",
        "run_id": run_id,
        "plan_id": lock.plan_id,
        "lock_sha256": lock.lock_sha256,
        "created_at_utc": created_at_utc,
        "command": request.command,
        "replay": request.replay,
        "parent_run_id": request.parent_run_id,
        "sources": {
            "gonka": dict(lock.gonka),
            "contracts": dict(lock.contracts),
        },
        "runner": dict(lock.runner),
        "platform": dict(lock.platform),
        "selection": dict(lock.selection),
        "limits": dict(lock.limits),
    }
    atomic_write_bytes(
        path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def _write_run_status(
    path: Path,
    *,
    run_id: str,
    plan_id: str,
    state: str,
    current_stage: str,
    planned_tasks: Sequence[str],
    completed_tasks: Sequence[str],
    current_task: Optional[str],
    pending_tasks: Sequence[str],
    first_error: Optional[Mapping[str, Any]],
    export_status: str,
) -> None:
    payload: Dict[str, Any] = {
        "schema_version": "e2e/run-status/1",
        "run_id": run_id,
        "plan_id": plan_id,
        "updated_at_utc": utc_now_iso(),
        "state": state,
        "current_stage": current_stage,
        "planned_tasks": list(planned_tasks),
        "completed_tasks": list(completed_tasks),
        "current_task": current_task,
        "pending_tasks": list(pending_tasks),
        "first_error": dict(first_error) if first_error is not None else None,
        "export_status": export_status,
    }
    atomic_write_bytes(
        path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def _completed_tasks_from_suite(
    suite_dir: Path, planned_task_ids: Sequence[str], *, suite_succeeded: bool
) -> tuple[List[str], List[str]]:
    suite_res_path = suite_dir / "suite-result.json"
    if suite_res_path.is_file() and not suite_res_path.is_symlink():
        try:
            data = json.loads(suite_res_path.read_text(encoding="utf-8"))
            results = data.get("tasks") or data.get("task_results") or []
            completed = [
                str(r.get("task_id"))
                for r in results
                if isinstance(r, Mapping)
                and r.get("task_id")
                and str(r.get("execution_status") or "") != "NOT_RUN"
            ]
            completed_set = set(completed)
            pending = [tid for tid in planned_task_ids if tid not in completed_set]
            return completed, pending
        except (OSError, json.JSONDecodeError):
            pass
    if suite_succeeded:
        return list(planned_task_ids), []
    return [], list(planned_task_ids)


def execute_plan(
    request: ExecutionRequest,
    *,
    git: Optional[GitClient] = None,
    build_runner: Optional[Callable[..., Any]] = None,
    docker_runner: Optional[Callable[..., Any]] = None,
    runner_root: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    emit: Optional[Callable[[str], None]] = None,
    suite_runner: Optional[Callable[..., int]] = None,
    snapshot_capture: Optional[Callable[[Path], Dict[str, Any]]] = None,
) -> int:
    """Build the selected sources and run the selected scenarios against them.

    Returns the exit code of the **run verdict**, not of the suite. The suite is
    one input to that verdict; the build manifest, the per-task evidence and the
    post-run provenance are the others, and a run in which any of them is
    missing is not a pass.

    Everything the run produces is written into a durable, runner-owned staging
    directory on the persistent workspace volume *first*. The host output
    directory is an export of that state, taken both on success and on failure,
    so that a lost or interrupted export can be repeated by ``recover`` instead
    of destroying the only copy of the evidence.
    """
    say = emit or (lambda message: None)
    lock = request.lock
    # First of all, before a single directory is created: a plan that permits
    # a modified source tree is never executed, and nothing it points at is
    # touched.
    assert_lock_executable(lock)
    capture = snapshot_capture or _capture_snapshot
    cancellation = request.cancellation or Cancellation()
    # Every build child must lead its own process group, or a cancellation can
    # only reach `make` and leaves the compilers it spawned running.
    build_runner = build_runner or make_build_runner(cancellation)

    def stop_if_cancelled(stage: str) -> None:
        cancellation.raise_if_requested(stage)

    layout = RunnerLayout(Path(runner_root) if runner_root else default_runner_root())
    layout.assert_complete()

    run_id = (request.run_id or generate_run_id()).strip()
    run_dir = Path(request.output_dir).resolve() / run_id
    if run_dir.exists():
        raise OutputCollisionError(
            "A run with this id already exists. Past evidence is never overwritten; choose "
            "another --run-id or another output location.",
            {"path": str(run_dir)},
        )

    workspace_root = Path(request.workspace_dir).resolve()
    workspace = workspace_root / run_id
    # The durable copy. The host directory can be a bind mount that disappears,
    # fills up or is shared with other runs; this one is owned by the runner.
    stage_dir = run_stage_dir(workspace_root, run_id)
    if stage_dir.exists() and any(stage_dir.iterdir()):
        raise OutputCollisionError(
            "This run id already has durable state in the workspace. Recovering it is "
            "`recover`'s job; a new run may not write into it.",
            {"path": str(stage_dir)},
        )

    # -- 1. planned environment, before the first child process -------------
    # Even resolving the runner image spawns `docker image inspect`, so the
    # environment is rebuilt from the lock first. Then "every child inherits the
    # planned environment" is literally true rather than nearly true.
    environment_record = apply_semantic_environment(lock, emit=say)

    # -- 1b. runner identity, before anything expensive ---------------------
    observed_image = resolve_runner_image(
        str(lock.runner.get("locator") or "") or None,
        docker_runner=docker_runner,
        env=env,
        emit=say,
    )
    assert_runner_image_matches(lock.runner, observed_image)
    assert_runner_matches_lock(lock, layout)

    try:
        planned_tasks = tasks_from_lock(lock)
    except EvidencePolicyError as exc:
        raise IntegrityError(
            f"The locked task selection is incompatible with this runner's catalog: {exc}",
            {"selection": lock.selection},
        ) from exc

    gonka_adapter = rebuild_adapter("gonka", lock, layout)
    contracts_adapter = rebuild_adapter("contracts", lock, layout)
    # Nothing expensive has started yet: stopping here costs nothing at all.
    stop_if_cancelled("preconditions")

    stage_dir.mkdir(parents=True, exist_ok=True)
    preserved_lock = copy_lock_into_run(request.lock_path, stage_dir)
    say(f"Run {run_id}: preserved the original lock at {preserved_lock}")
    preserved_bundles = preserve_source_bundles(
        lock,
        request.package_dir,
        stage_dir,
        allow_missing=request.allow_remote_refetch or bool(request.pre_acquired_sources),
    )
    if preserved_bundles:
        say(f"Run {run_id}: preserved {len(preserved_bundles)} source bundle(s) in durable staging")

    manifest = BuildManifest.start(lock=lock, run_id=run_id)
    manifest.execution_environment = environment_record
    manifest_path = stage_dir / BUILD_MANIFEST_FILENAME
    exec_manifest = ExecutionManifest(
        schema_version=EXECUTION_MANIFEST_SCHEMA,
        lock_sha256=lock.lock_sha256,
        plan_id=lock.plan_id,
        run_id=run_id,
        suite_id=run_id,
        created_at_utc=utc_now_iso(),
        command=request.command,
        fresh_network_state=True,
        replay_of_plan=lock.plan_id if request.replay else None,
        parent_run_id=request.parent_run_id,
        source_plan_path=str(preserved_lock),
        runner_image_id=observed_image.image_id,
        runner_version=str(lock.runner.get("runner_version") or "") or None,
        notes=list(request.notes),
    )
    exec_manifest_path = stage_dir / EXECUTION_MANIFEST_FILENAME
    # Durable from the first moment. A run killed by the host in the middle of a
    # two-hour build still leaves a readable package that says which plan it was
    # executing -- which is what makes it recoverable rather than lost.
    manifest.write(manifest_path)
    exec_manifest.write(exec_manifest_path)

    planned_task_ids = [t.task_id for t in planned_tasks]
    completed_task_ids: List[str] = []
    pending_task_ids: List[str] = list(planned_task_ids)
    current_task_id: Optional[str] = None
    first_error_record: Optional[Dict[str, Any]] = None
    run_inputs_path = stage_dir / RUN_INPUTS_FILENAME
    status_path = stage_dir / RUN_STATUS_FILENAME
    _write_run_inputs(
        run_inputs_path,
        request=request,
        lock=lock,
        run_id=run_id,
        created_at_utc=exec_manifest.created_at_utc,
    )

    def update_status(
        state: str,
        current_stage: str,
        *,
        export_status: str = "IN_PROGRESS",
    ) -> None:
        _write_run_status(
            status_path,
            run_id=run_id,
            plan_id=lock.plan_id,
            state=state,
            current_stage=current_stage,
            planned_tasks=planned_task_ids,
            completed_tasks=completed_task_ids,
            current_task=current_task_id,
            pending_tasks=pending_task_ids,
            first_error=first_error_record,
            export_status=export_status,
        )
        # The persistent stage is authoritative. This small operational file is
        # also published to the host during the run so progress remains visible
        # while the suite and final evidence export are still running.
        try:
            _write_run_status(
                run_dir / RUN_STATUS_FILENAME,
                run_id=run_id, plan_id=lock.plan_id, state=state,
                current_stage=current_stage, planned_tasks=planned_task_ids,
                completed_tasks=completed_task_ids, current_task=current_task_id,
                pending_tasks=pending_task_ids, first_error=first_error_record,
                export_status=export_status,
            )
        except (OSError, E2EError) as exc:
            say(f"warning: live status could not be published to {run_dir}: {exc}")

    update_status("initializing", "initializing")

    failure: Optional[BaseException] = None
    try:
        # -- 2. sources ----------------------------------------------------
        update_status("acquiring_sources", "acquiring_sources")
        # GitClient refuses credentials without a helper directory, because the
        # token must never be placed on an argv or in the environment.
        client = git or GitClient(
            credentials=request.credential,
            helper_dir=workspace / "git-helper",
        )
        acquirer = SourceAcquirer(
            client,
            package_dir=stage_dir,
            scratch_dir=workspace / "scratch",
        )
        worktree_root = workspace / "src"
        gonka_src = restore_source(
            "gonka", lock,
            acquirer=acquirer, package_dir=stage_dir,
            worktree_root=worktree_root,
            allow_remote_refetch=request.allow_remote_refetch, emit=say,
            pre_acquired=request.pre_acquired_sources,
        )
        contracts_src = restore_source(
            "contracts", lock,
            acquirer=acquirer, package_dir=stage_dir,
            worktree_root=worktree_root,
            allow_remote_refetch=request.allow_remote_refetch, emit=say,
            pre_acquired=request.pre_acquired_sources,
        )

        stop_if_cancelled("sources-restored")

        # -- 3. pristine sources -------------------------------------------
        # Measured before the first build step. A snapshot that is not
        # exactly its locked commit and tree stops the run here: building it
        # would describe a tree nobody selected.
        sources = {"gonka": gonka_src, "contracts": contracts_src}
        before = {role: _measure(capture, src, "before_build") for role, src in sources.items()}
        manifest.source_immutability = {
            "model": "immutable-source",
            "roles": {
                role: {
                    "expected_sha": src.commit_sha,
                    "expected_tree": _locked_tree(lock, role),
                    "before_build": before[role],
                }
                for role, src in sources.items()
            },
        }
        manifest.write(manifest_path)
        for role, src in sources.items():
            _assert_pristine(role, before[role], src.commit_sha, _locked_tree(lock, role), manifest)
        manifest.source_immutability["pristine_before_build"] = True
        manifest.write(manifest_path)

        # -- 4. builds ------------------------------------------------------
        update_status("building", "building")
        build_out = stage_dir / BUILD_OUTPUT_DIRNAME
        build_out.mkdir(parents=True, exist_ok=True)
        # Everything that decides what an image contains. Two builds that agree
        # on all of it are entitled to the same image; a build that differs in
        # any of it may not reuse a cached one.
        fingerprint = source_fingerprint(
            {
                "gonka_sha": gonka_src.commit_sha,
                "contracts_sha": contracts_src.commit_sha,
                "gonka_adapter": gonka_adapter.adapter_id,
                "contracts_adapter": contracts_adapter.adapter_id,
                "gonka_recipe": getattr(gonka_adapter.build, "recipe_id", None),
                "contracts_recipe": getattr(contracts_adapter.build, "recipe_id", None),
                "gonka_recipe_fingerprint": (
                    gonka_adapter.build.fingerprint() if gonka_adapter.build else None
                ),
                "contracts_recipe_fingerprint": (
                    contracts_adapter.build.fingerprint() if contracts_adapter.build else None
                ),
                "platform": str(lock.platform.get("docker_platform") or "linux/amd64"),
            }
        )
        builder = Builder(
            runner=build_runner,
            log_dir=stage_dir / BUILD_LOG_DIRNAME,
            emit=say,
            ledger_path=image_ledger_path(workspace_root),
            source_fingerprint=fingerprint,
            cancellation=cancellation,
        )
        manifest.source_fingerprint = fingerprint
        record_tool_versions(manifest, runner=build_runner)

        docker_platform = str(lock.platform.get("docker_platform") or "linux/amd64")
        platform_parts = docker_platform.split("/", 1)
        if len(platform_parts) != 2 or platform_parts[0] != "linux" or not platform_parts[1]:
            raise LockIntegrityError(
                "The locked Docker platform is not a supported linux/<architecture> value",
                {"docker_platform": docker_platform},
            )
        context = {
            "gonka": str(gonka_src.worktree),
            "contracts": str(contracts_src.worktree),
            "runner": str(layout.root),
            "build_out": str(build_out),
            "platform": docker_platform,
            "goarch": platform_parts[1],
            "gonka_sha": gonka_src.commit_sha,
            # What upstream's Makefiles call VERSION (`git describe --always`),
            # measured read-only in the snapshot; it reaches the binaries
            # through LDFLAGS and the image tags.
            "gonka_version": _describe_version(client, gonka_src.worktree),
            "contracts_sha": contracts_src.commit_sha,
            # Part of every component identity: the same command run in a
            # different runner image is a different build.
            "runner_image_id": str(observed_image.image_id or ""),
            "python": _python_executable(),
        }
        manifest.build_inputs = {
            key: context[key]
            for key in ("gonka_sha", "gonka_version", "contracts_sha", "platform", "goarch",
                        "runner_image_id")
        }
        roles = {
            "gonka": gonka_src.worktree,
            "contracts": contracts_src.worktree,
            # Steps that only need a working directory run in the build
            # directory, never inside a snapshot.
            "build_out": build_out,
        }
        started_epoch = time.time()

        for adapter in (contracts_adapter, gonka_adapter):
            if adapter.build is None:
                continue
            stop_if_cancelled(f"before-recipe:{adapter.build.recipe_id}")
            say(f"Running build recipe {adapter.build.recipe_id}")
            builder.run_recipe(
                adapter.build,
                manifest=manifest,
                context=context,
                roles=roles,
                started_epoch=started_epoch,
                adapter=adapter,
            )
            builder.prepare_runtime_dependencies(adapter.build, manifest=manifest,
                                                 context=context, roles=roles)

        manifest.finish(required_roles=_required_roles(gonka_adapter, contracts_adapter, context))
        if manifest.status != ManifestStatus.COMPLETE:
            manifest.write(manifest_path)
            raise BuildProvenanceError(
                "The build did not produce every declared artefact, so the selected sources "
                "are not what would be tested.",
                {"status": manifest.status, "failures": manifest.failures},
            )
        manifest.write(manifest_path)

        # -- 4b. the build must not have touched the snapshots --------------
        _record_and_compare(
            capture, sources, before, "after_build", manifest.source_immutability, manifest
        )
        manifest.write(manifest_path)

        # -- 5. suite -------------------------------------------------------
        e2e_context = _build_context(
            lock=lock,
            layout=layout,
            gonka_adapter=gonka_adapter,
            contracts_adapter=contracts_adapter,
            gonka_src=gonka_src,
            contracts_src=contracts_src,
            runner_image_id=observed_image.image_id,
            manifest=manifest,
            gradle_user_home=build_out / "gradle-home",
        )
        # The suite is the longest stage of all; never start it after the
        # operator has already asked the runner to stop.
        stop_if_cancelled("before-suite")
        update_status("running_tasks", "running_tasks")
        def on_task_progress(event: str, task_id: str) -> None:
            nonlocal current_task_id
            if event == "started":
                current_task_id = task_id
            elif event == "completed":
                if task_id not in completed_task_ids:
                    completed_task_ids.append(task_id)
                pending_task_ids[:] = [tid for tid in planned_task_ids if tid not in completed_task_ids]
                current_task_id = None
            update_status("running_tasks", "running_tasks")

        runner_fn = suite_runner or _default_suite_runner
        after_execution_error: Optional[BaseException] = None
        try:
            suite_exit = runner_fn(
                contracts_dir=contracts_src.worktree,
                gonka_dir=gonka_src.worktree,
                output_dir=suite_export_root(stage_dir),
                workspace_dir=suite_workspace_root(workspace),
                runtime_root=Path(request.runtime_root) if request.runtime_root else None,
                suite_id=run_id,
                tasks=planned_tasks,
                profile=lock.profile,
                scenarios=None if lock.profile else lock.scenarios,
                keep_resources=request.keep_resources,
                e2e_context=e2e_context,
                progress_callback=on_task_progress,
            )
        finally:
            current_task_id = None
            # Measure the snapshots immediately after the suite returns or raises,
            # before any SuiteExecutionError or cancellation raises out of the stage.
            try:
                _record_and_compare(
                    capture,
                    sources,
                    before,
                    "after_execution",
                    manifest.source_immutability,
                    manifest,
                )
            except BaseException as imm_exc:
                after_execution_error = imm_exc
            finally:
                exec_manifest.source_immutability = {
                    "after_execution": {
                        role: (manifest.source_immutability.get("roles", {}).get(role) or {}).get(
                            "after_execution"
                        )
                        for role in sources
                    },
                    "verdict": manifest.source_immutability.get("verdict"),
                }
                manifest.write(manifest_path)
                exec_manifest.write(exec_manifest_path)

        exec_manifest.notes.append(f"suite exit code: {suite_exit}")

        # The orchestrator appends the suite id to both roots, so record where
        # the evidence really is rather than leaving `report`/`recover` to guess.
        exported = suite_export_dir(stage_dir, run_id)
        exec_manifest.suite_export_relpath = str(
            exported.relative_to(stage_dir)
        ) if exported.is_dir() else None
        exec_manifest.suite_workspace_dir = str(suite_workspace_root(workspace))
        exec_manifest.write(exec_manifest_path)

        completed_task_ids, pending_task_ids = _completed_tasks_from_suite(
            exported, planned_task_ids, suite_succeeded=(suite_exit == 0)
        )

        # The orchestrator owns signal handlers while the suite runs, so its
        # signal exit code must also update the executor's durable stop record.
        if suite_exit in (130, 143):
            cancellation.request(suite_exit - 128, emit=say)
        stop_if_cancelled("after-suite")
        if suite_exit != 0:
            # Persist through the normal failure path, so offline grading also
            # rejects a green evidence package whose suite reported an error.
            raise SuiteExecutionError(
                "The suite returned a nonzero exit code.",
                {"suite_exit_code": suite_exit},
            )
        if after_execution_error is not None:
            raise after_execution_error

        # Resolve untrusted evidence paths inside the failure boundary so an
        # unsafe index still leaves sealed manifests, delivery state and a verdict.
        if exec_manifest.suite_export_relpath:
            index_relpath = f"{exec_manifest.suite_export_relpath}/artifact-index.json"
            index_path = resolve_within(stage_dir, index_relpath, what="suite artifact index")
            exec_manifest.artifact_index_relpath = index_relpath if index_path.is_file() else None

        # -- 6. post-run provenance ----------------------------------------
        update_status("verifying_provenance", "verifying_provenance")
        # What each task owes is decided by the *selection*, never by what the
        # build happened to produce: a boundary-only run starts no chain and
        # owes no live context, and a native task's duty does not disappear
        # because some other task left a live context behind.
        suite_root = exported if exported.is_dir() else suite_export_root(stage_dir)
        requirements, disagreements = _evidence_requirements(
            lock=lock,
            suite_id=run_id,
            suite_dir=suite_root,
            manifest=manifest,
        )
        findings = _verify_post_run_provenance(
            suite_root,
            manifest=manifest,
            adapter=gonka_adapter,
            sources_root=gonka_src.worktree,
            selected_sha=gonka_src.commit_sha,
            marketplace_sha=contracts_src.commit_sha,
            requirements=requirements,
            emit=say,
            platform=docker_platform,
        )
        findings["selection_disagreements"] = disagreements
        manifest.observed_runtime = findings
        stop_if_cancelled("after-provenance")
        manifest.write(manifest_path)

    except Exception as exc:
        failure = exc
        current_task_id = None
        if first_error_record is None:
            first_error_record = {
                "code": str(getattr(exc, "code", "E2E_EXECUTION_FAILED")),
                "message": str(exc),
            }
        if not manifest.failures:
            manifest.record_failure(
                getattr(exc, "code", "E2E_EXECUTION_FAILED"),
                str(exc),
                _error_details(exc),
            )
        if cancellation.requested:
            # Keep the interruption visible in the evidence: a cancelled run is
            # neither a pass nor a verdict about the sources.
            manifest.cancellation = cancellation.to_dict()
        manifest.completed_at_utc = utc_now_iso()
        manifest.write(manifest_path)
        exec_manifest.notes.append(f"execution failed: {exc}")

    # -- 7. seal, export, grade ---------------------------------------------
    # The same tail runs for a finished and for a broken run. A failed run that
    # left no exported package would be unreviewable, which is how a real
    # failure becomes an argument about whether it happened.
    exec_manifest.build_manifest_sha256 = content_sha256(manifest.to_dict())
    exec_manifest.delivery_manifest_relpath = DELIVERY_MANIFEST_FILENAME
    exec_manifest.write(exec_manifest_path)

    terminal_state = (
        "cancelled"
        if cancellation.requested
        else ("failed" if failure is not None else "completed")
    )
    update_status(terminal_state, "exporting", export_status="IN_PROGRESS")

    # Initialize delivery manifest in stage_dir as IN_PROGRESS before copying starts.
    # If the process is hard-killed or crashes during export, the status remains
    # IN_PROGRESS rather than falsely claiming COMPLETED.
    delivery = DeliveryManifest(
        run_id=run_id,
        status=DeliveryStatus.IN_PROGRESS,
        attempts=[
            DeliveryAttempt(
                attempt_number=1,
                command=request.command,
                started_at_utc=utc_now_iso(),
                destination=str(run_dir),
                status=DeliveryStatus.IN_PROGRESS,
            )
        ],
    )

    try:
        graded_root, export_failure, ledger_failure = export_and_record_delivery(
            stage_dir, run_dir, delivery, say=say
        )
    except Exception as delivery_error:
        # Lock acquisition and the initial ledger write precede the service's
        # result boundary. They must not hide a build error or cancellation.
        graded_root = stage_dir
        export_failure = delivery_error
        ledger_failure = None
    if export_failure is not None:
        if first_error_record is None:
            first_error_record = {
                "code": str(getattr(export_failure, "code", "DELIVERY_FAILED")),
                "message": str(export_failure),
            }
        try:
            update_status(
                "failed" if terminal_state == "completed" else terminal_state,
                terminal_state,
                export_status="FAILED",
            )
        except Exception:
            pass
    else:
        update_status(terminal_state, terminal_state, export_status="COMPLETED")
    if ledger_failure is not None:
        say(f"warning: failed to update delivery ledger: {ledger_failure}")

    effective_failure = failure or export_failure or ledger_failure
    secondary_failures = [
        error for error in (export_failure, ledger_failure)
        if error is not None and error is not effective_failure
    ]
    try:
        outcome = grade_run_package(
            graded_root, failure=effective_failure, additional_failures=secondary_failures
        )
    except Exception as grading_error:
        # The same storage failure may prevent writing the fallback verdict.
        # Keep the original failure as primary even when that last attempt fails.
        if effective_failure is not None:
            say(f"warning: failed to grade the durable run package: {grading_error}")
            raise effective_failure from grading_error
        raise
    for line in outcome.summary_lines():
        say(line)
    if failure is not None:
        raise failure
    return outcome.exit_code


def grade_run_package(
    root: Path, *, failure: Optional[BaseException] = None,
    additional_failures: Sequence[BaseException] = (),
) -> RunOutcome:
    """Load a run package, grade it once, and record the verdict next to it.

    ``run``, ``report`` and ``recover`` all go through here, so the three can
    not answer differently about the same run. The verdict file is written into
    the directory that was graded and is deliberately *not* part of what an
    export copies: it is derived, and re-deriving it must never look like
    somebody tampering with the evidence.
    """
    package = LoadedRunPackage.load(Path(root))
    verification = verify_run_package(package)
    outcome = evaluate_run(verification)
    errors = ([failure] if failure is not None else []) + list(additional_failures)
    for error in errors:
        outcome.add(
            Finding(
                str(getattr(error, "code", "E2E_EXECUTION_FAILED")),
                str(error),
                details=_error_details(error),
            )
        )
    if errors:
        outcome.recompute_status()
    outcome.write(Path(root) / RUN_RESULT_SHORT_FILENAME)
    outcome.write(Path(root) / RUN_RESULT_FILENAME)
    return outcome


def _error_details(exc: BaseException) -> Dict[str, Any]:
    details = getattr(exc, "details", None)
    return dict(details) if isinstance(details, Mapping) else {}


def _evidence_requirements(
    *,
    lock: RunLock,
    suite_id: str,
    suite_dir: Path,
    manifest: BuildManifest,
) -> "tuple[List[EvidenceRequirement], List[Dict[str, Any]]]":
    """What each selected task owes, read from the lock and the suite plan.

    An unreadable selection is a hard error rather than "no requirements": a
    policy that silently evaporates when it cannot be read is not a policy.
    """
    try:
        suite_plan = load_suite_plan(suite_dir) if Path(suite_dir).is_dir() else None
        return requirements_from_documents(
            lock=lock,
            suite_id=suite_id,
            build_produced_production_wasm=bool(
                (manifest.deployment or {}).get("production_wasm")
            ),
            suite_plan=suite_plan,
        )
    except EvidencePolicyError as exc:
        raise BuildProvenanceError(
            "The evidence this run owes could not be determined from its own selection, "
            f"so it cannot be graded: {exc}",
            {"suite_dir": str(suite_dir)},
        ) from exc


# ---------------------------------------------------------------------------
# source immutability
# ---------------------------------------------------------------------------
def _capture_snapshot(root: Path) -> Dict[str, Any]:
    """Measure one snapshot with the shared, read-only implementation."""
    return source_snapshot.capture(Path(root))


def _measure(
    capture: Callable[[Path], Dict[str, Any]], src: RestoredSource, phase: str
) -> Dict[str, Any]:
    try:
        return capture(src.worktree)
    except source_snapshot.SourceSnapshotError as exc:
        # A snapshot that cannot even be measured proves nothing about itself.
        raise SourceNotPristineError(
            f"The {src.role} snapshot could not be measured ({phase}); an unmeasurable "
            "snapshot is never treated as unchanged.",
            {"role": src.role, "phase": phase, "cause_code": exc.code, "cause": str(exc)},
        ) from exc


def _locked_tree(lock: RunLock, role: str) -> Optional[str]:
    value = lock.source_record(role).get("tree_sha")
    return str(value).lower() if value else None


def _assert_pristine(
    role: str,
    fingerprint: Mapping[str, Any],
    expected_sha: str,
    expected_tree: Optional[str],
    manifest: BuildManifest,
) -> None:
    """Refuse a snapshot that is not exactly the locked commit and tree."""
    problems = source_snapshot.pristine_violations(fingerprint, expected_sha=expected_sha)
    if not expected_tree:
        problems.append({"code": "SOURCE_TREE_UNLOCKED",
                         "reason": "the lock records no tree SHA for this source"})
    elif str(fingerprint.get("tree", "")).lower() != expected_tree:
        problems.append({"code": "SOURCE_TREE_MISMATCH", "expected": expected_tree,
                         "actual": fingerprint.get("tree")})
    if problems:
        details = {"role": role, "expected_sha": expected_sha, "problems": problems}
        manifest.record_failure(
            SourceNotPristineError.code,
            f"The {role} snapshot is not exactly the locked commit",
            details,
        )
        raise SourceNotPristineError(
            f"The {role} source snapshot is not exactly commit {expected_sha}: something was "
            "added, changed or deleted, a submodule drifted, or a file of the retired overlay "
            "mechanism is present. The build is refused.",
            details,
        )


def _record_and_compare(
    capture: Callable[[Path], Dict[str, Any]],
    sources: Mapping[str, RestoredSource],
    before: Mapping[str, Mapping[str, Any]],
    phase: str,
    record: Dict[str, Any],
    manifest: BuildManifest,
) -> None:
    """Measure every snapshot again, record it, and fail on any difference."""
    mutated: Dict[str, Any] = {}
    roles = record.setdefault("roles", {})
    for role, src in sources.items():
        after = _measure(capture, src, phase)
        roles.setdefault(role, {})[phase] = after
        diff = source_snapshot.differences(before[role], after)
        violations = source_snapshot.pristine_violations(after, expected_sha=src.commit_sha)
        if diff or violations:
            mutated[role] = {"differences": diff, "violations": violations}
    record[f"{phase}_verdict"] = "VIOLATED" if mutated else "UNCHANGED"
    record["verdict"] = (
        "VIOLATED"
        if mutated or record.get("verdict") == "VIOLATED"
        else "UNCHANGED"
    )
    if mutated:
        details = {"phase": phase, "mutated": mutated}
        manifest.record_failure(
            SourceSnapshotMutatedError.code,
            f"A source snapshot changed ({phase})",
            details,
        )
        raise SourceSnapshotMutatedError(
            f"A source snapshot changed between the pre-build measurement and {phase}. The "
            "tested tree is no longer the selected commit, so this run cannot pass.",
            details,
        )


def _describe_version(git: GitClient, worktree: Path) -> str:
    """``git describe --always`` in the snapshot, as upstream's Makefiles do."""
    return git.stdout(["-C", str(worktree), "describe", "--always"], timeout=120.0).strip()


def _python_executable() -> str:
    import sys

    return sys.executable or "python3"


def _required_roles(
    gonka_adapter: CompatibilityAdapter,
    contracts_adapter: CompatibilityAdapter,
    context: Mapping[str, str],
) -> List[str]:
    """Every artefact both recipes promised, as recorded by the builder."""
    roles: List[str] = []
    for adapter in (gonka_adapter, contracts_adapter):
        if adapter.build is None:
            continue
        for step in adapter.build.steps:
            for image in step.produces_images:
                resolved = image
                for key, value in context.items():
                    resolved = resolved.replace("{" + key + "}", str(value))
                roles.append(resolved)
            roles.extend(step.produces_files)
    return roles


#: Roles under which the A9 release step records its outputs. They are how the
#: executor tells the production contracts apart from the harness fixtures that
#: the same recipe also builds. The names live in
#: :mod:`ops.a8.e2e.deployment`, which is also what the offline reader uses, so
#: there is exactly one spelling of them in the tree.
A9_MANIFEST_ROLE = _A9_MANIFEST_ROLE
A9_RELEASE_ROLE_PREFIX = _A9_RELEASE_ROLE_PREFIX


# ---------------------------------------------------------------------------
# run directory layout
# ---------------------------------------------------------------------------
# One definition, used by the executor when it runs and by `report`/`recover`
# when they read the result back.
#
#   <output>/<run-id>/                      run_dir
#     run.lock.json                         the preserved plan
#     build-manifest.json
#     execution-manifest.json
#     build/, build-logs/
#     suite/                                suite_export_root
#       <run-id>/                           suite_export_dir  <- the orchestrator
#                                           appends the id itself
#
#   <workspace>/<run-id>/suite/             suite_workspace_root
#     runtime/<task-run-id>/                per-task runtime snapshots
#
# Suite evidence is written directly to the durable package's export root;
# the workspace root holds runtime state, not a second staged suite.


def suite_export_root(run_dir: Path) -> Path:
    """What is handed to the orchestrator as its output directory."""
    return Path(run_dir) / SUITE_EXPORT_DIRNAME


def suite_export_dir(run_dir: Path, suite_id: str) -> Path:
    """Where the exported evidence actually lands."""
    return suite_export_root(run_dir) / suite_id


def suite_workspace_root(workspace_dir: Path) -> Path:
    """What is handed to the orchestrator as its workspace directory."""
    return Path(workspace_dir) / SUITE_EXPORT_DIRNAME





def _a9_manifest_path(manifest: BuildManifest) -> Optional[Path]:
    """Where the verified release manifest of this build landed.

    Read back from the recorded artefact rather than recomputed, so the path
    handed to the suite is provably the one the builder hashed.
    """
    for item in manifest.binaries:
        if str(item.get("role", "")) == A9_MANIFEST_ROLE:
            path = item.get("path")
            if path:
                return Path(str(path))
    return None


def _build_context(
    # NOTE: this also *fills in* ``manifest.deployment``. Deciding which
    # artefacts are handed to the suite and recording which artefacts were
    # handed to the suite are the same decision, so they are made in one place.
    *,
    lock: RunLock,
    layout: RunnerLayout,
    gonka_adapter: CompatibilityAdapter,
    contracts_adapter: CompatibilityAdapter,
    gonka_src: RestoredSource,
    contracts_src: RestoredSource,
    runner_image_id: str,
    manifest: BuildManifest,
    gradle_user_home: Optional[Path] = None,
) -> E2ERunContext:
    expected_runtime: Dict[str, str] = {}
    for expectation in gonka_adapter.runtime:
        value = expectation.expected_value(
            sources_root=gonka_src.worktree, selected_sha=gonka_src.commit_sha
        )
        if value:
            expected_runtime[expectation.field_name] = value

    # Separate the production release artefacts from the harness fixtures. Only
    # the former are deployed as the contracts under test; conflating them would
    # let a fixture hash stand in for a production one.
    production_wasm = [
        item for item in manifest.wasm if str(item.get("role", "")).startswith(A9_RELEASE_ROLE_PREFIX)
    ]
    fixture_wasm = [
        item for item in manifest.wasm if not str(item.get("role", "")).startswith(A9_RELEASE_ROLE_PREFIX)
    ]
    a9_manifest_path = _a9_manifest_path(manifest)
    manifest.deployment = {
        "a9_manifest_path": str(a9_manifest_path) if a9_manifest_path else None,
        "a9_manifest_sha256": next(
            (
                str(item.get("sha256"))
                for item in manifest.binaries
                if str(item.get("role", "")) == A9_MANIFEST_ROLE
            ),
            None,
        ),
        "production_wasm": [
            {"role": item.get("role"), "sha256": item.get("sha256")} for item in production_wasm
        ],
        "fixture_wasm": [
            {"role": item.get("role"), "sha256": item.get("sha256")} for item in fixture_wasm
        ],
        "note": (
            "The suite is given --manifest, so it deploys these production artefacts instead "
            "of starting a second release build. The fixtures are harness-side contracts and "
            "are never presented as production evidence."
        ),
    }

    return E2ERunContext(
        harness_script=layout.harness_script,
        gradle_user_home=gradle_user_home,
        a9_manifest_path=a9_manifest_path,
        gonka_requested_sha=gonka_src.commit_sha,
        contracts_requested_sha=contracts_src.commit_sha,
        expected_gonka_sha=gonka_src.commit_sha,
        expected_marketplace_sha=contracts_src.commit_sha,
        # None: the harness uses its documented default next to the Gonka
        # snapshot, which is outside both snapshots by construction.
        work_root=None,
        testermint_harness_dir=layout.testermint_harness_dir,
        expected_runtime=expected_runtime,
        # Stated here as well as defaulted on the dataclass: the evidence this
        # run produces must be graded by the immutable-source rules and must
        # not be able to fall back to the historical ones.
        evidence_model=EVIDENCE_MODEL_IMMUTABLE,
        adapter_id=gonka_adapter.adapter_id,
        contracts_adapter_id=contracts_adapter.adapter_id,
        plan_id=lock.plan_id,
        lock_sha256=lock.lock_sha256,
        runner_image_id=runner_image_id,
        runner_version=str(lock.runner.get("runner_version") or "") or None,
        provenance={
            "gonka_commit_url": lock.gonka.get("commit_url"),
            "contracts_commit_url": lock.contracts.get("commit_url"),
            "built_images": [
                {"role": item.get("role"), "image_id": item.get("image_id")}
                for item in manifest.images
            ],
            "wasm": [
                {"role": item.get("role"), "sha256": item.get("sha256")}
                for item in manifest.wasm
            ],
        },
    )


def _default_suite_runner(
    *,
    contracts_dir: Path,
    gonka_dir: Path,
    output_dir: Path,
    workspace_dir: Path,
    runtime_root: Optional[Path],
    suite_id: str,
    tasks: Sequence[Any],
    profile: Optional[str] = None,
    scenarios: Optional[Sequence[str]] = None,
    keep_resources: bool,
    e2e_context: E2ERunContext,
    progress_callback: Optional[Callable[[str, str], None]] = None,
) -> int:
    """Delegate execution of the pre-resolved tasks to the suite orchestrator.

    Imported lazily so that planning, listing and reporting never pull in the
    execution machinery, and so that tests can substitute this callable.
    """
    from ..orchestrator import SuiteOrchestrator

    orchestrator = SuiteOrchestrator(
        marketplace_dir=contracts_dir,
        gonka_dir=gonka_dir,
        output_dir=output_dir,
        runtime_root=runtime_root,
        workspace_dir=workspace_dir,
        e2e_context=e2e_context,
        use_direct_sources=True,
    )
    return orchestrator.run_suite(
        tasks=tasks,
        profile=profile,
        requested_scenarios=scenarios,
        suite_id=suite_id,
        keep_resources=keep_resources,
        progress_callback=progress_callback,
    )


# ---------------------------------------------------------------------------
# post-run provenance
# ---------------------------------------------------------------------------
def _verify_post_run_provenance(
    suite_dir: Path,
    *,
    manifest: BuildManifest,
    adapter: CompatibilityAdapter,
    sources_root: Path,
    selected_sha: str,
    requirements: Sequence[EvidenceRequirement],
    emit: Callable[[str], None],
    platform: Optional[str] = None,
    marketplace_sha: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-check, offline, that the network really ran what was built.

    The harness already fails fast on a wrong running version while the network
    is up. This second pass reads the evidence the run left behind, so the same
    claim is checkable by a reviewer afterwards without any live system.

    Which evidence is read is decided by ``requirements`` -- the duties derived
    from the *selection* -- and each task's evidence is looked for in that
    task's own directory. A recursive search would let one task's live context
    answer for another's, and would ask a boundary probe that never starts a
    chain for a live context it was never supposed to write.
    """
    observed: Dict[str, Any] = {
        "live_contexts": [],
        "containers": [],
        "deployment": [],
        "source_immutability": [],
        "missing_evidence": [],
        "policy": [requirement.to_dict() for requirement in requirements],
    }

    expected = [req for req in requirements if req.requires_live_context]
    if not expected:
        emit(
            "No selected task starts a live network, so this run owes no live-context "
            "evidence. Its boundary artefacts are graded by the suite reporter."
        )
    for requirement in expected:
        context_file = requirement.live_context_path(suite_dir)
        if not context_file.is_file():
            # Absence is a finding about *this* task, not a reason to look
            # elsewhere for something that might pass in its place.
            observed["missing_evidence"].append(
                {
                    "task_id": requirement.task_id,
                    "run_id": requirement.run_id,
                    "expected_path": str(
                        context_file.relative_to(suite_dir)
                        if _is_relative_to(context_file, suite_dir)
                        else context_file
                    ),
                    "reason": "the task recorded no live-context.json",
                }
            )
            continue
        try:
            payload = json.loads(context_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityError(
                "A live-context.json in the evidence is unreadable",
                {"path": str(context_file), "cause": str(exc)},
            ) from exc
        runtime = ((payload.get("source") or {}).get("runtime")) or {}
        comparisons = verify_observed_runtime(
            adapter,
            runtime,
            sources_root=sources_root,
            selected_sha=selected_sha,
        )
        relative = str(context_file.relative_to(suite_dir))

        # The harness' own before/after measurement of both snapshots around
        # the Testermint run. Absent => incomplete, never a pass; changed =>
        # the run is refused outright.
        immutability_file = context_file.parent / SOURCE_IMMUTABILITY_FILENAME
        document: Optional[bytes] = None
        if immutability_file.is_file() and not immutability_file.is_symlink():
            document = immutability_file.read_bytes()
        network_file = context_file.parent / "network/network-manifest.json"
        network_document = (
            network_file.read_bytes()
            if network_file.is_file() and not network_file.is_symlink() else None
        )
        immutability = verify_source_immutability_evidence(
            document, payload, gonka_sha=selected_sha, marketplace_sha=marketplace_sha,
            network_manifest=network_document,
        )
        immutability["task_id"] = requirement.task_id
        immutability["path"] = str(immutability_file.relative_to(suite_dir))
        observed["source_immutability"].append(immutability)
        if immutability["status"] == _DEPLOYMENT_MISMATCHED:
            raise SourceSnapshotMutatedError(
                "The harness reports that a source snapshot changed during the live run, or "
                "names a commit other than the selected one.",
                {"live_context": relative, "mismatches": immutability["mismatches"]},
            )
        if immutability["status"] == _DEPLOYMENT_INCOMPLETE:
            observed["missing_evidence"].append(
                {
                    "task_id": requirement.task_id,
                    "run_id": requirement.run_id,
                    "expected_path": immutability["path"],
                    "reason": "source immutability evidence is incomplete: "
                    + ", ".join(immutability["missing"]),
                }
            )
        observed["live_contexts"].append(
            {
                "task_id": requirement.task_id,
                "path": relative,
                "runtime": dict(runtime),
                "comparisons": comparisons,
            }
        )
        if not requirement.requires_deployment_evidence:
            continue
        report = verify_deployed_artifacts(
            payload,
            built_wasm=manifest.wasm,
            expected_manifest_sha256=(manifest.deployment or {}).get("a9_manifest_sha256"),
        )
        report["task_id"] = requirement.task_id
        report["path"] = relative
        observed["deployment"].append(report)
        if report["mismatches"]:
            raise BuildProvenanceError(
                "The contracts deployed by the suite are not the ones this build produced. "
                "The build manifest would otherwise describe artefacts that were never tested.",
                {"live_context": relative, "mismatches": report["mismatches"]},
            )

    containers, missing = verify_ownership_evidence(
        suite_dir, requirements=requirements, build=manifest,
        runtime_external_images=adapter.build.runtime_external_images if adapter.build else {},
        platform=platform,
    )
    observed["containers"] = containers
    observed["missing_evidence"].extend(missing)
    return observed


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        Path(path).relative_to(Path(root))
    except ValueError:
        return False
    return True


__all__ = [
    "ExecutionRequest",
    "RestoredSource",
    "assert_runner_matches_lock",
    "execute_plan",
    "export_and_record_delivery",
    "generate_run_id",
    "grade_run_package",
    "rebuild_adapter",
    "restore_source",
]
