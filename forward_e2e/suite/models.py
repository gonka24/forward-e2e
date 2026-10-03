"""Strongly typed models and serialization for the A8 unified runner.

Uses strictly the Python standard library. No Linux-specific or external imports.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SUPPORTED_SCHEMA_VERSIONS = {"1.0.0"}


class ExecutionStatus(str, Enum):
    NOT_RUN = "NOT_RUN"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class EvidenceStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    INVALID = "INVALID"
    NOT_COLLECTED = "NOT_COLLECTED"


class CleanupStatus(str, Enum):
    NOT_NEEDED = "NOT_NEEDED"
    CLEANED = "CLEANED"
    KEPT = "KEPT"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class AcceptanceStatus(str, Enum):
    """The automated runner NEVER has the authority to declare manual acceptance PASS.

    Any new automated run has status NOT_REVIEWED until a human reviewer inspects it.
    """
    NOT_REVIEWED = "NOT_REVIEWED"


class ProofLevel(str, Enum):
    NATIVE = "NATIVE"
    GO_BOUNDARY = "GO_BOUNDARY"
    WASM_ABI = "WASM_ABI"
    CONTRACT_TEST = "CONTRACT_TEST"


#: Characters of the task ID kept in a child run ID. Shared with the collector,
#: which recognises a task's run directory by this truncated form; a longer
#: task ID never appears whole in an artifact path.
TASK_RUN_ID_TASK_CHARS = 25


def make_task_run_id(suite_id: str, ordinal: int, task_id: str) -> str:
    """Generate bounded child RunId (<= 65 chars).

    Defined here so that the orchestrator (which creates run directories) and
    the offline reporter (which audits them) can never drift apart.
    """
    short_sid = suite_id[:30]
    short_tid = task_id[:TASK_RUN_ID_TASK_CHARS]
    return f"{short_sid}-{ordinal:02d}-{short_tid}"


# scripts/acceptance_harness.py writes phases either into a named scenario record
# (context["scenarios"][name]["phases"]) or, via append_phase()/--name bootstrap,
# into the harness-level context["phases"] list. This sentinel names the latter
# so that a task can declare it explicitly instead of relying on a fallback.
TOP_LEVEL_SCOPE = "<top-level>"



#: Fields that only historical (overlay / prepared-build) documents carry. They
#: are still parsed so that an old ``suite-plan.json`` stays readable, but the
#: immutable-source path never sets them, and ``to_dict`` omits them when unset
#: so that a new document cannot even appear to make a prepared-tree claim.
HISTORICAL_SOURCE_IDENTITY_FIELDS = ("gonka_overlay_manifest_sha256", "gonka_prepared_sha")


@dataclass
class SourceIdentity:
    marketplace_commit_sha: str
    gonka_commit_sha: str
    runner_version_hash: str
    catalog_version_hash: str
    # Immutable-source facts: the Git tree objects of both selected commits and
    # the harness's verdict that neither snapshot changed. ``None`` means
    # "not known", never "unchanged".
    gonka_tree_sha: Optional[str] = None
    marketplace_tree_sha: Optional[str] = None
    source_immutability_verdict: Optional[str] = None
    # Historical, read-only (see HISTORICAL_SOURCE_IDENTITY_FIELDS).
    gonka_overlay_manifest_sha256: Optional[str] = None
    gonka_prepared_sha: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        for name in HISTORICAL_SOURCE_IDENTITY_FIELDS:
            if data.get(name) is None:
                data.pop(name, None)
        return data

    @property
    def is_historical(self) -> bool:
        """True when this identity describes an overlay / prepared-build tree."""
        return any(getattr(self, name) for name in HISTORICAL_SOURCE_IDENTITY_FIELDS)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SourceIdentity:
        return cls(
            marketplace_commit_sha=str(data["marketplace_commit_sha"]),
            gonka_commit_sha=str(data["gonka_commit_sha"]),
            runner_version_hash=str(data["runner_version_hash"]),
            catalog_version_hash=str(data["catalog_version_hash"]),
            gonka_tree_sha=data.get("gonka_tree_sha"),
            marketplace_tree_sha=data.get("marketplace_tree_sha"),
            source_immutability_verdict=data.get("source_immutability_verdict"),
            gonka_overlay_manifest_sha256=data.get("gonka_overlay_manifest_sha256"),
            gonka_prepared_sha=data.get("gonka_prepared_sha"),
        )


@dataclass
class TaskPlan:
    task_id: str
    ordinal: int
    proof_level: ProofLevel
    description: str
    scenario_selector: str
    timeout_minutes: int
    stage_timeout_seconds: int
    expected_artifacts: List[str]
    expected_checkpoints: List[str]
    coverage_ids: List[str]
    limitations: List[str]
    exact_test_method: Optional[str] = None
    gradle_timeout_minutes: int = 45
    # Exact producer scopes (scripts/acceptance_harness.py) that may contribute
    # evidence for this task: named scenario keys and/or TOP_LEVEL_SCOPE for the
    # harness-level context["phases"] list. An empty list means the task has no
    # live-context evidence and keeps the selector-only behaviour.
    evidence_scopes: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["proof_level"] = self.proof_level.value
        if not self.aliases:
            d.pop("aliases", None)
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TaskPlan:
        timeout_minutes = int(data["timeout_minutes"])
        return cls(
            task_id=str(data["task_id"]),
            ordinal=int(data["ordinal"]),
            proof_level=ProofLevel(data["proof_level"]),
            description=str(data["description"]),
            scenario_selector=str(data["scenario_selector"]),
            timeout_minutes=timeout_minutes,
            stage_timeout_seconds=int(data.get("stage_timeout_seconds", timeout_minutes * 60)),
            expected_artifacts=list(data.get("expected_artifacts", [])),
            expected_checkpoints=list(data.get("expected_checkpoints", [])),
            coverage_ids=list(data.get("coverage_ids", [])),
            limitations=list(data.get("limitations", [])),
            exact_test_method=data.get("exact_test_method"),
            gradle_timeout_minutes=int(data.get("gradle_timeout_minutes", 45)),
            evidence_scopes=list(data.get("evidence_scopes", [])),
            aliases=list(data.get("aliases", [])),
        )


@dataclass
class SuitePlan:
    schema_version: str
    suite_id: str
    created_at_utc: str
    profile: Optional[str]
    requested_scenarios: Optional[List[str]]
    source_identity: SourceIdentity
    tasks: List[TaskPlan]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "suite_id": self.suite_id,
            "created_at_utc": self.created_at_utc,
            "profile": self.profile,
            "requested_scenarios": self.requested_scenarios,
            "source_identity": self.source_identity.to_dict(),
            "tasks": [t.to_dict() for t in self.tasks],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SuitePlan:
        ver = str(data.get("schema_version", "1.0.0"))
        if ver not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"Unsupported SuitePlan schema_version: {ver!r}")
        return cls(
            schema_version=ver,
            suite_id=str(data["suite_id"]),
            created_at_utc=str(data["created_at_utc"]),
            profile=data.get("profile"),
            requested_scenarios=data.get("requested_scenarios"),
            source_identity=SourceIdentity.from_dict(data["source_identity"]),
            tasks=[TaskPlan.from_dict(t) for t in data["tasks"]],
        )


@dataclass
class TaskResult:
    task_id: str
    ordinal: int
    run_id: str
    proof_level: ProofLevel
    execution_status: ExecutionStatus
    evidence_status: EvidenceStatus
    cleanup_status: CleanupStatus
    acceptance_status: AcceptanceStatus
    start_time_utc: Optional[str]
    end_time_utc: Optional[str]
    duration_seconds: Optional[float]
    phase: str
    exit_code: Optional[int]
    primary_failure: Optional[str]
    secondary_errors: List[str]
    expected_cases: List[str]
    observed_passed_cases: List[str]
    missing_cases: List[str]
    missing_artifacts: List[str]
    raw_evidence_dir: Optional[str]
    exported_evidence_dir: Optional[str]
    raw_execution_status: Optional[ExecutionStatus] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "task_id": self.task_id,
            "ordinal": self.ordinal,
            "run_id": self.run_id,
            "proof_level": self.proof_level.value,
            "execution_status": self.execution_status.value,
            "evidence_status": self.evidence_status.value,
            "cleanup_status": self.cleanup_status.value,
            "acceptance_status": self.acceptance_status.value,
            "start_time_utc": self.start_time_utc,
            "end_time_utc": self.end_time_utc,
            "duration_seconds": self.duration_seconds,
            "phase": self.phase,
            "exit_code": self.exit_code,
            "primary_failure": self.primary_failure,
            "secondary_errors": list(self.secondary_errors),
            "expected_cases": list(self.expected_cases),
            "observed_passed_cases": list(self.observed_passed_cases),
            "missing_cases": list(self.missing_cases),
            "missing_artifacts": list(self.missing_artifacts),
            "raw_evidence_dir": self.raw_evidence_dir,
            "exported_evidence_dir": self.exported_evidence_dir,
        }
        if self.raw_execution_status is not None:
            d["raw_execution_status"] = self.raw_execution_status.value
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TaskResult:
        raw_exec = data.get("raw_execution_status")
        return cls(
            task_id=str(data["task_id"]),
            ordinal=int(data["ordinal"]),
            run_id=str(data["run_id"]),
            proof_level=ProofLevel(data["proof_level"]),
            execution_status=ExecutionStatus(data["execution_status"]),
            evidence_status=EvidenceStatus(data["evidence_status"]),
            cleanup_status=CleanupStatus(data["cleanup_status"]),
            acceptance_status=AcceptanceStatus(data.get("acceptance_status", AcceptanceStatus.NOT_REVIEWED.value)),
            start_time_utc=data.get("start_time_utc"),
            end_time_utc=data.get("end_time_utc"),
            duration_seconds=data.get("duration_seconds"),
            phase=str(data.get("phase", "UNKNOWN")),
            exit_code=data.get("exit_code"),
            primary_failure=data.get("primary_failure"),
            secondary_errors=list(data.get("secondary_errors", [])),
            expected_cases=list(data.get("expected_cases", [])),
            observed_passed_cases=list(data.get("observed_passed_cases", [])),
            missing_cases=list(data.get("missing_cases", [])),
            missing_artifacts=list(data.get("missing_artifacts", [])),
            raw_evidence_dir=data.get("raw_evidence_dir"),
            exported_evidence_dir=data.get("exported_evidence_dir"),
            raw_execution_status=ExecutionStatus(raw_exec) if raw_exec else None,
        )


@dataclass
class SuiteResult:
    schema_version: str
    suite_id: str
    created_at_utc: str
    completed_at_utc: Optional[str]
    source_identity: SourceIdentity
    overall_status: ExecutionStatus
    tasks: List[TaskResult]
    summary_message: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "suite_id": self.suite_id,
            "created_at_utc": self.created_at_utc,
            "completed_at_utc": self.completed_at_utc,
            "source_identity": self.source_identity.to_dict(),
            "overall_status": self.overall_status.value,
            "tasks": [t.to_dict() for t in self.tasks],
            "summary_message": self.summary_message,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SuiteResult:
        ver = str(data.get("schema_version", "1.0.0"))
        if ver not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"Unsupported SuiteResult schema_version: {ver!r}")
        return cls(
            schema_version=ver,
            suite_id=str(data["suite_id"]),
            created_at_utc=str(data["created_at_utc"]),
            completed_at_utc=data.get("completed_at_utc"),
            source_identity=SourceIdentity.from_dict(data["source_identity"]),
            overall_status=ExecutionStatus(data["overall_status"]),
            tasks=[TaskResult.from_dict(t) for t in data["tasks"]],
            summary_message=str(data.get("summary_message", "")),
        )


@dataclass
class ArtifactEntry:
    relative_path: str
    size_bytes: int
    sha256: str
    run_id: str
    task_id: str
    artifact_kind: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ArtifactEntry:
        return cls(
            relative_path=str(data["relative_path"]),
            size_bytes=int(data["size_bytes"]),
            sha256=str(data["sha256"]),
            run_id=str(data["run_id"]),
            task_id=str(data["task_id"]),
            artifact_kind=str(data["artifact_kind"]),
        )


@dataclass
class EventRecord:
    timestamp_utc: str
    suite_id: str
    run_id: Optional[str]
    task_id: Optional[str]
    phase: str
    event_type: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":")) + "\n"

    @classmethod
    def from_json_line(cls, line: str) -> EventRecord:
        data = json.loads(line)
        return cls(
            timestamp_utc=data["timestamp_utc"],
            suite_id=data["suite_id"],
            run_id=data.get("run_id"),
            task_id=data.get("task_id"),
            phase=data["phase"],
            event_type=data["event_type"],
            message=data["message"],
            details=data.get("details", {}),
        )


def calculate_suite_outcome(
    tasks: Sequence[TaskResult],
    artifact_integrity_errors: Optional[Sequence[str]] = None,
    is_interrupted: bool = False,
    interrupted: Optional[bool] = None,
    is_cancelled: bool = False,
    has_corruption_or_index_errors: bool = False,
    has_export_or_secondary_errors: bool = False,
    interrupted_signal: Optional[int] = None,
) -> Tuple[ExecutionStatus, int]:
    """Single authoritative outcome calculation function used by both orchestrator and offline reporter.

    Outcome determination precedence:
    1. User cancellation (SIGINT/SIGTERM): CANCELLED (exit 130 or 143)
    2. Artifact corruption, index errors, or hash mismatch: FAILED (exit 1)
    3. Empty task set: FAILED (exit 1)
    4. Any task failed execution: FAILED (exit 1)
    5. Any task timed out: TIMED_OUT (exit 1)
    6. Completed tasks have incomplete/missing evidence (or any evidence is INVALID): FAILED (exit 1)
    7. Resources are KEPT, cleanup FAILED, or successful tasks have UNKNOWN cleanup: FAILED (exit 1)
    8. Export or secondary errors: FAILED (exit 1)
    9. Premature stop / unexecuted tasks without failure: INTERRUPTED (exit 1)
    10. All tasks PASSED with COMPLETE evidence and CLEANED/NOT_NEEDED cleanup: PASSED (exit 0)
    """
    user_cancelled = is_cancelled or interrupted is True or any(
        t.execution_status == ExecutionStatus.CANCELLED for t in tasks
    )
    if user_cancelled:
        cancel_code = interrupted_signal if interrupted_signal in (130, 143) else 130
        return ExecutionStatus.CANCELLED, cancel_code

    # Outside cancellation, corruption takes precedence over task outcomes.
    if has_corruption_or_index_errors or (artifact_integrity_errors and len(artifact_integrity_errors) > 0):
        return ExecutionStatus.FAILED, 1

    if not tasks:
        return ExecutionStatus.FAILED, 1

    # Check for execution failures
    if any(t.execution_status == ExecutionStatus.FAILED for t in tasks):
        return ExecutionStatus.FAILED, 1

    # Check for timeout
    if any(t.execution_status == ExecutionStatus.TIMED_OUT for t in tasks):
        return ExecutionStatus.TIMED_OUT, 1

    # Check for evidence incompleteness or invalidity among completed tasks
    if any(
        t.evidence_status == EvidenceStatus.INVALID
        or (t.execution_status == ExecutionStatus.PASSED and t.evidence_status != EvidenceStatus.COMPLETE)
        for t in tasks
    ):
        return ExecutionStatus.FAILED, 1

    # Check for cleanup failures or kept resources
    if any(
        t.cleanup_status in (CleanupStatus.KEPT, CleanupStatus.FAILED)
        or (t.execution_status == ExecutionStatus.PASSED
            and t.cleanup_status not in (CleanupStatus.CLEANED, CleanupStatus.NOT_NEEDED))
        for t in tasks
    ):
        return ExecutionStatus.FAILED, 1

    # Check for secondary/collection errors
    if has_export_or_secondary_errors or any(t.secondary_errors for t in tasks):
        return ExecutionStatus.FAILED, 1

    # Pending tasks need not have evidence yet, but cannot hide completed failures.
    if is_interrupted or any(t.execution_status != ExecutionStatus.PASSED for t in tasks):
        return ExecutionStatus.INTERRUPTED, 1

    return ExecutionStatus.PASSED, 0
