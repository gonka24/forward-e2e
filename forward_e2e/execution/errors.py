"""Typed errors for the E2E runner.

Every error carries a stable machine readable ``code`` and an ``exit_code``.
The CLI maps them to process exit codes; evidence and logs quote the ``code``
so that a reviewer can grep for a precise failure class instead of matching
free-form English.

Exit code contract (identical to the legacy A8 runner so that existing
automation keeps working):

* ``0``   success
* ``1``   operational failure (acquisition, integrity, build, execution)
* ``2``   usage / invalid parameters, detected before any long operation
* ``130`` SIGINT, ``143`` SIGTERM
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


class E2EError(RuntimeError):
    """Base class for every error raised by the E2E runner."""

    code = "E2E_ERROR"
    exit_code = 1

    def __init__(self, message: str, details: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details: Dict[str, Any] = dict(details or {})

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.details:
            rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(self.details.items()))
            return f"[{self.code}] {self.message} ({rendered})"
        return f"[{self.code}] {self.message}"


class UsageError(E2EError):
    """Invalid command line usage. Always detected before any slow work."""

    code = "USAGE"
    exit_code = 2


class SourceSpecError(UsageError):
    """A repository/path/SHA argument is syntactically unacceptable."""

    code = "INVALID_SOURCE_SPEC"


class SelectionError(UsageError):
    """Profile / scenario selection is empty, unknown or contradictory."""

    code = "INVALID_SELECTION"


class LockOverrideError(UsageError):
    """``--from`` was combined with a flag that would change the semantics."""

    code = "LOCK_OVERRIDE_REJECTED"


class OutputCollisionError(UsageError):
    """The requested output identity already exists."""

    code = "OUTPUT_COLLISION"


class GitCommandError(E2EError):
    """A ``git`` invocation returned a non-zero exit code."""

    code = "GIT_COMMAND_FAILED"


class SourceAcquisitionError(E2EError):
    """The requested commit could not be obtained *exactly*.

    Never raised as a soft warning: the runner must refuse to continue with a
    different version than the one the user pinned.
    """

    code = "SOURCE_ACQUISITION_FAILED"


class UnsupportedSourceError(E2EError):
    """The source snapshot cannot be represented completely (LFS, submodule)."""

    code = "UNSUPPORTED_SOURCE"


class IntegrityError(E2EError):
    """A stored artefact does not match its recorded hash, or a path escapes."""

    code = "INTEGRITY_FAILED"


class PathSafetyError(IntegrityError):
    """A filesystem path violates containment or symlink safety requirements."""

    code = "PATH_SAFETY_VIOLATION"


class UnsupportedCompatibilityError(E2EError):
    """No compatibility adapter can drive the selected source combination."""

    code = "UNSUPPORTED_COMPATIBILITY"


class PatchPreconditionError(E2EError):
    """A declared test-only patch does not match the real target tree."""

    code = "PATCH_PRECONDITION_MISMATCH"


class RunnerImageError(E2EError):
    """The runner image is missing, ambiguous, or different from the lock."""

    code = "RUNNER_IMAGE_MISMATCH"


class LockSchemaError(E2EError):
    """A lock or manifest document has an unsupported or corrupted schema."""

    code = "LOCK_SCHEMA_UNSUPPORTED"


class LockIntegrityError(E2EError):
    """The lock or its referenced data violates integrity requirements.

    Covers modified documents, missing or mismatched bundles, and invalid
    execution parameters recorded in the lock.
    """

    code = "LOCK_TAMPERED"


class BuildProvenanceError(E2EError):
    """A built artefact cannot be tied back to the requested sources."""

    code = "BUILD_PROVENANCE_FAILED"


class SuiteExecutionError(E2EError):
    """The suite returned an unsuccessful exit code, regardless of its evidence."""

    code = "SUITE_EXECUTION_FAILED"


class ObservedVersionError(BuildProvenanceError):
    """A running component reports a version the adapter does not expect."""

    code = "OBSERVED_VERSION_MISMATCH"


class ExecutionCancelled(E2EError):
    """The operator asked the run to stop (SIGINT/SIGTERM).

    This is not a verdict about the sources: it is an interruption. It carries
    the signal's own exit code so that ``run`` reports 130/143 rather than the
    generic failure code, and it is raised at the first safe point after the
    signal so that the partial evidence written so far is preserved.
    """

    code = "EXECUTION_CANCELLED"
    exit_code = 143

    def __init__(self, message, details=None, *, exit_code: int = 143):
        super().__init__(message, details)
        self.exit_code = exit_code


class ExportConflict(E2EError):
    """Conflicting destination state or missing source state prevents safe export."""

    code = "RUN_EXPORT_CONFLICT"


class PlanSchemaSupersededError(E2EError):
    """A plan written for the overlay / prepared-commit model was given to run/rerun.

    Such a plan *permits* a modified source tree (an overlay allow-list, a
    prepared commit that differs from the selected one). Executing it under
    the immutable-source runner would either silently drop those permissions or
    honour them; both would make the result mean something other than what the
    plan says. The only honest answer is to ask for a new plan. Old reports and
    raw evidence are never touched.
    """

    code = "PLAN_SCHEMA_SUPERSEDED"
    exit_code = 1


class SourceNotPristineError(BuildProvenanceError):
    """A materialised source snapshot is not exactly its selected commit.

    Raised before the first build step: a snapshot with a changed, added or
    deleted file, a rewritten index, a drifted submodule or a planted binary
    would make the build describe a tree nobody selected.
    """

    code = "SOURCE_NOT_PRISTINE"


class SourceSnapshotMutatedError(BuildProvenanceError):
    """A source snapshot changed between two measurements of the same run.

    Raised after the build and after execution. Whatever wrote into the
    snapshot -- a build tool, a test, a mount -- the tested tree is no longer
    the selected one, so the run can never be a PASS.
    """

    code = "SOURCE_SNAPSHOT_MUTATED"


class BuildOutputInSourceError(BuildProvenanceError):
    """A build step would write (or stage) an output inside a source snapshot."""

    code = "BUILD_OUTPUT_IN_SOURCE"


__all__ = [
    "BuildOutputInSourceError",
    "BuildProvenanceError",
    "E2EError",
    "ExecutionCancelled",
    "ExportConflict",
    "GitCommandError",
    "IntegrityError",
    "LockIntegrityError",
    "LockOverrideError",
    "LockSchemaError",
    "ObservedVersionError",
    "OutputCollisionError",
    "PatchPreconditionError",
    "PathSafetyError",
    "PlanSchemaSupersededError",
    "RunnerImageError",
    "SelectionError",
    "SourceAcquisitionError",
    "SourceNotPristineError",
    "SourceSnapshotMutatedError",
    "SourceSpecError",
    "SuiteExecutionError",
    "UnsupportedCompatibilityError",
    "UnsupportedSourceError",
    "UsageError",
]
