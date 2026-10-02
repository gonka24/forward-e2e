"""The durable run package: how a run is stored, read back and exported.

Two problems this module exists to solve
----------------------------------------
**1. Durability.** The executor used to write the lock, the build manifest and
the execution manifest straight into the host output directory. The raw suite
was staged on the persistent workspace volume, but that external provenance was
not: if the export failed or the host directory was lost, there was nothing left
to recover it from. Here the run is written to an *owned staging directory* on
the persistent volume first, and the host output is an **export** of that state.

**2. Reading it back.** ``report`` used to read only the nested suite, so a run
whose post-run provenance failed could still be reported as ``PASSED``. This
module loads the *whole* package -- lock, build manifest, execution manifest,
suite -- checks the links between those documents, and hands the result to the
single verdict function in :mod:`ops.a8.e2e.outcome`.

Reading is strict on purpose. A missing or unreadable manifest is not a reason
to fall back to "just grade the suite". The two are deliberately not the same
state, though, and the finding codes say which is which: a document that is
simply **absent** is ``INCOMPLETE_*`` -- the run did not get that far, and an
incomplete E2E run stays an incomplete E2E run. A document that is **present
but corrupt** is ``DOCUMENT_UNREADABLE``/``SCHEMA_UNSUPPORTED``, which is a
failure rather than an absence: something wrote bytes that claim to be the
run's provenance and are not, and treating that as "not finished yet" would be
the more forgiving of the two readings.

Nothing here starts a daemon, a build, a container or a network call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import stat
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import evidence as evidence_policy
from .builder import verify_running_images
from .ownership import verify_ownership_evidence
from .errors import BuildProvenanceError
from .errors import E2EError, ExportConflict, PathSafetyError
from .file_safety import atomic_publish_file, ensure_directory_safe, read_checked_file_bytes, sha256_checked_file, sha256_file, sha256_size_checked_file
from .outcome import NOTE, Finding, RUN_RESULT_FILENAME
from .runlock import (
    BUILD_MANIFEST_FILENAME,
    DELIVERY_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    RUN_LOCK_FILENAME,
    BuildManifest,
    DeliveryManifest,
    ExecutionManifest,
    RunLock,
    content_sha256,
    load_run_lock,
)
from .sources import assert_no_symlinks_in_path, resolve_within

SUITE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,64}\Z")


def assert_tree_safe(root_dir: Path, *, what: str) -> None:
    """Ensure root_dir, its ancestor chain, and all entries within it are symlink-free and stay inside root_dir."""
    assert_no_symlinks_in_path(root_dir, what=what)
    if not root_dir.is_dir():
        return
    resolved_root = root_dir.resolve()
    for item in root_dir.rglob("*"):
        if item.is_symlink():
            raise PathSafetyError(
                f"Refusing to operate on {what} containing a symlink",
                {"path": str(item)},
            )
        try:
            item.resolve().relative_to(resolved_root)
        except (ValueError, RuntimeError) as exc:
            raise PathSafetyError(
                f"Path inside {what} resolves outside its root",
                {"path": str(item), "root": str(resolved_root)},
            ) from exc


#: Inside the persistent workspace, one run owns ``<workspace>/<run-id>/``.
#: ``run/`` holds everything the executor itself produces; ``suite/`` is the
#: orchestrator's own staging, which it already owned before this change.
RUN_STAGE_DIRNAME = "run"

#: Directories of the run package, in the order a reader cares about them.
BUILD_OUTPUT_DIRNAME = "build"
BUILD_LOG_DIRNAME = "build-logs"
SUITE_EXPORT_DIRNAME = "suite"

#: Files that make a run package what it is. A package missing any of them is
#: incomplete -- which is a state, not an error to be papered over.
REQUIRED_DOCUMENTS = (
    RUN_LOCK_FILENAME,
    BUILD_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
)

#: Never exported. The verdict is *derived* from the package and is re-derived
#: wherever the package is read, so its bytes legitimately differ between two
#: locations. Delivery manifest is the mutable transport ledger.
#: Copying them directly would make a perfectly correct re-grade or repeated
#: recovery look like a modified file and block the very recovery it is supposed to support.
EXPORT_EXCLUDED_NAMES = frozenset({RUN_RESULT_FILENAME, DELIVERY_MANIFEST_FILENAME})


@dataclass
class ExportReport:
    """What an export actually did, file by file."""

    destination: str
    copied: List[str] = field(default_factory=list)
    identical: List[str] = field(default_factory=list)
    conflicts: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "destination": self.destination,
            "copied": list(self.copied),
            "identical": list(self.identical),
            "conflicts": list(self.conflicts),
        }


def run_stage_dir(workspace_dir: Path, run_id: str) -> Path:
    """The durable, runner-owned directory for one run's own documents."""
    return Path(workspace_dir) / run_id / RUN_STAGE_DIRNAME


def _copy_file_atomic(source_path: Path, destination_path: Path) -> None:
    """Copy a file to destination atomically via a temporary file with integrity check.

    Delegates to file_safety.atomic_publish_file to guarantee exclusive tempfile
    creation, streaming SHA256 verification, mode/timestamp preservation, and atomic publication.
    Refuses destinations created concurrently; existing-file comparisons belong to export_run_package.
    """
    atomic_publish_file(source_path, destination_path, what="Export destination file", replace=False)


def assert_export_paths_disjoint(stage_dir: Path, destination: Path) -> None:
    """Reject equal or nested export roots before either tree can be mutated."""
    source = Path(stage_dir)
    target = Path(destination)
    assert_no_symlinks_in_path(source, what="Export source directory")
    assert_no_symlinks_in_path(target, what="Export destination directory")
    source_resolved = source.resolve()
    target_resolved = target.resolve()
    if (
        source_resolved == target_resolved
        or source_resolved in target_resolved.parents
        or target_resolved in source_resolved.parents
    ):
        raise ExportConflict(
            "The export destination and durable staging directory must be separate trees.",
            {"stage_dir": str(source_resolved), "destination": str(target_resolved)},
        )


