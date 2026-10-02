#!/usr/bin/env python3
"""Run the Marketplace Testermint scenarios *next to* Gonka, never inside it.

Why this module exists
----------------------
The old runner made Gonka testable by changing it: a test overlay was copied
into the checkout, committed as a "prepared" commit, and Testermint was built
and run from that modified tree (it even wrote a ``gradlew.a8-linux`` copy into
it). A PASS therefore described a tree nobody had selected.

In the immutable-source model the two product snapshots are read-only. What the
old overlay did is split into four runner-owned pieces, all implemented here so
the online path (``scripts/a8_acceptance.py run-live``) and the build-only path
(``build-external-harness``) share one implementation:

* **API compatibility** -- the external Kotlin project compiles against the
  selected Gonka's unmodified Testermint. The symbols it needs are declared in
  ``ops/a8/harness/testermint/required-upstream-api.json`` and are checked
  *before* any network is created, so an incompatible Gonka fails with the
  name of every missing API instead of a Kotlin compiler error mid-run.
* **Network root** -- Testermint derives every compose/resource path from
  ``GONKA_REPO_ROOT``. That root is a separate work directory holding
  byte-identical copies of the upstream resources Testermint reads plus the
  runner-generated compose files, each at its own path. Provenance of the
  sources is established separately (``ops/a8/source_snapshot.py``).
* **Out-of-tree Gradle** -- both Gradle builds are launched through the
  selected Gonka's own wrapper *jar* (so the Gradle version is Gonka's), with
  every cache, build directory and Kotlin state directory redirected into the
  work root. No ``gradlew`` copy is ever written anywhere.
* **Evidence** -- JUnit XML is collected from the external build directory and
  a selected test that did not run is an error, never an empty pass; the B3
  genesis fixture is verified as an exact, isolated delta.

Constraints
-----------
Standard library only, and no ``ops.*`` or relative imports: this file is loaded
by path from ``scripts/a8_acceptance.py`` inside the runner image and re-entered
by Testermint, where the ``ops`` package is not importable. The one shared
dependency, ``ops/a8/source_snapshot.py``, is loaded by path for the same
reason (see ``load_source_snapshot``).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

RUNNER_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SNAPSHOT_PATH = RUNNER_ROOT / "ops" / "a8" / "source_snapshot.py"
DEFAULT_TESTERMINT_HARNESS_DIR = RUNNER_ROOT / "ops" / "a8" / "harness" / "testermint"
DEFAULT_NETWORK_TEMPLATES_DIR = RUNNER_ROOT / "ops" / "a8" / "harness" / "network"
CONTAINER_CONTROL_PATH = RUNNER_ROOT / "ops" / "a8" / "harness" / "container_control.py"

REQUIRED_API_SCHEMA = "a8.testermint-required-api/1"
API_COMPAT_SCHEMA = "a8.testermint-api-compat/1"
NETWORK_MANIFEST_SCHEMA = "a8.network-manifest/1"
HARNESS_INPUTS_SCHEMA = "a8.external-harness-inputs/1"
JUNIT_SUMMARY_SCHEMA = "a8.testermint-junit/1"
B3_VERIFICATION_SCHEMA = "a8.b3-genesis-verification/1"

OWNERSHIP_LABEL = "io.gonka.a8.run-id"

#: ``rootProject.name`` of ops/a8/harness/testermint (settings.gradle.kts). The
#: out-of-tree init script places the build directory at
#: ``<a8.outRoot>/<rootProject.name>``, so JUnit XML is found under it.
HARNESS_ROOT_PROJECT_NAME = "a8-marketplace-testermint-harness"

# Mandatory upstream paths (relative to the Gonka repository root) that must
# exist in the selected commit before full working-tree materialisation.
# The runtime copy itself materialises the entire tracked Gonka tree (including
# submodule files) rather than a selective allowlist, so newly added upstream
# files are automatically present in GONKA_REPO_ROOT.
REQUIRED_UPSTREAM_NETWORK_DIRS = ("local-test-net", "testermint/src/main/resources")
REQUIRED_UPSTREAM_NETWORK_FILES = ("inference-chain/test_genesis_overrides.json",)

# Runner-generated files: template path (relative to the templates dir) ->
# path inside the network root. They never share a path with an upstream copy.
GENERATED_NETWORK_FILES = (
    ("a8-ownership.yml", "local-test-net/a8-ownership.yml", False),
    ("a8-nats.yml", "local-test-net/a8-nats.yml", False),
    ("a8-b3-genesis.yml", "local-test-net/a8-b3-genesis.yml", True),
    ("genesis/a8-genesis-provision.sh", "a8/genesis/a8-genesis-provision.sh", True),
)
PAIR_NAMES = ("genesis", "join1", "join2")
BASE_ADDITIONAL_COMPOSE_FILES = ("a8-ownership.yml", "a8-nats.yml")
B3_ADDITIONAL_COMPOSE_FILE = "a8-b3-genesis.yml"

# Directories the removed DockerGroup.dockerBindOwner patch used to create (and
# chown back to the host user) before compose started. Prepared here, by the
# runner, in its own work root. Upstream Testermint still wipes and recreates
# ``prod-local`` for the genesis pair; compose short-syntax binds then recreate
# any missing bind source, so pre-creation is a convenience, never a
# correctness dependency.
PROD_LOCAL_DIRECTORIES = tuple(
    [f"prod-local/{pair}" for pair in PAIR_NAMES]
    + [f"prod-local/mock-server/{pair}/{leaf}" for pair in PAIR_NAMES for leaf in ("mappings", "__files")]
    + [f"prod-local/nats/{pair}" for pair in PAIR_NAMES]
)

#: sha256 of ``inference-chain/scripts/init-docker-genesis.sh`` at Gonka
#: c33c9eaa5bc40c53b564159b5e1534bbfdab8a08, the script the B3 provisioner
#: (``ops/a8/harness/network/genesis/a8-genesis-provision.sh``) reproduces step
#: by step. A different upstream script means the provisioner might silently
#: skip or reorder an upstream step, so B3 is refused, never auto-adapted.
GENESIS_PROVISIONER_UPSTREAM_SHA256 = "02355d2ca35647c8ea92deefc4ddd0ca2ee02e3b5f8d4efee6c13d2a8fa8dd1e"
GENESIS_PROVISIONER_UPSTREAM_SOURCE_SHA = "c33c9eaa5bc40c53b564159b5e1534bbfdab8a08"
UPSTREAM_GENESIS_SCRIPT = "inference-chain/scripts/init-docker-genesis.sh"
PROVISION_DIR_IN_PROD_LOCAL = "prod-local/genesis/a8-provision"

# B3 fixture. Must equal the Kotlin constants in
# ops/a8/harness/testermint/src/test/kotlin/MarketplaceContractAcceptanceTests.kt
# (B3_FOREIGN_ADDRESS / B3_FOREIGN_DENOM / B3_FOREIGN_AMOUNT) and the values in
# ops/a8/harness/network/a8-b3-genesis.yml.
B3_FOREIGN_ADDRESS = "gonka1k4swv40ur28fvu54p8mskjj4lxkgsj07u9f8ny"
B3_FOREIGN_DENOM = "ua8b3foreign"
B3_FOREIGN_AMOUNT = 12345

UPSTREAM_TESTERMINT_TEST = "testermint/src/test/kotlin/TestermintTest.kt"
UPSTREAM_GRADLE_WRAPPER_JAR = "testermint/gradle/wrapper/gradle-wrapper.jar"
DEFAULT_API_SEARCH_GLOB_ROOT = "testermint/src/main/kotlin"

# Error codes (stable strings; the harness maps them to exit code 1).
CODE_API_MISSING = "TESTERMINT_API_MISSING"
CODE_API_SPEC_INVALID = "TESTERMINT_API_SPEC_INVALID"
CODE_NETWORK_ROOT_NOT_EMPTY = "NETWORK_ROOT_NOT_EMPTY"
CODE_NETWORK_ROOT_SYMLINK = "NETWORK_ROOT_SYMLINK"
CODE_NETWORK_SOURCE_MISSING = "NETWORK_SOURCE_MISSING"
CODE_NETWORK_SYMLINK = "NETWORK_SOURCE_SYMLINK"
CODE_NETWORK_COPY_MISMATCH = "NETWORK_COPY_MISMATCH"
CODE_NETWORK_COLLISION = "NETWORK_GENERATED_COLLISION"
CODE_NETWORK_TEMPLATE_MISSING = "NETWORK_TEMPLATE_MISSING"
CODE_GENESIS_INCOMPATIBLE = "GENESIS_PROVISIONER_INCOMPATIBLE"
CODE_SELECTED_TEST_NOT_EXECUTED = "SELECTED_TEST_NOT_EXECUTED"
CODE_JUNIT_INVALID = "JUNIT_REPORT_INVALID"
CODE_GRADLE_WRAPPER_MISSING = "GRADLE_WRAPPER_MISSING"
CODE_HARNESS_INPUT_INVALID = "HARNESS_INPUT_INVALID"
CODE_WORK_ROOT_INSIDE_SNAPSHOT = "WORK_ROOT_INSIDE_SNAPSHOT"
CODE_B3_EVIDENCE_MISSING = "B3_GENESIS_EVIDENCE_MISSING"


class ExternalHarnessError(RuntimeError):
    """A fail-closed refusal of the external harness, with a stable code."""

    def __init__(self, message: str, *, code: str, details: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.details: Dict[str, Any] = dict(details or {})


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


def load_source_snapshot():
    """Load ``ops/a8/source_snapshot.py`` by path (``ops`` is not importable here)."""
    name = "a8_source_snapshot"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SOURCE_SNAPSHOT_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ExternalHarnessError(
            f"cannot load {SOURCE_SNAPSHOT_PATH}", code=CODE_HARNESS_INPUT_INVALID
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def default_work_root(gonka_dir: Path, run_id: str) -> Path:
    """``<gonka_dir>/../a8-work/<run_id>``: beside, never inside, the snapshots."""
    return Path(gonka_dir).resolve().parent / "a8-work" / run_id


def assert_work_root_outside(work_root: Path, snapshots: Iterable[Path]) -> None:
    """Refuse a work root that would put build output inside a source snapshot."""
    for snapshot in snapshots:
        snapshot = Path(snapshot).resolve()
        work = Path(work_root).resolve()
        if _is_within(work, snapshot) or _is_within(snapshot, work):
            raise ExternalHarnessError(
                f"work root {work} overlaps source snapshot {snapshot}",
                code=CODE_WORK_ROOT_INSIDE_SNAPSHOT,
                details={"work_root": str(work), "snapshot": str(snapshot)},
            )


class WorkLayout:
    """The fixed layout of one work root W (design contract, section 1)."""

    def __init__(self, work_root: Path, *, gradle_user_home: Optional[Path] = None):
        self.root = Path(work_root).resolve()
        self.gradle_home = Path(gradle_user_home).resolve() if gradle_user_home is not None else self.root / "gradle-home"
        self.upstream_project_cache = self.root / "gradle-project-cache" / "upstream"
        self.harness_project_cache = self.root / "gradle-project-cache" / "harness"
        self.kotlin_upstream = self.root / "kotlin" / "upstream"
        self.kotlin_harness = self.root / "kotlin" / "harness"
        self.upstream_build = self.root / "upstream-build"
        self.harness_build = self.root / "harness-build"
        self.classpath_file = self.root / "testermint-classpath.txt"
        self.network_root = self.root / "network-root"
        self.wasm_target = self.root / "wasm-target"

    @property
    def junit_results_dir(self) -> Path:
        # The shared init script sets each root project's build directory to
        # ``<a8.outRoot>/<rootProject.name>``.
        return self.harness_build / HARNESS_ROOT_PROJECT_NAME / "test-results" / "test"

    def create(self) -> None:
        for directory in (
            self.gradle_home,
            self.upstream_project_cache,
            self.harness_project_cache,
            self.kotlin_upstream,
            self.kotlin_harness,
            self.upstream_build,
            self.harness_build,
            self.wasm_target,
        ):
            directory.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# (a) Required upstream API
# ---------------------------------------------------------------------------


def _load_api_spec(spec_path: Path) -> Dict[str, Any]:
    try:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExternalHarnessError(
            f"cannot read required upstream API spec {spec_path}: {exc}",
            code=CODE_API_SPEC_INVALID,
        ) from exc
    if not isinstance(spec, dict) or spec.get("schema") != REQUIRED_API_SCHEMA:
        raise ExternalHarnessError(
            f"{spec_path} is not a {REQUIRED_API_SCHEMA} document", code=CODE_API_SPEC_INVALID
        )
    symbols = spec.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise ExternalHarnessError(
            f"{spec_path} declares no symbols; an empty API contract proves nothing",
            code=CODE_API_SPEC_INVALID,
        )
    seen = set()
    for item in symbols:
        if not isinstance(item, dict):
            raise ExternalHarnessError("API symbol must be an object", code=CODE_API_SPEC_INVALID)
        for key in ("id", "kind", "pattern", "description"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ExternalHarnessError(
                    f"API symbol {item.get('id')!r} has no {key!r}", code=CODE_API_SPEC_INVALID
                )
        if item["id"] in seen:
            raise ExternalHarnessError(
                f"duplicate API symbol id {item['id']!r}", code=CODE_API_SPEC_INVALID
            )
        seen.add(item["id"])
        try:
            re.compile(item["pattern"], re.MULTILINE)
        except re.error as exc:
            raise ExternalHarnessError(
                f"API symbol {item['id']!r} has an invalid pattern: {exc}",
                code=CODE_API_SPEC_INVALID,
            ) from exc
    return spec


def _default_api_search_files(gonka_dir: Path) -> List[Path]:
    root = Path(gonka_dir) / DEFAULT_API_SEARCH_GLOB_ROOT
    files = sorted(path for path in root.rglob("*.kt") if path.is_file() and not path.is_symlink())
    test_file = Path(gonka_dir) / UPSTREAM_TESTERMINT_TEST
    if test_file.is_file() and not test_file.is_symlink():
        files.append(test_file)
    return files


def _resolve_symbol_file(gonka_dir: Path, rel: str, symbol_id: str) -> List[Path]:
    """A spec ``file`` is repo-relative; escapes and symlinks are refused.

    A declared file that simply does not exist yields no search file, so the
    symbol is reported missing -- absence is never agreement.
    """
    relative = Path(rel)
    if relative.is_absolute() or ".." in relative.parts:
        raise ExternalHarnessError(
            f"API symbol {symbol_id!r} names a file outside the repository: {rel}",
            code=CODE_API_SPEC_INVALID,
        )
    candidate = gonka_dir / relative
    probe = gonka_dir
    for part in relative.parts:
        probe = probe / part
        if probe.is_symlink():
            raise ExternalHarnessError(
                f"API symbol {symbol_id!r} file {rel} goes through a symlink",
                code=CODE_API_SPEC_INVALID,
            )
    if not _is_within(candidate, gonka_dir):
        raise ExternalHarnessError(
            f"API symbol {symbol_id!r} file {rel} resolves outside the repository",
            code=CODE_API_SPEC_INVALID,
        )
    return [candidate] if candidate.is_file() else []


def evaluate_required_upstream_api(gonka_dir: Path, spec_path: Path) -> Dict[str, Any]:
    """Match every declared symbol against the selected Gonka. Reads only."""
    gonka_dir = Path(gonka_dir).resolve()
    spec = _load_api_spec(spec_path)
    default_files = _default_api_search_files(gonka_dir)
    cache: Dict[Path, str] = {}

    def text_of(path: Path) -> str:
        if path not in cache:
            cache[path] = path.read_text(encoding="utf-8", errors="replace")
        return cache[path]

    results = []
    missing = []
    for item in spec["symbols"]:
        pattern = re.compile(item["pattern"], re.MULTILINE)
        if item.get("file"):
            files = _resolve_symbol_file(gonka_dir, str(item["file"]), item["id"])
        else:
            files = default_files
        matched = [
            path.relative_to(gonka_dir).as_posix() for path in files if pattern.search(text_of(path))
        ]
        found = bool(matched)
        results.append(
            {
                "id": item["id"],
                "kind": item["kind"],
                "description": item["description"],
                "pattern": item["pattern"],
                "found": found,
                "matched_files": matched,
            }
        )
        if not found:
            missing.append({"id": item["id"], "kind": item["kind"], "description": item["description"]})
    return {
        "schema": API_COMPAT_SCHEMA,
        "spec_path": str(Path(spec_path).resolve()),
        "spec_sha256": sha256_file(Path(spec_path)),
        "upstream_reference_sha": spec.get("upstream_reference_sha"),
        "gonka_dir": str(gonka_dir),
        "searched_files": len(default_files),
        "symbols": results,
        "missing": missing,
        "verdict": "COMPATIBLE" if not missing else "INCOMPATIBLE",
    }


def check_required_upstream_api(gonka_dir: Path, spec_path: Path) -> Dict[str, Any]:
    """Return the compat result, or raise naming every missing upstream API.

    Called before any network is created: an incompatible Gonka must fail with
    the missing names, never be patched and never half-start a network.
    """
    result = evaluate_required_upstream_api(gonka_dir, spec_path)
    if result["missing"]:
        names = ", ".join(f"{item['id']} ({item['description']})" for item in result["missing"])
        raise ExternalHarnessError(
            f"selected Gonka Testermint lacks required API: {names}",
            code=CODE_API_MISSING,
            details={"result": result},
        )
    return result


# ---------------------------------------------------------------------------
# (b) Network root
# ---------------------------------------------------------------------------


def _is_under_required_network_path(rel: str) -> bool:
    for rel_root in REQUIRED_UPSTREAM_NETWORK_DIRS:
        if rel == rel_root or rel.startswith(rel_root + "/"):
            return True
    return rel in REQUIRED_UPSTREAM_NETWORK_FILES


def _iter_full_upstream_tree(gonka_dir: Path) -> List[Tuple[str, Path, str]]:
    """Walk the full materialised Gonka tree (including submodules), excluding .git."""
    for rel_root in REQUIRED_UPSTREAM_NETWORK_DIRS:
        source_root = gonka_dir / rel_root
        if source_root.is_symlink():
            raise ExternalHarnessError(
                f"upstream network source {rel_root} is a symlink; symlinks are refused",
                code=CODE_NETWORK_SYMLINK,
                details={"path": rel_root},
            )
        if not source_root.is_dir():
            raise ExternalHarnessError(
                f"upstream network source {rel_root} is missing in {gonka_dir}",
                code=CODE_NETWORK_SOURCE_MISSING,
                details={"path": rel_root},
            )
    for rel in REQUIRED_UPSTREAM_NETWORK_FILES:
        source = gonka_dir / rel
        if source.parent.is_symlink() or source.is_symlink() or not source.is_file():
            raise ExternalHarnessError(
                f"upstream network source {rel} is missing or not a regular file",
                code=CODE_NETWORK_SOURCE_MISSING,
                details={"path": rel},
            )

    entries: List[Tuple[str, Path, str]] = []
    for current, dirnames, filenames in os.walk(gonka_dir, followlinks=False):
        current_path = Path(current)
        kept_dirs: List[str] = []
        for dirname in sorted(dirnames):
            if dirname == ".git":
                continue
            dir_entry = current_path / dirname
            rel = dir_entry.relative_to(gonka_dir).as_posix()
            if dir_entry.is_symlink():
                link_target = os.readlink(dir_entry)
                if (
                    _is_under_required_network_path(rel)
                    or Path(link_target).is_absolute()
                    or not _is_within(dir_entry.resolve(), gonka_dir)
                ):
                    raise ExternalHarnessError(
                        f"upstream network source contains an unsafe or forbidden symlink: {rel}",
                        code=CODE_NETWORK_SYMLINK,
                        details={"path": rel},
                    )
                entries.append((rel, dir_entry, "symlink"))
            else:
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for name in sorted(filenames):
            if name == ".git":
                continue
            entry = current_path / name
            rel = entry.relative_to(gonka_dir).as_posix()
            if entry.is_symlink():
                link_target = os.readlink(entry)
                if (
                    _is_under_required_network_path(rel)
                    or Path(link_target).is_absolute()
                    or not _is_within(entry.resolve(), gonka_dir)
                ):
                    raise ExternalHarnessError(
                        f"upstream network source contains an unsafe or forbidden symlink: {rel}",
                        code=CODE_NETWORK_SYMLINK,
                        details={"path": rel},
                    )
                entries.append((rel, entry, "symlink"))
            elif entry.is_file():
                entries.append((rel, entry, "file"))
            else:
                raise ExternalHarnessError(
                    f"upstream network source is not a regular file: {rel}",
                    code=CODE_NETWORK_SYMLINK,
                    details={"path": rel},
                )
    return sorted(entries, key=lambda item: item[0])


def compose_files_by_pair(*, b3: bool) -> Dict[str, List[str]]:
    """The additionalDockerFilesByKeyName the Kotlin harness passes per pair."""
    result = {pair: list(BASE_ADDITIONAL_COMPOSE_FILES) for pair in PAIR_NAMES}
    if b3:
        result["genesis"].append(B3_ADDITIONAL_COMPOSE_FILE)
    return result


def check_genesis_provisioner_compatible(gonka_dir: Path) -> Dict[str, Any]:
    """Refuse B3 unless the selected upstream genesis script is the pinned one."""
    script = Path(gonka_dir) / UPSTREAM_GENESIS_SCRIPT
    if not script.is_file() or script.is_symlink():
        raise ExternalHarnessError(
            f"selected Gonka has no regular {UPSTREAM_GENESIS_SCRIPT}",
            code=CODE_GENESIS_INCOMPATIBLE,
            details={"path": UPSTREAM_GENESIS_SCRIPT},
        )
    actual = sha256_file(script)
    record = {
        "upstream_script": UPSTREAM_GENESIS_SCRIPT,
        "upstream_script_sha256": actual,
        "pinned_sha256": GENESIS_PROVISIONER_UPSTREAM_SHA256,
        "pinned_source_sha": GENESIS_PROVISIONER_UPSTREAM_SOURCE_SHA,
        "compatible": actual == GENESIS_PROVISIONER_UPSTREAM_SHA256,
    }
    if not record["compatible"]:
        raise ExternalHarnessError(
            "B3 genesis provisioner was derived from init-docker-genesis.sh "
            f"{GENESIS_PROVISIONER_UPSTREAM_SHA256} (Gonka {GENESIS_PROVISIONER_UPSTREAM_SOURCE_SHA}); "
            f"the selected Gonka has {actual}. Re-derive the provisioner; it is never auto-adapted",
            code=CODE_GENESIS_INCOMPATIBLE,
            details=record,
        )
    return record


def prepare_network_root(
    gonka_dir: Path,
    network_root: Path,
    templates_dir: Path,
    *,
    b3: bool,
    manifest_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build GONKA_REPO_ROOT as a full working copy of the selected Gonka tree.

    Every tracked upstream file (including submodule files) is copied into
    ``network_root`` and re-verified before tests start; runner-owned files are
    written to their own paths and may never replace an upstream file.
    """
    gonka_dir = Path(gonka_dir).resolve()
    network_root = Path(network_root)
    templates_dir = Path(templates_dir).resolve()
    if network_root.is_symlink():
        raise ExternalHarnessError(
            f"network root {network_root} must not be a symlink",
            code=CODE_NETWORK_ROOT_SYMLINK,
            details={"network_root": str(network_root)},
        )
    resolved_network_root = network_root.resolve()
    if _is_within(resolved_network_root, gonka_dir) or _is_within(gonka_dir, resolved_network_root):
        raise ExternalHarnessError(
            f"network root {resolved_network_root} overlaps the selected Gonka snapshot {gonka_dir}",
            code=CODE_WORK_ROOT_INSIDE_SNAPSHOT,
            details={"network_root": str(resolved_network_root), "snapshot": str(gonka_dir)},
        )
    if network_root.exists() and (not network_root.is_dir() or any(network_root.iterdir())):
        raise ExternalHarnessError(
            f"network root {network_root} already exists and is not empty",
            code=CODE_NETWORK_ROOT_NOT_EMPTY,
            details={"network_root": str(network_root)},
        )
    genesis_provisioner = check_genesis_provisioner_compatible(gonka_dir) if b3 else None

    planned = _iter_full_upstream_tree(gonka_dir)
    upstream_paths = {rel for rel, _, _ in planned}

    generated_plan = []
    for template_rel, target_rel, b3_only in GENERATED_NETWORK_FILES:
        if b3_only and not b3:
            continue
        template = templates_dir / template_rel
        if not template.is_file() or template.is_symlink():
            raise ExternalHarnessError(
                f"runner network template missing: {template}",
                code=CODE_NETWORK_TEMPLATE_MISSING,
                details={"template": template_rel},
            )
        if target_rel in upstream_paths:
            raise ExternalHarnessError(
                f"generated file {target_rel} would replace an upstream file",
                code=CODE_NETWORK_COLLISION,
                details={"path": target_rel},
            )
        generated_plan.append((template_rel, target_rel, template))

    network_root.mkdir(parents=True, exist_ok=True)
    network_root = network_root.resolve()
    copies = []
    for rel, source, kind in planned:
        target = network_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == "symlink":
            link_target = os.readlink(source)
            source_digest = sha256_bytes(link_target.encode("utf-8", "surrogateescape"))
            os.symlink(link_target, target)
            copied_target = os.readlink(target)
            copied_digest = sha256_bytes(copied_target.encode("utf-8", "surrogateescape"))
            mode_str = "0o120000"
        else:
            source_digest = sha256_file(source)
            shutil.copyfile(source, target, follow_symlinks=False)
            mode = stat.S_IMODE(source.stat().st_mode)
            os.chmod(target, mode)
            copied_digest = sha256_file(target)
            mode_str = oct(mode)
        if copied_digest != source_digest:
            raise ExternalHarnessError(
                f"copied network file {rel} differs from its upstream source",
                code=CODE_NETWORK_COPY_MISMATCH,
                details={"path": rel, "source_sha256": source_digest, "sha256": copied_digest},
            )
        copies.append(
            {
                "path": rel,
                "sha256": copied_digest,
                "source_sha256": source_digest,
                "mode": mode_str,
                **({"kind": "symlink"} if kind == "symlink" else {}),
            }
        )

    generated = []
    for template_rel, target_rel, template in generated_plan:
        target = network_root / target_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, target, follow_symlinks=False)
        mode = 0o755 if target_rel.endswith(".sh") else 0o644
        os.chmod(target, mode)
        generated.append(
            {
                "path": target_rel,
                "template": template_rel,
                "sha256": sha256_file(target),
                "template_sha256": sha256_file(template),
                "mode": oct(mode),
            }
        )

    directories = []
    for rel in PROD_LOCAL_DIRECTORIES:
        directory = network_root / rel
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o777)
        directories.append({"path": rel, "mode": "0o777"})

    manifest: Dict[str, Any] = {
        "schema": NETWORK_MANIFEST_SCHEMA,
        "gonka_dir": str(gonka_dir),
        "network_root": str(network_root),
        "copy_mode": "full_working_tree",
        "b3": bool(b3),
        "upstream_copies": copies,
        "generated": generated,
        "directories": directories,
        "compose_files_by_pair": {
            pair: [f"local-test-net/{name}" for name in names]
            for pair, names in compose_files_by_pair(b3=b3).items()
        },
        "ownership_label": OWNERSHIP_LABEL,
        "genesis_provisioner": genesis_provisioner,
    }
    pre_run = verify_network_root_integrity(network_root, manifest)
    if pre_run["verdict"] != "UNCHANGED":
        raise ExternalHarnessError(
            "prepared network root failed pre-run completeness or integrity check",
            code=CODE_NETWORK_COPY_MISMATCH,
            details=pre_run,
        )
    if manifest_path is not None:
        write_json(Path(manifest_path), manifest)
    return manifest


