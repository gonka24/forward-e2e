"""Planning: turn an explicit source selection into an immutable run lock.

The planner is the only place that decides *what will be proven*. It runs
before anything is built and before the inner Docker daemon is started, so a
selection that cannot possibly work is rejected here rather than after an hour
of compilation.

What the planner does, in order:

1. normalise the scenario selection (profile XOR explicit scenarios);
2. resolve the runner image to an immutable image id;
3. fetch both pinned commits into portable, self-contained Git bundles;
4. measure each checkout and choose a compatibility adapter from what is
   actually in the tree;
5. refuse a selection containing a scenario the chosen adapter cannot run;
6. hash everything that influences the result -- the runner's own harness,
   verifier and catalog, the runner version, the external Kotlin tests, the
   network templates, the Go/Wasm probe harnesses, the build recipes, the
   platform and the semantic environment -- and write ``run.lock.json``.

There is no overlay any more: the lock records each product commit together
with its Git tree SHA and states, in ``source_policy``, that both snapshots are
used unmodified. Nothing in a lock can permit a changed source tree.

The planner never runs code from the target repositories: it only reads files
and asks Git for objects.
"""

from __future__ import annotations

import hashlib
import os
import platform as platform_mod
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..catalog import CATALOG_SCHEMA_VERSION, compute_catalog_hash, resolve_e2e_selection
from ..models import TaskPlan
from .compat import (
    AdapterMatch,
    CompatibilityAdapter,
    assert_scenarios_supported,
    contracts_adapters,
    gonka_adapters,
    select_adapter,
)
from .errors import (
    IntegrityError,
    OutputCollisionError,
    SelectionError,
    UsageError,
)
from .gitio import CredentialProvider, GitClient
from .runlock import (
    RUN_LOCK_FILENAME,
    RUN_LOCK_SCHEMA,
    RunLock,
    content_sha256,
    utc_now_iso,
    write_run_lock,
)
from .runner_image import RunnerImageIdentity, resolve_runner_image
from .sources import AcquiredSource, SourceAcquirer, SourceSpec, resolve_within, sha256_path

# ---------------------------------------------------------------------------
# what belongs to the runner
# ---------------------------------------------------------------------------
#: The acceptance harness. It always comes from the runner image, never from
#: the contracts checkout under test, so that selecting a different contracts
#: SHA cannot silently swap the thing doing the judging.
HARNESS_FILES: Tuple[str, ...] = (
    "scripts/a8_acceptance.py",
    "scripts/a9_release.py",
    "scripts/run_a8_go_boundary.py",
    "scripts/test_wasm_query_boundary.mjs",
    # Drives the runner-owned external Kotlin project against the unmodified
    # upstream Testermint.
    "scripts/a8_external_harness.py",
    # The one implementation of "this snapshot is exactly its commit", loaded
    # by path by the harness and imported by the E2E layer.
    "ops/a8/source_snapshot.py",
    # Stops/starts the *same* API container by id and ownership label; it
    # replaces the helper the old overlay patched into LocalInferencePair.kt.
    "ops/a8/harness/container_control.py",
)

#: Evidence collection, verification and reporting. Same rule: runner-owned.
VERIFIER_FILES: Tuple[str, ...] = (
    "ops/a8/verifier.py",
    "ops/a8/collector.py",
    "ops/a8/reporter.py",
    "ops/a8/models.py",
    "ops/a8/catalog.py",
    # Selects which grading rules apply to a piece of evidence, including
    # whether the running binary must report the prepared commit. Editing it
    # changes what "passed" means, so it belongs in the hash even though doing
    # so invalidates locks planned before it existed.
    "ops/a8/evidence_model.py",
)

#: Files of the runner that decide how the local network is shaped. A change
#: to any of them changes what a run means, so they are hashed into the lock.
#: They are runner templates copied into the Testermint network root, never
#: into the Gonka snapshot (upstream ``init-docker-genesis.sh``,
#: ``DockerGroup.kt`` and ``LocalInferencePair.kt`` are used unmodified).
NETWORK_FILES: Tuple[str, ...] = (
    "ops/a8/harness/network/a8-ownership.yml",
    "ops/a8/harness/network/a8-nats.yml",
    "ops/a8/harness/network/a8-b3-genesis.yml",
    "ops/a8/harness/network/genesis/a8-genesis-provision.sh",
)