def export_run_package(
    stage_dir: Path,
    destination: Path,
    *,
    emit: Optional[Callable[[str], None]] = None,
) -> ExportReport:
    """Copy the durable run state into the host output directory.

    Re-exporting the same run twice is safe and is expected: recovery may be run
    again after a partial copy. A file that is already there with exactly the
    same bytes is left alone. A file that is there with *different* bytes is a
    conflict: it belongs to another run or was edited, and silently overwriting
    somebody else's evidence is precisely what must not happen.
    """
    say = emit or (lambda message: None)
    source = Path(stage_dir)
    target = Path(destination)
    report = ExportReport(destination=str(target))
    assert_export_paths_disjoint(source, target)
    if not source.is_dir():
        raise ExportConflict(
            "There is no durable run state to export.", {"stage_dir": str(source)}
        )
    ensure_directory_safe(target)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            # A symlink in evidence is not evidence: it points somewhere this
            # package does not own.
            raise PathSafetyError(
                "Refusing to export a symlink from the run staging directory",
                {"path": str(path)},
            )
        relative = path.relative_to(source)
        resolve_within(target, str(relative), what="export destination")
        destination_path = target / relative
        assert_no_symlinks_in_path(destination_path, what="Export destination")

        if (
            (len(relative.parts) == 1 and relative.name in EXPORT_EXCLUDED_NAMES)
            or (len(relative.parts) == 1 and relative.name == "result.json")
            or (len(relative.parts) == 1 and relative.name == "status.json")
            or (relative.name.startswith(".") and ".tmp." in relative.name)
        ):
            continue

        if path.is_dir():
            ensure_directory_safe(destination_path)
            continue
        if destination_path.exists():
            if sha256_checked_file(destination_path) == sha256_checked_file(path):
                report.identical.append(str(relative))
                continue
            report.conflicts.append(
                {
                    "path": str(relative),
                    "reason": "a different file already exists at the destination",
                }
            )
            continue
        try:
            _copy_file_atomic(path, destination_path)
        except ExportConflict:
            report.conflicts.append({
                "path": str(relative),
                "reason": "destination appeared during publication",
            })
            continue
        report.copied.append(str(relative))
    if report.conflicts:
        raise ExportConflict(
            "The destination already contains different files for this run. Nothing was "
            "overwritten; export to a new --output directory instead.",
            {"destination": str(target), "conflicts": report.conflicts},
        )
    say(
        f"Exported {len(report.copied)} file(s) to {target}"
        + (f"; {len(report.identical)} already identical" if report.identical else "")
    )
    return report


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def _validate_provenance_shapes(manifest: BuildManifest) -> None:
    """Reject malformed replay inputs before verification can leave a stale verdict."""
    def object_list(value: Any, field: str) -> list:
        if not isinstance(value, list):
            raise ValueError(f"{field} must be a list")
        if any(not isinstance(item, Mapping) for item in value):
            raise ValueError(f"{field} entries must be objects")
        return value

    observed = manifest.observed_runtime
    for section, nested in (("containers", "findings"), ("live_contexts", "comparisons")):
        for index, summary in enumerate(object_list(observed.get(section, []), f"observed_runtime.{section}")):
            field = f"observed_runtime.{section}[{index}]"
            if not isinstance(summary.get("path"), str):
                raise ValueError(f"{field}.path must be a string")
            object_list(summary.get(nested), f"{field}.{nested}")
    for index, summary in enumerate(
        object_list(observed.get("source_immutability", []), "observed_runtime.source_immutability")
    ):
        field = f"observed_runtime.source_immutability[{index}]"
        if not isinstance(summary.get("path"), str):
            raise ValueError(f"{field}.path must be a string")
        if "verified" in summary and not isinstance(summary.get("verified"), bool):
            raise ValueError(f"{field}.verified must be a boolean")
    object_list(observed.get("missing_evidence", []), "observed_runtime.missing_evidence")
    for index, failure in enumerate(manifest.failures):
        if not isinstance(failure.get("details", {}), Mapping):
            raise ValueError(f"failures[{index}].details must be an object")


