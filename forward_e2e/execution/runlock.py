"""``run.lock.json`` and the post-build manifests.

Separation of concerns
----------------------
``run.lock.json`` is the *immutable specification of the inputs*. It is
written **before** any target project is built, so it can only contain facts
that are already known: which commits, which runner image, which adapter,
which scenarios, which pinned recipes. It must never be rewritten afterwards.

``build-manifest.json`` records what a specific execution actually produced:
real image ids and digests, real Wasm hashes, the real Gonka binary hash, the
observed runtime versions, every explicit build argument, the staged build
contexts and the source-snapshot fingerprints taken before and after the
build. It is bound to the lock by ``lock_sha256`` so the pair cannot be
recombined.

Format versions
---------------
``e2e/run-lock/2`` (and ``/2`` of the build and execution manifests) is the
immutable-source model: there is no overlay section, no prepared commit and no
allow-list of changed paths, and a lock records product SHAs with their Git
tree SHAs, the runner version and the hashes of the external tests, build
recipes and network templates. ``/1`` documents remain *readable* so that old
packages can be reported and recovered, and are classified as historical
prepared-build evidence; they are never executable (``run``/``rerun`` raise
:class:`PlanSchemaSupersededError`) and never count as immutable-source proof.

``execution-manifest.json`` records the run identity: run id, parent run for a
replay, artefact index reference and whether the network state was created
fresh (it always is; resume is deliberately not supported).

A failed or partial run writes a manifest with a non-``COMPLETE`` status. A
``COMPLETE`` build manifest is only ever produced when every required artefact
was verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .errors import (
    ExportConflict,
    IntegrityError,
    LockIntegrityError,
    LockSchemaError,
    PathSafetyError,
    PlanSchemaSupersededError,
)
from .file_safety import atomic_write_bytes, read_checked_file_bytes, sha256_file
from .sources import assert_no_symlinks_in_path

LOCK_ENVELOPE_SCHEMA = "e2e/run-lock-envelope/1"
RUN_LOCK_SCHEMA = "e2e/run-lock/2"
BUILD_MANIFEST_SCHEMA = "e2e/build-manifest/2"
EXECUTION_MANIFEST_SCHEMA = "e2e/execution-manifest/2"
DELIVERY_MANIFEST_SCHEMA = "e2e/delivery-manifest/1"

#: The overlay / prepared-commit formats. Readable for report/recover only.
RUN_LOCK_SCHEMA_V1 = "e2e/run-lock/1"
BUILD_MANIFEST_SCHEMA_V1 = "e2e/build-manifest/1"
EXECUTION_MANIFEST_SCHEMA_V1 = "e2e/execution-manifest/1"

SUPPORTED_LOCK_SCHEMAS = frozenset({RUN_LOCK_SCHEMA, RUN_LOCK_SCHEMA_V1})
#: Only these may be handed to ``run``/``rerun``.
EXECUTABLE_LOCK_SCHEMAS = frozenset({RUN_LOCK_SCHEMA})
SUPPORTED_ENVELOPE_SCHEMAS = frozenset({LOCK_ENVELOPE_SCHEMA})
SUPPORTED_BUILD_MANIFEST_SCHEMAS = frozenset({BUILD_MANIFEST_SCHEMA, BUILD_MANIFEST_SCHEMA_V1})
SUPPORTED_EXECUTION_MANIFEST_SCHEMAS = frozenset(
    {EXECUTION_MANIFEST_SCHEMA, EXECUTION_MANIFEST_SCHEMA_V1}
)
SUPPORTED_DELIVERY_MANIFEST_SCHEMAS = frozenset({DELIVERY_MANIFEST_SCHEMA})

#: Provenance classification of a lock / package. Mirrors
#: ``forward_e2e.suite.evidence_model.PROVENANCE_MODEL_*`` (kept literal here to avoid a
#: dependency from the lock format on the grading policy module).
PROVENANCE_IMMUTABLE = "immutable-source"
PROVENANCE_HISTORICAL_PREPARED = "historical-prepared-build"

RUN_LOCK_FILENAME = "run.lock.json"
BUILD_MANIFEST_FILENAME = "build-manifest.json"
EXECUTION_MANIFEST_FILENAME = "execution-manifest.json"
DELIVERY_MANIFEST_FILENAME = "delivery.json"

#: Everything a ``--from`` replay is forbidden to change. Operational knobs
#: (run id, output location, credential source, cache/transport location) are
#: intentionally absent: changing them does not change what is being proven.
SEMANTIC_LOCK_SECTIONS_V1 = (
    "gonka",
    "contracts",
    "runner",
    "platform",
    "compatibility",
    "selection",
    "limits",
    "network",
    "build",
    "overlay",
    "semantic_inputs",
    "source_package",
)

#: ``/2`` drops ``overlay`` and adds ``source_policy`` (what the run promises
#: about the snapshots) and ``external_tests`` (hashes of the runner-owned
#: test code that exercises them).
SEMANTIC_LOCK_SECTIONS = (
    "gonka",
    "contracts",
    "runner",
    "platform",
    "compatibility",
    "selection",
    "limits",
    "network",
    "build",
    "source_policy",
    "external_tests",
    "semantic_inputs",
    "source_package",
)

#: Key fragments that only a plan permitting a modified tree contains.
SUPERSEDED_LOCK_KEYS = (
    "overlay",
    "prepared",
    "patches",
    "allowed_test_paths",
    "allow_production_overwrite",
)


def sections_for_schema(schema: str) -> tuple:
    return SEMANTIC_LOCK_SECTIONS_V1 if schema == RUN_LOCK_SCHEMA_V1 else SEMANTIC_LOCK_SECTIONS


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON used for hashing (sorted keys, no insignificant space)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _require_mapping(value: Any, what: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LockSchemaError(f"{what} must be a JSON object")
    return dict(value)


@dataclass
class RunLock:
    """Immutable specification of the inputs of one planned execution."""

    schema_version: str
    plan_id: str
    created_at_utc: str
    gonka: Dict[str, Any]
    contracts: Dict[str, Any]
    runner: Dict[str, Any]
    platform: Dict[str, Any]
    compatibility: Dict[str, Any]
    selection: Dict[str, Any]
    limits: Dict[str, Any]
    network: Dict[str, Any]
    build: Dict[str, Any]
    semantic_inputs: Dict[str, Any]
    source_package: Dict[str, Any]
    #: ``/2`` only: the promise about the source snapshots.
    source_policy: Dict[str, Any] = field(default_factory=dict)
    #: ``/2`` only: hashes of the runner-owned external tests and probes.
    external_tests: Dict[str, Any] = field(default_factory=dict)
    #: ``/1`` only (historical): the overlay the old runner applied.
    overlay: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    # -- serialisation -------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "created_at_utc": self.created_at_utc,
            "gonka": dict(self.gonka),
            "contracts": dict(self.contracts),
            "runner": dict(self.runner),
            "platform": dict(self.platform),
            "compatibility": dict(self.compatibility),
            "selection": dict(self.selection),
            "limits": dict(self.limits),
            "network": dict(self.network),
            "build": dict(self.build),
            "semantic_inputs": dict(self.semantic_inputs),
            "source_package": dict(self.source_package),
            "notes": list(self.notes),
        }
        # Each format serialises exactly its own sections, so a historical
        # lock re-serialises to the very bytes its lock_sha256 was taken over.
        if self.schema_version == RUN_LOCK_SCHEMA_V1:
            body["overlay"] = dict(self.overlay)
        else:
            body["source_policy"] = dict(self.source_policy)
            body["external_tests"] = dict(self.external_tests)
        return body

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunLock":
        schema = str(data.get("schema_version", ""))
        if schema not in SUPPORTED_LOCK_SCHEMAS:
            raise LockSchemaError(
                "Unsupported run lock schema version",
                {"schema_version": schema, "supported": sorted(SUPPORTED_LOCK_SCHEMAS)},
            )
        missing = [key for key in sections_for_schema(schema) if key not in data]
        if missing:
            raise LockSchemaError(
                "Run lock is missing required sections",
                {"missing": missing},
            )
        v1 = schema == RUN_LOCK_SCHEMA_V1
        return cls(
            schema_version=schema,
            plan_id=str(data["plan_id"]),
            created_at_utc=str(data["created_at_utc"]),
            gonka=_require_mapping(data["gonka"], "lock.gonka"),
            contracts=_require_mapping(data["contracts"], "lock.contracts"),
            runner=_require_mapping(data["runner"], "lock.runner"),
            platform=_require_mapping(data["platform"], "lock.platform"),
            compatibility=_require_mapping(data["compatibility"], "lock.compatibility"),
            selection=_require_mapping(data["selection"], "lock.selection"),
            limits=_require_mapping(data["limits"], "lock.limits"),
            network=_require_mapping(data["network"], "lock.network"),
            build=_require_mapping(data["build"], "lock.build"),
            overlay=_require_mapping(data["overlay"], "lock.overlay") if v1 else {},
            source_policy=(
                {} if v1 else _require_mapping(data["source_policy"], "lock.source_policy")
            ),
            external_tests=(
                {} if v1 else _require_mapping(data["external_tests"], "lock.external_tests")
            ),
            semantic_inputs=_require_mapping(data["semantic_inputs"], "lock.semantic_inputs"),
            source_package=_require_mapping(data["source_package"], "lock.source_package"),
            notes=[str(n) for n in data.get("notes", [])],
        )

    # -- identity ------------------------------------------------------
    @property
    def lock_sha256(self) -> str:
        return content_sha256(self.to_dict())

    def envelope(self) -> Dict[str, Any]:
        body = self.to_dict()
        return {
            "envelope_schema": LOCK_ENVELOPE_SCHEMA,
            "lock_sha256": content_sha256(body),
            "lock": body,
        }

    # -- classification ------------------------------------------------
    @property
    def provenance_model(self) -> str:
        """What kind of claim a run of this lock can make at all."""
        if self.schema_version in EXECUTABLE_LOCK_SCHEMAS and not superseded_lock_markers(
            self.to_dict()
        ):
            return PROVENANCE_IMMUTABLE
        return PROVENANCE_HISTORICAL_PREPARED

    # -- convenience ---------------------------------------------------
    @property
    def scenarios(self) -> List[str]:
        return [str(s) for s in self.selection.get("scenarios", [])]

    @property
    def profile(self) -> Optional[str]:
        value = self.selection.get("profile")
        return str(value) if value else None

    @property
    def runner_image_id(self) -> Optional[str]:
        value = self.runner.get("image_id")
        return str(value) if value else None

    def source_record(self, role: str) -> Dict[str, Any]:
        if role == "gonka":
            return dict(self.gonka)
        if role == "contracts":
            return dict(self.contracts)
        raise LockSchemaError("Unknown source role", {"role": role})


def superseded_lock_markers(payload: Any, path: str = "lock") -> List[str]:
    """Key paths in a lock body that only an overlay / prepared-commit plan has.

    Keys, not values: a free-text note mentioning the old model is harmless; a
    key such as ``overlay``, ``gonka_prepared_sha`` or ``patches`` is a
    permission to accept a modified tree.
    """
    found: List[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            child = f"{path}.{key}"
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in SUPERSEDED_LOCK_KEYS):
                found.append(child)
            found.extend(superseded_lock_markers(value, child))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(superseded_lock_markers(value, f"{path}[{index}]"))
    return found


def assert_lock_executable(lock: RunLock) -> None:
    """Refuse a plan written for the overlay / prepared-commit model.

    Called by ``run`` and ``rerun`` (through :func:`execute_plan`) before
    anything is written. A historical plan is still readable by ``report`` and
    ``recover``; it is only its *execution* that is refused.
    """
    markers = superseded_lock_markers(lock.to_dict())
    if lock.schema_version not in EXECUTABLE_LOCK_SCHEMAS or markers:
        raise PlanSchemaSupersededError(
            "This plan was created for the retired overlay / prepared-commit runner, which "
            "allowed the Gonka source tree to be modified before the build. The "
            "immutable-source runner will not execute it. Create a new plan with `plan` "
            "(existing reports and raw evidence are left untouched).",
            {
                "plan_id": lock.plan_id,
                "schema_version": lock.schema_version,
                "executable_schemas": sorted(EXECUTABLE_LOCK_SCHEMAS),
                "superseded_keys": markers[:20],
            },
        )


def write_run_lock(lock: RunLock, target: Path) -> Path:
    """Write the lock envelope. Refuses to overwrite an existing lock."""
    path = Path(target)
    data = (json.dumps(lock.envelope(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        return atomic_write_bytes(path, data, what="Run lock destination", replace=False, mode=0o644)
    except ExportConflict as exc:
        raise LockIntegrityError(
            "A run lock already exists at this location; a lock is never rewritten",
            {"path": str(path)},
        ) from exc


def load_run_lock(path: Path, *, raw_bytes: Optional[bytes] = None) -> RunLock:
    """Load and verify a lock envelope, proving it was not modified."""
    lock_path = Path(path)
    assert_no_symlinks_in_path(lock_path, what="Run lock source")
    if lock_path.is_dir():
        lock_path = lock_path / RUN_LOCK_FILENAME
        assert_no_symlinks_in_path(lock_path, what="Run lock source")
    if not lock_path.is_file():
        raise LockSchemaError("Run lock file not found", {"path": str(path)})
    try:
        data = raw_bytes if raw_bytes is not None else read_checked_file_bytes(lock_path)
        raw = json.loads(data.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError, PathSafetyError, IntegrityError) as exc:
        raise LockSchemaError(
            "Run lock file is not readable JSON", {"path": str(lock_path)}
        ) from exc
    if not isinstance(raw, Mapping):
        raise LockSchemaError("Run lock file must contain a JSON object")
    envelope_schema = str(raw.get("envelope_schema", ""))
    if envelope_schema not in SUPPORTED_ENVELOPE_SCHEMAS:
        raise LockSchemaError(
            "Unsupported run lock envelope schema",
            {"envelope_schema": envelope_schema, "supported": sorted(SUPPORTED_ENVELOPE_SCHEMAS)},
        )
    body = raw.get("lock")
    if not isinstance(body, Mapping):
        raise LockSchemaError("Run lock envelope has no 'lock' object")
    recorded = str(raw.get("lock_sha256", ""))
    try:
        actual = content_sha256(body)
    except UnicodeError as exc:
        # JSON accepts escaped lone surrogates, but canonical UTF-8 does not.
        raise LockSchemaError(
            "Run lock body cannot be encoded as UTF-8", {"path": str(lock_path)}
        ) from exc
    if recorded != actual:
        raise LockIntegrityError(
            "Run lock content hash does not match the recorded lock_sha256; the lock was "
            "modified after it was created.",
            {"path": str(lock_path), "recorded": recorded, "actual": actual},
        )
    try:
        lock = RunLock.from_dict(body)
    except (KeyError, TypeError, ValueError) as exc:
        raise LockSchemaError("Run lock contains missing or invalid fields") from exc
    # Consumers bind manifests to the reconstructed object's hash. Accepting a
    # body that changes during deserialization would silently change that identity.
    if lock.lock_sha256 != recorded:
        raise LockSchemaError("Run lock deserialization changes its verified identity")
    return lock


def resolve_package_root(lock_path: Path) -> Path:
    """The source package root is the directory that contains the lock file."""
    path = Path(lock_path)
    if path.is_dir():
        return path.resolve()
    return path.resolve().parent


# ----------------------------------------------------------------------------
# build manifest
# ----------------------------------------------------------------------------
class ManifestStatus:
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    IN_PROGRESS = "IN_PROGRESS"


@dataclass
class BuildManifest:
    """What a specific execution really produced, tied to one lock."""

    schema_version: str
    lock_sha256: str
    plan_id: str
    run_id: str
    started_at_utc: str
    status: str = ManifestStatus.IN_PROGRESS
    completed_at_utc: Optional[str] = None
    instrumented: bool = False
    images: List[Dict[str, Any]] = field(default_factory=list)
    runtime_dependencies: List[Dict[str, Any]] = field(default_factory=list)
    binaries: List[Dict[str, Any]] = field(default_factory=list)
    wasm: List[Dict[str, Any]] = field(default_factory=list)
    tools: List[Dict[str, Any]] = field(default_factory=list)
    observed_runtime: Dict[str, Any] = field(default_factory=dict)
    #: ``/1`` only (historical): the prepared commit of the retired runner.
    prepared_runtime: Dict[str, Any] = field(default_factory=dict)
    #: ``/2``: source-snapshot fingerprints per role and phase
    #: (``before_build``, ``after_build``, ``after_execution``) and verdicts.
    source_immutability: Dict[str, Any] = field(default_factory=dict)
    #: ``/2``: staged Docker build contexts with the digest of what was staged.
    staged_contexts: List[Dict[str, Any]] = field(default_factory=list)
    #: ``/2``: values every recipe placeholder resolved to (selected SHAs,
    #: ``git describe`` version, platform), so build arguments are auditable.
    build_inputs: Dict[str, Any] = field(default_factory=dict)
    #: How the process environment was rebuilt from the lock before any stage
    #: ran. Proves offline that a replay did not inherit the caller's shell.
    execution_environment: Dict[str, Any] = field(default_factory=dict)
    #: The artefacts actually handed to the suite for deployment, with the
    #: hashes this build produced. Without it the manifest could not show which
    #: production binaries were the ones under test.
    deployment: Dict[str, Any] = field(default_factory=dict)
    #: Digest of the source identity this build was entitled to produce. It is
    #: what makes a later Docker cache hit provable instead of merely plausible.
    source_fingerprint: str = ""
    #: Present only when the operator stopped the run. It records the signal
    #: and which build process groups were signalled, so an incomplete run can
    #: never be mistaken for a failed verdict about the sources.
    cancellation: Dict[str, Any] = field(default_factory=dict)
    patch_manifest: List[Dict[str, Any]] = field(default_factory=list)
    build_commands: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def start(cls, *, lock: RunLock, run_id: str) -> "BuildManifest":
        return cls(
            schema_version=BUILD_MANIFEST_SCHEMA,
            lock_sha256=lock.lock_sha256,
            plan_id=lock.plan_id,
            run_id=run_id,
            started_at_utc=utc_now_iso(),
        )

    def record_failure(self, code: str, message: str, details: Optional[Mapping[str, Any]] = None) -> None:
        self.failures.append(
            {"code": code, "message": message, "details": dict(details or {}),
             "recorded_at_utc": utc_now_iso()}
        )
        self.status = ManifestStatus.FAILED

    def finish(self, *, required_roles: Sequence[str] = ()) -> None:
        """Close the manifest.

        A ``COMPLETE`` status is only reachable when nothing failed and every
        required artefact role is present. Otherwise the manifest stays
        ``PARTIAL``/``FAILED`` so that a crashed run can never masquerade as a
        finished build.
        """
        self.completed_at_utc = utc_now_iso()
        if self.failures:
            self.status = ManifestStatus.FAILED
            return
        produced = {str(item.get("role")) for item in self.images}
        produced |= {str(item.get("role")) for item in self.binaries}
        produced |= {str(item.get("role")) for item in self.wasm}
        missing = [role for role in required_roles if role not in produced]
        if missing:
            self.status = ManifestStatus.PARTIAL
            self.failures.append(
                {
                    "code": "BUILD_ARTIFACT_MISSING",
                    "message": "Required build artefacts were not produced",
                    "details": {"missing_roles": missing},
                    "recorded_at_utc": utc_now_iso(),
                }
            )
            return
        self.status = ManifestStatus.COMPLETE

    def to_dict(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "lock_sha256": self.lock_sha256,
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "status": self.status,
            "started_at_utc": self.started_at_utc,
            "completed_at_utc": self.completed_at_utc,
            "images": list(self.images),
            "runtime_dependencies": list(self.runtime_dependencies),
            "binaries": list(self.binaries),
            "wasm": list(self.wasm),
            "tools": list(self.tools),
            "observed_runtime": dict(self.observed_runtime),
            "execution_environment": dict(self.execution_environment),
            "deployment": dict(self.deployment),
            "source_fingerprint": self.source_fingerprint,
            "cancellation": dict(self.cancellation),
            "build_commands": list(self.build_commands),
            "failures": list(self.failures),
        }
        if self.schema_version == BUILD_MANIFEST_SCHEMA_V1:
            # Historical documents keep their own keys so they re-hash to the
            # value their execution manifest recorded.
            body["instrumented"] = bool(self.instrumented)
            body["prepared_runtime"] = dict(self.prepared_runtime)
            body["patch_manifest"] = list(self.patch_manifest)
        else:
            body["source_immutability"] = dict(self.source_immutability)
            body["staged_contexts"] = list(self.staged_contexts)
            body["build_inputs"] = dict(self.build_inputs)
        return body

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BuildManifest":
        schema = str(data.get("schema_version", ""))
        if schema not in SUPPORTED_BUILD_MANIFEST_SCHEMAS:
            raise LockSchemaError(
                "Unsupported build manifest schema", {"schema_version": schema}
            )
        return cls(
            schema_version=schema,
            lock_sha256=str(data["lock_sha256"]),
            plan_id=str(data["plan_id"]),
            run_id=str(data["run_id"]),
            started_at_utc=str(data["started_at_utc"]),
            status=str(data.get("status", ManifestStatus.IN_PROGRESS)),
            completed_at_utc=data.get("completed_at_utc"),
            instrumented=bool(data.get("instrumented", False)),
            images=[dict(x) for x in data.get("images", [])],
            runtime_dependencies=[dict(x) for x in data.get("runtime_dependencies", [])],
            binaries=[dict(x) for x in data.get("binaries", [])],
            wasm=[dict(x) for x in data.get("wasm", [])],
            tools=[dict(x) for x in data.get("tools", [])],
            observed_runtime=dict(data.get("observed_runtime", {})),
            prepared_runtime=dict(data.get("prepared_runtime", {})),
            source_immutability=dict(data.get("source_immutability", {})),
            staged_contexts=[dict(x) for x in data.get("staged_contexts", [])],
            build_inputs=dict(data.get("build_inputs", {})),
            execution_environment=dict(data.get("execution_environment", {})),
            deployment=dict(data.get("deployment", {})),
            source_fingerprint=str(data.get("source_fingerprint", "") or ""),
            cancellation=dict(data.get("cancellation", {})),
            patch_manifest=[dict(x) for x in data.get("patch_manifest", [])],
            build_commands=[dict(x) for x in data.get("build_commands", [])],
            failures=[dict(x) for x in data.get("failures", [])],
        )

    def write(self, target: Path) -> Path:
        data = (json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
        return atomic_write_bytes(target, data, what="Build manifest destination", mode=0o644)


@dataclass
class ExecutionManifest:
    """Run identity and its relationship to the plan and to a parent run."""

    schema_version: str
    lock_sha256: str
    plan_id: str
    run_id: str
    suite_id: str
    created_at_utc: str
    command: str
    fresh_network_state: bool = True
    replay_of_plan: Optional[str] = None
    parent_run_id: Optional[str] = None
    source_plan_path: Optional[str] = None
    build_manifest_sha256: Optional[str] = None
    artifact_index_relpath: Optional[str] = None
    #: Where the exported suite evidence really landed, relative to the run
    #: directory. The orchestrator appends the suite id itself, so this cannot
    #: be derived by convention without repeating that detail in three places.
    suite_export_relpath: Optional[str] = None
    #: Absolute path of the raw staged suite on the workspace volume. `recover`
    #: needs the *parent* of the `suites/` directory, which is likewise not the
    #: value that was passed in.
    suite_workspace_dir: Optional[str] = None
    runner_image_id: Optional[str] = None
    export_status: Optional[str] = None
    export_error: Optional[str] = None
    delivery_manifest_relpath: Optional[str] = None
    #: ``/2``: the runner version recorded in the lock, repeated so a package
    #: states which runner produced it without reopening the lock.
    runner_version: Optional[str] = None
    #: ``/2``: fingerprints of both snapshots after execution and the verdict
    #: of the post-run comparison with the pre-build measurement.
    source_immutability: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        body = self._common_dict()
        if self.schema_version != EXECUTION_MANIFEST_SCHEMA_V1:
            body["runner_version"] = self.runner_version
            body["source_immutability"] = dict(self.source_immutability)
        return body

    def _common_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "lock_sha256": self.lock_sha256,
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "suite_id": self.suite_id,
            "created_at_utc": self.created_at_utc,
            "command": self.command,
            "fresh_network_state": bool(self.fresh_network_state),
            "replay_of_plan": self.replay_of_plan,
            "parent_run_id": self.parent_run_id,
            "source_plan_path": self.source_plan_path,
            "build_manifest_sha256": self.build_manifest_sha256,
            "artifact_index_relpath": self.artifact_index_relpath,
            "suite_export_relpath": self.suite_export_relpath,
            "suite_workspace_dir": self.suite_workspace_dir,
            "runner_image_id": self.runner_image_id,
            "export_status": self.export_status,
            "export_error": self.export_error,
            "delivery_manifest_relpath": self.delivery_manifest_relpath,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionManifest":
        schema = str(data.get("schema_version", ""))
        if schema not in SUPPORTED_EXECUTION_MANIFEST_SCHEMAS:
            raise LockSchemaError(
                "Unsupported execution manifest schema", {"schema_version": schema}
            )
        return cls(
            schema_version=schema,
            lock_sha256=str(data["lock_sha256"]),
            plan_id=str(data["plan_id"]),
            run_id=str(data["run_id"]),
            suite_id=str(data["suite_id"]),
            created_at_utc=str(data["created_at_utc"]),
            command=str(data.get("command", "run")),
            fresh_network_state=bool(data.get("fresh_network_state", True)),
            replay_of_plan=data.get("replay_of_plan"),
            parent_run_id=data.get("parent_run_id"),
            source_plan_path=data.get("source_plan_path"),
            build_manifest_sha256=data.get("build_manifest_sha256"),
            artifact_index_relpath=data.get("artifact_index_relpath"),
            suite_export_relpath=data.get("suite_export_relpath"),
            suite_workspace_dir=data.get("suite_workspace_dir"),
            runner_image_id=data.get("runner_image_id"),
            export_status=data.get("export_status"),
            export_error=data.get("export_error"),
            delivery_manifest_relpath=data.get("delivery_manifest_relpath"),
            runner_version=data.get("runner_version"),
            source_immutability=dict(data.get("source_immutability", {})),
            notes=[str(n) for n in data.get("notes", [])],
        )

    def write(self, target: Path) -> Path:
        data = (json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
        return atomic_write_bytes(target, data, what="Execution manifest destination", mode=0o644)


class DeliveryStatus:
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


ALL_DELIVERY_STATUSES = frozenset({
    DeliveryStatus.PENDING,
    DeliveryStatus.IN_PROGRESS,
    DeliveryStatus.COMPLETED,
    DeliveryStatus.FAILED,
})
ALL_DELIVERY_COMMANDS = frozenset({"run", "rerun", "recover"})


@dataclass
class DeliveryAttempt:
    attempt_number: int
    command: str  # "run" | "rerun" | "recover"
    started_at_utc: str
    destination: str
    status: str  # "IN_PROGRESS" | "COMPLETED" | "FAILED"
    completed_at_utc: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt_number": self.attempt_number,
            "command": self.command,
            "started_at_utc": self.started_at_utc,
            "destination": self.destination,
            "status": self.status,
            "completed_at_utc": self.completed_at_utc,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DeliveryAttempt":
        cmd = str(data.get("command", ""))
        if cmd not in ALL_DELIVERY_COMMANDS:
            raise LockSchemaError(
                "Unsupported delivery attempt command",
                {"command": cmd, "supported": sorted(ALL_DELIVERY_COMMANDS)},
            )
        status = str(data.get("status", ""))
        if status not in ALL_DELIVERY_STATUSES:
            raise LockSchemaError(
                "Unsupported delivery attempt status",
                {"status": status, "supported": sorted(ALL_DELIVERY_STATUSES)},
            )
        return cls(
            attempt_number=int(data["attempt_number"]),
            command=cmd,
            started_at_utc=str(data["started_at_utc"]),
            destination=str(data["destination"]),
            status=status,
            completed_at_utc=data.get("completed_at_utc"),
            error=data.get("error"),
        )


@dataclass
class DeliveryManifest:
    """The transport and delivery ledger for a run package.

    Kept distinct from the execution manifest so that delivery retries,
    recoveries to different destinations, and transport attempts never mutate
    the immutable execution provenance.
    """

    schema_version: str = DELIVERY_MANIFEST_SCHEMA
    run_id: str = ""
    status: str = DeliveryStatus.PENDING
    attempts: List[DeliveryAttempt] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status,
            "attempts": [a.to_dict() for a in self.attempts],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DeliveryManifest":
        schema = str(data.get("schema_version", ""))
        if schema not in SUPPORTED_DELIVERY_MANIFEST_SCHEMAS:
            raise LockSchemaError(
                "Unsupported delivery manifest schema", {"schema_version": schema}
            )
        run_id = str(data.get("run_id", "")).strip()
        if not run_id:
            raise LockSchemaError("Missing or empty run_id in delivery manifest")
        status = str(data.get("status", ""))
        if status not in ALL_DELIVERY_STATUSES:
            raise LockSchemaError(
                "Unsupported delivery manifest status",
                {"status": status, "supported": sorted(ALL_DELIVERY_STATUSES)},
            )
        raw_attempts = data.get("attempts", [])
        if not isinstance(raw_attempts, list):
            raise LockSchemaError("Delivery manifest attempts must be a list")
        attempts = [DeliveryAttempt.from_dict(a) for a in raw_attempts]
        return cls(
            schema_version=schema,
            run_id=run_id,
            status=status,
            attempts=attempts,
        )

    def write(self, target: Path, *, package_root: Optional[Path] = None) -> Path:
        return write_delivery_manifest(self, target, package_root=package_root)


def write_delivery_manifest(
    manifest: DeliveryManifest,
    target: Path,
    *,
    package_root: Optional[Path] = None,
) -> Path:
    """Safely and atomically write a delivery manifest.

    Delegates to file_safety.atomic_write_bytes to guarantee exclusive candidate
    creation, symlink refusal, and atomic rename.
    """
    data = (json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    return atomic_write_bytes(
        target,
        data,
        package_root=package_root,
        what="Delivery manifest destination",
        mode=0o644,
    )


def assert_manifest_matches_lock(manifest: BuildManifest, lock: RunLock) -> None:
    if manifest.lock_sha256 != lock.lock_sha256:
        raise LockIntegrityError(
            "Build manifest belongs to a different lock",
            {"manifest_lock_sha256": manifest.lock_sha256, "lock_sha256": lock.lock_sha256},
        )


def copy_lock_into_run(lock_path: Path, run_dir: Path) -> Path:
    """Preserve the original lock document verbatim inside the run directory."""
    source = Path(lock_path)
    if source.is_dir():
        source = source / RUN_LOCK_FILENAME
    assert_no_symlinks_in_path(source, what="Run lock source")
    if not source.is_file():
        raise LockSchemaError("Cannot preserve a lock that does not exist", {"path": str(source)})
    destination = Path(run_dir) / RUN_LOCK_FILENAME
    try:
        return atomic_write_bytes(
            destination, source.read_bytes(), what="Run lock destination", replace=False, mode=0o644
        )
    except ExportConflict as exc:
        raise LockIntegrityError(
            "Refusing to overwrite the lock already preserved in this run",
            {"path": str(destination)},
        ) from exc


def preserve_source_bundles(
    lock: RunLock, package_dir: Path, run_dir: Path, *, allow_missing: bool = False
) -> List[Path]:
    """Preserve source bundles referenced by the lock into the durable run directory.

    Copies bundles to their lock-relative paths, validating their checksums
    against lock records without falling back to any remote or network call.
    """
    from .sources import resolve_within

    package_root = Path(package_dir)
    target_root = Path(run_dir)
    preserved: List[Path] = []
    for role in ("gonka", "contracts"):
        try:
            record = lock.source_record(role)
        except Exception:
            continue
        bundle_rel = record.get("bundle_relpath")
        if not bundle_rel:
            continue
        src_path = resolve_within(package_root, str(bundle_rel), what=f"{role} source bundle")
        if not src_path.is_file():
            if allow_missing:
                continue
            raise LockIntegrityError(
                f"Source package is missing a bundle referenced by the lock",
                {"role": role, "bundle_relpath": str(bundle_rel)},
            )
        dst_path = resolve_within(target_root, str(bundle_rel), what=f"{role} destination bundle")
        expected_sha = str(record.get("bundle_sha256") or "")
        if dst_path.exists():
            if dst_path.is_symlink():
                raise PathSafetyError("Bundle destination is a symlink", {"path": str(dst_path)})
            actual_sha = sha256_file(dst_path)
            if expected_sha and actual_sha != expected_sha:
                raise LockIntegrityError(
                    f"Stored bundle hash does not match the lock for {role}",
                    {"role": role, "expected": expected_sha, "actual": actual_sha},
                )
        else:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path, follow_symlinks=False)
            actual_sha = sha256_file(dst_path)
            if expected_sha and actual_sha != expected_sha:
                raise LockIntegrityError(
                    f"Stored bundle hash does not match the lock for {role}",
                    {"role": role, "expected": expected_sha, "actual": actual_sha},
                )
        preserved.append(dst_path)

        for sub in record.get("submodules", []) or []:
            sub_rel = sub.get("bundle_relpath")
            if not sub_rel:
                continue
            src_sub = resolve_within(package_root, str(sub_rel), what="submodule bundle")
            if not src_sub.is_file():
                if allow_missing:
                    continue
                raise LockIntegrityError(
                    "Source package is missing a submodule bundle referenced by the lock",
                    {"role": role, "bundle_relpath": str(sub_rel)},
                )
            dst_sub = resolve_within(target_root, str(sub_rel), what="submodule destination")
            sub_sha = str(sub.get("bundle_sha256") or "")
            if dst_sub.exists():
                if dst_sub.is_symlink():
                    raise PathSafetyError("Submodule bundle destination is a symlink", {"path": str(dst_sub)})
                if sub_sha and sha256_file(dst_sub) != sub_sha:
                    raise LockIntegrityError(
                        "Stored submodule bundle hash does not match the lock",
                        {"expected": sub_sha, "actual": sha256_file(dst_sub)},
                    )
            else:
                dst_sub.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_sub, dst_sub, follow_symlinks=False)
                if sub_sha and sha256_file(dst_sub) != sub_sha:
                    raise LockIntegrityError(
                        "Stored submodule bundle hash does not match the lock",
                        {"expected": sub_sha, "actual": sha256_file(dst_sub)},
                    )
            preserved.append(dst_sub)
    return preserved


__all__ = [
    "ALL_DELIVERY_COMMANDS",
    "ALL_DELIVERY_STATUSES",
    "BUILD_MANIFEST_FILENAME",
    "BUILD_MANIFEST_SCHEMA",
    "BUILD_MANIFEST_SCHEMA_V1",
    "EXECUTABLE_LOCK_SCHEMAS",
    "EXECUTION_MANIFEST_SCHEMA_V1",
    "PROVENANCE_HISTORICAL_PREPARED",
    "PROVENANCE_IMMUTABLE",
    "RUN_LOCK_SCHEMA_V1",
    "SEMANTIC_LOCK_SECTIONS_V1",
    "assert_lock_executable",
    "sections_for_schema",
    "superseded_lock_markers",
    "BuildManifest",
    "DELIVERY_MANIFEST_FILENAME",
    "DELIVERY_MANIFEST_SCHEMA",
    "DeliveryAttempt",
    "DeliveryManifest",
    "DeliveryStatus",
    "EXECUTION_MANIFEST_FILENAME",
    "EXECUTION_MANIFEST_SCHEMA",
    "ExecutionManifest",
    "LOCK_ENVELOPE_SCHEMA",
    "ManifestStatus",
    "RUN_LOCK_FILENAME",
    "RUN_LOCK_SCHEMA",
    "RunLock",
    "SEMANTIC_LOCK_SECTIONS",
    "assert_manifest_matches_lock",
    "canonical_json",
    "content_sha256",
    "copy_lock_into_run",
    "load_run_lock",
    "preserve_source_bundles",
    "resolve_package_root",
    "utc_now_iso",
    "write_delivery_manifest",
    "write_run_lock",
]
