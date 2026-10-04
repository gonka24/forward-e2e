"""The single E2E command parser and dispatcher.

Both public wrappers -- ``ops/e2e/Run-E2E.ps1`` and ``ops/e2e/run-e2e.sh`` --
forward their arguments verbatim to this module inside the runner container, so
there is exactly one parser and the documented examples cannot drift from it.

Commands
--------
``list``    profiles, scenarios, adapters, proof levels and limits. No dockerd.
``plan``    acquire and verify the pinned sources, write the lock. No dockerd,
            no target build, no code from the target repositories is executed.
``run``     plan (or load a plan with ``--from``), then build and execute.
``rerun``   the same execution path as ``run --from``, always with a new run id
            and a fresh network.
``report``  recompute the report from existing evidence. No dockerd.
``recover`` re-export a stored run. No dockerd, no tests.

Exit codes: ``0`` success, ``1`` failure, ``2`` usage error, ``130``/``143`` on
SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import E2EError, LockOverrideError, PathSafetyError, UsageError
from .file_safety import read_checked_file_bytes
from .gitio import CredentialProvider, redact
from .outcome import RUN_RESULT_FILENAME
from .runlock import (
    BUILD_MANIFEST_FILENAME,
    DELIVERY_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    RUN_LOCK_FILENAME,
    load_run_lock,
    resolve_package_root,
)
from .sources import assert_no_symlinks_in_path, build_source_spec

PROG = "run-e2e"

#: Deliberately the same character set as ``forward_e2e.suite.runtime.RUN_ID_REGEX`` and
#: ``forward_e2e.suite.orchestrator.SUITE_ID_REGEX`` so every run id accepted by the CLI is
#: valid across staging, runtime snapshots, and recovery.
RUN_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,64}\Z")

#: Flags that describe *what is proven*. ``--from`` replays a decision that was
#: already made, so none of them may be combined with it. Operational flags
#: (``--run-id``, output location, credential source, cache/transport location)
#: are deliberately absent from this list.
SEMANTIC_FLAGS: Tuple[Tuple[str, str], ...] = (
    ("gonka_repo", "--gonka-repo"),
    ("gonka_path", "--gonka-path"),
    ("gonka_sha", "--gonka-sha"),
    ("contracts_repo", "--contracts-repo"),
    ("contracts_path", "--contracts-path"),
    ("contracts_sha", "--contracts-sha"),
    ("profile", "--profile"),
    ("scenario", "--scenario"),
    ("runner_image", "--runner-image"),
)

#: Flags that name the *identity* of the plan rather than what it proves. A
#: replay takes that identity from the lock it was given, so these are refused
#: with ``--from`` as well. They are deliberately not part of ``SEMANTIC_FLAGS``:
#: reporting ``--plan-id`` as "changing what is proven" would be false, and the
#: semantic set is pinned by tests as the list of proof-changing flags.
LOCK_IDENTITY_FLAGS: Tuple[Tuple[str, str], ...] = (
    ("plan_id", "--plan-id"),
)

#: The complete list of flags a replay accepts, repeated in every refusal so
#: the operator never has to guess which flag to move to a new plan instead.
ALLOWED_WITH_FROM: Tuple[str, ...] = (
    "--run-id", "--output", "--workspace", "--runtime-root",
    "--credential-file", "--credential-username", "--parent-run-id",
)

SUBCOMMANDS = ("list", "plan", "run", "rerun", "report", "recover")


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(
        "sources",
        "Exactly one repository or path and exactly one full 40-hex commit SHA per source. "
        "Branches, tags, HEAD, short SHAs and revision expressions are rejected: look the "
        "commit up first and pass it explicitly.",
    )
    group.add_argument("--gonka-repo", default=None, metavar="HTTPS_URL")
    group.add_argument("--gonka-path", default=None, metavar="DIR")
    group.add_argument("--gonka-sha", default=None, metavar="FULL_40_HEX_SHA")
    group.add_argument("--contracts-repo", default=None, metavar="HTTPS_URL")
    group.add_argument("--contracts-path", default=None, metavar="DIR")
    group.add_argument("--contracts-sha", default=None, metavar="FULL_40_HEX_SHA")


def _add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(
        "selection",
        "--profile and --scenario are mutually exclusive and one of them is required.",
    )
    group.add_argument("--profile", default=None, choices=("smoke", "native", "boundary", "all"))
    group.add_argument(
        "--scenario",
        action="append",
        default=None,
        metavar="ID",
        help="Repeatable. Duplicates are normalised; execution order comes from the catalog.",
    )


def _add_operational_arguments(parser: argparse.ArgumentParser, *, with_run_id: bool) -> None:
    group = parser.add_argument_group("operational")

    group.add_argument(
        "--output",
        default=None,
        metavar="DIR",
        help="Where the plan package (and, for run, the evidence) is written.",
    )
    group.add_argument(
        "--workspace",
        default=((os.environ.get("E2E_WORKSPACE_DIR") or "").strip() or "/workspace"),
        metavar="DIR",
    )
    group.add_argument("--runtime-root", default=None, metavar="DIR")
    group.add_argument(
        "--runner-image",
        default=None,
        metavar="IMAGE",
        help="Locator of the runner image. A tag is only a locator; the immutable image id "
        "is what gets recorded.",
    )
    group.add_argument(
        "--credential-file",
        default=os.environ.get("E2E_CREDENTIAL_FILE") or None,
        metavar="FILE",
        help="File holding a token for private repositories. Its value never reaches argv, "
        "a URL, a log, the lock or the evidence.",
    )
    group.add_argument("--credential-username", default="x-access-token", metavar="NAME")
    if with_run_id:
        group.add_argument("--run-id", default=None, metavar="ID")
        group.add_argument(
            "--keep-resources",
            action="store_true",
            help="Rejected inside the container: the inner network dies with it.",
        )


def build_parser(subcommand: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} {subcommand}",
        description=__doc__.splitlines()[0] if __doc__ else None,
    )
    if subcommand == "list":
        parser.add_argument(
            "--json", action="store_true", help="Machine-readable catalog listing."
        )
        return parser

    if subcommand == "plan":
        _add_source_arguments(parser)
        _add_selection_arguments(parser)
        _add_operational_arguments(parser, with_run_id=False)
        parser.add_argument("--plan-id", default=None, metavar="ID")
        return parser

    if subcommand in ("run", "rerun"):
        _add_source_arguments(parser)
        _add_selection_arguments(parser)
        _add_operational_arguments(parser, with_run_id=True)
        parser.add_argument("--plan-id", default=None, metavar="ID")
        parser.add_argument(
            "--from",
            dest="from_lock",
            default=None,
            metavar="RUN_LOCK_JSON",
            help="Execute a saved plan exactly. Source, SHA, selection, semantic configuration "
            "and compatibility may not be overridden; changing a version means a new plan.",
        )
        parser.add_argument("--parent-run-id", default=None, metavar="ID")
        return parser

    if subcommand in ("report", "recover"):
        parser.add_argument("--run", required=True, metavar="PATH_OR_ID")
        parser.add_argument("--output", default=None, metavar="DIR")
        parser.add_argument(
            "--workspace",
            default=((os.environ.get("E2E_WORKSPACE_DIR") or "").strip() or "/workspace"),
            metavar="DIR",
        )
        return parser

    raise UsageError(f"Unknown command {subcommand!r}. Available: {', '.join(SUBCOMMANDS)}")


def parse_e2e_args(argv: Optional[Sequence[str]] = None) -> Tuple[str, argparse.Namespace]:
    args_list = list(argv) if argv is not None else sys.argv[1:]
    if not args_list:
        raise UsageError(
            f"A command is required. Available: {', '.join(SUBCOMMANDS)}.",
            {"hint": f"Try `{PROG} list`."},
        )
    subcommand = args_list[0]
    if subcommand not in SUBCOMMANDS:
        raise UsageError(
            f"Unknown command {subcommand!r}. Available: {', '.join(SUBCOMMANDS)}.",
        )
    parser = build_parser(subcommand)
    return subcommand, parser.parse_args(args_list[1:])


def assert_no_semantic_overrides(args: argparse.Namespace) -> None:
    """``--from`` replays a decision; it never re-opens it.

    Two kinds of flag are refused. Semantic flags would change *what* is
    proven. ``--plan-id`` would not, but a replay has no plan identity of its
    own -- it inherits the one sealed in the lock -- so accepting the flag and
    then ignoring it, which is what happened before this check existed, let an
    operator believe a plan had been renamed when nothing had changed.
    """
    offenders = [
        flag
        for attr, flag in SEMANTIC_FLAGS
        if getattr(args, attr, None) not in (None, [], ())
    ]
    if offenders:
        raise LockOverrideError(
            "--from executes a saved plan exactly. These flags would change what is being "
            "proven and are therefore refused. Create a new plan instead.",
            {
                "rejected_flags": offenders,
                "allowed_with_from": list(ALLOWED_WITH_FROM),
            },
        )
    identity_offenders = [
        flag
        for attr, flag in LOCK_IDENTITY_FLAGS
        if getattr(args, attr, None) not in (None, [], ())
    ]
    if identity_offenders:
        raise LockOverrideError(
            "--from executes a saved plan exactly: the plan id comes from the lock and "
            "cannot be overridden, so --plan-id is refused together with --from. Name the "
            "execution with --run-id, or create a new plan under the id you want.",
            {
                "rejected_flags": identity_offenders,
                "allowed_with_from": list(ALLOWED_WITH_FROM),
            },
        )


def _credential(args: argparse.Namespace) -> Optional[CredentialProvider]:
    path = getattr(args, "credential_file", None)
    if not path:
        return None
    provider = CredentialProvider(
        secret_file=Path(path), username=getattr(args, "credential_username", "x-access-token")
    )
    provider.validate()
    return provider


def _require_run_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    candidate = value.strip()
    if not RUN_ID_RE.match(candidate):
        raise UsageError(
            "Invalid --run-id. Use letters, digits, '_' or '-' (max 65 characters); "
            "it has to survive the workspace and recovery path checks too.",
            {"run_id": value},
        )
    return candidate


def _require_plan_id(value: Optional[str]) -> Optional[str]:
    """Plan IDs also name directories, including when read from a saved lock."""
    if value is not None and not RUN_ID_RE.fullmatch(value):
        raise UsageError(
            "Invalid plan id. Use letters, digits, '_' or '-' (max 65 characters).",
            {"plan_id": value},
        )
    return value


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_list(args: argparse.Namespace, *, out=None) -> int:
    """Print what can be selected. Never starts the inner Docker daemon."""
    stream = out or sys.stdout
    from ..suite.catalog import (
        BOUNDARY_TASKS,
        NATIVE_TASKS,
        compute_catalog_hash,
    )
    from .compat import contracts_adapters, gonka_adapters

    adapters = list(gonka_adapters()) + list(contracts_adapters())

    if getattr(args, "json", False):
        payload: Dict[str, Any] = {
            "catalog_hash": compute_catalog_hash(),
            "boundary": [t.to_dict() for t in BOUNDARY_TASKS],
            "native": [t.to_dict() for t in NATIVE_TASKS],
            "profiles": ["smoke", "native", "boundary", "all"],
            "adapters": [a.declaration() for a in adapters],
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=stream)
        return 0

    print("=== E2E catalog ===", file=stream)
    print(f"catalog hash: {compute_catalog_hash()}", file=stream)
    print("\n-- Boundary tasks (no Testermint network) --", file=stream)
    for task in BOUNDARY_TASKS:
        print(
            f"  {task.task_id:<24} {task.proof_level.value:<16} limit {task.timeout_minutes}m",
            file=stream,
        )
    print("\n-- Native tasks (owned local network per task) --", file=stream)
    for task in NATIVE_TASKS:
        print(
            f"  {task.task_id:<24} {task.proof_level.value:<16} limit {task.timeout_minutes}m",
            file=stream,
        )
    print("\n-- Profiles --", file=stream)
    print("  smoke | native | boundary | all", file=stream)
    print("\n-- Compatibility adapters --", file=stream)
    for adapter in adapters:
        print(f"  {adapter.adapter_id} ({adapter.role})", file=stream)
        print(f"      {adapter.description}", file=stream)
        for scenario, reason in sorted(adapter.unsupported_scenarios.items()):
            print(f"      UNSUPPORTED {scenario}: {reason}", file=stream)
        for limitation in adapter.limitations:
            print(f"      note: {limitation}", file=stream)
    print(
        "\nVersions are selected by full 40-hex commit SHA only. There is no branch, tag, "
        "HEAD or latest resolution anywhere in this tool.",
        file=stream,
    )
    return 0


def default_output_root() -> Path:
    """Where runs go when the user did not say.

    The wrappers always bind a writable ``/out``; the plan package is bound
    read-only at ``/input/plan``. Defaulting an execution to the package
    directory would therefore try to write into a read-only mount, so the
    writable output root is the only correct default.
    """
    return Path((os.environ.get("E2E_OUTPUT_DIR") or "").strip() or "/out")


def _plan_from_args(
    args: argparse.Namespace, *, emit, require_output: bool,
    worktree_root: Optional[Path] = None, create_bundles: bool = True,
) -> Any:
    from .planner import PlanRequest, build_plan, generate_plan_id

    gonka = build_source_spec(
        role="gonka",
        repo=args.gonka_repo,
        path=args.gonka_path,
        sha=args.gonka_sha,
        repo_flag="--gonka-repo",
        path_flag="--gonka-path",
        sha_flag="--gonka-sha",
    )
    contracts = build_source_spec(
        role="contracts",
        repo=args.contracts_repo,
        path=args.contracts_path,
        sha=args.contracts_sha,
        repo_flag="--contracts-repo",
        path_flag="--contracts-path",
        sha_flag="--contracts-sha",
    )
    plan_id = _require_plan_id(args.plan_id)
    if args.output:
        output_dir = Path(args.output)
    elif require_output:
        # `plan` produces nothing but the package, so its destination is the
        # whole point of the command and must be stated.
        raise UsageError("--output is required: the plan package needs a destination directory.")
    else:
        # `run` without --output still has to put the package somewhere
        # writable, and two consecutive runs must not collide. Naming the
        # directory after the plan id gives both. Never the source package:
        # that mount is read-only.
        plan_id = (plan_id or generate_plan_id()).strip()
        output_dir = default_output_root() / "plans" / plan_id
    request = PlanRequest(
        gonka=gonka,
        contracts=contracts,
        output_dir=output_dir,
        profile=args.profile,
        scenarios=args.scenario,
        runner_image_locator=args.runner_image,
        credential=_credential(args),
        plan_id=plan_id,
        worktree_root=worktree_root,
        create_bundles=create_bundles,
    )
    return build_plan(request, emit=emit)


def cmd_plan(args: argparse.Namespace, *, emit) -> int:
    result = _plan_from_args(args, emit=emit, require_output=True)
    emit(f"Plan {result.lock.plan_id} written to {result.lock_path}")
    emit(f"Scenarios ({len(result.tasks)}): {', '.join(result.lock.scenarios)}")
    emit(
        "Nothing was built and no network was started. Execute this plan with "
        f"`{PROG} run --from {result.lock_path}`."
    )
    return 0


def cmd_run(args: argparse.Namespace, subcommand: str, *, emit) -> int:
    replay = bool(args.from_lock) or subcommand == "rerun"
    if subcommand == "rerun" and not args.from_lock:
        raise UsageError("rerun requires --from <run.lock.json>: it replays a saved plan.")
    if getattr(args, "keep_resources", False):
        raise UsageError(
            "--keep-resources is not supported inside the runner container: the inner "
            "network lifecycle ends with the container. Raw diagnostics are always kept in "
            "the persistent workspace volume."
        )

    if args.from_lock:
        assert_no_semantic_overrides(args)
        lock_path = Path(args.from_lock)
        lock = load_run_lock(lock_path)
        _require_plan_id(lock.plan_id)
        package_dir = resolve_package_root(lock_path)
        if lock_path.is_dir():
            lock_path = lock_path / RUN_LOCK_FILENAME
        emit(f"Replaying plan {lock.plan_id} (lock_sha256={lock.lock_sha256})")
        return _execute_selected_run(
            args, subcommand, emit=emit, lock=lock, lock_path=lock_path,
            package_dir=package_dir, replay=replay, pre_acquired_sources=None,
        )
    else:
        # Keep both pinned checkouts alive until the suite has finished. Planning
        # alone still writes portable bundles; the ordinary run uses its two
        # already-verified checkouts directly and records URLs/SHAs in the lock.
        with tempfile.TemporaryDirectory(prefix="e2e-run-sources-") as source_root:
            result = _plan_from_args(
                args, emit=emit, require_output=False,
                worktree_root=Path(source_root), create_bundles=False,
            )
            return _execute_selected_run(
                args, subcommand, emit=emit, lock=result.lock,
                lock_path=result.lock_path, package_dir=result.package_dir,
                replay=replay,
                pre_acquired_sources={
                    "gonka": result.gonka_source,
                    "contracts": result.contracts_source,
                },
            )


def _execute_selected_run(
    args: argparse.Namespace, subcommand: str, *, emit, lock, lock_path: Path,
    package_dir: Path, replay: bool, pre_acquired_sources,
) -> int:
    from .cancel import Cancellation
    from .executor import ExecutionRequest, execute_plan

    # The source package may be a read-only mount (the wrappers bind it at
    # /input/plan read-only), so it is never a valid destination. Runs always
    # go to the writable output root unless the user named somewhere else.
    output_dir = Path(args.output) if args.output else default_output_root() / "runs"
    # One token for the whole execution: the handler installed below flips it,
    # the executor checks it between stages, and the build runner uses it to
    # stop the process group of whatever is currently compiling.
    cancellation = Cancellation()
    request = ExecutionRequest(
        lock=lock,
        lock_path=lock_path,
        package_dir=package_dir,
        output_dir=output_dir,
        workspace_dir=Path(args.workspace),
        run_id=_require_run_id(args.run_id),
        command=subcommand,
        replay=replay,
        parent_run_id=args.parent_run_id,
        runtime_root=Path(args.runtime_root) if args.runtime_root else None,
        keep_resources=False,
        credential=_credential(args),
        cancellation=cancellation,
        pre_acquired_sources=pre_acquired_sources,
    )
    # The executor is allowed to generate the final run id.  Bootstrap can
    # fail before that happens, so its durable diagnostics need an identity
    # that already exists at this point.  The immutable plan id supplies one
    # without changing the executor's run-id semantics.
    diagnostic_id = request.run_id or lock.plan_id
    return _with_inner_dockerd(
        lambda: execute_plan(request, emit=emit),
        workspace_dir=Path(args.workspace),
        diagnostics_dir=output_dir / "bootstrap-failures" / diagnostic_id,
        emit=emit,
        cancellation=cancellation,
    )


def cmd_report(args: argparse.Namespace, *, emit) -> int:
    """Recompute the verdict from stored evidence. Never starts dockerd.

    A full E2E run is graded as a whole. Reading only the nested suite is what
    allowed a run that failed its post-run provenance to be reported as
    ``PASSED`` afterwards, so pointing at a suite inside a run package is
    resolved *upwards* to the package.
    """
    from .executor import RUN_RESULT_SHORT_FILENAME, grade_run_package

    package_root = _resolve_run_package(args, emit=emit)
    if package_root is not None:
        try:
            assert_no_symlinks_in_path(package_root)
            for verdict_name in (RUN_RESULT_FILENAME, RUN_RESULT_SHORT_FILENAME):
                if (package_root / verdict_name).is_symlink():
                    raise PathSafetyError(
                        f"run verdict path is a symlink: {package_root / verdict_name}",
                        {"path": str(package_root / verdict_name)},
                    )
        except PathSafetyError as exc:
            emit(f"Error: {exc}")
            return getattr(exc, "exit_code", 1)
        try:
            outcome = grade_run_package(package_root)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            emit(f"Error grading the run package at {package_root}: {exc}")
            return 1
        for line in outcome.summary_lines():
            emit(line)
        if not any(f.code == "SUITE_PATH_UNSAFE" for f in outcome.findings):
            from ..suite.models import SuiteResult

            raw_suite_dir = getattr(outcome, "_suite_dir", None)
            if isinstance(raw_suite_dir, Path):
                try:
                    suite_dir: Optional[Path] = package_root / raw_suite_dir.relative_to(
                        package_root.resolve()
                    )
                except ValueError:
                    suite_dir = raw_suite_dir
            else:
                suite_dir = _suite_dir_of_package(package_root)
            raw_suite_result = getattr(outcome, "_suite_result", None)
            suite_result = raw_suite_result if isinstance(raw_suite_result, SuiteResult) else None
            raw_integrity_errors = getattr(outcome, "_suite_integrity_errors", None)
            integrity_errors = (
                list(raw_integrity_errors)
                if isinstance(raw_integrity_errors, (list, tuple))
                else None
            )
            if suite_dir is not None:
                suite_res = _regenerate_suite_report(
                    suite_dir,
                    suite_result=suite_result,
                    integrity_errors=integrity_errors,
                    emit=emit,
                )
                if suite_res is None:
                    return 1
                if suite_res.overall_status.value != "PASSED" and outcome.exit_code == 0:
                    emit(f"Verdict: {package_root / RUN_RESULT_FILENAME}")
                    return 1
        emit(f"Verdict: {package_root / RUN_RESULT_FILENAME}")
        return outcome.exit_code

    for cand in _candidate_run_dirs(args):
        if cand.is_dir() and ((cand / "suite-plan.json").is_file() or (cand / "suite-result.json").is_file()):
            emit(
                f"Error: This directory is a bare suite directory, not an E2E run package ({cand}): "
                "a suite-level directory is not an E2E run verdict. "
                "Bare suite directories without an E2E run package envelope cannot produce a passing E2E verdict."
            )
            return 1

    target = str(args.run).strip()
    raise UsageError(
        "The requested run was not found: No suite evidence was found for that run. Pass the run directory produced by "
        "`run`, or the run id together with the --output and --workspace that produced it.",
        {
            "run": target,
            "searched": [str(p) for p in _candidate_run_dirs(args)],
        },
    )


def _regenerate_suite_report(
    suite_dir: Path,
    *,
    suite_result: Optional[Any] = None,
    integrity_errors: Optional[Sequence[str]] = None,
    emit,
) -> Optional[Any]:
    """Rebuild ``summary.md``/``coverage.json`` for one suite directory."""
    from ..suite.reporter import OfflineReporter
    from ..suite.verifier import verify_and_recalculate_suite

    try:
        _assert_tree_has_no_symlinks(suite_dir, what="suite directory")
        reporter = OfflineReporter(suite_dir)
        if suite_result is None:
            verification = verify_and_recalculate_suite(suite_dir)
            suite_result = verification.result
            integrity_errors = verification.integrity_errors
        result = reporter.write_reports(
            suite_result,
            integrity_errors=integrity_errors,
            write_suite_result=False,
        )
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        emit(f"Error generating the offline suite report: {exc}")
        return None
    emit(f"Suite {result.suite_id}: {result.overall_status.value}")
    emit(f"Summary: {suite_dir / 'summary.md'}")
    return result


def cmd_recover(args: argparse.Namespace, *, emit) -> int:
    """Re-export a durable E2E run package from staging and re-grade it. No dockerd, no tests repeated."""
    from ..suite.orchestrator import reconcile_suite_runtime_evidence
    from .delivery import CorruptDeliveryLedgerError, DeliveryService
    from .executor import grade_run_package, suite_export_dir
    from .runpackage import ExportConflict, assert_export_paths_disjoint, run_stage_dir

    suite_id, workspace_root = _resolve_recovery_target(args, emit=emit)
    output = Path(args.output) if args.output else default_output_root()
    emit(
        f"Recovering suite {suite_id} from {workspace_root} into {output}. "
        "No tests are repeated and no daemon is started."
    )

    stage_dir = run_stage_dir(Path(args.workspace), suite_id)
    run_dir = output / suite_id
    try:
        assert_no_symlinks_in_path(output, what="Recovery output directory")
        assert_no_symlinks_in_path(run_dir, what="Recovery run directory")
        if stage_dir.exists() or stage_dir.is_symlink():
            _assert_tree_has_no_symlinks(stage_dir, what="Durable run staging directory")
        runtime_dir = workspace_root / "runtime"
        if runtime_dir.exists() or runtime_dir.is_symlink():
            _assert_tree_has_no_symlinks(runtime_dir, what="Workspace runtime directory")
    except PathSafetyError as exc:
        emit(f"Error: {exc}")
        return getattr(exc, "exit_code", 1)

    if not stage_dir.is_dir():
        emit(
            f"Error: Durable run package not found at {stage_dir}; "
            "legacy standalone suites without a durable run package are not supported."
        )
        return 1

    try:
        assert_export_paths_disjoint(stage_dir, run_dir)
    except E2EError as exc:
        emit(f"Error: {exc}")
        return exc.exit_code

    # Reconcile any un-staged raw runtime artifacts from interrupted steps directly
    # into the durable run package stage before delivery exports it.
    staged_suite_in_pkg = suite_export_dir(stage_dir, suite_id)
    if (staged_suite_in_pkg / "suite-plan.json").is_file():
        reconcile_ok, reconcile_errs = reconcile_suite_runtime_evidence(
            staged_suite_in_pkg, workspace_root / "runtime", suite_id
        )
        if not reconcile_ok:
            for err in reconcile_errs:
                emit(f"Error: {err}")
            return 1

    try:
        res = DeliveryService.deliver_recovery(
            stage_dir, run_dir, suite_id=suite_id, emit=emit
        )
    except (PathSafetyError, CorruptDeliveryLedgerError) as exc:
        emit(f"Error: {exc}")
        return getattr(exc, "exit_code", 1)
    except Exception as exc:
        emit(f"Error recording recovery attempt in staging: {exc}")
        return 1

    if not res.success:
        if res.ledger_failure is not None and res.export_failure is not None:
            emit(f"warning: failed to update delivery ledger in staging: {res.ledger_failure}")
        exc = res.export_failure or res.ledger_failure
        if isinstance(exc, ExportConflict):
            emit(f"Error: {exc}")
            return getattr(exc, "exit_code", 1)
        emit(f"Error recovering run package: {exc}")
        return 1

    suite_destination = suite_export_dir(run_dir, suite_id)
    if (suite_destination / "suite-plan.json").is_file():
        emit(
            f"The run package carries its suite at {suite_destination}; "
            "the recovered bytes match the durable package."
        )

    outcome = grade_run_package(run_dir)
    for line in outcome.summary_lines():
        emit(line)
    emit(f"Verdict: {run_dir / RUN_RESULT_FILENAME}")
    return outcome.exit_code


def _assert_tree_has_no_symlinks(root_dir: Path, *, what: str) -> None:
    """Verify that root_dir, its parents, and all files/directories within it contain no symlinks and do not escape root_dir."""
    from .runpackage import assert_tree_safe

    assert_tree_safe(root_dir, what=what)


def _resolve_run_package(args: argparse.Namespace, *, emit) -> Optional[Path]:
    """The root of a run package, if what the user named belongs to one.

    Accepts the run directory, the durable staging directory, or any directory
    inside a run package (in particular its nested suite): a reader who points
    at a part is answered about the whole, because the parts are only
    meaningful together.

    An explicitly specified run package is always preferred over its ancestors.
    When resolving from a nested path within a run package, the search resolves
    to the closest enclosing run package, never bleeding upward into a parent
    plan package or enclosing run.
    """
    from .runpackage import run_stage_dir

    target = str(args.run).strip()
    candidates: List[Path] = list(_candidate_run_dirs(args))
    if RUN_ID_RE.match(target):
        candidates.append(run_stage_dir(Path(args.workspace), target))

    # Phase 1: If any candidate is directly a run package, return it immediately.
    for path in candidates:
        if not path.is_dir():
            continue
        if _is_run_package(path):
            return path

    # Phase 2: If a candidate is inside a run package (e.g. nested suite),
    # search upwards for the closest enclosing run package.
    for path in candidates:
        if not path.is_dir():
            continue
        for parent in list(path.resolve().parents)[:6]:
            if _is_run_package(parent):
                emit(
                    f"{path} is part of the run package at {parent}; grading the whole "
                    "run, because a suite result on its own is not an E2E verdict."
                )
                return parent
            if _is_plan_package(parent):
                # Stop at plan package boundary so a parent plan package does not enclose child
                break

    # Phase 3: If no candidate was a run package or inside one, check if an
    # isolated plan package was passed.
    for path in candidates:
        if not path.is_dir():
            continue
        if _is_plan_package(path):
            return path

    return None


def _is_run_package(path: Path) -> bool:
    """True if path contains evidence of an E2E run execution (completed, partial, or failed).

    A plan package (which contains only run.lock.json and optional bundles) is NOT a run package.
    """
    if not path.is_dir() or path.is_symlink():
        return False
    # A suite directory itself is a suite, not an enclosing run package.
    if (path / "suite-plan.json").is_file() or (path / "suite-result.json").is_file():
        return False
    if (path / EXECUTION_MANIFEST_FILENAME).is_file():
        return True
    if (path / BUILD_MANIFEST_FILENAME).is_file():
        return True
    if (path / DELIVERY_MANIFEST_FILENAME).is_file():
        return True
    if (path / RUN_RESULT_FILENAME).is_file():
        return True
    suite_dir = path / "suite" / path.name
    if suite_dir.is_dir() and not suite_dir.is_symlink():
        if (
            (suite_dir / "e2e-context.json").is_file()
            or (suite_dir / "suite-plan.json").is_file()
            or (suite_dir / "suite-result.json").is_file()
        ):
            return True
    return False


def _is_plan_package(path: Path) -> bool:
    """True if path has a run lock but no execution evidence."""
    if not path.is_dir() or path.is_symlink():
        return False
    if not (path / RUN_LOCK_FILENAME).is_file():
        return False
    return not _is_run_package(path)


def _suite_dir_of_package(package_root: Path) -> Optional[Path]:
    """The exported suite inside a run package, resolved via LoadedRunPackage."""
    from .runpackage import LoadedRunPackage

    try:
        assert_no_symlinks_in_path(package_root, what="run package root")
    except PathSafetyError:
        return None
    package = LoadedRunPackage.load(package_root)
    if package.suite_dir is None:
        return None
    if not (package.suite_dir / "suite-plan.json").is_file():
        return None
    try:
        rel = package.suite_dir.relative_to(package.root)
        return package_root / rel
    except ValueError:
        return package.suite_dir


def _load_execution_manifest(run_dir: Path) -> Optional[Any]:
    """Read a run's own record of where it put things, if it got that far."""
    from .runlock import ExecutionManifest

    path = Path(run_dir) / EXECUTION_MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        return ExecutionManifest.from_dict(
            json.loads(read_checked_file_bytes(path).decode("utf-8"))
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, E2EError):
        # A damaged manifest must not stop recovery; fall back to the layout.
        return None