@dataclass(frozen=True)
class PackageVerification:
    """Explicit verification result for a loaded E2E run package."""

    package: "LoadedRunPackage"
    suite_verification: Optional[Any] = None
    recomputed_suite_result: Optional[Any] = None
    suite_integrity_errors: Tuple[str, ...] = ()
    suite_status: Optional[str] = None
    task_evidence: Tuple[Dict[str, Any], ...] = ()
    findings: Tuple[Finding, ...] = ()
    provenance: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LoadedRunPackage:
    """A run package on disk, loaded strictly and reported honestly."""

    root: Path
    lock: Optional[RunLock] = None
    build_manifest: Optional[BuildManifest] = None
    execution_manifest: Optional[ExecutionManifest] = None
    delivery_manifest: Optional[DeliveryManifest] = None
    suite_dir: Optional[Path] = None
    suite_result: Optional[Mapping[str, Any]] = None
    suite_plan: Optional[Mapping[str, Any]] = None
    build_manifest_sha256: Optional[str] = None
    recomputed_suite_result: Optional[Any] = None
    suite_integrity_errors: List[str] = field(default_factory=list)
    _findings: List[Finding] = field(default_factory=list)
    _documents: Dict[str, Any] = field(default_factory=dict)
    _document_bytes: Dict[str, bytes] = field(default_factory=dict)
    _suite_index_bytes: Optional[bytes] = None
    _verification: Optional[PackageVerification] = None

    # -- construction ----------------------------------------------------
    @classmethod
    def load(cls, root: Path) -> "LoadedRunPackage":
        # Check before resolve() erases symlink components from the input.
        assert_no_symlinks_in_path(Path(root), what="run package root")
        package = cls(root=Path(root).resolve())
        package._load_documents()
        package._load_suite()
        return package

    def _record_document(self, name: str, path: Path) -> bool:
        try:
            assert_no_symlinks_in_path(path, what="run package document")
        except PathSafetyError as exc:
            self._documents[name] = {"present": True}
            self._findings.append(Finding(
                "DELIVERY_MANIFEST_SYMLINK" if name == DELIVERY_MANIFEST_FILENAME else "DOCUMENT_PATH_UNSAFE",
                str(exc), details={"document": name},
            ))
            return False
        try:
            file_stat = path.stat()
        except FileNotFoundError:
            self._documents[name] = {"present": False}
            return True
        except OSError as exc:
            self._documents[name] = {"present": True}
            self._findings.append(Finding("DOCUMENT_UNREADABLE", str(exc), details={"document": name}))
            return False
        entry: Dict[str, Any] = {"present": True}
        if not stat.S_ISREG(file_stat.st_mode):
            self._documents[name] = entry
            self._findings.append(Finding(
                "DOCUMENT_UNREADABLE", f"{name} is not a regular file", details={"document": name}
            ))
            return False
        try:
            data = read_checked_file_bytes(path)
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            entry["size_bytes"] = len(data)
            self._document_bytes[name] = data
        except PathSafetyError as exc:
            self._documents[name] = entry
            self._findings.append(Finding("DOCUMENT_PATH_UNSAFE", str(exc), details={"document": name}))
            return False
        except (OSError, E2EError) as exc:
            self._documents[name] = entry
            self._findings.append(Finding("DOCUMENT_UNREADABLE", str(exc), details={"document": name}))
            return False
        self._documents[name] = entry
        return True

    def _load_documents(self) -> None:
        lock_path = self.root / RUN_LOCK_FILENAME
        build_path = self.root / BUILD_MANIFEST_FILENAME
        execution_path = self.root / EXECUTION_MANIFEST_FILENAME
        delivery_path = self.root / DELIVERY_MANIFEST_FILENAME
        safe_documents = set()
        for name, path in (
            (RUN_LOCK_FILENAME, lock_path),
            (BUILD_MANIFEST_FILENAME, build_path),
            (EXECUTION_MANIFEST_FILENAME, execution_path),
            (DELIVERY_MANIFEST_FILENAME, delivery_path),
        ):
            if self._record_document(name, path):
                safe_documents.add(name)

        if RUN_LOCK_FILENAME in safe_documents and lock_path.is_file():
            try:
                self.lock = load_run_lock(lock_path, raw_bytes=self._document_bytes[RUN_LOCK_FILENAME])
            except (E2EError, KeyError, TypeError, ValueError, OSError) as exc:
                # A lock that no longer hashes to its own recorded value is
                # tampering, not a missing file. Both are blocking, but a reader
                # must be able to tell them apart.
                self._findings.append(
                    Finding(
                        getattr(exc, "code", "LOCK_UNREADABLE"),
                        f"The preserved lock could not be verified: {exc}",
                        details=dict(getattr(exc, "details", {}) or {}),
                    )
                )

        if BUILD_MANIFEST_FILENAME in safe_documents and build_path.is_file():
            payload = self._read_json(build_path, BUILD_MANIFEST_FILENAME)
            if payload is not None:
                try:
                    manifest = BuildManifest.from_dict(payload)
                    # Validate nested summary shapes before replay or reporting:
                    # malformed evidence must produce a verdict, not leave an old pass on disk.
                    _validate_provenance_shapes(manifest)
                    digest = content_sha256(manifest.to_dict())
                    self.build_manifest = manifest
                    self.build_manifest_sha256 = digest
                except (E2EError, KeyError, TypeError, ValueError, OSError) as exc:
                    self._findings.append(
                        Finding(
                            "SCHEMA_UNSUPPORTED" if isinstance(exc, E2EError) else "DOCUMENT_UNREADABLE",
                            f"The build manifest cannot be read by this runner: {exc}",
                            details={"document": BUILD_MANIFEST_FILENAME},
                        )
                    )

        if EXECUTION_MANIFEST_FILENAME in safe_documents and execution_path.is_file():
            payload = self._read_json(execution_path, EXECUTION_MANIFEST_FILENAME)
            if payload is not None:
                try:
                    self.execution_manifest = ExecutionManifest.from_dict(payload)
                except (E2EError, KeyError, TypeError, ValueError, OSError) as exc:
                    self._findings.append(
                        Finding(
                            "SCHEMA_UNSUPPORTED" if isinstance(exc, E2EError) else "DOCUMENT_UNREADABLE",
                            f"The execution manifest cannot be read by this runner: {exc}",
                            details={"document": EXECUTION_MANIFEST_FILENAME},
                        )
                    )

        if DELIVERY_MANIFEST_FILENAME in safe_documents and delivery_path.is_file():
            payload = self._read_json(delivery_path, DELIVERY_MANIFEST_FILENAME)
            if payload is not None:
                try:
                    self.delivery_manifest = DeliveryManifest.from_dict(payload)
                except Exception as exc:
                    self._findings.append(
                        Finding(
                            "DELIVERY_MANIFEST_INVALID",
                            f"The delivery manifest is invalid: {exc}",
                            details={"document": DELIVERY_MANIFEST_FILENAME, "error": str(exc)},
                        )
                    )

    def _read_json(self, path: Path, name: str) -> Optional[Mapping[str, Any]]:
        try:
            data = self._document_bytes.get(name)
            payload = json.loads((data if data is not None else read_checked_file_bytes(path)).decode("utf-8"))
        except (OSError, ValueError) as exc:
            self._findings.append(
                Finding(
                    "DOCUMENT_UNREADABLE",
                    f"{name} is present but unreadable: {exc}",
                    details={"document": name},
                )
            )
            return None
        if not isinstance(payload, Mapping):
            self._findings.append(
                Finding(
                    "DOCUMENT_UNREADABLE",
                    f"{name} is not a JSON object",
                    details={"document": name},
                )
            )
            return None
        return payload

    def _load_suite(self) -> None:
        self.suite_dir = self._resolve_suite_dir()
        if self.suite_dir is None:
            return
        result_path = self.suite_dir / "suite-result.json"
        result_safe = self._record_document("suite-result.json", result_path)
        if result_safe and result_path.is_file():
            payload = self._read_json(result_path, "suite-result.json")
            if payload is not None:
                self.suite_result = payload

        plan_path = self.suite_dir / "suite-plan.json"
        if not self._record_document("suite-plan.json", plan_path):
            return
        if not plan_path.is_file():
            self._findings.append(
                Finding(
                    "SUITE_PLAN_MISSING",
                    "Mandatory suite-plan.json is missing from suite directory",
                    details={"path": str(plan_path)},
                )
            )
        else:
            try:
                self.suite_plan = evidence_policy.load_suite_plan(
                    self.suite_dir, raw_bytes=self._document_bytes["suite-plan.json"]
                )
                if self.suite_plan:
                    schema_ver = self.suite_plan.get("schema_version")
                    from ..models import SUPPORTED_SCHEMA_VERSIONS
                    if not isinstance(schema_ver, str) or schema_ver not in SUPPORTED_SCHEMA_VERSIONS:
                        self._findings.append(
                            Finding(
                                "SUITE_PLAN_UNSUPPORTED",
                                f"Unsupported suite plan schema version: {schema_ver!r}",
                                details={"schema_version": schema_ver},
                            )
                        )
            except evidence_policy.EvidencePolicyError as exc:
                self._findings.append(
                    Finding("SUITE_PLAN_UNREADABLE", str(exc))
                )

        e2e_ctx_path = self.suite_dir / "e2e-context.json"
        if e2e_ctx_path.exists() or e2e_ctx_path.is_symlink():
            if self._record_document("e2e-context.json", e2e_ctx_path) and e2e_ctx_path.is_file():
                ctx_payload = self._read_json(e2e_ctx_path, "e2e-context.json")
                if ctx_payload is not None:
                    from ..evidence_model import EVIDENCE_MODEL_IMMUTABLE, HISTORICAL_EVIDENCE_MODELS
                    ctx_model = ctx_payload.get("evidence_model")
                    if ctx_model is not None and not isinstance(ctx_model, str):
                        self._findings.append(
                            Finding(
                                "EVIDENCE_MODEL_INVALID",
                                "e2e-context.json evidence_model must be a string.",
                                details={"actual_type": type(ctx_model).__name__},
                            )
                        )
                    elif ctx_model in HISTORICAL_EVIDENCE_MODELS:
                        self._findings.append(
                            Finding(
                                "HISTORICAL_EVIDENCE_MODEL_NOT_ACCEPTED",
                                f"e2e-context.json declares historical evidence model {ctx_model!r}; "
                                f"only {EVIDENCE_MODEL_IMMUTABLE!r} is accepted as immutable-source proof.",
                                details={"evidence_model": ctx_model},
                            )
                        )

    def _verify_suite_artifacts(self) -> Tuple[Optional[Any], Optional[Any], Tuple[str, ...], List[Finding]]:
        """Run strict suite artifact validation and outcome recalculation via ops.a8.verifier.

        Returns (suite_verification, recomputed_result, integrity_errors, findings)
        without mutating stored suite_result or self._findings.
        """
        findings: List[Finding] = []
        if self.suite_dir is None:
            return None, None, (), findings
        if not (self.suite_dir / "suite-plan.json").is_file():
            return None, None, (), findings
        from ..models import ExecutionStatus
        from ..verifier import verify_and_recalculate_suite
        try:
            plan_bytes = self._document_bytes["suite-plan.json"]
            try:
                index_bytes = read_checked_file_bytes(self.suite_dir / "artifact-index.json")
            except FileNotFoundError:
                index_bytes = None
            self._suite_index_bytes = index_bytes
            verification = verify_and_recalculate_suite(
                self.suite_dir,
                artifact_index_bytes=index_bytes,
                artifact_index_checked=True,
                artifact_digester=sha256_size_checked_file,
                artifact_reader=read_checked_file_bytes,
                suite_plan_bytes=plan_bytes,
                suite_result_bytes=self._document_bytes.get("suite-result.json"),
                suite_result_checked=True,
            )
            if read_checked_file_bytes(self.suite_dir / "suite-plan.json") != plan_bytes:
                findings.append(Finding(
                    "SUITE_PLAN_CHANGED",
                    "suite-plan.json changed after the package loaded it; the suite cannot be graded against two plans.",
                ))
            try:
                current_result_bytes = read_checked_file_bytes(self.suite_dir / "suite-result.json")
            except FileNotFoundError:
                current_result_bytes = None
            if current_result_bytes != self._document_bytes.get("suite-result.json"):
                findings.append(Finding(
                    "SUITE_RESULT_CHANGED",
                    "suite-result.json changed after the package loaded it; the suite cannot be graded against two results.",
                ))
            assert_tree_safe(self.suite_dir, what="verified suite directory")
            result = verification.result
            all_integrity_errors = tuple(verification.integrity_errors)
            for err in all_integrity_errors:
                findings.append(
                    Finding(
                        "SUITE_INTEGRITY_VIOLATION",
                        f"Suite artifact integrity check failed: {err}",
                        details={"error": err},
                    )
                )

            stored_overall = (
                self.suite_result.get("overall_status")
                if isinstance(self.suite_result, dict)
                else None
            )
            if (result.overall_status != ExecutionStatus.PASSED and stored_overall == "PASSED") or (
                result.overall_status == ExecutionStatus.PASSED
                and stored_overall not in (None, "PASSED")
            ):
                findings.append(
                    Finding(
                        "SUITE_RESULT_DISAGREEMENT",
                        f"Stored suite overall_status {stored_overall!r} contradicts recalculated task outcome {result.overall_status.value!r}",
                        details={
                            "stored_status": stored_overall,
                            "calculated_status": result.overall_status.value,
                        },
                    )
                )

            return verification, result, all_integrity_errors, findings
        except Exception as exc:
            findings.append(
                Finding(
                    "SUITE_VALIDATION_FAILED",
                    f"Offline suite validation failed: {exc}",
                    details={"error": str(exc)},
                )
            )
            return None, None, (), findings

    def _resolve_suite_dir(self) -> Optional[Path]:
        """Where this run's exported suite is, according to the run itself.

        Identifiers are checked against SUITE_ID_RE; explicit and fallback paths
        are checked with resolve_within(self.root, ...) and symlink checks
        across all intermediate directories and suite entries before reading or
        writing any file.
        """
        try:
            assert_no_symlinks_in_path(self.root, what="run package root")
            export_root_check = self.root / SUITE_EXPORT_DIRNAME
            if export_root_check.is_symlink():
                raise PathSafetyError(
                    "Suite export directory is a symlink",
                    {"path": str(export_root_check)},
                )
        except PathSafetyError as exc:
            self._findings.append(
                Finding(
                    "SUITE_PATH_UNSAFE",
                    f"The run package suite directory traverses a symlink: {exc}",
                    details=dict(getattr(exc, "details", {}) or {}),
                )
            )
            return None

        manifest = self.execution_manifest
        for field_name, value in (
            ("execution_manifest.suite_id", manifest.suite_id if manifest is not None else None),
            ("execution_manifest.run_id", manifest.run_id if manifest is not None else None),
            ("build_manifest.run_id", self.build_manifest.run_id if self.build_manifest is not None else None),
            ("delivery_manifest.run_id", self.delivery_manifest.run_id if self.delivery_manifest is not None else None),
        ):
            if value is not None and str(value) and not SUITE_ID_RE.match(str(value)):
                self._findings.append(
                    Finding(
                        "SUITE_PATH_UNSAFE",
                        f"The manifest field {field_name} ({value!r}) is not a valid safe identifier.",
                        details={"field": field_name, "value": str(value)},
                    )
                )
                return None

        if manifest is not None and manifest.suite_export_relpath:
            try:
                resolve_within(
                    self.root,
                    str(manifest.suite_export_relpath),
                    what="suite export path",
                )
                candidate = self.root / manifest.suite_export_relpath
                assert_tree_safe(candidate, what="suite export directory")
            except PathSafetyError as exc:
                self._findings.append(
                    Finding(
                        "SUITE_PATH_UNSAFE",
                        "The execution manifest points at a suite outside this run "
                        f"package or traverses a symlink: {exc}",
                        details=dict(getattr(exc, "details", {}) or {}),
                    )
                )
                return None
            if candidate.is_dir():
                return candidate
            self._findings.append(
                Finding(
                    "INCOMPLETE_SUITE_MISSING",
                    "The execution manifest names an exported suite that is not there.",
                    details={"expected": str(manifest.suite_export_relpath)},
                )
            )
            return None

        export_root = self.root / SUITE_EXPORT_DIRNAME
        if not export_root.is_dir() or export_root.is_symlink():
            return None
        suite_id = (
            (manifest.suite_id if manifest is not None and manifest.suite_id else None)
            or (self.build_manifest.run_id if self.build_manifest is not None and self.build_manifest.run_id else None)
            or (self.delivery_manifest.run_id if self.delivery_manifest is not None and self.delivery_manifest.run_id else None)
            or self.root.name
        )
        if not suite_id or not SUITE_ID_RE.match(str(suite_id)):
            self._findings.append(
                Finding(
                    "SUITE_PATH_UNSAFE",
                    f"The resolved suite identifier {suite_id!r} is not a valid safe identifier.",
                    details={"suite_id": str(suite_id)},
                )
            )
            return None
        try:
            resolve_within(
                self.root,
                f"{SUITE_EXPORT_DIRNAME}/{suite_id}",
                what="suite export path",
            )
            documented = self.root / SUITE_EXPORT_DIRNAME / str(suite_id)
            assert_tree_safe(documented, what="suite export directory")
        except PathSafetyError as exc:
            self._findings.append(
                Finding(
                    "SUITE_PATH_UNSAFE",
                    f"The suite export path is outside this run package or contains a symlink: {exc}",
                    details=dict(getattr(exc, "details", {}) or {}),
                )
            )
            return None
        if documented.is_dir() and not documented.is_symlink():
            return documented
        return None

    # -- accessors used by the verdict ------------------------------------
    def document_report(self) -> Dict[str, Any]:
        return dict(self._documents)

    def load_findings(self) -> List[Finding]:
        return list(self._findings)

    @property
    def suite_status(self) -> Optional[str]:
        if any(
            f.code in ("SUITE_PLAN_MISSING", "SUITE_PLAN_UNREADABLE", "SUITE_PLAN_UNSUPPORTED")
            for f in self._findings
        ):
            return "FAILED"
        if self.recomputed_suite_result is not None:
            return self.recomputed_suite_result.overall_status.value
        if not isinstance(self.suite_result, Mapping):
            return None
        value = self.suite_result.get("overall_status")
        return str(value) if value else None

    def _requirements(self) -> Tuple[List[Any], List[Finding]]:
        """Evidence requirements for this package, with any disagreements."""
        findings: List[Finding] = []
        if self.lock is None:
            return [], findings
        suite_id = (
            self.execution_manifest.suite_id
            if self.execution_manifest is not None
            else str((self.suite_plan or {}).get("suite_id") or "")
        )
        if not suite_id:
            return [], findings
        produced_wasm = bool(
            self.build_manifest is not None
            and (self.build_manifest.deployment or {}).get("production_wasm")
        )
        try:
            requirements, disagreements = evidence_policy.requirements_from_documents(
                lock=self.lock,
                suite_id=suite_id,
                build_produced_production_wasm=produced_wasm,
                suite_plan=self.suite_plan,
            )
        except evidence_policy.EvidencePolicyError as exc:
            findings.append(Finding("SELECTION_UNREADABLE", str(exc)))
            return [], findings
        for item in disagreements:
            findings.append(
                Finding(
                    str(item.get("code")),
                    str(item.get("message")),
                    details={k: v for k, v in item.items() if k not in {"code", "message"}},
                )
            )
        return requirements, findings

    def _compute_task_evidence(self) -> Tuple[List[Dict[str, Any]], List[Finding]]:
        requirements, findings = self._requirements()
        if not requirements or self.suite_dir is None:
            return [req.to_dict() for req in requirements], findings
        located = evidence_policy.locate_task_evidence(
            requirements, suite_dir=self.suite_dir
        )
        for item in located:
            for problem in item.problems:
                findings.append(
                    Finding(
                        str(problem.get("code") or "TASK_EVIDENCE_ERROR"),
                        str(problem.get("message") or ""),
                        details=dict(problem),
                    )
                )
        unclaimed = evidence_policy.unclaimed_live_contexts(
            requirements, suite_dir=self.suite_dir
        )
        if unclaimed:
            findings.append(
                Finding(
                    "UNCLAIMED_LIVE_CONTEXT",
                    "The suite produced live contexts that belong to no selected task; "
                    "they prove nothing about this selection.",
                    severity=NOTE,
                    details={"paths": unclaimed},
                )
            )
        return [item.to_dict() for item in located], findings

    def task_evidence(self) -> List[Dict[str, Any]]:
        if self._verification is not None:
            return [dict(item) for item in self._verification.task_evidence]
        items, _ = self._compute_task_evidence()
        return items

    def verify(self) -> PackageVerification:
        """Run explicit package verification (suite integrity, task evidence, provenance)."""
        if self._verification is not None:
            return self._verification
        suite_ver, recomputed_result, integrity_errors, suite_findings = self._verify_suite_artifacts()
        self.recomputed_suite_result = recomputed_result
        self.suite_integrity_errors = list(integrity_errors)
        task_ev, task_findings = self._compute_task_evidence()
        prov_findings = self.provenance_findings()
        combined_findings = (
            list(self._findings)
            + suite_findings
            + task_findings
            + prov_findings
        )
        verification = PackageVerification(
            package=self,
            suite_verification=suite_ver,
            recomputed_suite_result=recomputed_result,
            suite_integrity_errors=tuple(integrity_errors),
            suite_status=self.suite_status,
            task_evidence=tuple(task_ev),
            findings=tuple(combined_findings),
            provenance=self.provenance_report(),
        )
        self._verification = verification
        return verification

    def _read_indexed_suite_artifact(self, path: Path) -> bytes:
        """Read provenance evidence from the same index snapshot as suite grading."""
        if self.suite_dir is None:
            raise ValueError("The package has no suite directory")
        rel = path.relative_to(self.suite_dir).as_posix()
        index_bytes = self._suite_index_bytes
        if index_bytes is None:
            # Direct provenance_findings() callers have not graded the suite yet.
            index_bytes = read_checked_file_bytes(self.suite_dir / "artifact-index.json")
        index = json.loads(index_bytes.decode("utf-8"))
        if not isinstance(index, Mapping) or not isinstance(index.get("artifacts"), list):
            raise ValueError("artifact-index.json has no artifact list")
        entries = [
            item for item in index.get("artifacts", [])
            if isinstance(item, Mapping) and item.get("relative_path") == rel
        ]
        if len(entries) != 1 or not isinstance(entries[0].get("sha256"), str):
            raise ValueError(f"Provenance evidence {rel} has no unique indexed hash")
        data = read_checked_file_bytes(path)
        if hashlib.sha256(data).hexdigest() != entries[0]["sha256"]:
            raise ValueError(f"Provenance evidence {rel} differs from its indexed bytes")
        return data

    def provenance_findings(self) -> List[Finding]:
        """Re-check the recorded provenance offline, from the stored facts.

        This is a replay, not a reading of somebody's conclusion: the stored
        comparison *values* are re-compared, and the deployment hashes in each
        live context are re-checked against the build manifest.
        """
        findings: List[Finding] = []
        build = self.build_manifest
        if build is None:
            return findings

        for failure in build.failures or []:
            findings.append(
                Finding(
                    str(failure.get("code") or "BUILD_FAILED"),
                    str(failure.get("message") or "Build recorded a failure"),
                    details=dict(failure.get("details") or {}),
                )
            )

        # 1. deployment and per-task source immutability: the same rules the executor applied online.
        requirements, _ = self._requirements()
        suite_integrity_passed = self._suite_index_bytes is not None and not self.suite_integrity_errors
        if self.suite_dir is not None:
            for requirement in requirements:
                if not (requirement.requires_deployment_evidence or requirement.requires_source_immutability):
                    continue
                path = requirement.live_context_path(self.suite_dir)
                if not path.is_file():
                    # A missing file in an already invalid suite is reported by
                    # task evidence. If integrity just passed, it disappeared
                    # during this replay and must not preserve a PASS.
                    if suite_integrity_passed:
                        findings.append(Finding(
                            "PROVENANCE_EVIDENCE_UNREADABLE",
                            "The task's live context disappeared after suite integrity passed.",
                            details={"task_id": requirement.task_id, "path": str(path)},
                        ))
                    continue
                try:
                    raw_payload = json.loads(self._read_indexed_suite_artifact(path).decode("utf-8"))
                except (E2EError, OSError, UnicodeDecodeError, ValueError) as exc:
                    findings.append(Finding(
                        "PROVENANCE_EVIDENCE_UNREADABLE",
                        "The task's live context changed or became unreadable during provenance replay.",
                        details={"task_id": requirement.task_id, "path": str(path), "error": str(exc)},
                    ))
                    continue
                if not isinstance(raw_payload, Mapping):
                    continue
                payload = raw_payload
                if requirement.requires_source_immutability:
                    imm_path = requirement.source_immutability_path(self.suite_dir)
                    if not imm_path.is_file() and suite_integrity_passed:
                        findings.append(Finding(
                            "PROVENANCE_EVIDENCE_UNREADABLE",
                            "The task's source immutability evidence disappeared after suite integrity passed.",
                            details={"task_id": requirement.task_id, "path": str(imm_path)},
                        ))
                    if imm_path.is_file():
                        try:
                            imm_bytes = self._read_indexed_suite_artifact(imm_path)
                        except (E2EError, OSError, ValueError) as exc:
                            findings.append(Finding(
                                "PROVENANCE_EVIDENCE_UNREADABLE",
                                "The task's source immutability evidence changed or became unreadable during provenance replay.",
                                details={"task_id": requirement.task_id, "path": str(imm_path), "error": str(exc)},
                            ))
                            imm_bytes = None
                        if imm_bytes is not None:
                            network_path = path.parent / "network/network-manifest.json"
                            network_bytes = None
                            if not network_path.is_file() and suite_integrity_passed:
                                findings.append(Finding(
                                    "PROVENANCE_EVIDENCE_UNREADABLE",
                                    "The task's network manifest disappeared after suite integrity passed.",
                                    details={"task_id": requirement.task_id, "path": str(network_path)},
                                ))
                            if network_path.is_file():
                                try:
                                    network_bytes = self._read_indexed_suite_artifact(network_path)
                                except (E2EError, OSError, ValueError) as exc:
                                    findings.append(Finding(
                                        "PROVENANCE_EVIDENCE_UNREADABLE",
                                        "The task's network manifest changed or became unreadable during provenance replay.",
                                        details={"task_id": requirement.task_id, "path": str(network_path), "error": str(exc)},
                                    ))
                            imm_report = evidence_policy_verify_source_immutability(
                                imm_bytes, payload, lock=self.lock,
                                network_manifest=network_bytes,
                            )
                            if imm_report["mismatches"]:
                                findings.append(
                                    Finding(
                                        "SOURCE_IMMUTABILITY_VIOLATED",
                                        "A task's recorded source-immutability.json reports a mutated "
                                        "snapshot or disagrees with live-context.json or the locked commits.",
                                        details={
                                            "task_id": requirement.task_id,
                                            "mismatches": imm_report["mismatches"],
                                        },
                                    )
                                )
                            elif imm_report["missing"]:
                                findings.append(
                                    Finding(
                                        "SOURCE_IMMUTABILITY_INCOMPLETE",
                                        "A task's source-immutability.json or live-context.json is missing "
                                        "required source immutability fields.",
                                        details={
                                            "task_id": requirement.task_id,
                                            "missing": imm_report["missing"],
                                        },
                                    )
                                )
                if not requirement.requires_deployment_evidence:
                    continue
                report = evidence_policy_verify_deployment(payload, build=build)
                if report["mismatches"]:
                    findings.append(
                        Finding(
                            "DEPLOYED_ARTEFACT_MISMATCH",
                            "The contracts recorded as deployed are not the ones this "
                            "build produced.",
                            details={
                                "task_id": requirement.task_id,
                                "mismatches": report["mismatches"],
                            },
                        )
                    )
                elif report["missing"]:
                    findings.append(
                        Finding(
                            "DEPLOYED_ARTEFACT_NOT_STATED",
                            "A native task did not state which contracts it deployed, so "
                            "its result cannot be tied to this build.",
                            details={
                                "task_id": requirement.task_id,
                                "missing": report["missing"],
                            },
                        )
                    )

        # 2. runtime: re-derive each recorded comparison's verdict.
        observed = build.observed_runtime or {}
        for entry in observed.get("live_contexts", []) or []:
            for comparison in entry.get("comparisons", []) or []:
                expected = comparison.get("expected")
                actual = comparison.get("observed", comparison.get("actual"))
                recorded = str(comparison.get("status", "")).upper()
                if comparison.get("kind") == "literal_pattern":
                    pattern = comparison.get("source_of_truth")
                    try:
                        agrees = (isinstance(pattern, str) and bool(pattern) and bool(actual)
                                  and re.search(pattern, str(actual)) is not None)
                    except re.error:
                        agrees = False
                else:
                    agrees = (expected is not None and actual is not None
                              and str(expected).lower() == str(actual).lower())
                if recorded in {"MATCH", "MATCHED", "OK"} and not agrees:
                    findings.append(
                        Finding(
                            "RUNTIME_COMPARISON_INCONSISTENT",
                            "A recorded runtime comparison is marked as matching but its "
                            "own values disagree.",
                            details={"path": entry.get("path"), "comparison": comparison},
                        )
                    )
                elif not agrees and recorded not in {"MATCH", "MATCHED", "OK"}:
                    findings.append(
                        Finding(
                            "RUNTIME_MISMATCH",
                            "The runtime observed during the run does not match what was "
                            "built.",
                            details={"path": entry.get("path"), "comparison": comparison},
                        )
                    )

        # Re-read the raw evidence even when the stored summary claims success.
        if self.suite_dir is not None:
            try:
                containers, missing = verify_ownership_evidence(
                    self.suite_dir, requirements=requirements, build=build,
                    runtime_external_images=(self.lock.build.get("gonka_recipe", {}).get(
                        "runtime_external_images", {}) if self.lock else {}),
                    platform=str(self.lock.platform.get("docker_platform", "")) if self.lock else None,
                )
                for item in missing:
                    findings.append(Finding("OWNERSHIP_EVIDENCE_MISSING",
                                            "A native task left no ownership evidence.", details=item))
                recorded_by_path = {entry.get("path"): entry for entry in observed.get("containers", [])}
                for entry in containers:
                    recorded = recorded_by_path.get(entry["path"])
                    if recorded is not None:
                        def identities(items):
                            return sorted((str(item.get("container")), str(item.get("image_id")),
                                           str(item.get("image_reference") or "")) for item in items)
                        if identities(recorded.get("findings", [])) != identities(entry["findings"]):
                            findings.append(Finding("OWNERSHIP_SUMMARY_MISMATCH",
                                                    "Ownership evidence disagrees with its recorded summary.",
                                                    details={"path": entry["path"]}))
            except E2EError as exc:
                findings.append(Finding("OWNERSHIP_EVIDENCE_INVALID", str(exc), details=exc.details))

        # 3. images: replay image identities and pinned dependency facts instead
        # of trusting the producer's from_this_build/pinned_runtime_dependency flags.
        for entry in observed.get("containers", []) or []:
            try:
                verify_running_images(
                    expected_images=build.images,
                    running={str(i): {"image": f.get("image_id"),
                                      "image_reference": f.get("image_reference")}
                             for i, f in enumerate(entry.get("findings", []) or [])},
                    runtime_external_images=(self.lock.build.get("gonka_recipe", {}).get(
                        "runtime_external_images", {}) if self.lock else {}),
                    runtime_dependencies=build.runtime_dependencies,
                    platform=str(self.lock.platform.get("docker_platform", "")) if self.lock else None,
                )
            except BuildProvenanceError as exc:
                findings.append(Finding("RUNNING_IMAGE_MISMATCH", str(exc),
                                        details={"path": entry.get("path"), **exc.details}))

        # 4. evidence the executor itself recorded as missing while the run was
        # still online. It is kept as a separate finding from the reader's own
        # lookup: the two agreeing is the point, and a package where only one of
        # them notices is a package worth looking at.
        for entry in observed.get("missing_evidence", []) or []:
            findings.append(
                Finding(
                    "TASK_EVIDENCE_MISSING_AT_RUNTIME",
                    "The run itself recorded that a selected task left required evidence missing.",
                    details=dict(entry),
                )
            )
        return findings

    def provenance_report(self) -> Dict[str, Any]:
        build = self.build_manifest
        if build is None:
            return {}
        return {
            "source_fingerprint": build.source_fingerprint,
            "source_immutability": dict(build.source_immutability or {}),
            "build_inputs": dict(build.build_inputs or {}),
            "staged_contexts": list(build.staged_contexts or []),
            "component_identities": (build.prepared_runtime or {}).get(
                "component_identities", {}
            ),
            "instrumented": bool(build.instrumented),
            "deployment": dict(build.deployment or {}),
            "observed_runtime_summary": {
                "live_contexts": len((build.observed_runtime or {}).get("live_contexts", []) or []),
                "containers": len((build.observed_runtime or {}).get("containers", []) or []),
                "source_immutability": len(
                    (build.observed_runtime or {}).get("source_immutability", []) or []
                ),
            },
        }


