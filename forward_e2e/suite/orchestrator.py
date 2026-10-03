"""Sequential suite orchestrator for A8 test execution and evidence collection.

Guarantees:
- Fixed Git SHAs across the entire suite (exported once, cloned per step)
- Single global lock held across the entire suite without gaps or self-deadlock
- Per-task single-use clean snapshots
- Scenario failures are recorded and later tasks continue after safe cleanup
- Infrastructure/provenance failures halt the suite (remaining tasks NOT_RUN)
- KeepResources halts sequence, skips cleanup, marks remainder NOT_RUN, returns 1
- Full journal and metadata written to disk with heartbeat streaming
- Exit codes: 0 = all pass; 1 = failure / incomplete / kept; 2 = invalid params; 130 = cancel
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import random
import re
import signal
import string
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .adapters import BoundaryTaskAdapter, NativeTaskAdapter
from .catalog import (
    compute_catalog_hash,
    get_task_by_id_or_alias,
)
from .collector import (
    ALLOWED_ARTIFACT_PATTERNS,
    CollectorSecurityError,
    classify_artifact_kind,
    collect_snapshot_artifacts,
    copy_runtime_artifact,
    is_path_safe,
    sha256_checked_artifact,
    update_artifact_index,
    validate_artifact_index_record,
)
from .evidence_model import EVIDENCE_MODEL_IMMUTABLE
from .lock import LockContentionError, LockOperationError, RuntimeLock
from .models import (
    AcceptanceStatus,
    ArtifactEntry,
    CleanupStatus,
    EventRecord,
    EvidenceStatus,
    ExecutionStatus,
    ProofLevel,
    SourceIdentity,
    SuitePlan,
    SuiteResult,
    TaskPlan,
    TaskResult,
    calculate_suite_outcome,
    make_task_run_id,
)
from .reporter import OfflineReporter
from .source_snapshot import assert_pristine, capture
from .runtime import (
    SuiteRuntimeError,
    RuntimeSnapshot,
    create_git_bundle,
    get_default_runtime_root,
    get_git_clean_head,
    git_tree_sha,
    perform_runtime_cleanup,
    prepare_runtime_snapshot,
    sha256_file,
)
from .verifier import evaluate_task_evidence, verify_and_recalculate_suite


# make_task_run_id is defined in models and re-exported through this import, so
# that the orchestrator, the recovery path, and the offline reporter all derive
# run directory names from one single formula.


def generate_suite_id() -> str:
    """Generate compliant unique Suite ID with timestamp and random suffix (max 65 chars)."""
    now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    rand_suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"suite-{now_str}-{rand_suffix}"


def compute_runner_hash(ops_dir: Path) -> str:
    """Combined SHA256 of the runner's Python sources under ``forward_e2e``.

    Subpackages are included: the E2E layer lives in ``forward_e2e/execution`` and very
    much decides what a run does, so leaving it out would let the runner change
    while its recorded identity stayed the same. Paths are hashed alongside the
    bytes so that moving code between files is visible too.
    """
    import hashlib
    root = Path(ops_dir)
    if root.is_symlink() or not root.is_dir():
        raise SuiteRuntimeError(f"Runner Python source root is missing or a symlink: {root}")
    sources: List[Path] = []

    def fail_walk(error: OSError) -> None:
        raise SuiteRuntimeError(f"Cannot enumerate runner Python sources: {error}") from error

    for current, dirnames, filenames in os.walk(root, followlinks=False, onerror=fail_walk):
        current_path = Path(current)
        relative_dir = current_path.relative_to(root)
        visible_dirs = []
        for name in dirnames:
            if name == "__pycache__" or (relative_dir == Path(".") and name == "tests"):
                continue
            directory = current_path / name
            if directory.is_symlink():
                raise SuiteRuntimeError(f"Runner Python source directory is a symlink: {directory}")
            visible_dirs.append(name)
        dirnames[:] = sorted(visible_dirs)
        for name in filenames:
            if not name.endswith(".py"):
                continue
            source = current_path / name
            if source.is_symlink() or not source.is_file():
                raise SuiteRuntimeError(f"Runner Python source is a symlink or not a file: {source}")
            sources.append(source)
    h = hashlib.sha256()
    for py_file in sorted(sources):
        relative = py_file.relative_to(root).as_posix()
        h.update(relative.encode("utf-8"))
        h.update(b"\0")
        h.update(py_file.read_bytes())
    return h.hexdigest()


def reconcile_suite_runtime_evidence(
    suite_dir: Path, runtime_root: Path, suite_id: str
) -> Tuple[bool, List[str]]:
    """Reconcile un-staged raw runtime artifacts from interrupted steps directly into suite_dir.

    Returns (ok, errors).
    - Fails closed if any symlink or path traversal is found in suite_dir or runtime_root before any write.
    - Verifies all existing indexed artifacts in suite_dir against artifact-index.json.
    - Checks any runtime files corresponding to existing collected/indexed artifacts and refuses to overwrite on hash mismatch.
    - Supplements only provably incomplete task collections (where task result.json or suite-result.json is missing).
    """
    if not suite_dir.exists() and not suite_dir.is_symlink():
        return True, []

    # 1. Pre-flight boundary and symlink safety check BEFORE any file reads/writes
    resolved_suite = suite_dir.resolve()
    if suite_dir.is_symlink() or not resolved_suite.is_dir():
        return False, [f"Security: suite directory is a symlink or not a directory: {suite_dir}"]

    _SYSTEM_SYMLINK_ALLOWLIST = frozenset(
        {
            Path("/var"),
            Path("/tmp"),
            Path("/etc"),
            Path("/private/var"),
            Path("/private/tmp"),
            Path("/private/etc"),
        }
    )

    parent_check = suite_dir
    while parent_check != parent_check.parent:
        if parent_check not in _SYSTEM_SYMLINK_ALLOWLIST and parent_check.is_symlink():
            return False, [f"Security: suite directory parent is a symlink: {parent_check}"]
        parent_check = parent_check.parent

    for item in suite_dir.rglob("*"):
        if item.is_symlink():
            return False, [f"Security: suite directory contains a symlink: {item}"]
        try:
            item.resolve().relative_to(resolved_suite)
        except (ValueError, RuntimeError):
            return False, [f"Security: item resolves outside suite directory: {item}"]

    if runtime_root.is_symlink():
        return False, [f"Security: runtime root is a symlink: {runtime_root}"]
    runtime_parent = runtime_root
    while runtime_parent != runtime_parent.parent:
        if runtime_parent not in _SYSTEM_SYMLINK_ALLOWLIST and runtime_parent.is_symlink():
            return False, [f"Security: runtime root parent is a symlink: {runtime_parent}"]
        runtime_parent = runtime_parent.parent
    if runtime_root.is_dir():
        resolved_runtime = runtime_root.resolve()
        for item in runtime_root.rglob("*"):
            if item.is_symlink():
                return False, [f"Security: runtime directory contains a symlink: {item}"]
            try:
                item.resolve().relative_to(resolved_runtime)
            except (ValueError, RuntimeError):
                return False, [f"Security: runtime item resolves outside runtime root: {item}"]

    # 2. Verify existing indexed artifacts in suite_dir
    indexed_artifacts: Dict[str, str] = {}
    index_file = suite_dir / "artifact-index.json"
    if index_file.is_file():
        try:
            index_data = json.loads(index_file.read_text(encoding="utf-8"))
            if not isinstance(index_data, dict) or not isinstance(index_data.get("artifacts"), list):
                return False, ["Integrity error: artifact-index.json missing 'artifacts' list"]
            if index_data.get("schema_version") != "1.0.0":
                return False, ["Integrity error: unsupported artifact-index.json schema"]
            if type(index_data.get("total_artifacts")) is not int or index_data["total_artifacts"] != len(index_data["artifacts"]):
                return False, ["Integrity error: artifact-index.json count mismatch"]
            for entry in index_data["artifacts"]:
                rel_p = validate_artifact_index_record(entry)
                if rel_p in indexed_artifacts:
                    return False, [f"Integrity error: duplicate artifact-index.json path: {rel_p}"]
                sha = entry["sha256"]
                indexed_artifacts[rel_p] = sha
        except Exception as exc:
            return False, [f"Integrity error: failed to parse artifact-index.json: {exc}"]

    for rel_p, expected_sha in indexed_artifacts.items():
        disk_path = suite_dir / rel_p
        if disk_path.is_symlink():
            return False, [f"Security: indexed artifact is a symlink: {disk_path}"]
        if not disk_path.is_file():
            return False, [f"Integrity error: indexed artifact missing from suite: {rel_p}"]
        try:
            actual_sha = sha256_checked_artifact(suite_dir, disk_path)
        except CollectorSecurityError as exc:
            return False, [str(exc)]
        if actual_sha != expected_sha:
            return False, [
                f"Integrity error: indexed artifact {rel_p} sha256 mismatch "
                f"(expected {expected_sha}, actual {actual_sha})"
            ]

    # 3. Determine expected runs from suite-plan.json and events.jsonl
    plan: Optional[SuitePlan] = None
    expected_runs: Dict[str, str] = {}
    plan_file = suite_dir / "suite-plan.json"
    if plan_file.is_file():
        try:
            plan_data = json.loads(plan_file.read_text(encoding="utf-8"))
            plan = SuitePlan.from_dict(plan_data)
            for t in plan.tasks:
                expected_run_id = make_task_run_id(suite_id, t.ordinal, t.task_id)
                expected_runs[expected_run_id] = t.task_id
        except Exception as exc:
            print(f"Warning: failed to parse suite-plan.json during recovery: {exc}", file=sys.stderr)

    events_file = suite_dir / "events.jsonl"
    if events_file.is_file():
        try:
            for line in events_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                ev = json.loads(line)
                r_id = ev.get("run_id")
                t_id = ev.get("task_id")
                if r_id and t_id:
                    expected_runs[r_id] = t_id
        except Exception:
            pass

    suite_result_exists = (suite_dir / "suite-result.json").is_file()
    all_planned_tasks_have_result = bool(expected_runs) and all(
        (suite_dir / "runs" / r_id / "result.json").is_file() for r_id in expected_runs
    )
    suite_collection_complete = suite_result_exists and all_planned_tasks_have_result

    short_sid = suite_id[:30]
    new_recovered_entries: List[ArtifactEntry] = []

    candidate_runtime_dirs: List[Path] = []
    if runtime_root.is_dir():
        for child in sorted(runtime_root.iterdir()):
            if child.name == "runs" and child.is_dir():
                candidate_runtime_dirs.extend(sorted(p for p in child.iterdir() if p.is_dir()))
            elif child.is_dir():
                candidate_runtime_dirs.append(child)

    for r_dir in candidate_runtime_dirs:
        cand_run_id = r_dir.name
        belongs_to_suite = (
            cand_run_id in expected_runs
            or cand_run_id == suite_id
            or cand_run_id.startswith(f"{short_sid}-")
        )
        if not belongs_to_suite:
            continue

        ident_file = r_dir / "identity.json"
        task_id = expected_runs.get(cand_run_id)
        if ident_file.is_file():
            try:
                ident = json.loads(ident_file.read_text(encoding="utf-8"))
                rec_run_id = ident.get("run_id")
                if rec_run_id and rec_run_id != cand_run_id:
                    print(f"Warning: skipping candidate directory {r_dir.name} due to identity run_id mismatch", file=sys.stderr)
                    continue
                if plan and plan.source_identity:
                    m_sha = ident.get("marketplace_commit_sha")
                    if plan.source_identity.marketplace_commit_sha and m_sha and m_sha != plan.source_identity.marketplace_commit_sha:
                        print(f"Warning: skipping candidate directory {r_dir.name} due to marketplace commit SHA mismatch", file=sys.stderr)
                        continue
            except Exception as exc:
                print(f"Warning: failed to read {ident_file}: {exc}", file=sys.stderr)
                continue

        if not task_id:
            prefix = f"{short_sid}-"
            if cand_run_id.startswith(prefix):
                remainder = cand_run_id[len(prefix):]
                parts = remainder.split("-", 1)
                if len(parts) == 2 and parts[0].isdigit():
                    task_id = parts[1]
                else:
                    task_id = remainder
            else:
                task_id = "unknown"

        dest_run_dir = suite_dir / "runs" / cand_run_id
        task_collection_complete = (dest_run_dir / "result.json").is_file() and suite_collection_complete

        for pattern in ALLOWED_ARTIFACT_PATTERNS:
            for src_art in r_dir.glob(pattern):
                if not (src_art.is_file() and is_path_safe(r_dir, src_art)):
                    continue
                art_rel = src_art.relative_to(r_dir).as_posix()
                rel_path = f"runs/{cand_run_id}/{art_rel}"
                dest_art = dest_run_dir / art_rel

                # Always check against existing indexed or collected file; never overwrite on mismatch
                if rel_path in indexed_artifacts or dest_art.exists() or dest_art.is_symlink():
                    if dest_art.is_symlink():
                        return False, [f"Security: destination artifact path is a symlink: {dest_art}"]
                    try:
                        src_sha = sha256_checked_artifact(r_dir, src_art)
                        dest_sha = sha256_checked_artifact(suite_dir, dest_art) if dest_art.is_file() else None
                    except CollectorSecurityError as exc:
                        return False, [str(exc)]
                    if rel_path in indexed_artifacts:
                        expected_sha = indexed_artifacts[rel_path]
                        if src_sha != expected_sha or (dest_sha is not None and dest_sha != expected_sha):
                            return False, [
                                f"Integrity error: runtime artifact {rel_path} (sha256={src_sha}) "
                                f"conflicts with already-collected suite artifact (sha256={expected_sha}). "
                                "Refusing to overwrite existing evidence."
                            ]
                        continue

                    # dest_art exists on disk in suite_dir, but its entry was not yet written to artifact-index.json
                    if dest_sha is not None and src_sha != dest_sha:
                        return False, [
                            f"Integrity error: runtime artifact {rel_path} (sha256={src_sha}) "
                            f"conflicts with already-collected suite artifact (sha256={dest_sha}). "
                            "Refusing to overwrite existing evidence."
                        ]
                    if not task_collection_complete and dest_art.is_file() and dest_sha is not None:
                        entry = ArtifactEntry(
                            relative_path=rel_path,
                            size_bytes=dest_art.stat().st_size,
                            sha256=dest_sha,
                            run_id=cand_run_id,
                            task_id=task_id,
                            artifact_kind=classify_artifact_kind(art_rel),
                        )
                        new_recovered_entries.append(entry)
                        indexed_artifacts[rel_path] = dest_sha
                    continue

                # Only supplement missing artifacts if task/suite collection was provably incomplete
                if task_collection_complete:
                    continue

                try:
                    entry = copy_runtime_artifact(r_dir, src_art, suite_dir, cand_run_id, task_id)
                except CollectorSecurityError as exc:
                    return False, [str(exc)]
                new_recovered_entries.append(entry)
                indexed_artifacts[rel_path] = entry.sha256

    # 4. Only update artifact index or regenerate suite reports if incomplete collection was supplemented
    #    or if suite-result.json is missing.
    if new_recovered_entries:
        update_artifact_index(suite_dir, new_recovered_entries)

    if new_recovered_entries or not suite_result_exists:
        try:
            verification = verify_and_recalculate_suite(suite_dir)
            reporter = OfflineReporter(suite_dir)
            reporter.write_reports(
                verification.result,
                integrity_errors=verification.integrity_errors,
                write_suite_result=True,
            )
            refreshed_reports: List[ArtifactEntry] = []
            for rep_rel, rep_kind in (
                ("suite-result.json", "SUITE_RESULT_JSON"),
                ("summary.md", "REPORT_MARKDOWN"),
            ):
                rep_path = suite_dir / rep_rel
                if rep_rel in indexed_artifacts and rep_path.is_file():
                    refreshed_reports.append(
                        ArtifactEntry(
                            relative_path=rep_rel,
                            size_bytes=rep_path.stat().st_size,
                            sha256=sha256_file(rep_path),
                            run_id=suite_id,
                            task_id="suite",
                            artifact_kind=rep_kind,
                        )
                    )
            if refreshed_reports:
                update_artifact_index(suite_dir, refreshed_reports)
        except Exception as exc:
            print(f"Warning: offline report generation in staged suite failed: {exc}", file=sys.stderr)

    return True, []


class SuiteOrchestrator:
    """Orchestrates plan, run, and reporting workflows."""

    def __init__(
        self,
        marketplace_dir: Path,
        gonka_dir: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        runtime_root: Optional[Path] = None,
        workspace_dir: Optional[Path] = None,
        distro: str = "Ubuntu",
        e2e_context: Optional[Any] = None,
        use_direct_sources: bool = False,
    ):
        if e2e_context is None:
            raise ValueError("SuiteOrchestrator requires an explicit e2e_context")
        # forward_e2e.execution.context.E2ERunContext describing an explicit-SHA plan:
        # which harness to run and which commits the user selected (and which,
        # under the immutable-source model, are exactly the commits under
        # test). Read by attribute only, so this module never imports the E2E
        # package that builds on top of it.
        self.e2e_context = e2e_context
        self.use_direct_sources = bool(use_direct_sources)
        self.marketplace_dir = Path(marketplace_dir).resolve()
        self.gonka_dir = Path(gonka_dir).resolve() if gonka_dir else None

        from forward_e2e.suite.runtime import resolve_suite_env

        env_workspace = resolve_suite_env("E2E_WORKSPACE_DIR", "A8_WORKSPACE_DIR")
        if workspace_dir:
            self.workspace_dir: Optional[Path] = Path(workspace_dir).resolve()
        elif env_workspace:
            self.workspace_dir = Path(env_workspace).resolve()
        else:
            self.workspace_dir = None

        env_output = resolve_suite_env("E2E_OUTPUT_DIR", "A8_OUTPUT_DIR")
        if output_dir:
            self.output_dir = Path(output_dir).resolve()
        elif env_output:
            self.output_dir = Path(env_output).resolve()
        else:
            self.output_dir = (self.marketplace_dir / "artifacts" / "a8-suites").resolve()

        if runtime_root:
            self.runtime_root = Path(runtime_root).resolve()
        elif self.workspace_dir:
            self.runtime_root = (self.workspace_dir / "runtime").resolve()
        else:
            self.runtime_root = get_default_runtime_root()

        self.distro = distro
        self.ops_dir = Path(__file__).resolve().parents[1]
        self.active_lock: Optional[RuntimeLock] = None
        self.current_adapter = None
        self.interrupted: bool = False
        self.interrupted_signal: Optional[int] = None

    def run_suite(
        self,
        tasks: Sequence[TaskPlan],
        *,
        profile: Optional[str] = None,
        requested_scenarios: Optional[Sequence[str]] = None,
        suite_id: Optional[str] = None,
        keep_resources: bool = False,
        progress_callback: Optional[Callable[[str, str], None]] = None,
    ) -> int:
        """Executes the full managed lifecycle for the pre-resolved tasks."""
        # 1. Validate inputs and pre-resolved tasks against catalog
        if not self.gonka_dir:
            print("Error: -GonkaDir is required for run action", file=sys.stderr)
            return 2

        if not tasks:
            print("Parameter validation error: tasks list cannot be empty", file=sys.stderr)
            return 2

        validated_tasks: List[TaskPlan] = []
        for idx, task in enumerate(tasks, start=1):
            if not isinstance(task, TaskPlan):
                print(f"Parameter validation error: invalid task entry at index {idx}", file=sys.stderr)
                return 2
            cat_task = get_task_by_id_or_alias(task.task_id)
            if cat_task is None or cat_task.task_id != task.task_id:
                print(f"Parameter validation error: unknown task {task.task_id!r}", file=sys.stderr)
                return 2
            if cat_task.proof_level != task.proof_level:
                print(
                    f"Parameter validation error: proof level mismatch for {task.task_id!r} "
                    f"(expected {cat_task.proof_level.value}, got {task.proof_level.value})",
                    file=sys.stderr,
                )
                return 2
            if task.ordinal != idx:
                print(
                    f"Parameter validation error: non-sequential task ordinal {task.ordinal} at index {idx}",
                    file=sys.stderr,
                )
                return 2
            validated_tasks.append(task)

        tasks = validated_tasks
        resolved_prof = profile
        resolved_scenarios = (
            list(requested_scenarios)
            if requested_scenarios is not None
            else ([t.task_id for t in tasks] if not profile else None)
        )

        # 2. Check Suite ID format and uniqueness
        sid = suite_id.strip() if suite_id else generate_suite_id()
        if not re.match(r"^[a-zA-Z0-9_-]{1,65}$", sid):
            print(f"Error: Invalid SuiteId {sid!r}. Must match '^[a-zA-Z0-9_-]{{1,65}}$'", file=sys.stderr)
            return 2

        suite_target_dir = self.output_dir / sid
        try:
            suite_target_dir.resolve().relative_to(self.output_dir.resolve())
        except (ValueError, RuntimeError):
            print(f"Error: Suite directory {suite_target_dir} escapes output directory", file=sys.stderr)
            return 2

        if suite_target_dir.exists():
            print(f"Error: Suite directory {suite_target_dir} already exists. Refusing to overwrite.", file=sys.stderr)
            return 2

        # 3. Read and freeze clean source HEADs and their Git trees
        try:
            m_sha = get_git_clean_head(self.marketplace_dir)
            g_sha = get_git_clean_head(self.gonka_dir)
        except SuiteRuntimeError as exc:
            print(f"Source repository cleanliness error: {exc}", file=sys.stderr)
            return 2

        # The immutable-source model has one Gonka commit, not two: the
        # checkout handed to the suite must BE the selected commit. A checkout
        # at any other commit is a tree nobody selected (the retired "prepared"
        # commit was exactly that), so the suite refuses to start rather than
        # recording two names for what it tests.
        e2e_selected_gonka_sha = getattr(self.e2e_context, "gonka_requested_sha", None)
        if e2e_selected_gonka_sha and e2e_selected_gonka_sha != g_sha:
            print(
                f"Source identity error: the Gonka checkout is at {g_sha}, but the plan selected "
                f"{e2e_selected_gonka_sha}; the immutable-source runner never tests another tree",
                file=sys.stderr,
            )
            return 2
        e2e_selected_contracts_sha = getattr(self.e2e_context, "contracts_requested_sha", None)
        if e2e_selected_contracts_sha and e2e_selected_contracts_sha != m_sha:
            print(
                f"Source identity error: the marketplace checkout is at {m_sha}, but the plan "
                f"selected {e2e_selected_contracts_sha}",
                file=sys.stderr,
            )
            return 2
        try:
            g_tree = git_tree_sha(self.gonka_dir)
            m_tree = git_tree_sha(self.marketplace_dir)
        except SuiteRuntimeError as exc:
            print(f"Source tree identity error: {exc}", file=sys.stderr)
            return 2

        source_id = SourceIdentity(
            marketplace_commit_sha=m_sha,
            gonka_commit_sha=g_sha,
            runner_version_hash=compute_runner_hash(self.ops_dir),
            catalog_version_hash=compute_catalog_hash(),
            gonka_tree_sha=g_tree,
            marketplace_tree_sha=m_tree,
        )

        plan = SuitePlan(
            schema_version="1.0.0",
            suite_id=sid,
            created_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
            profile=resolved_prof,
            requested_scenarios=resolved_scenarios,
            source_identity=source_id,
            tasks=tasks,
        )

        # 4. Initialize Suite Target Directory, Plan, and Temporary Bundles
        suite_target_dir.mkdir(parents=True, exist_ok=False)
        plan_file = suite_target_dir / "suite-plan.json"
        plan_file.write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")

        # Under an E2E plan the suite evidence carries the plan identity, the
        # lock hash and the clickable commit links, so that a reviewer reading
        # only the exported evidence can tell exactly which sources were proven.
        if self.e2e_context is not None and hasattr(self.e2e_context, "to_dict"):
            (suite_target_dir / "e2e-context.json").write_text(
                json.dumps(self.e2e_context.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        events_file = suite_target_dir / "events.jsonl"

        def log_event(phase: str, event_type: str, msg: str, run_id: Optional[str] = None, task_id: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
            ev = EventRecord(
                timestamp_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                suite_id=sid,
                run_id=run_id,
                task_id=task_id,
                phase=phase,
                event_type=event_type,
                message=msg,
                details=details or {},
            )
            with events_file.open("a", encoding="utf-8") as f:
                f.write(ev.to_json_line())

        log_event("INIT", "SUITE_STARTED", f"Started A8 suite {sid}", details=plan.to_dict())

        suite_m_bundle: Optional[Path] = None
        suite_g_bundle: Optional[Path] = None
        if not self.use_direct_sources:
            # Create suite Git bundles once in runtime_root to guarantee identical snapshot pairs across all steps
            # without polluting the durable suite evidence directory.
            suite_bundles_dir = (self.runtime_root / "bundles" / sid).resolve()
            suite_bundles_dir.mkdir(parents=True, exist_ok=True)
            suite_m_bundle = suite_bundles_dir / "marketplace.bundle"
            suite_g_bundle = suite_bundles_dir / "gonka.bundle"
            try:
                create_git_bundle(self.marketplace_dir, suite_m_bundle)
                create_git_bundle(self.gonka_dir, suite_g_bundle)
            except Exception as exc:
                log_event("INIT", "BUNDLE_FAILED", str(exc))
                print(f"Error creating suite Git bundles: {exc}", file=sys.stderr)
                return 2

        # 5. Acquire global flock for entire suite
        self.active_lock = RuntimeLock(self.runtime_root / "exclusive.lock")
        try:
            self.active_lock.acquire()
        except LockContentionError as exc:
            log_event("LOCK", "LOCK_CONTENTION", str(exc))
            print(f"Lock contention: {exc}", file=sys.stderr)
            return 1
        except LockOperationError as exc:
            log_event("LOCK", exc.code, str(exc))
            print(f"Runtime lock error: {exc}", file=sys.stderr)
            return exc.exit_code

        # Register SIGINT and SIGTERM handlers
        self.interrupted = False
        self.interrupted_signal = None

        def handle_signal(signum, frame):
            sig_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
            print(f"\nReceived {sig_name}. Cancelling suite safely...", file=sys.stderr)
            self.interrupted = True
            self.interrupted_signal = 130 if signum == signal.SIGINT else 143
            if self.current_adapter and hasattr(self.current_adapter, "terminate_own_tree"):
                try:
                    self.current_adapter.terminate_own_tree()
                except Exception:
                    pass

        old_sigint = signal.signal(signal.SIGINT, handle_signal)
        old_sigterm = signal.signal(signal.SIGTERM, handle_signal)

        task_results: List[TaskResult] = []
        stop_further_tasks = False
        stop_reason = "Suite cancelled by operator"

        try:
            for task in tasks:
                run_id = make_task_run_id(sid, task.ordinal, task.task_id)

                if stop_further_tasks or self.interrupted:
                    # Mark remaining tasks as NOT_RUN
                    tr = TaskResult(
                        task_id=task.task_id,
                        ordinal=task.ordinal,
                        run_id=run_id,
                        proof_level=task.proof_level,
                        execution_status=ExecutionStatus.NOT_RUN,
                        evidence_status=EvidenceStatus.NOT_COLLECTED,
                        cleanup_status=CleanupStatus.NOT_NEEDED,
                        acceptance_status=AcceptanceStatus.NOT_REVIEWED,
                        start_time_utc=None,
                        end_time_utc=None,
                        duration_seconds=None,
                        phase="NOT_RUN",
                        exit_code=None,
                        primary_failure=f"Skipped: {stop_reason}",
                        secondary_errors=[],
                        expected_cases=task.expected_checkpoints,
                        observed_passed_cases=[],
                        missing_cases=task.expected_checkpoints,
                        missing_artifacts=task.expected_artifacts,
                        raw_evidence_dir=None,
                        exported_evidence_dir=None,
                        raw_execution_status=ExecutionStatus.NOT_RUN,
                    )
                    task_results.append(tr)
                    log_event("NOT_RUN", "TASK_SKIPPED", f"Task {task.task_id} skipped", run_id=run_id, task_id=task.task_id)
                    continue

                print(f"\n>>> [{task.ordinal}/{len(tasks)}] Starting task {task.task_id} ({task.proof_level.value})")
                if progress_callback is not None:
                    progress_callback("started", task.task_id)
                log_event("TASK_PREPARE", "TASK_START", f"Preparing task {task.task_id}", run_id=run_id, task_id=task.task_id)

                adapter: Optional[Any] = None
                snapshot: Optional[RuntimeSnapshot] = None
                exec_status = ExecutionStatus.FAILED
                raw_exec_status: Optional[ExecutionStatus] = None
                exit_code: Optional[int] = None
                prim_failure: Optional[str] = None
                clean_status = CleanupStatus.NOT_NEEDED
                ev_status = EvidenceStatus.INCOMPLETE
                obs_cases: List[str] = []
                missing_arts: List[str] = list(task.expected_artifacts)
                collect_errs: List[str] = []
                exported_dir: Optional[str] = None
                infrastructure_failure = False

                t_start = time.monotonic()
                t_start_utc = dt.datetime.now(dt.timezone.utc).isoformat()

                # Per-task protected lifecycle
                try:
                    # Step 5a: Prepare Snapshot using fixed suite bundles or direct immutable checkouts
                    prep_kwargs: Dict[str, Any] = {
                        "marketplace_source": self.marketplace_dir,
                        "gonka_source": self.gonka_dir,
                        "run_id": run_id,
                        "runtime_root": self.runtime_root,
                        "lock": self.active_lock,
                        "fixed_marketplace_sha": m_sha,
                        "fixed_gonka_sha": g_sha,
                        "existing_marketplace_bundle": suite_m_bundle,
                        "existing_gonka_bundle": suite_g_bundle,
                        "check_cosmwasm": True,
                        "recorded_gonka_source_sha": e2e_selected_gonka_sha,
                        # A context that does not say which model it expects
                        # gets the only gradable one, never a weaker default.
                        "evidence_model": getattr(self.e2e_context, "evidence_model", None) or EVIDENCE_MODEL_IMMUTABLE,
                    }
                    if self.use_direct_sources:
                        prep_kwargs["use_direct_sources"] = True
                    snapshot = prepare_runtime_snapshot(**prep_kwargs)

                    # Step 5b: Heartbeat callback
                    def on_heartbeat(phase: str, elapsed: float, limit: float, log_p: Path):
                        log_event("HEARTBEAT", "TASK_HEARTBEAT", f"Task {task.task_id} {phase}: {elapsed:.1f}s / {limit:.1f}s", run_id=run_id, task_id=task.task_id)
                        print(f"  [Heartbeat] {task.task_id} | Phase: {phase} | Elapsed: {elapsed:.0f}s / {limit:.0f}s | Log: {log_p.name}")

                    # Step 5c: Run Adapter
                    if task.proof_level == ProofLevel.NATIVE:
                        adapter = NativeTaskAdapter(
                            task, snapshot, heartbeat_callback=on_heartbeat,
                            e2e_context=self.e2e_context,
                        )
                    else:
                        adapter = BoundaryTaskAdapter(
                            task, snapshot, heartbeat_callback=on_heartbeat,
                            e2e_context=self.e2e_context,
                        )

                    self.current_adapter = adapter
                    log_event("EXECUTE", "ADAPTER_START", f"Executing {task.task_id}", run_id=run_id, task_id=task.task_id)

                    exec_status, exit_code, prim_failure = adapter.execute()
                    raw_exec_status = exec_status
                    log_event("EXECUTE", "ADAPTER_COMPLETED", f"Execution finished with status {exec_status.value} (code {exit_code})", run_id=run_id, task_id=task.task_id)

                    # Step 5d: Verify Evidence with task-specific SourceIdentity
                    task_source_id = SourceIdentity(
                        marketplace_commit_sha=source_id.marketplace_commit_sha,
                        gonka_commit_sha=source_id.gonka_commit_sha,
                        runner_version_hash=source_id.runner_version_hash,
                        catalog_version_hash=source_id.catalog_version_hash,
                        gonka_tree_sha=source_id.gonka_tree_sha,
                        marketplace_tree_sha=source_id.marketplace_tree_sha,
                    )
                    final_exec, ev_stat, cases_obs, arts_missing, eval_err = evaluate_task_evidence(
                        task=task,
                        evidence_dir=snapshot.evidence_dir,
                        junit_dir=snapshot.junit_dir,
                        raw_status=exec_status,
                        exit_code=exit_code,
                        run_id=run_id,
                        source_identity=task_source_id,
                        e2e_evidence_expected=True,
                    )
                    exec_status = final_exec
                    ev_status = ev_stat
                    obs_cases = cases_obs
                    missing_arts = arts_missing
                    if eval_err and not prim_failure:
                        prim_failure = eval_err

                except Exception as task_exc:
                    infrastructure_failure = True
                    exec_status = ExecutionStatus.FAILED
                    if raw_exec_status is None:
                        raw_exec_status = ExecutionStatus.FAILED
                    exit_code = exit_code or 1
                    prim_failure = f"Unexpected error during task {task.task_id}: {task_exc}"
                    log_event("EXECUTE", "TASK_EXCEPTION", prim_failure, run_id=run_id, task_id=task.task_id)
                    print(f"Task exception in {task.task_id}: {task_exc}", file=sys.stderr)

                finally:
                    t_end = time.monotonic()
                    t_end_utc = dt.datetime.now(dt.timezone.utc).isoformat()
                    dur = t_end - t_start

                    # Ensure process-tree termination
                    if adapter and hasattr(adapter, "terminate_own_tree"):
                        try:
                            adapter.terminate_own_tree()
                        except Exception as term_exc:
                            collect_errs.append(f"Error terminating adapter tree: {term_exc}")

                    # Always perform runtime cleanup if snapshot was prepared
                    if snapshot:
                        source_record = snapshot.task_evidence_dir / "source-immutability.json"
                        if source_record.exists() or source_record.is_symlink():
                            try:
                                if not is_path_safe(snapshot.run_dir, source_record):
                                    raise ValueError("unsafe source-immutability evidence path")
                                source_document = json.loads(source_record.read_text(encoding="utf-8"))
                                if source_document.get("verdict") == "VIOLATED":
                                    raise ValueError("source immutability was violated during the task")
                            except Exception as source_exc:
                                infrastructure_failure = True
                                collect_errs.append(f"Source provenance failure: {source_exc}")
                        try:
                            cleanup_res = perform_runtime_cleanup(
                                snapshot,
                                keep_resources=keep_resources,
                                lock=self.active_lock,
                            )
                            clean_status = CleanupStatus(cleanup_res.get("status", CleanupStatus.UNKNOWN.value))
                        except Exception as cl_exc:
                            clean_status = CleanupStatus.FAILED
                            collect_errs.append(f"Cleanup error: {cl_exc}")
                        log_event("CLEANUP", "CLEANUP_COMPLETED", f"Cleanup status {clean_status.value}", run_id=run_id, task_id=task.task_id)

                        # A failed task may have touched the shared direct
                        # snapshots. Re-measure them before another scenario
                        # can consume them; inability to prove safety halts.
                        if self.use_direct_sources and task.ordinal < len(tasks) and (
                            exec_status != ExecutionStatus.PASSED
                            or ev_status != EvidenceStatus.COMPLETE
                        ):
                            try:
                                for label, root, sha in (
                                    ("marketplace", self.marketplace_dir, m_sha),
                                    ("gonka", self.gonka_dir, g_sha),
                                ):
                                    assert_pristine(capture(root), expected_sha=sha, label=label)
                            except Exception as source_exc:
                                infrastructure_failure = True
                                collect_errs.append(f"Unsafe sources after failed task: {source_exc}")

                        # Always collect available evidence directly into the durable suite directory
                        try:
                            exported_entries, c_errs = collect_snapshot_artifacts(
                                snapshot=snapshot,
                                task_id=task.task_id,
                                output_suite_dir=suite_target_dir,
                            )
                            update_artifact_index(suite_target_dir, exported_entries)
                            collect_errs.extend(c_errs)
                            exported_dir = str(suite_target_dir / "runs" / run_id)
                        except Exception as col_exc:
                            collect_errs.append(f"Artifact collection error: {col_exc}")

                    # Ensure run directory exists in suite directory for task result
                    run_target_dir = suite_target_dir / "runs" / run_id
                    run_target_dir.mkdir(parents=True, exist_ok=True)

                    tr = TaskResult(
                        task_id=task.task_id,
                        ordinal=task.ordinal,
                        run_id=run_id,
                        proof_level=task.proof_level,
                        execution_status=exec_status,
                        evidence_status=ev_status,
                        cleanup_status=clean_status,
                        acceptance_status=AcceptanceStatus.NOT_REVIEWED,
                        start_time_utc=t_start_utc,
                        end_time_utc=t_end_utc,
                        duration_seconds=dur,
                        phase="COMPLETED",
                        exit_code=exit_code,
                        primary_failure=prim_failure,
                        secondary_errors=collect_errs,
                        expected_cases=task.expected_checkpoints,
                        observed_passed_cases=obs_cases,
                        missing_cases=[c for c in task.expected_checkpoints if c not in obs_cases],
                        missing_artifacts=missing_arts,
                        raw_evidence_dir=str(snapshot.run_dir) if snapshot else None,
                        exported_evidence_dir=exported_dir,
                        raw_execution_status=raw_exec_status or exec_status,
                    )
                    task_results.append(tr)

                    task_res_file = run_target_dir / "result.json"
                    task_res_bytes = (json.dumps(tr.to_dict(), indent=2) + "\n").encode("utf-8")
                    task_res_file.write_bytes(task_res_bytes)
                    update_artifact_index(suite_target_dir, [ArtifactEntry(
                        relative_path=f"runs/{run_id}/result.json",
                        size_bytes=len(task_res_bytes),
                        sha256=hashlib.sha256(task_res_bytes).hexdigest(),
                        run_id=run_id,
                        task_id=task.task_id,
                        artifact_kind="METADATA_JSON",
                    )])
                    self.current_adapter = None

                if progress_callback is not None:
                    progress_callback("completed", task.task_id)

                print(f"Task {task.task_id} completed: Execution={exec_status.value}, Evidence={ev_status.value}, Cleanup={clean_status.value}")

                # A failed assertion or preparation command is a task result,
                # not a reason to discard the rest of the selected coverage.
                # Continue only when no shared-state safety check failed.
                if keep_resources:
                    print(f"\n[KeepResources] Environment kept for task {task.task_id} at {snapshot.run_dir if snapshot else 'unknown'}")
                    stop_further_tasks = True
                    stop_reason = f"Resources kept after {task.task_id}"
                elif (
                    infrastructure_failure or collect_errs
                    or clean_status not in (CleanupStatus.CLEANED, CleanupStatus.NOT_NEEDED)
                    or ev_status == EvidenceStatus.INVALID
                    or exec_status in (ExecutionStatus.CANCELLED, ExecutionStatus.INTERRUPTED)
                ):
                    details = [f"cleanup={clean_status.value}", *collect_errs]
                    if ev_status == EvidenceStatus.INVALID:
                        details.append("evidence=INVALID")
                    if prim_failure:
                        details.append(prim_failure)
                    stop_reason = f"Unsafe continuation after {task.task_id}: {'; '.join(details)}"
                    log_event("STOP", "UNSAFE_CONTINUATION", stop_reason, run_id=run_id, task_id=task.task_id)
                    print(f"\n[Stop-for-safety] {stop_reason}", file=sys.stderr)
                    stop_further_tasks = True
                elif exec_status != ExecutionStatus.PASSED or ev_status != EvidenceStatus.COMPLETE:
                    log_event("CONTINUE", "TASK_FAILED_CONTINUING", f"Task {task.task_id} failed; cleanup completed, continuing", run_id=run_id, task_id=task.task_id)
                    print(f"\n[Continue-after-failure] Task {task.task_id} recorded; continuing suite.", file=sys.stderr)

        finally:
            signal.signal(signal.SIGINT, old_sigint)
            signal.signal(signal.SIGTERM, old_sigterm)
            if self.active_lock:
                self.active_lock.release()
                self.active_lock = None

        # Build final SuiteResult using centralized outcome calculation authority
        has_sec_errors = any(len(t.secondary_errors) > 0 for t in task_results)
        overall_status, overall_exit_code = calculate_suite_outcome(
            tasks=task_results,
            interrupted=self.interrupted,
            interrupted_signal=self.interrupted_signal,
            has_corruption_or_index_errors=False,
            has_export_or_secondary_errors=has_sec_errors,
        )

        summary_msg = f"Suite {sid} completed with status {overall_status.value}"
        suite_res = SuiteResult(
            schema_version="1.0.0",
            suite_id=sid,
            created_at_utc=plan.created_at_utc,
            completed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
            source_identity=source_id,
            overall_status=overall_status,
            tasks=task_results,
            summary_message=summary_msg,
        )

        res_file = suite_target_dir / "suite-result.json"
        res_file.write_text(json.dumps(suite_res.to_dict(), indent=2) + "\n", encoding="utf-8")

        # Verify suite evidence and generate human summary and coverage report in suite directory
        verification = verify_and_recalculate_suite(suite_target_dir)
        reporter = OfflineReporter(suite_target_dir)
        suite_res = reporter.write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
            write_suite_result=True,
        )
        overall_status = suite_res.overall_status
        if overall_status != ExecutionStatus.PASSED and overall_exit_code == 0:
            overall_exit_code = 1

        print("\n=======================================================")
        print(f"Suite Finished: {sid}")
        print(f"Overall Status: {overall_status.value}")
        print(f"Summary Report: {suite_target_dir / 'summary.md'}")
        print("=======================================================\n")

        return overall_exit_code