def _candidate_run_dirs(args: argparse.Namespace) -> List[Path]:
    """Every place a run directory could be, given what the user typed."""
    target = str(args.run).strip()
    candidate = Path(target)
    output = Path(args.output) if args.output else default_output_root()
    seen: List[Path] = []
    for path in (candidate, output / target, output / "runs" / target):
        if path not in seen:
            seen.append(path)
    return seen


def _resolve_recovery_target(args: argparse.Namespace, *, emit) -> Tuple[str, Path]:
    """Run/suite id and the workspace root that holds its raw runtime and durable stage.

    Only durable run packages (`<workspace>/<run_id>/run` or exported run packages
    backed by a durable stage) are accepted. Legacy standalone suites are rejected.
    """
    from .executor import suite_workspace_root
    from .runpackage import run_stage_dir

    target = str(args.run).strip()
    workspace = Path(args.workspace)
    searched: List[str] = []

    for run_dir in _candidate_run_dirs(args):
        searched.append(str(run_dir))
        if not run_dir.is_dir():
            continue
        manifest = _load_execution_manifest(run_dir)
        if manifest is not None:
            if not RUN_ID_RE.match(str(manifest.suite_id)) or not RUN_ID_RE.match(str(manifest.run_id)):
                raise PathSafetyError(
                    "Execution manifest contains an unsafe run_id or suite_id",
                    {"run_id": str(manifest.run_id), "suite_id": str(manifest.suite_id)},
                )
            if manifest.suite_workspace_dir:
                root = Path(manifest.suite_workspace_dir)
                assert_no_symlinks_in_path(root, what="suite workspace directory")
                try:
                    root.resolve().relative_to(workspace.resolve())
                except ValueError as exc:
                    raise PathSafetyError(
                        "Suite workspace directory escapes --workspace",
                        {"suite_workspace_dir": str(root), "workspace": str(workspace)},
                    ) from exc
            else:
                root = suite_workspace_root(workspace / manifest.run_id)
            emit(f"Recovering suite {manifest.suite_id} as recorded in {run_dir}.")
            return manifest.suite_id, root
        name = run_dir.name
        if not RUN_ID_RE.match(name):
            continue
        staged_pkg = run_stage_dir(workspace, name)
        searched.append(str(staged_pkg))
        if (
            (run_dir / RUN_LOCK_FILENAME).is_file()
            or _is_run_package(staged_pkg)
            or (staged_pkg.is_dir() and (staged_pkg / RUN_LOCK_FILENAME).is_file())
        ):
            emit(f"Using the durable run package at {run_dir}.")
            return name, suite_workspace_root(workspace / name)

    if RUN_ID_RE.match(target):
        staged_pkg = run_stage_dir(workspace, target)
        searched.append(str(staged_pkg))
        if _is_run_package(staged_pkg) or (staged_pkg.is_dir() and (staged_pkg / RUN_LOCK_FILENAME).is_file()):
            emit(f"Using the durable run package at {staged_pkg}.")
            return target, suite_workspace_root(workspace / target)

    raise UsageError(
        "The requested run was not found. Pass the run directory, or the run id together "
        "with the --workspace that produced its durable run package.",
        {"run": target, "searched": searched},
    )