def evidence_policy_verify_deployment(
    payload: Mapping[str, Any], *, build: BuildManifest
) -> Dict[str, Any]:
    """Offline form of the executor's deployment check.

    Kept next to the reader rather than imported from the executor so that
    reporting never pulls in the execution machinery, and implemented by calling
    the *same* shared function so the two can not drift.
    """
    from .deployment import verify_deployed_artifacts

    return verify_deployed_artifacts(
        payload,
        built_wasm=build.wasm,
        expected_manifest_sha256=(build.deployment or {}).get("a9_manifest_sha256"),
    )


def evidence_policy_verify_source_immutability(
    document: bytes, payload: Mapping[str, Any], *, lock: Optional[RunLock] = None,
    network_manifest: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Offline form of the executor's task-level source-immutability check."""
    from .deployment import verify_source_immutability_evidence

    gonka_sha = str((lock.gonka or {}).get("commit_sha") or "") or None if lock is not None else None
    marketplace_sha = (
        str((lock.contracts or {}).get("commit_sha") or "") or None if lock is not None else None
    )
    return verify_source_immutability_evidence(
        document,
        payload,
        gonka_sha=gonka_sha,
        marketplace_sha=marketplace_sha,
        network_manifest=network_manifest,
    )


def verify_run_package(package: LoadedRunPackage) -> PackageVerification:
    """Explicitly verify a loaded run package's suite evidence, task evidence, and provenance."""
    return package.verify()


__all__ = [
    "BUILD_LOG_DIRNAME",
    "BUILD_OUTPUT_DIRNAME",
    "EXPORT_EXCLUDED_NAMES",
    "ExportConflict",
    "ExportReport",
    "LoadedRunPackage",
    "PackageVerification",
    "REQUIRED_DOCUMENTS",
    "RUN_STAGE_DIRNAME",
    "SUITE_EXPORT_DIRNAME",
    "_copy_file_atomic",
    "assert_tree_safe",
    "export_run_package",
    "run_stage_dir",
    "sha256_file",
    "verify_run_package",
]