#: The runner's own version, recorded separately from the product commits: a
#: plan says which runner judged which pair of product commits.
RUNNER_VERSION_FILE = "ops/a8/RUNNER_VERSION"

#: Runner-owned test code, hashed as whole trees into ``lock.external_tests``.
#: The Kotlin project holds the marketplace scenarios; the network directory
#: holds every template the harness writes into the network root; the Go
#: boundary directory holds the classification module, while wasm-probe holds
#: the optional live P0 Wasm fixture. The synthetic ABI crate lives under
#: tests/contracts and is bound through the selected Marketplace source tree.
EXTERNAL_TEST_DIRS: Tuple[Tuple[str, str], ...] = (
    ("testermint_harness", "ops/a8/harness/testermint"),
    ("network_templates", "ops/a8/harness/network"),
    ("go_boundary", "ops/a8/harness/go-boundary"),
    ("wasm_probe", "ops/a8/harness/wasm-probe"),
)

#: Environment variables that carry *semantic* meaning: they can change the
#: outcome of a run. They are frozen into the lock so that a replay cannot be
#: quietly re-parameterised from a different shell.
SEMANTIC_ENV_PREFIXES: Tuple[str, ...] = ("A8_", "E2E_", "GONKA_", "TESTERMINT_")

#: Operational variables matching the prefixes above that say *where* things
#: live rather than *what* is proven. They are recorded as operational and are
#: allowed to differ on replay.
OPERATIONAL_ENV_NAMES: Tuple[str, ...] = (
    "A8_OUTPUT_DIR",
    "A8_WORKSPACE_DIR",
    "E2E_RUNNER_IMAGE",
    "E2E_RUNNER_IMAGE_ID",
    "E2E_RUNNER_IMAGE_DIGEST",
    "E2E_CREDENTIAL_FILE",
    "E2E_OUTPUT_DIR",
    "E2E_WORKSPACE_DIR",
)


def _reject_runner_asset_symlinks(root: Path, relpath: str) -> None:
    """Refuse links in an asset or any parent below the runner root."""
    current = Path(root)
    for part in Path(relpath).parts:
        current = current / part
        if current.is_symlink():
            raise IntegrityError(
                "A runner asset or its parent is a symlink",
                {"path": current.relative_to(root).as_posix()},
            )


def default_runner_root() -> Path:
    """Repository root of the runner itself (``/app`` inside the image)."""
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class RunnerLayout:
    """Where the runner's own, versioned assets live."""

    root: Path

    @property
    def harness_script(self) -> Path:
        return self.root / "scripts" / "a8_acceptance.py"

    @property
    def testermint_harness_dir(self) -> Path:
        return self.root / "ops" / "a8" / "harness" / "testermint"

    @property
    def runner_version_file(self) -> Path:
        return self.root / RUNNER_VERSION_FILE

    def runner_version(self) -> str:
        text = self.runner_version_file.read_text(encoding="utf-8").strip()
        if not text:
            raise IntegrityError("The runner version file is empty", {"path": RUNNER_VERSION_FILE})
        return text

    def assert_complete(self) -> None:
        for rel in HARNESS_FILES + VERIFIER_FILES + NETWORK_FILES + (RUNNER_VERSION_FILE,):
            _reject_runner_asset_symlinks(self.root, rel)
        for _, rel in EXTERNAL_TEST_DIRS:
            _reject_runner_asset_symlinks(self.root, rel)
        missing = [
            rel
            for rel in HARNESS_FILES + VERIFIER_FILES + NETWORK_FILES + (RUNNER_VERSION_FILE,)
            if not (self.root / rel).is_file()
        ]
        missing += [rel for _, rel in EXTERNAL_TEST_DIRS if not (self.root / rel).is_dir()]
        if missing:
            raise IntegrityError(
                "The runner image is incomplete: files that define the harness, the verifier, "
                "the external tests or the network templates are missing. Rebuild the runner "
                "image.",
                {"root": str(self.root), "missing": missing},
            )


def hash_runner_files(root: Path, relpaths: Sequence[str]) -> str:
    """Hash a fixed list of runner files, path names included.

    A missing file is an error rather than a silently shorter hash: otherwise
    deleting the verifier would produce a perfectly valid-looking lock.
    """
    digest = hashlib.sha256()
    for rel in sorted(relpaths):
        _reject_runner_asset_symlinks(Path(root), rel)
        path = Path(root) / rel
        if not path.is_file():
            raise IntegrityError(
                "A runner file required for the lock identity is missing",
                {"path": rel, "root": str(root)},
            )
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