# ---------------------------------------------------------------------------
# inner daemon lifecycle
# ---------------------------------------------------------------------------
def _write_daemon_diagnostic(supervisor, destination: Path, *, phase: str, error: str) -> Path:
    """Persist daemon bootstrap/shutdown facts outside the daemon data-root."""
    assert_no_symlinks_in_path(destination, what="Daemon diagnostic directory")
    destination.mkdir(parents=True, exist_ok=True)
    # Replaying the same plan must preserve diagnostics from every attempt.
    destination = Path(tempfile.mkdtemp(prefix="attempt-", dir=destination))
    copied_log = destination / "dockerd.log"
    if supervisor.log_file.is_file():
        with supervisor.log_file.open("r", encoding="utf-8", errors="replace") as source:
            with copied_log.open("w", encoding="utf-8") as target:
                for line in source:
                    target.write(redact(line))
    payload = {
        "schema": "a8/dockerd-diagnostic/1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "error": redact(error),
        "data_root": str(supervisor.data_root),
        "socket_path": str(supervisor.socket_path),
        "log_file": copied_log.name if copied_log.is_file() else None,
    }
    (destination / "diagnostic.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return destination


def _with_inner_dockerd(
    action,
    *,
    workspace_dir: Path,
    emit,
    cancellation=None,
    diagnostics_dir: Optional[Path] = None,
) -> int:
    """Start the private dockerd for the duration of an execution.

    ``list``, ``plan``, ``report`` and ``recover`` never reach this function, so
    they can never bring up the inner daemon.

    The signal handler does not merely remember that a signal arrived: it flips
    the shared cancellation token, which stops the build process group that is
    running right now and makes the executor abandon the remaining stages. A
    run that only noted the signal would keep compiling for hours after Ctrl-C.
    """
    from ..suite.supervisor import DinDSupervisor
    from ..suite.lock import LockContentionError, LockOperationError
    from ..suite.runtime import SuiteRuntimeError

    supervisor = DinDSupervisor(workspace_dir=workspace_dir)
    state = {"interrupted": False, "code": None}

    def handle_term(signum, frame):  # pragma: no cover - signal path
        state["interrupted"] = True
        state["code"] = 130 if signum == signal.SIGINT else 143
        if cancellation is not None:
            cancellation.request(signum, emit=emit)
        else:
            emit("Received a termination signal; stopping the runner safely.")

    old_sigint = signal.signal(signal.SIGINT, handle_term)
    old_sigterm = signal.signal(signal.SIGTERM, handle_term)
    try:
        try:
            supervisor.start_dockerd(timeout_seconds=60.0)
        except LockContentionError:
            emit(
                "The exclusive runtime lock is busy: another runner is active. Concurrent runs "
                "are prevented to protect the shared Docker storage."
            )
            return state["code"] if state["interrupted"] else 1
        except (SuiteRuntimeError, LockOperationError) as exc:
            emit(f"Infrastructure error starting the inner Docker daemon: {redact(str(exc))}")
            if diagnostics_dir is not None:
                try:
                    written = _write_daemon_diagnostic(
                        supervisor, diagnostics_dir, phase="bootstrap", error=redact(str(exc))
                    )
                    emit(f"Daemon bootstrap diagnostics: {written}")
                except Exception as diagnostic_exc:  # noqa: BLE001 - preserve root error
                    emit(f"Could not write daemon bootstrap diagnostics: {redact(str(diagnostic_exc))}")
            return state["code"] if state["interrupted"] else 1

        exit_code = 1
        try:
            exit_code = action()
        except E2EError as exc:
            # A cancellation unwinds as an error so that the partial evidence is
            # written by the executor's failure path; here it only decides the exit
            # code, and it is reported as an interruption rather than a verdict.
            if getattr(exc, "code", "") != "EXECUTION_CANCELLED":
                raise
            emit(f"Run cancelled: {exc}")
            exit_code = exc.exit_code
        finally:
            stopped = supervisor.stop_dockerd(timeout_seconds=60.0)
            if state["interrupted"]:
                exit_code = state["code"] or 143
            elif not stopped:
                emit("The inner Docker daemon did not terminate cleanly.")
                if diagnostics_dir is not None:
                    shutdown_dir = diagnostics_dir.with_name(diagnostics_dir.name + "-shutdown")
                    try:
                        written = _write_daemon_diagnostic(
                            supervisor,
                            shutdown_dir,
                            phase="shutdown",
                            error="inner dockerd did not terminate within 60 seconds",
                        )
                        emit(f"Daemon shutdown diagnostics: {written}")
                    except Exception as diagnostic_exc:  # noqa: BLE001 - preserve run result
                        emit(f"Could not write daemon shutdown diagnostics: {redact(str(diagnostic_exc))}")
                if exit_code == 0:
                    exit_code = 1
        return exit_code
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    def emit(message: str) -> None:
        print(message)

    try:
        subcommand, args = parse_e2e_args(argv)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except SystemExit as exc:  # argparse already printed the diagnostics
        return int(exc.code if exc.code is not None else 0)

    try:
        if subcommand == "list":
            return cmd_list(args)
        if subcommand == "plan":
            return cmd_plan(args, emit=emit)
        if subcommand in ("run", "rerun"):
            return cmd_run(args, subcommand, emit=emit)
        if subcommand == "report":
            return cmd_report(args, emit=emit)
        if subcommand == "recover":
            return cmd_recover(args, emit=emit)
    except E2EError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, indent=2, sort_keys=True, default=str), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover - signal path
        return 130
    return 2


__all__ = [
    "ALLOWED_WITH_FROM",
    "LOCK_IDENTITY_FLAGS",
    "SEMANTIC_FLAGS",
    "SUBCOMMANDS",
    "assert_no_semantic_overrides",
    "build_parser",
    "cmd_list",
    "cmd_plan",
    "cmd_recover",
    "cmd_report",
    "cmd_run",
    "main",
    "parse_e2e_args",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