def verify_network_root_integrity(
    network_root: Path,
    manifest: Mapping[str, Any],
    *,
    manifest_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Verify every original upstream and runner-generated file in ``network_root``.

    Any modified, replaced or deleted original file produces a ``VIOLATED``
    verdict; files created during the scenario (e.g. ``prod-local/**``) are
    recorded separately in ``runtime_additions``.
    """
    network_root = Path(network_root)
    if network_root.is_symlink():
        verification = {
            "verdict": "VIOLATED",
            "checked_upstream_files": 0,
            "checked_generated_files": 0,
            "violations": [
                {
                    "code": CODE_NETWORK_ROOT_SYMLINK,
                    "path": str(network_root),
                    "reason": "network root was replaced by a symlink",
                }
            ],
            "runtime_additions": [],
        }
        if manifest_path is not None:
            updated = dict(manifest)
            updated["post_run_verification"] = verification
            write_json(Path(manifest_path), updated)
        return verification
    network_root = network_root.resolve()
    violations: List[Dict[str, Any]] = []
    upstream_copies = list(manifest.get("upstream_copies") or [])
    generated_files = list(manifest.get("generated") or [])
    expected_paths = set()

    for item in upstream_copies:
        rel = str(item.get("path", ""))
        expected_paths.add(rel)
        target = network_root / rel
        kind = item.get("kind", "file")
        if not os.path.lexists(target):
            violations.append(
                {"code": CODE_NETWORK_COPY_MISMATCH, "path": rel, "reason": "missing from working copy"}
            )
            continue
        if kind == "symlink":
            if not target.is_symlink():
                violations.append(
                    {"code": CODE_NETWORK_COPY_MISMATCH, "path": rel, "reason": "expected symlink"}
                )
                continue
            actual_sha = sha256_bytes(os.readlink(target).encode("utf-8", "surrogateescape"))
        else:
            if target.is_symlink() or not target.is_file():
                violations.append(
                    {"code": CODE_NETWORK_COPY_MISMATCH, "path": rel, "reason": "not a regular file"}
                )
                continue
            actual_sha = sha256_file(target)
            actual_mode = oct(stat.S_IMODE(target.stat().st_mode))
            if item.get("mode") and actual_mode != item.get("mode"):
                violations.append(
                    {
                        "code": CODE_NETWORK_COPY_MISMATCH,
                        "path": rel,
                        "reason": "mode changed",
                        "expected_mode": item.get("mode"),
                        "actual_mode": actual_mode,
                    }
                )
        if actual_sha != item.get("source_sha256"):
            violations.append(
                {
                    "code": CODE_NETWORK_COPY_MISMATCH,
                    "path": rel,
                    "reason": "content changed",
                    "expected_sha256": item.get("source_sha256"),
                    "actual_sha256": actual_sha,
                }
            )

    for item in generated_files:
        rel = str(item.get("path", ""))
        expected_paths.add(rel)
        target = network_root / rel
        if not os.path.lexists(target) or target.is_symlink() or not target.is_file():
            violations.append(
                {"code": CODE_NETWORK_COPY_MISMATCH, "path": rel, "reason": "generated file missing or replaced"}
            )
            continue
        actual_sha = sha256_file(target)
        if actual_sha != item.get("template_sha256"):
            violations.append(
                {
                    "code": CODE_NETWORK_COPY_MISMATCH,
                    "path": rel,
                    "reason": "generated file modified",
                    "expected_sha256": item.get("template_sha256"),
                    "actual_sha256": actual_sha,
                }
            )

    runtime_additions: List[str] = []
    if network_root.is_dir():
        for current, dirnames, filenames in os.walk(network_root, followlinks=False):
            current_path = Path(current)
            dirnames.sort()
            for name in sorted(filenames):
                entry = current_path / name
                rel = entry.relative_to(network_root).as_posix()
                if rel not in expected_paths:
                    runtime_additions.append(rel)

    verification = {
        "verdict": "UNCHANGED" if not violations else "VIOLATED",
        "checked_upstream_files": len(upstream_copies),
        "checked_generated_files": len(generated_files),
        "violations": violations,
        "runtime_additions": sorted(runtime_additions),
    }
    if manifest_path is not None:
        updated = dict(manifest)
        updated["post_run_verification"] = verification
        write_json(Path(manifest_path), updated)
    return verification


def verify_network_manifest(manifest: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Findings for a manifest whose copies are not byte-identical. Empty = fine."""
    findings = []
    for item in manifest.get("upstream_copies") or []:
        if item.get("sha256") != item.get("source_sha256"):
            findings.append({"code": CODE_NETWORK_COPY_MISMATCH, "path": item.get("path")})
    upstream = {item.get("path") for item in manifest.get("upstream_copies") or []}
    for item in manifest.get("generated") or []:
        if item.get("path") in upstream:
            findings.append({"code": CODE_NETWORK_COLLISION, "path": item.get("path")})
    post_run = manifest.get("post_run_verification")
    if isinstance(post_run, Mapping) and post_run.get("verdict") not in (None, "UNCHANGED"):
        for v in post_run.get("violations") or [{"code": CODE_NETWORK_COPY_MISMATCH, "path": None}]:
            findings.append({"code": v.get("code", CODE_NETWORK_COPY_MISMATCH), "path": v.get("path")})
    return findings


# ---------------------------------------------------------------------------
# (c) Gradle argv builders
# ---------------------------------------------------------------------------


def gradle_launcher(gonka_dir: Path, *, java: str = "java") -> List[str]:
    """Run the selected Gonka's own Gradle wrapper jar, without writing a gradlew."""
    jar = Path(gonka_dir).resolve() / UPSTREAM_GRADLE_WRAPPER_JAR
    if not jar.is_file():
        raise ExternalHarnessError(
            f"selected Gonka has no Gradle wrapper jar at {UPSTREAM_GRADLE_WRAPPER_JAR}",
            code=CODE_GRADLE_WRAPPER_MISSING,
            details={"path": UPSTREAM_GRADLE_WRAPPER_JAR},
        )
    return [java, "-cp", str(jar), "org.gradle.wrapper.GradleWrapperMain"]


def gradle_environment(layout: WorkLayout) -> Dict[str, str]:
    """Use the explicit run cache, or a task-local cache for standalone checks."""
    return {"GRADLE_USER_HOME": str(layout.gradle_home)}


def init_script_path(harness_dir: Path) -> Path:
    return Path(harness_dir).resolve() / "gradle" / "a8-out-of-tree.init.gradle.kts"


def upstream_classpath_argv(gonka_dir: Path, harness_dir: Path, layout: WorkLayout, *, java: str = "java") -> List[str]:
    """Compile the unmodified upstream Testermint and export its real classpath."""
    gonka_dir = Path(gonka_dir).resolve()
    return [
        *gradle_launcher(gonka_dir, java=java),
        "--no-daemon",
        "--no-watch-fs",
        "--no-build-cache",
        "--no-configuration-cache",
        "--project-dir",
        str(gonka_dir / "testermint"),
        "--project-cache-dir",
        str(layout.upstream_project_cache),
        "--init-script",
        str(init_script_path(harness_dir)),
        f"-Pkotlin.project.persistent.dir={layout.kotlin_upstream}",
        f"-Pa8.outRoot={layout.upstream_build}",
        f"-Pa8.classpathFile={layout.classpath_file}",
        "a8ExportClasspath",
    ]


def harness_gradle_argv(
    gonka_dir: Path,
    harness_dir: Path,
    layout: WorkLayout,
    *,
    tasks: Sequence[str],
    test_name: Optional[str] = None,
    java: str = "java",
) -> List[str]:
    """Build (and optionally run one test of) the external harness project."""
    gonka_dir = Path(gonka_dir).resolve()
    upstream_test = gonka_dir / UPSTREAM_TESTERMINT_TEST
    if not upstream_test.is_file() or upstream_test.is_symlink():
        raise ExternalHarnessError(
            f"selected Gonka has no regular {UPSTREAM_TESTERMINT_TEST}",
            code=CODE_HARNESS_INPUT_INVALID,
            details={"path": UPSTREAM_TESTERMINT_TEST},
        )
    argv = [
        *gradle_launcher(gonka_dir, java=java),
        "--no-daemon",
        "--no-watch-fs",
        "--no-build-cache",
        "--no-configuration-cache",
        "--project-dir",
        str(Path(harness_dir).resolve()),
        "--project-cache-dir",
        str(layout.harness_project_cache),
        "--init-script",
        str(init_script_path(harness_dir)),
        f"-Pkotlin.project.persistent.dir={layout.kotlin_harness}",
        f"-Pa8.outRoot={layout.harness_build}",
        f"-Pa8.upstreamClasspathFile={layout.classpath_file}",
        f"-Pa8.upstreamTestermintTest={upstream_test}",
        f"-Pa8.upstreamTestermintTestSha256={sha256_file(upstream_test)}",
        *tasks,
    ]
    if test_name is not None:
        argv += ["--tests", test_name, "-DexcludeTags=unstable,exclude"]
    return argv


# ---------------------------------------------------------------------------
# (d) JUnit collection
# ---------------------------------------------------------------------------


def split_test_name(test_name: str) -> Tuple[str, str]:
    """``Class.method with spaces`` -> (``Class``, ``method with spaces``)."""
    cls, sep, method = test_name.partition(".")
    if not sep or not cls or not method:
        raise ExternalHarnessError(
            f"selected test {test_name!r} is not Class.method", code=CODE_SELECTED_TEST_NOT_EXECUTED
        )
    return cls, method


def _case_matches(classname: str, name: str, cls: str, method: str) -> bool:
    class_ok = classname == cls or classname.endswith("." + cls)
    stripped = name[:-2] if name.endswith("()") else name
    return class_ok and stripped == method


def collect_junit(results_dir: Path, dest_dir: Path, test_name: str) -> Dict[str, Any]:
    """Copy JUnit XML out of the external build and prove the selected test ran."""
    cls, method = split_test_name(test_name)
    results_dir = Path(results_dir)
    dest_dir = Path(dest_dir)
    xml_files = []
    if results_dir.is_dir() and not results_dir.is_symlink():
        xml_files = sorted(path for path in results_dir.glob("*.xml"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    files = []
    cases = []
    for path in xml_files:
        if path.is_symlink() or not path.is_file():
            raise ExternalHarnessError(
                f"JUnit report {path.name} is not a regular file", code=CODE_JUNIT_INVALID
            )
        target = dest_dir / path.name
        shutil.copyfile(path, target, follow_symlinks=False)
        files.append({"path": path.name, "sha256": sha256_file(target)})
        try:
            root = ElementTree.parse(str(target)).getroot()
        except ElementTree.ParseError as exc:
            raise ExternalHarnessError(
                f"JUnit report {path.name} is not valid XML: {exc}", code=CODE_JUNIT_INVALID
            ) from exc
        for case in root.iter("testcase"):
            classname = case.get("classname", "")
            name = case.get("name", "")
            if not _case_matches(classname, name, cls, method):
                continue
            if case.find("skipped") is not None:
                outcome = "skipped"
            elif case.find("failure") is not None:
                outcome = "failed"
            elif case.find("error") is not None:
                outcome = "error"
            else:
                outcome = "passed"
            cases.append({"classname": classname, "name": name, "outcome": outcome, "file": path.name})
    executed = [case for case in cases if case["outcome"] != "skipped"]
    summary = {
        "schema": JUNIT_SUMMARY_SCHEMA,
        "selected_test": test_name,
        "class": cls,
        "method": method,
        "source_dir": str(results_dir),
        "files": files,
        "cases": cases,
        "executed": len(executed),
        "passed": sum(1 for case in executed if case["outcome"] == "passed"),
        "failed": sum(1 for case in executed if case["outcome"] in ("failed", "error")),
        "skipped": len(cases) - len(executed),
    }
    write_json(dest_dir / "junit-summary.json", summary)
    if not executed:
        raise ExternalHarnessError(
            f"selected test {test_name!r} was not executed "
            f"({len(xml_files)} JUnit file(s), {len(cases)} matching case(s))",
            code=CODE_SELECTED_TEST_NOT_EXECUTED,
            details={"summary": summary},
        )
    return summary


# ---------------------------------------------------------------------------
# (e) B3 genesis verification
# ---------------------------------------------------------------------------


def _app_state(genesis: Mapping[str, Any]) -> Mapping[str, Any]:
    value = genesis.get("app_state") if isinstance(genesis, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _bank(genesis: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _app_state(genesis).get("bank")
    return value if isinstance(value, Mapping) else {}


def _accounts_by_address(genesis: Mapping[str, Any]) -> Dict[str, Any]:
    auth = _app_state(genesis).get("auth")
    accounts = auth.get("accounts") if isinstance(auth, Mapping) else None
    result: Dict[str, Any] = {}
    for account in accounts or []:
        if isinstance(account, Mapping):
            address = account.get("address")
            if address is None and isinstance(account.get("base_account"), Mapping):
                address = account["base_account"].get("address")
            if isinstance(address, str):
                result[address] = account
    return result


def _coins(value: Any) -> Dict[str, int]:
    coins: Dict[str, int] = {}
    for coin in value or []:
        if isinstance(coin, Mapping) and isinstance(coin.get("denom"), str):
            try:
                coins[coin["denom"]] = coins.get(coin["denom"], 0) + int(str(coin.get("amount")))
            except ValueError:
                coins[coin["denom"]] = -1
    return coins


def _balances_by_address(genesis: Mapping[str, Any]) -> Dict[str, Dict[str, int]]:
    result: Dict[str, Dict[str, int]] = {}
    for entry in _bank(genesis).get("balances") or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("address"), str):
            result[entry["address"]] = _coins(entry.get("coins"))
    return result


def _without_b3_surfaces(genesis: Mapping[str, Any]) -> Dict[str, Any]:
    copy = json.loads(json.dumps(genesis))
    app_state = copy.get("app_state") if isinstance(copy.get("app_state"), dict) else {}
    auth = app_state.get("auth")
    if isinstance(auth, dict):
        auth.pop("accounts", None)
    bank = app_state.get("bank")
    if isinstance(bank, dict):
        bank.pop("balances", None)
        bank.pop("supply", None)
    return copy


def verify_b3_genesis_delta(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    address: str,
    denom: str,
    amount: int,
) -> List[Dict[str, Any]]:
    """The effect of one ``genesis add-genesis-account`` must be exactly the fixture.

    ``before``/``after`` are the genesis immediately before and after the B3
    command. Findings (empty = exact): the address gains exactly
    ``amount denom`` and nothing else, no other account or balance changes,
    bank supply changes by exactly that coin, and nothing else in the document
    changes.
    """
    findings: List[Dict[str, Any]] = []
    if amount <= 0:
        findings.append({"code": "B3_AMOUNT_NOT_POSITIVE", "amount": amount})

    before_balances = _balances_by_address(before)
    after_balances = _balances_by_address(after)
    if address in before_balances:
        findings.append({"code": "B3_ADDRESS_ALREADY_FUNDED", "coins": before_balances[address]})
    if after_balances.get(address) != {denom: amount}:
        findings.append(
            {"code": "B3_BALANCE_NOT_EXACT", "expected": {denom: amount}, "actual": after_balances.get(address)}
        )
    for other in sorted(set(before_balances) | set(after_balances)):
        if other == address:
            continue
        if before_balances.get(other) != after_balances.get(other):
            findings.append(
                {
                    "code": "B3_OTHER_BALANCE_CHANGED",
                    "address": other,
                    "before": before_balances.get(other),
                    "after": after_balances.get(other),
                }
            )

    before_accounts = _accounts_by_address(before)
    after_accounts = _accounts_by_address(after)
    if address in before_accounts:
        findings.append({"code": "B3_ACCOUNT_ALREADY_EXISTS"})
    if address not in after_accounts:
        findings.append({"code": "B3_ACCOUNT_NOT_CREATED"})
    for other in sorted(set(before_accounts) | set(after_accounts)):
        if other == address:
            continue
        if before_accounts.get(other) != after_accounts.get(other):
            findings.append(
                {
                    "code": "B3_OTHER_ACCOUNT_CHANGED",
                    "address": other,
                    "before": before_accounts.get(other),
                    "after": after_accounts.get(other),
                }
            )

    before_supply = _coins(_bank(before).get("supply"))
    after_supply = _coins(_bank(after).get("supply"))
    expected_supply = dict(before_supply)
    expected_supply[denom] = expected_supply.get(denom, 0) + amount
    if before_supply.get(denom):
        findings.append({"code": "B3_DENOM_ALREADY_IN_SUPPLY", "amount": before_supply[denom]})
    if after_supply != expected_supply:
        findings.append(
            {"code": "B3_SUPPLY_DELTA_NOT_EXACT", "expected": expected_supply, "actual": after_supply}
        )

    if _without_b3_surfaces(before) != _without_b3_surfaces(after):
        findings.append({"code": "B3_UNRELATED_GENESIS_CHANGED"})
    return findings


def verify_b3_final_genesis(final: Mapping[str, Any], address: str, denom: str, amount: int) -> List[Dict[str, Any]]:
    """After gentx/patch-genesis/overrides the fixture must still be exact and alone."""
    findings: List[Dict[str, Any]] = []
    balances = _balances_by_address(final)
    if balances.get(address, {}).get(denom) != amount:
        findings.append(
            {"code": "B3_FINAL_BALANCE_NOT_EXACT", "expected": amount, "actual": balances.get(address)}
        )
    holders = sorted(other for other, coins in balances.items() if other != address and coins.get(denom))
    if holders:
        findings.append({"code": "B3_FINAL_DENOM_HELD_ELSEWHERE", "addresses": holders})
    supply = _coins(_bank(final).get("supply"))
    if supply.get(denom) != amount:
        findings.append({"code": "B3_FINAL_SUPPLY_NOT_EXACT", "expected": amount, "actual": supply.get(denom)})
    if address not in _accounts_by_address(final):
        findings.append({"code": "B3_FINAL_ACCOUNT_MISSING"})
    return findings


B3_EVIDENCE_FILES = (
    "genesis-before-b3.json",
    "genesis-after-b3.json",
    "genesis-final.json",
    "b3-provision.json",
)


def collect_b3_genesis_evidence(
    provision_dir: Path,
    dest_dir: Path,
    *,
    address: str = B3_FOREIGN_ADDRESS,
    denom: str = B3_FOREIGN_DENOM,
    amount: int = B3_FOREIGN_AMOUNT,
) -> Dict[str, Any]:
    """Copy the provisioner's output and write ``b3-genesis-verification.json``.

    Absence is never agreement: a missing file is a FAIL, not a skipped check.
    """
    provision_dir = Path(provision_dir)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    findings: List[Dict[str, Any]] = []
    loaded: Dict[str, Any] = {}
    hashes: Dict[str, str] = {}
    for name in B3_EVIDENCE_FILES:
        source = provision_dir / name
        if source.is_symlink() or not source.is_file():
            findings.append({"code": CODE_B3_EVIDENCE_MISSING, "file": name})
            continue
        target = dest_dir / name
        shutil.copyfile(source, target, follow_symlinks=False)
        hashes[name] = sha256_file(target)
        try:
            loaded[name] = json.loads(target.read_text(encoding="utf-8"))
        except ValueError:
            findings.append({"code": "B3_EVIDENCE_NOT_JSON", "file": name})
    provision = loaded.get("b3-provision.json")
    if isinstance(provision, Mapping):
        recorded = provision.get("sha256") if isinstance(provision.get("sha256"), Mapping) else {}
        for name in ("genesis-before-b3.json", "genesis-after-b3.json", "genesis-final.json"):
            if name in hashes and recorded.get(name) != hashes[name]:
                findings.append(
                    {"code": "B3_EVIDENCE_HASH_MISMATCH", "file": name, "recorded": recorded.get(name), "actual": hashes[name]}
                )
        if provision.get("derived_from_sha256") != GENESIS_PROVISIONER_UPSTREAM_SHA256:
            findings.append(
                {"code": CODE_GENESIS_INCOMPATIBLE, "recorded": provision.get("derived_from_sha256")}
            )
        expected_command = f"inferenced genesis add-genesis-account {address} {amount}{denom}"
        if not str(provision.get("command", "")).startswith(expected_command):
            findings.append(
                {"code": "B3_COMMAND_NOT_EXPECTED", "expected_prefix": expected_command, "actual": provision.get("command")}
            )
    if "genesis-before-b3.json" in loaded and "genesis-after-b3.json" in loaded:
        findings.extend(
            verify_b3_genesis_delta(
                loaded["genesis-before-b3.json"], loaded["genesis-after-b3.json"], address, denom, amount
            )
        )
    if "genesis-final.json" in loaded:
        findings.extend(verify_b3_final_genesis(loaded["genesis-final.json"], address, denom, amount))
    verification = {
        "schema": B3_VERIFICATION_SCHEMA,
        "address": address,
        "denom": denom,
        "amount": str(amount),
        "provision_dir": str(provision_dir),
        "files_sha256": hashes,
        "command": provision.get("command") if isinstance(provision, Mapping) else None,
        "findings": findings,
        "verdict": "PASS" if not findings else "FAIL",
    }
    write_json(dest_dir / "b3-genesis-verification.json", verification)
    return verification


# ---------------------------------------------------------------------------
# (f) Harness inputs
# ---------------------------------------------------------------------------


def _hash_tree(root: Path, *, exclude_dirs: Sequence[str] = ()) -> List[Dict[str, str]]:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ExternalHarnessError(f"harness input {root} is not a directory", code=CODE_HARNESS_INPUT_INVALID)
    entries = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in exclude_dirs)
        for name in sorted(dirnames + filenames):
            path = Path(current) / name
            if path.is_symlink():
                raise ExternalHarnessError(
                    f"harness input contains a symlink: {path}", code=CODE_HARNESS_INPUT_INVALID
                )
        for name in sorted(filenames):
            path = Path(current) / name
            entries.append({"path": path.relative_to(root).as_posix(), "sha256": sha256_file(path)})
    return sorted(entries, key=lambda item: item["path"])


def _tree_digest(entries: Sequence[Mapping[str, str]]) -> str:
    digest = hashlib.sha256()
    for item in entries:
        digest.update(f"{item['path']}\0{item['sha256']}\n".encode("utf-8"))
    return digest.hexdigest()


def harness_inputs(
    *,
    harness_dir: Path,
    gonka_dir: Path,
    classpath_file: Path,
    templates_dir: Path = DEFAULT_NETWORK_TEMPLATES_DIR,
    runner_files: Sequence[Path] = (Path(__file__).resolve(), CONTAINER_CONTROL_PATH, SOURCE_SNAPSHOT_PATH),
) -> Dict[str, Any]:
    """Hash every input that decides what the external test run means."""
    harness_files = _hash_tree(harness_dir, exclude_dirs=(".gradle", "build", ".kotlin"))
    template_files = _hash_tree(templates_dir)
    upstream_test = Path(gonka_dir).resolve() / UPSTREAM_TESTERMINT_TEST
    classpath_file = Path(classpath_file)
    if not classpath_file.is_file():
        raise ExternalHarnessError(
            f"exported upstream classpath {classpath_file} is missing", code=CODE_HARNESS_INPUT_INVALID
        )
    entries = [line for line in classpath_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    runner = []
    for path in runner_files:
        path = Path(path)
        if not path.is_file():
            raise ExternalHarnessError(f"runner file {path} is missing", code=CODE_HARNESS_INPUT_INVALID)
        runner.append({"path": path.name, "sha256": sha256_file(path)})
    return {
        "schema": HARNESS_INPUTS_SCHEMA,
        "harness_dir": str(Path(harness_dir).resolve()),
        "harness_files": harness_files,
        "harness_tree_sha256": _tree_digest(harness_files),
        "network_templates": template_files,
        "network_templates_sha256": _tree_digest(template_files),
        "runner_files": runner,
        "upstream_testermint_test": {
            "path": UPSTREAM_TESTERMINT_TEST,
            "sha256": sha256_file(upstream_test) if upstream_test.is_file() else None,
        },
        "classpath_file": {
            "path": str(classpath_file),
            "sha256": sha256_file(classpath_file),
            "entries": len(entries),
        },
    }