#: Never part of a tree hash: interpreter caches appear when the runner's own
#: tests run and say nothing about the test code.
_TREE_HASH_IGNORED = ("__pycache__",)


def hash_runner_tree(root: Path, reldir: str) -> Dict[str, Any]:
    """Hash every file under one runner directory, path names included.

    Symlinks are refused rather than followed: a link could make the hash
    describe a file outside the runner. A missing directory is an error, never
    an empty hash.
    """
    _reject_runner_asset_symlinks(Path(root), reldir)
    base = Path(root) / reldir
    if base.is_symlink() or not base.is_dir():
        raise IntegrityError(
            "A runner directory required for the lock identity is missing or is a symlink",
            {"path": reldir, "root": str(root)},
        )
    digest = hashlib.sha256()
    files: List[str] = []
    for current, dirnames, filenames in os.walk(base, followlinks=False):
        for name in dirnames:
            path = Path(current) / name
            if path.is_symlink():
                raise IntegrityError(
                    "A runner test directory is a symlink",
                    {"path": path.relative_to(Path(root)).as_posix()},
                )
        dirnames[:] = sorted(d for d in dirnames if d not in _TREE_HASH_IGNORED)
        for name in sorted(filenames):
            path = Path(current) / name
            rel = path.relative_to(Path(root)).as_posix()
            if path.is_symlink():
                raise IntegrityError("A runner test file is a symlink", {"path": rel})
            files.append(rel)
    for rel in sorted(files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update((Path(root) / rel).read_bytes())
        digest.update(b"\0")
    return {"relpath": reldir, "sha256": digest.hexdigest(), "file_count": len(files)}


def collect_semantic_environment(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Split the relevant environment into semantic and operational halves."""
    environ = dict(env if env is not None else os.environ)
    semantic: Dict[str, str] = {}
    operational: List[str] = []
    for name in sorted(environ):
        if not name.startswith(SEMANTIC_ENV_PREFIXES):
            continue
        if name in OPERATIONAL_ENV_NAMES:
            operational.append(name)
            continue
        semantic[name] = environ[name]
    return {
        "semantic_environment": semantic,
        "operational_environment_names": operational,
        "note": (
            "Semantic values are frozen here so a replay cannot be re-parameterised from a "
            "different shell. Operational variables are recorded by name only because they "
            "describe where things live, not what is proven."
        ),
    }


def describe_platform() -> Dict[str, Any]:
    machine = platform_mod.machine().lower()
    docker_platform = "linux/arm64" if machine in ("arm64", "aarch64") else "linux/amd64"
    return {
        "os": platform_mod.system(),
        "release": platform_mod.release(),
        "machine": machine,
        "docker_platform": docker_platform,
        "python": platform_mod.python_version(),
    }


# ---------------------------------------------------------------------------
# request / result
# ---------------------------------------------------------------------------
@dataclass
class PlanRequest:
    """Everything the user chose, already parsed but not yet validated."""

    gonka: SourceSpec
    contracts: SourceSpec
    output_dir: Path
    profile: Optional[str] = None
    scenarios: Optional[Sequence[str]] = None
    runner_image_locator: Optional[str] = None
    credential: Optional[CredentialProvider] = None
    # Explicit directories are caller-owned and retained; defaults are temporary.
    scratch_dir: Optional[Path] = None
    worktree_root: Optional[Path] = None
    plan_id: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    create_bundles: bool = True


@dataclass
class PlanResult:
    """Plan metadata; source worktrees survive only with an explicit worktree_root."""

    lock: RunLock
    lock_path: Path
    package_dir: Path
    tasks: List[TaskPlan]
    gonka_source: AcquiredSource
    contracts_source: AcquiredSource
    gonka_match: AdapterMatch
    contracts_match: AdapterMatch
    runner_image: RunnerImageIdentity


def generate_plan_id() -> str:
    return f"plan-{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------
def build_plan(
    request: PlanRequest,
    *,
    git: Optional[GitClient] = None,
    docker_runner: Optional[Callable[..., Any]] = None,
    runner_root: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    emit: Optional[Callable[[str], None]] = None,
) -> PlanResult:
    """Produce and persist an immutable ``run.lock.json`` for this selection."""
    say = emit or (lambda message: None)
    layout = RunnerLayout(Path(runner_root) if runner_root else default_runner_root())
    layout.assert_complete()

    # 1. selection ---------------------------------------------------------
    try:
        tasks, resolved_profile, normalised_scenarios = resolve_e2e_selection(
            profile=request.profile, scenarios=request.scenarios
        )
    except ValueError as exc:
        raise SelectionError(str(exc)) from exc
    scenario_ids = [task.task_id for task in tasks]
    selection_label = resolved_profile and f"--profile {resolved_profile}" or "--scenario selection"

    # 2. output location ---------------------------------------------------
    package_dir = Path(request.output_dir).resolve()
    lock_path = package_dir / RUN_LOCK_FILENAME
    if package_dir.exists() and not package_dir.is_dir():
        raise OutputCollisionError(
            "The --output path exists and is not a directory.",
            {"path": str(package_dir)},
        )
    if lock_path.exists():
        raise OutputCollisionError(
            "A plan already exists at this output location. A lock is never rewritten; "
            "choose a new --output directory or replay the existing plan with --from.",
            {"path": str(lock_path)},
        )
    if package_dir.exists() and any(package_dir.iterdir()):
        raise OutputCollisionError(
            "The --output directory is not empty. Planning into it could mix two source "
            "packages, so it is refused before anything is fetched.",
            {"path": str(package_dir)},
        )
    package_dir.mkdir(parents=True, exist_ok=True)

    # 3. runner identity ---------------------------------------------------
    runner_image = resolve_runner_image(
        request.runner_image_locator, docker_runner=docker_runner, env=env, emit=say
    )

    # 4. sources -----------------------------------------------------------
    if request.credential is not None:
        request.credential.validate()
    # Only planner-owned temporary directories are cleaned up, on success and
    # failure alike. Explicit roots belong to the caller and remain available
    # for inspection; neither default lives inside the portable package.
    with ExitStack() as temporary_dirs:
        scratch_dir = (
            Path(request.scratch_dir) if request.scratch_dir is not None else
            Path(temporary_dirs.enter_context(TemporaryDirectory(prefix="e2e-plan-scratch-")))
        )
        worktree_root = (
            Path(request.worktree_root) if request.worktree_root is not None else
            Path(temporary_dirs.enter_context(TemporaryDirectory(prefix="e2e-plan-worktrees-")))
        )
        # Authentication helpers share the scratch directory lifecycle.
        client = git or GitClient(
            credentials=request.credential, helper_dir=scratch_dir / "git-helper"
        )
        acquirer = SourceAcquirer(client, package_dir=package_dir, scratch_dir=scratch_dir)
        acquire_kwargs = {} if request.create_bundles else {"create_bundle": False}

        say(f"Fetching Gonka commit {request.gonka.commit_sha} from {request.gonka.origin}")
        gonka_source = acquirer.acquire(request.gonka, worktree_root=worktree_root, **acquire_kwargs)
        say(f"Fetching contracts commit {request.contracts.commit_sha} from {request.contracts.origin}")
        contracts_source = acquirer.acquire(request.contracts, worktree_root=worktree_root, **acquire_kwargs)

        # 5. compatibility -----------------------------------------------------
        gonka_match = select_adapter(
            gonka_adapters(),
            root=gonka_source.worktree,
            commit_sha=request.gonka.commit_sha,
            role="gonka",
        )
        contracts_match = select_adapter(
            contracts_adapters(),
            root=contracts_source.worktree,
            commit_sha=request.contracts.commit_sha,
            role="contracts",
        )
        say(f"Gonka adapter: {gonka_match.adapter.adapter_id} ({gonka_match.match_mode})")
        say(f"Contracts adapter: {contracts_match.adapter.adapter_id} ({contracts_match.match_mode})")

        assert_scenarios_supported(
            gonka_match.adapter, scenario_ids, selection_label=selection_label
        )
        assert_scenarios_supported(
            contracts_match.adapter, scenario_ids, selection_label=selection_label
        )

        # 6. lock --------------------------------------------------------------
        plan_id = (request.plan_id or generate_plan_id()).strip()
        if not plan_id:
            raise UsageError("Plan id cannot be empty")

        lock = RunLock(
            schema_version=RUN_LOCK_SCHEMA,
            plan_id=plan_id,
            created_at_utc=utc_now_iso(),
            gonka=_product_record(gonka_source, client),
            contracts=_product_record(contracts_source, client),
            runner=_runner_section(layout, runner_image),
            platform=describe_platform(),
            compatibility={
                "gonka": gonka_match.adapter.lock_record(
                    match_mode=gonka_match.match_mode, measurements=gonka_match.measurements
                ),
                "contracts": contracts_match.adapter.lock_record(
                    match_mode=contracts_match.match_mode,
                    measurements=contracts_match.measurements,
                ),
            },
            selection=_selection_section(resolved_profile, normalised_scenarios, tasks),
            limits=_limits_section(tasks),
            network=_network_section(layout),
            build=_build_section(gonka_match.adapter, contracts_match.adapter),
            source_policy=_source_policy_section(),
            external_tests=_external_tests_section(layout),
            semantic_inputs=collect_semantic_environment(env),
            source_package=_source_package_section(gonka_source, contracts_source),
            notes=list(request.notes),
        )

        write_run_lock(lock, lock_path)
        say(f"Wrote {lock_path} (lock_sha256={lock.lock_sha256})")

        return PlanResult(
            lock=lock,
            lock_path=lock_path,
            package_dir=package_dir,
            tasks=tasks,
            gonka_source=gonka_source,
            contracts_source=contracts_source,
            gonka_match=gonka_match,
            contracts_match=contracts_match,
            runner_image=runner_image,
        )


# ---------------------------------------------------------------------------
# lock sections
# ---------------------------------------------------------------------------
def _runner_section(layout: RunnerLayout, image: RunnerImageIdentity) -> Dict[str, Any]:
    from ..orchestrator import compute_runner_hash
    from ..runtime import RuntimeErrorA8

    section = image.to_dict()
    try:
        runner_source_hash = compute_runner_hash(layout.root / "ops" / "a8")
    except RuntimeErrorA8 as exc:
        raise IntegrityError(str(exc)) from exc
    section.update(
        {
            "runner_source_hash": runner_source_hash,
            "harness_hash": hash_runner_files(layout.root, HARNESS_FILES),
            "harness_files": list(HARNESS_FILES),
            "verifier_hash": hash_runner_files(layout.root, VERIFIER_FILES),
            "verifier_files": list(VERIFIER_FILES),
            "catalog_hash": compute_catalog_hash(),
            "catalog_schema_version": CATALOG_SCHEMA_VERSION,
            "harness_script_relpath": "scripts/a8_acceptance.py",
            # Recorded separately from the product commits: which runner
            # judged, independent of which Gonka/Marketplace pair was judged.
            "runner_version": layout.runner_version(),
            "runner_version_file": RUNNER_VERSION_FILE,
            "runner_version_sha256": sha256_path(layout.runner_version_file),
            "note": (
                "The harness, the catalog and the verifier are always taken from this runner "
                "image. Selecting a different contracts commit never changes them."
            ),
        }
    )
    return section


def _selection_section(
    profile: Optional[str],
    normalised: Optional[Sequence[str]],
    tasks: Sequence[TaskPlan],
) -> Dict[str, Any]:
    return {
        "profile": profile,
        "scenarios": [task.task_id for task in tasks],
        "requested_scenarios": list(normalised) if normalised is not None else None,
        "ordered_by": "catalog",
        "proof_levels": {task.task_id: task.proof_level.value for task in tasks},
    }


def _limits_section(tasks: Sequence[TaskPlan]) -> Dict[str, Any]:
    return {
        "task_timeout_minutes": {task.task_id: task.timeout_minutes for task in tasks},
        "stage_timeout_seconds": {task.task_id: task.stage_timeout_seconds for task in tasks},
        "gradle_timeout_minutes": {
            task.task_id: task.gradle_timeout_minutes for task in tasks
        },
        "total_budget_minutes": sum(task.timeout_minutes for task in tasks),
    }


def _network_section(layout: RunnerLayout) -> Dict[str, Any]:
    files: Dict[str, str] = {}
    for rel in NETWORK_FILES:
        path = layout.root / rel
        if not path.is_file():
            raise IntegrityError("A required network file is missing", {"path": rel})
        files[rel] = sha256_path(path)
    return {
        "topology": "local-test-net (genesis + join1 + join2), one owned network per task",
        "state_reuse": "never; every run and every replay starts from a fresh network",
        "config_hashes": files,
        "genesis_note": (
            "Upstream init-docker-genesis.sh runs unmodified. Genesis shaping for B3 comes "
            "from the runner's external provisioner and compose templates, whose hashes are "
            "recorded here. A different template is a different plan."
        ),
    }


def _build_section(
    gonka_adapter: CompatibilityAdapter, contracts_adapter: CompatibilityAdapter
) -> Dict[str, Any]:
    gonka_recipe = gonka_adapter.build.to_dict() if gonka_adapter.build else None
    contracts_recipe = contracts_adapter.build.to_dict() if contracts_adapter.build else None
    payload = {
        "gonka_recipe": gonka_recipe,
        "contracts_recipe": contracts_recipe,
        "artifact_reuse": (
            "disabled; previously built images and binaries are never substituted for a "
            "build of the selected sources"
        ),
    }
    payload["recipes_hash"] = content_sha256(
        {"gonka": gonka_recipe, "contracts": contracts_recipe}
    )
    return payload


def _product_record(source: AcquiredSource, git: GitClient) -> Dict[str, Any]:
    """The acquired commit plus its Git tree SHA.

    The tree SHA is what the pristine-snapshot check compares against: two
    commits can share a tree, but a snapshot whose tree differs from the one
    planned is not the planned source, whatever its HEAD says.
    """
    record = source.to_lock_record()
    record["tree_sha"] = git.stdout(
        ["-C", str(source.worktree), "rev-parse", "--verify", f"{source.spec.commit_sha}^{{tree}}"],
        timeout=120.0,
    ).strip().lower()
    return record


def _source_policy_section() -> Dict[str, Any]:
    return {
        "model": "immutable-source",
        "modifications_allowed": False,
        "checks": [
            "HEAD and HEAD^{tree} equal the locked commit and tree",
            "index lists exactly the blobs of HEAD",
            "no tracked file changed, deleted or retyped (content hashed from disk)",
            "no untracked or ignored file present (git status --ignored)",
            "every submodule checked out at its pinned commit",
            "no file of the retired overlay mechanism present",
        ],
        "phases": ["before_build", "after_build", "after_execution"],
        "note": (
            "Both product snapshots are materialised from Git objects and must stay byte-for-"
            "byte the locked commits from before the first build step until after the last "
            "scenario. Any difference is a blocking finding; there is no allow-list."
        ),
    }


def _external_tests_section(layout: RunnerLayout) -> Dict[str, Any]:
    trees = {label: hash_runner_tree(layout.root, rel) for label, rel in EXTERNAL_TEST_DIRS}
    return {
        "trees": trees,
        "tests_hash": content_sha256({label: tree["sha256"] for label, tree in trees.items()}),
        "note": (
            "Runner-owned test code. It is compiled and run against the unmodified selected "
            "commits and never copied into them."
        ),
    }


def _source_package_section(
    gonka_source: AcquiredSource, contracts_source: AcquiredSource
) -> Dict[str, Any]:
    return {
        "layout": "relative to the directory containing run.lock.json",
        "entries": [
            {
                "role": "gonka",
                "bundle_relpath": gonka_source.bundle_relpath,
                "bundle_sha256": gonka_source.bundle_sha256,
                "bundle_ref": gonka_source.bundle_ref,
                "commit_sha": gonka_source.spec.commit_sha,
            },
            {
                "role": "contracts",
                "bundle_relpath": contracts_source.bundle_relpath,
                "bundle_sha256": contracts_source.bundle_sha256,
                "bundle_ref": contracts_source.bundle_ref,
                "commit_sha": contracts_source.spec.commit_sha,
            },
        ]
        + [
            {
                "role": f"{source.spec.role}-submodule:{sub.path}",
                "bundle_relpath": sub.bundle_relpath,
                "bundle_sha256": sub.bundle_sha256,
                "bundle_ref": "refs/e2e/submodule",
                "commit_sha": sub.commit_sha,
            }
            for source in (gonka_source, contracts_source)
            for sub in source.submodules
        ],
        "portability": (
            "Self-contained Git bundles; replay works offline."
            if gonka_source.bundle_relpath and contracts_source.bundle_relpath else
            "The selected URLs and full commit SHAs are recorded; replay fetches "
            "those exact commits again when no bundles were requested."
        ),
    }


__all__ = [
    "EXTERNAL_TEST_DIRS",
    "HARNESS_FILES",
    "RUNNER_VERSION_FILE",
    "hash_runner_tree",
    "NETWORK_FILES",
    "VERIFIER_FILES",
    "PlanRequest",
    "PlanResult",
    "RunnerLayout",
    "build_plan",
    "collect_semantic_environment",
    "default_runner_root",
    "describe_platform",
    "generate_plan_id",
    "hash_runner_files",
]
