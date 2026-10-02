"""The one place a full E2E run is graded.

Why one place
-------------
A run has three verdicts hiding inside it: the suite's own result, the build
manifest's status, and the post-run provenance findings. Grading any of them
alone gives a different answer:

* the suite can pass while the build manifest records a provenance mismatch;
* the build can be complete while the suite never ran;
* a cancelled run has no verdict about the sources at all.

Before this module, ``run`` returned the suite's exit code and ``report`` read
only the nested suite directory, so the same run could exit non-zero at the end
of ``run`` and be reported ``PASSED`` afterwards. :func:`evaluate_run` is
therefore the single function used by ``run``, ``report`` and ``recover``, and
the machine-readable result, the summary and the exit code all come from it.

Provenance models
-----------------
Only an ``e2e/run-lock/2`` package -- built from untouched source snapshots
whose fingerprints were taken before the build, after the build and after
execution -- can be ``PASSED``. A package written by the retired overlay /
prepared-commit runner (``/1`` documents) stays readable and is graded with the
same rules as before, but it is classified ``historical-prepared-build`` and
carries the blocking ``HISTORICAL_PREPARED_BUILD`` finding: it is evidence about
a tree that the runner itself modified, so it can never be turned into proof
about unmodified sources. Its original ``e2e-run-result.json`` is never
overwritten; a re-derived verdict for such a package is written next to it
under :data:`HISTORICAL_REGRADE_FILENAME`.

Nothing here executes anything: it reads documents that already exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .file_safety import atomic_write_bytes
from .runlock import (
    ALL_DELIVERY_STATUSES,
    BUILD_MANIFEST_SCHEMA_V1,
    DELIVERY_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_SCHEMA_V1,
    PROVENANCE_HISTORICAL_PREPARED,
    PROVENANCE_IMMUTABLE,
    DeliveryStatus,
    utc_now_iso,
)

RUN_RESULT_FILENAME = "e2e-run-result.json"
#: Where a re-derived verdict of a *historical* package goes when the package
#: already carries its own ``e2e-run-result.json``. The original is the old
#: runner's classification and is left byte-for-byte as it was.
HISTORICAL_REGRADE_FILENAME = "e2e-run-result.historical-regrade.json"
#: ``/2`` adds ``provenance_model``. ``/1`` results remain readable.
RUN_RESULT_SCHEMA = "e2e/run-result/2"
RUN_RESULT_SCHEMA_V1 = "e2e/run-result/1"
SUPPORTED_RUN_RESULT_SCHEMAS = frozenset({RUN_RESULT_SCHEMA, RUN_RESULT_SCHEMA_V1})

#: The finding every historical prepared-build package carries. It is what
#: keeps such a package from ever being graded ``PASSED`` by this runner.
HISTORICAL_PREPARED_BUILD = "HISTORICAL_PREPARED_BUILD"

#: Blocking codes that say "this run cannot be graded as immutable-source
#: proof" rather than "something that had to hold did not hold". They keep a
#: run from passing without turning it into a failure verdict on their own.
_NON_VERDICT_CODES = frozenset({"DELIVERY_MANIFEST_MISSING", HISTORICAL_PREPARED_BUILD})

#: The only value of a source-immutability verdict that counts as proof.
IMMUTABILITY_UNCHANGED = "UNCHANGED"
#: Phases the executor measures each snapshot in, in order.
IMMUTABILITY_PHASES = ("before_build", "after_build", "after_execution")
#: Lock roles whose snapshots must have been measured.
IMMUTABILITY_ROLES = ("gonka", "contracts")


class RunStatus:
    """The only four honest answers about a full E2E run."""

    #: Everything that had to happen happened, and every applicable check passed.
    PASSED = "PASSED"
    #: Something that had to hold did not hold. This is a verdict.
    FAILED = "FAILED"
    #: The run did not get far enough to have a verdict (crash, lost documents).
    INCOMPLETE = "INCOMPLETE"
    #: The operator stopped it. Not a verdict about the sources either.
    CANCELLED = "CANCELLED"


#: Exit codes. ``CANCELLED`` keeps the POSIX 128+signal value recorded by the
#: cancellation token when there is one; the fallback matches SIGTERM.
_EXIT_CODES = {
    RunStatus.PASSED: 0,
    RunStatus.FAILED: 1,
    RunStatus.INCOMPLETE: 1,
    RunStatus.CANCELLED: 143,
}

BLOCKING = "BLOCKING"
NOTE = "NOTE"


@dataclass
class Finding:
    """One reason the verdict is what it is."""

    code: str
    message: str
    severity: str = BLOCKING
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Finding":
        return cls(
            code=str(data.get("code", "")),
            message=str(data.get("message", "")),
            severity=str(data.get("severity", BLOCKING)),
            details=dict(data.get("details", {}) or {}),
        )


@dataclass
class RunOutcome:
    """The graded result of one full E2E run."""

    schema_version: str = RUN_RESULT_SCHEMA
    status: str = RunStatus.INCOMPLETE
    run_id: Optional[str] = None
    plan_id: Optional[str] = None
    lock_sha256: Optional[str] = None
    suite_id: Optional[str] = None
    suite_status: Optional[str] = None
    build_status: Optional[str] = None
    evaluated_at_utc: str = ""
    #: Which documents were found, and the hash of each, so a later reader can
    #: prove it graded the same bytes.
    documents: Dict[str, Any] = field(default_factory=dict)
    #: Per-task evidence requirements and what was found for each.
    task_evidence: List[Dict[str, Any]] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    #: Free-form provenance detail carried through for the reader.
    provenance: Dict[str, Any] = field(default_factory=dict)
    #: Set when the run was stopped by a signal, so the exit code can match it.
    cancellation_exit_code: Optional[int] = None
    #: ``immutable-source`` or ``historical-prepared-build`` (``None`` when not
    #: a single document of the package could be read). Decided from the
    #: format versions of the package's own documents, never from a flag.
    provenance_model: Optional[str] = None
    #: Where :meth:`write` actually put the verdict. Not serialised.
    written_path: Optional[Path] = field(default=None, repr=False, compare=False)

    # -- derived ---------------------------------------------------------
    @property
    def exit_code(self) -> int:
        if self.status == RunStatus.CANCELLED and self.cancellation_exit_code:
            return int(self.cancellation_exit_code)
        return _EXIT_CODES.get(self.status, 1)

    @property
    def blocking(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == BLOCKING]

    def add(self, finding: Finding) -> "RunOutcome":
        for existing in self.findings:
            if (
                existing.code == finding.code
                and existing.message == finding.message
                and existing.severity == finding.severity
                and existing.details == finding.details
            ):
                return self
        self.findings.append(finding)
        return self

    def recompute_status(self) -> str:
        """Re-grade after a finding was added by the caller.

        The executor learns about an exception only after the package has been
        loaded. It must fold that into the same grading rule rather than
        assigning a status of its own, or there would be two graders again.
        """
        self.status = _status_from_findings(self.findings)
        return self.status

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "exit_code": self.exit_code,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "lock_sha256": self.lock_sha256,
            "suite_id": self.suite_id,
            "suite_status": self.suite_status,
            "build_status": self.build_status,
            "evaluated_at_utc": self.evaluated_at_utc or utc_now_iso(),
            "documents": dict(self.documents),
            "task_evidence": list(self.task_evidence),
            "findings": [f.to_dict() for f in self.findings],
            "provenance": dict(self.provenance),
            "cancellation_exit_code": self.cancellation_exit_code,
            "provenance_model": self.provenance_model,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunOutcome":
        schema = str(data.get("schema_version", ""))
        if schema not in SUPPORTED_RUN_RESULT_SCHEMAS:
            raise ValueError(f"Unsupported run result schema: {schema!r}")
        return cls(
            schema_version=schema,
            status=str(data.get("status", RunStatus.INCOMPLETE)),
            run_id=data.get("run_id"),
            plan_id=data.get("plan_id"),
            lock_sha256=data.get("lock_sha256"),
            suite_id=data.get("suite_id"),
            suite_status=data.get("suite_status"),
            build_status=data.get("build_status"),
            evaluated_at_utc=str(data.get("evaluated_at_utc", "")),
            documents=dict(data.get("documents", {}) or {}),
            task_evidence=[dict(x) for x in data.get("task_evidence", []) or []],
            findings=[Finding.from_dict(x) for x in data.get("findings", []) or []],
            provenance=dict(data.get("provenance", {}) or {}),
            cancellation_exit_code=data.get("cancellation_exit_code"),
            # A /1 result predates the classification: it stays unclassified
            # rather than being guessed into one.
            provenance_model=data.get("provenance_model"),
        )

    def write(self, target: Path) -> Path:
        """Write the verdict; never over an old runner's historical verdict.

        For a historical package, an existing ``e2e-run-result.json`` is the
        original classification made by the runner that produced it. Replacing
        it with this runner's re-derivation would silently reclassify old
        evidence, so the re-derived verdict goes to
        :data:`HISTORICAL_REGRADE_FILENAME` beside it instead.
        """
        path = Path(target)
        preserve_original = (
            self.provenance_model == PROVENANCE_HISTORICAL_PREPARED
            and path.name == RUN_RESULT_FILENAME
        )
        if (
            preserve_original
            and (path.exists() or path.is_symlink())
        ):
            path = path.with_name(HISTORICAL_REGRADE_FILENAME)
        payload = (json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
        atomic_write_bytes(
            path, payload, what="Run verdict", mode=0o644,
            replace=not (preserve_original and path.name == RUN_RESULT_FILENAME),
        )
        self.written_path = path
        return path

    # -- human output ----------------------------------------------------
    def summary_lines(self) -> List[str]:
        """The same facts as the JSON, in the order an operator needs them."""
        lines = [
            f"E2E run {self.run_id or '<unknown>'}: {self.status} (exit {self.exit_code})",
            f"  plan            {self.plan_id or '<unknown>'}",
            f"  lock_sha256     {self.lock_sha256 or '<unknown>'}",
            f"  provenance      {self.provenance_model or '<unknown>'}",
            f"  build manifest  {self.build_status or '<missing>'}",
            f"  suite           {self.suite_id or '<none>'}: {self.suite_status or '<missing>'}",
        ]
        for evidence in self.task_evidence:
            lines.append(
                "  task {task_id:<24} {proof_level:<14} evidence {status}".format(
                    task_id=str(evidence.get("task_id")),
                    proof_level=str(evidence.get("proof_level")),
                    status=str(evidence.get("status")),
                )
            )
        for finding in self.findings:
            lines.append(f"  [{finding.severity}] {finding.code}: {finding.message}")
        if self.status != RunStatus.PASSED:
            lines.append(
                "  A suite result alone is not an E2E verdict; the reasons above are part of it."
            )
        return lines


def _status_from_findings(findings: Sequence[Finding]) -> str:
    """Worst outcome wins, with cancellation and incompleteness kept distinct."""
    blocking = [f for f in findings if f.severity == BLOCKING]
    if not blocking:
        return RunStatus.PASSED
    codes = {f.code for f in blocking}
    if "RUN_CANCELLED" in codes:
        return RunStatus.CANCELLED
    # An incomplete run is only incomplete if nothing actually failed: a real
    # failure is a verdict and must not be softened into "did not finish".
    failure_codes = {
        code
        for code in codes
        if not (code.startswith("INCOMPLETE_") or code in _NON_VERDICT_CODES)
    }
    if not failure_codes:
        return RunStatus.INCOMPLETE
    return RunStatus.FAILED


def _document_models(lock: Any, build: Any, execution: Any) -> Dict[str, str]:
    """The provenance model each *present* document was written for."""
    models: Dict[str, str] = {}
    if lock is not None:
        models["run.lock.json"] = str(
            getattr(lock, "provenance_model", None) or PROVENANCE_HISTORICAL_PREPARED
        )
    if build is not None:
        models["build-manifest.json"] = (
            PROVENANCE_HISTORICAL_PREPARED
            if build.schema_version == BUILD_MANIFEST_SCHEMA_V1
            else PROVENANCE_IMMUTABLE
        )
    if execution is not None:
        models["execution-manifest.json"] = (
            PROVENANCE_HISTORICAL_PREPARED
            if execution.schema_version == EXECUTION_MANIFEST_SCHEMA_V1
            else PROVENANCE_IMMUTABLE
        )
    return models


def classify_provenance_model(lock: Any, build: Any, execution: Any) -> Optional[str]:
    """Which provenance claim the documents of a package can make at all.

    Every document that is present votes; one historical document makes the
    whole package historical. A package cannot be half immutable-source: a
    ``/2`` lock paired with a ``/1`` build manifest is a recombination, and
    the weaker claim is the only one it can carry. ``None`` means nothing
    readable was found, which grading already reports as incomplete.
    """
    models = _document_models(lock, build, execution)
    if not models:
        return None
    if all(model == PROVENANCE_IMMUTABLE for model in models.values()):
        return PROVENANCE_IMMUTABLE
    return PROVENANCE_HISTORICAL_PREPARED


def source_immutability_findings(lock: Any, build: Any, execution: Any) -> List[Finding]:
    """Replay the executor's snapshot measurements from the stored fingerprints.

    The recorded verdicts are not taken on trust: every phase fingerprint is
    re-compared with the pre-build one and re-checked against the locked
    commit and tree with the same functions the executor used
    (:mod:`ops.a8.source_snapshot`). A phase that was never measured is
    ``INCOMPLETE_*`` -- absence is never agreement -- and anything that shows
    a change, or a recorded verdict other than ``UNCHANGED``, is a failure.
    """
    from .. import source_snapshot

    findings: List[Finding] = []
    if build is None:
        return findings
    record = build.source_immutability if isinstance(build.source_immutability, Mapping) else {}
    roles = record.get("roles") if isinstance(record.get("roles"), Mapping) else {}
    if not record:
        findings.append(
            Finding(
                "INCOMPLETE_SOURCE_IMMUTABILITY_MISSING",
                "The build manifest records no source-snapshot measurements, so it cannot "
                "show that the sources under test were the selected, unmodified commits.",
                details={"document": "build-manifest.json", "field": "source_immutability"},
            )
        )
    else:
        for role in IMMUTABILITY_ROLES:
            entry = roles.get(role)
            if not isinstance(entry, Mapping):
                findings.append(
                    Finding(
                        "INCOMPLETE_SOURCE_IMMUTABILITY_MISSING",
                        f"The build manifest has no snapshot measurements for {role}.",
                        details={"role": role},
                    )
                )
                continue
            findings.extend(_replay_role(role, entry, lock, source_snapshot))
        if record.get("pristine_before_build") is not True:
            findings.append(
                Finding(
                    "INCOMPLETE_SOURCE_IMMUTABILITY_MISSING",
                    "The build manifest does not record that the snapshots were pristine "
                    "before the build.",
                    details={"field": "source_immutability.pristine_before_build"},
                )
            )
        for key in ("after_build_verdict", "after_execution_verdict", "verdict"):
            findings.extend(_verdict_finding("build-manifest.json", key, record.get(key)))

    if execution is not None:
        exec_record = (
            execution.source_immutability
            if isinstance(execution.source_immutability, Mapping)
            else {}
        )
        findings.extend(
            _verdict_finding("execution-manifest.json", "verdict", exec_record.get("verdict"))
        )
        after = exec_record.get("after_execution")
        if not isinstance(after, Mapping):
            findings.append(Finding(
                "INCOMPLETE_SOURCE_IMMUTABILITY_MISSING",
                "The execution manifest has no post-execution source fingerprints.",
                details={"document": "execution-manifest.json", "field": "source_immutability.after_execution"},
            ))
        else:
            for role in IMMUTABILITY_ROLES:
                if not isinstance(after.get(role), Mapping):
                    findings.append(Finding(
                        "INCOMPLETE_SOURCE_IMMUTABILITY_MISSING",
                        f"The execution manifest has no post-execution fingerprint for {role}.",
                        details={"document": "execution-manifest.json", "role": role},
                    ))
                    continue
                recorded = (roles.get(role) or {}).get("after_execution") if isinstance(
                    roles.get(role), Mapping) else None
                if after[role] != recorded:
                    findings.append(Finding(
                        "SOURCE_IMMUTABILITY_RECORD_DISAGREEMENT",
                        "The execution manifest and the build manifest record different "
                        f"post-execution fingerprints for {role}.",
                        details={"role": role},
                    ))
            for role in set(after) - set(IMMUTABILITY_ROLES):
                findings.append(Finding(
                    "SOURCE_IMMUTABILITY_RECORD_DISAGREEMENT",
                    "The execution manifest records an unexpected source fingerprint role.",
                    details={"role": str(role)},
                ))
    return findings


def _verdict_finding(document: str, key: str, value: Any) -> List[Finding]:
    if value is None:
        return [
            Finding(
                "INCOMPLETE_SOURCE_IMMUTABILITY_MISSING",
                f"{document} records no source_immutability.{key}; the run did not reach "
                "that measurement, so it proves nothing about the sources.",
                details={"document": document, "field": f"source_immutability.{key}"},
            )
        ]
    if value == "INCOMPLETE":
        return [
            Finding(
                "SOURCE_IMMUTABILITY_INCOMPLETE",
                f"{document} records source_immutability.{key} = {value!r}: source "
                "immutability measurement is incomplete.",
                details={"document": document, "field": f"source_immutability.{key}",
                         "actual": value, "expected": IMMUTABILITY_UNCHANGED},
            )
        ]
    if value != IMMUTABILITY_UNCHANGED:
        return [
            Finding(
                "SOURCE_IMMUTABILITY_VIOLATED",
                f"{document} records source_immutability.{key} = {value!r}: a source "
                "snapshot was not the selected, unmodified commit.",
                details={"document": document, "field": f"source_immutability.{key}",
                         "actual": value, "expected": IMMUTABILITY_UNCHANGED},
            )
        ]
    return []


def _replay_role(role: str, entry: Mapping[str, Any], lock: Any, snapshot: Any) -> List[Finding]:
    """Re-derive one role's verdict from its recorded fingerprints."""
    findings: List[Finding] = []
    locked: Dict[str, Any] = {}
    if lock is not None:
        try:
            locked = dict(lock.source_record(role))
        except Exception:  # noqa: BLE001 - an unreadable record is reported below
            locked = {}
    locked_sha = str(locked.get("commit_sha") or "").lower()
    locked_tree = str(locked.get("tree_sha") or "").lower()
    expected_sha = str(entry.get("expected_sha") or "").lower()
    expected_tree = str(entry.get("expected_tree") or "").lower()
    # The measurement must be about the locked commit and tree: a record of a
    # pristine snapshot of some other commit proves nothing about this plan.
    if lock is not None and (not locked_sha or expected_sha != locked_sha):
        findings.append(
            Finding(
                "SOURCE_IMMUTABILITY_FOREIGN_COMMIT",
                f"The {role} measurements were taken for a commit other than the locked one.",
                details={"role": role, "locked": locked_sha, "measured": expected_sha},
            )
        )
    if lock is not None and (not locked_tree or expected_tree != locked_tree):
        findings.append(
            Finding(
                "SOURCE_IMMUTABILITY_FOREIGN_COMMIT",
                f"The {role} measurements name a tree other than the locked tree SHA.",
                details={"role": role, "locked_tree": locked_tree, "measured_tree": expected_tree},
            )
        )
    before = entry.get("before_build")
    if not isinstance(before, Mapping):
        findings.append(
            Finding(
                "INCOMPLETE_SOURCE_IMMUTABILITY_MISSING",
                f"The {role} snapshot was never measured before the build.",
                details={"role": role, "phase": "before_build"},
            )
        )
        return findings
    sha_for_check = expected_sha or locked_sha
    problems: List[Dict[str, Any]] = []
    try:
        problems.extend(
            dict(p, phase="before_build")
            for p in snapshot.pristine_violations(before, expected_sha=sha_for_check)
        )
        tree_for_check = expected_tree or locked_tree
        if tree_for_check and str(before.get("tree", "")).lower() != tree_for_check:
            problems.append({"code": "SOURCE_TREE_MISMATCH", "phase": "before_build",
                             "expected": tree_for_check, "actual": before.get("tree")})
        for phase in IMMUTABILITY_PHASES[1:]:
            after = entry.get(phase)
            if not isinstance(after, Mapping):
                findings.append(
                    Finding(
                        "INCOMPLETE_SOURCE_IMMUTABILITY_MISSING",
                        f"The {role} snapshot was never measured {phase.replace('_', ' ')}.",
                        details={"role": role, "phase": phase},
                    )
                )
                continue
            problems.extend(dict(d, phase=phase) for d in snapshot.differences(before, after))
            problems.extend(
                dict(p, phase=phase)
                for p in snapshot.pristine_violations(after, expected_sha=sha_for_check)
            )
    except snapshot.SourceSnapshotError as exc:
        findings.append(
            Finding(
                "SOURCE_IMMUTABILITY_RECORD_MALFORMED",
                f"The recorded {role} snapshot fingerprints cannot be replayed: {exc}",
                details={"role": role},
            )
        )
        return findings
    if problems:
        findings.append(
            Finding(
                "SOURCE_IMMUTABILITY_VIOLATED",
                f"Replaying the recorded {role} fingerprints shows the snapshot was not the "
                "selected commit or changed during the run.",
                details={"role": role, "problems": problems[:20]},
            )
        )
    return findings


def evaluate_run(target: Any) -> RunOutcome:
    """Grade a verified run package. The single verdict used everywhere.

    ``target`` is either a :class:`ops.a8.e2e.runpackage.PackageVerification`
    or a :class:`ops.a8.e2e.runpackage.LoadedRunPackage`. It is taken as
    ``Any`` only to keep the import one-way: the loader imports this module,
    not the other way round.
    """
    if hasattr(target, "package") and hasattr(target, "suite_integrity_errors"):
        verification = target
        package = target.package
    else:
        package = target
        verification = getattr(target, "_verification", None)
        if verification is None and callable(getattr(target, "verify", None)):
            maybe_ver = target.verify()
            if hasattr(maybe_ver, "package"):
                verification = maybe_ver

    outcome = RunOutcome(evaluated_at_utc=utc_now_iso())
    outcome.documents = dict(package.document_report())
    outcome.findings.extend(package.load_findings())

    lock = package.lock
    build = package.build_manifest
    execution = package.execution_manifest

    if lock is not None:
        outcome.lock_sha256 = lock.lock_sha256
        outcome.plan_id = lock.plan_id
    if execution is not None:
        outcome.run_id = execution.run_id
        outcome.suite_id = execution.suite_id
        if lock is None:
            outcome.plan_id = execution.plan_id
            outcome.lock_sha256 = execution.lock_sha256
    elif build is not None:
        outcome.run_id = build.run_id
    if build is not None:
        outcome.build_status = build.status

    # -- 0. which claim this package can make at all --------------------
    outcome.provenance_model = classify_provenance_model(lock, build, execution)
    document_models = _document_models(lock, build, execution)
    if len(set(document_models.values())) > 1:
        outcome.add(
            Finding(
                "PROVENANCE_FORMAT_MIXED",
                "The documents of this package were written for different provenance "
                "models, so they cannot belong to one run.",
                details={"documents": document_models},
            )
        )
    if outcome.provenance_model == PROVENANCE_HISTORICAL_PREPARED:
        outcome.add(
            Finding(
                HISTORICAL_PREPARED_BUILD,
                "This package was produced by the retired overlay / prepared-commit runner, "
                "which modified the Gonka source tree before building it. It is kept and "
                "graded as historical evidence only and can never prove a run on unmodified "
                "sources. Create a new plan to obtain an immutable-source verdict.",
                details={
                    "lock_schema": getattr(lock, "schema_version", None),
                    "build_manifest_schema": getattr(build, "schema_version", None),
                    "execution_manifest_schema": getattr(execution, "schema_version", None),
                },
            )
        )
    elif outcome.provenance_model == PROVENANCE_IMMUTABLE:
        for finding in source_immutability_findings(lock, build, execution):
            outcome.add(finding)

    # -- 1. the documents themselves ------------------------------------
    if lock is None:
        outcome.add(
            Finding(
                "INCOMPLETE_LOCK_MISSING",
                "The run does not contain the lock it was executed from, so there is "
                "nothing to grade it against.",
            )
        )
    if build is None:
        outcome.add(
            Finding(
                "INCOMPLETE_BUILD_MANIFEST_MISSING",
                "The run has no build manifest. What it built cannot be established, "
                "so it stays an incomplete E2E run rather than becoming a legacy suite.",
            )
        )
    if execution is None:
        outcome.add(
            Finding(
                "INCOMPLETE_EXECUTION_MANIFEST_MISSING",
                "The run has no execution manifest, so its identity and its link to the "
                "plan cannot be established.",
            )
        )

    # -- 2. the links between them --------------------------------------
    if lock is not None and build is not None and build.lock_sha256 != lock.lock_sha256:
        outcome.add(
            Finding(
                "BUILD_MANIFEST_FOREIGN",
                "The build manifest belongs to a different lock than the one preserved "
                "in this run.",
                details={"manifest": build.lock_sha256, "lock": lock.lock_sha256},
            )
        )
    if lock is not None and build is not None and build.plan_id != lock.plan_id:
        outcome.add(
            Finding(
                "BUILD_PLAN_FOREIGN",
                "The build manifest names a different plan than the preserved lock.",
                details={"build": build.plan_id, "lock": lock.plan_id},
            )
        )
    if lock is not None and execution is not None and execution.lock_sha256 != lock.lock_sha256:
        outcome.add(
            Finding(
                "EXECUTION_MANIFEST_FOREIGN",
                "The execution manifest belongs to a different lock than the one "
                "preserved in this run.",
                details={"manifest": execution.lock_sha256, "lock": lock.lock_sha256},
            )
        )
    if lock is not None and execution is not None and execution.plan_id != lock.plan_id:
        outcome.add(
            Finding(
                "EXECUTION_PLAN_FOREIGN",
                "The execution manifest names a different plan than the preserved lock.",
                details={"execution": execution.plan_id, "lock": lock.plan_id},
            )
        )
    if build is not None and execution is not None:
        if build.run_id != execution.run_id:
            outcome.add(
                Finding(
                    "RUN_ID_DISAGREEMENT",
                    "The build manifest and the execution manifest describe different runs.",
                    details={"build": build.run_id, "execution": execution.run_id},
                )
            )
        recorded = execution.build_manifest_sha256
        actual = package.build_manifest_sha256
        if recorded and actual and recorded != actual:
            outcome.add(
                Finding(
                    "BUILD_MANIFEST_MODIFIED",
                    "The build manifest does not hash to the value the execution manifest "
                    "recorded for it, so it was changed after the run.",
                    details={"recorded": recorded, "actual": actual},
                )
            )
        elif not recorded:
            outcome.add(
                Finding(
                    "INCOMPLETE_BUILD_MANIFEST_UNSEALED",
                    "The execution manifest never recorded the hash of the build manifest, "
                    "so the run did not reach its own end.",
                )
            )
    if execution is not None and package.suite_dir is not None:
        expected_index = (
            Path(package.suite_dir).relative_to(Path(package.root)) / "artifact-index.json"
        ).as_posix()
        if (execution.artifact_index_relpath
                and execution.artifact_index_relpath != expected_index):
            outcome.add(Finding(
                "ARTIFACT_INDEX_POINTER_MISMATCH",
                "The execution manifest names a different artifact index than the one verified.",
                details={"recorded": execution.artifact_index_relpath, "verified": expected_index},
            ))

    # -- 3. cancellation and build status -------------------------------
    if build is not None and build.cancellation.get("requested"):
        outcome.cancellation_exit_code = build.cancellation.get("exit_code")
        outcome.add(
            Finding(
                "RUN_CANCELLED",
                "The operator stopped this run. A cancelled run is not a verdict about "
                "the sources.",
                details=dict(build.cancellation),
            )
        )
    if build is not None and build.status != "COMPLETE":
        outcome.add(
            Finding(
                "BUILD_NOT_COMPLETE",
                f"The build manifest is {build.status}, so the artefacts under test are "
                "not the ones the plan selected.",
                details={"failures": list(build.failures)},
            )
        )

    # -- 3b. delivery / export -------------------------------------------
    delivery = getattr(package, "delivery_manifest", None)
    delivery_rel = (
        getattr(execution, "delivery_manifest_relpath", None)
        if execution is not None
        else None
    )
    if execution is not None:
        if delivery_rel and delivery_rel != DELIVERY_MANIFEST_FILENAME:
            outcome.add(Finding(
                "DELIVERY_POINTER_MISMATCH",
                "The execution manifest names a different delivery manifest than the one graded.",
                details={"recorded": delivery_rel, "graded": DELIVERY_MANIFEST_FILENAME},
            ))

    if delivery is not None:
        if execution is not None and execution.run_id and delivery.run_id != execution.run_id:
            outcome.add(
                Finding(
                    "DELIVERY_MANIFEST_IDENTITY_MISMATCH",
                    "The delivery manifest describes a different run than the execution manifest.",
                    details={"delivery_run_id": delivery.run_id, "execution_run_id": execution.run_id},
                )
            )
        if delivery.status not in ALL_DELIVERY_STATUSES:
            outcome.add(
                Finding(
                    "DELIVERY_MANIFEST_INVALID",
                    f"The delivery manifest has an unsupported status: {delivery.status}",
                    details={"status": delivery.status},
                )
            )
        elif delivery.status == DeliveryStatus.FAILED:
            last_err = None
            if delivery.attempts:
                last_err = delivery.attempts[-1].error
            outcome.add(
                Finding(
                    "EXPORT_FAILED",
                    f"The run package export failed: {last_err or 'unknown error'}",
                    details={"status": delivery.status, "attempts": [a.to_dict() for a in delivery.attempts]},
                )
            )
        elif delivery.status in (DeliveryStatus.IN_PROGRESS, DeliveryStatus.PENDING):
            outcome.add(
                Finding(
                    "EXPORT_NOT_COMPLETED",
                    f"The run package export was interrupted or not completed (status={delivery.status}).",
                    details={"status": delivery.status, "attempts": [a.to_dict() for a in delivery.attempts]},
                )
            )
        elif delivery.status == DeliveryStatus.COMPLETED:
            if not delivery.attempts or delivery.attempts[-1].status != DeliveryStatus.COMPLETED:
                outcome.add(
                    Finding(
                        "DELIVERY_MANIFEST_INCONSISTENT",
                        "The delivery manifest is marked COMPLETED but lacks a completed delivery attempt.",
                        details={"attempts": [a.to_dict() for a in delivery.attempts]},
                    )
                )
    else:
        if not any(
            f.details.get("document") == DELIVERY_MANIFEST_FILENAME
            or f.code in ("DELIVERY_MANIFEST_SYMLINK", "DELIVERY_MANIFEST_INVALID")
            for f in outcome.findings
        ):
            outcome.add(
                Finding(
                    "DELIVERY_MANIFEST_MISSING",
                    "The execution manifest requires a delivery manifest, but none was found in the package.",
                    details={"delivery_manifest_relpath": delivery_rel or DELIVERY_MANIFEST_FILENAME},
                )
            )

    # -- 4. the suite ----------------------------------------------------
    suite_status = (
        verification.suite_status
        if verification is not None
        else package.suite_status
    )
    outcome.suite_status = suite_status
    if package.suite_dir is None:
        outcome.add(
            Finding(
                "INCOMPLETE_SUITE_MISSING",
                "No exported suite was found in this run, so no scenario result exists.",
            )
        )
    elif package.suite_result is None or suite_status is None:
        outcome.add(
            Finding(
                "INCOMPLETE_SUITE_RESULT_MISSING",
                "The suite directory has no readable recorded suite result; reconstructed task status cannot establish a passing run.",
            )
        )
    elif verification is not None and (
        verification.suite_verification is None
        or verification.suite_verification.stored_result is None
    ):
        outcome.add(
            Finding(
                "SUITE_RESULT_INVALID",
                "The recorded suite result does not match the plan or supported schema; reconstructed tasks cannot establish a passing run.",
            )
        )
    elif suite_status != "PASSED":
        outcome.add(
            Finding(
                "SUITE_NOT_PASSED",
                f"The suite result is {suite_status}.",
            )
        )

    # -- 5. per-task evidence and post-run provenance --------------------
    outcome.task_evidence = (
        list(verification.task_evidence)
        if verification is not None
        else list(package.task_evidence())
    )
    for evidence in outcome.task_evidence:
        if evidence.get("status") == "MISSING":
            outcome.add(
                Finding(
                    "TASK_EVIDENCE_MISSING",
                    f"Task {evidence.get('task_id')} owes evidence it did not leave.",
                    details=dict(evidence),
                )
            )
        for problem in evidence.get("problems", []) or []:
            outcome.add(
                Finding(
                    str(problem.get("code") or "DOCUMENT_UNREADABLE"),
                    str(problem.get("message") or ""),
                    details=dict(problem),
                )
            )
    if verification is not None:
        for finding in verification.findings:
            outcome.add(finding)
        outcome.provenance = dict(verification.provenance)
        outcome._suite_dir = getattr(package, "suite_dir", None)
        outcome._suite_result = verification.recomputed_suite_result
        outcome._suite_integrity_errors = list(verification.suite_integrity_errors)
    else:
        for finding in package.provenance_findings():
            outcome.add(finding)
        for finding in package.load_findings():
            outcome.add(finding)
        outcome.provenance = dict(package.provenance_report())
        outcome._suite_dir = getattr(package, "suite_dir", None)
        outcome._suite_result = getattr(package, "recomputed_suite_result", None)
        outcome._suite_integrity_errors = list(
            getattr(package, "suite_integrity_errors", ()) or ()
        )

    outcome.status = _status_from_findings(outcome.findings)
    return outcome


__all__ = [
    "BLOCKING",
    "Finding",
    "HISTORICAL_PREPARED_BUILD",
    "HISTORICAL_REGRADE_FILENAME",
    "NOTE",
    "RUN_RESULT_FILENAME",
    "RUN_RESULT_SCHEMA",
    "RUN_RESULT_SCHEMA_V1",
    "RunOutcome",
    "RunStatus",
    "SUPPORTED_RUN_RESULT_SCHEMAS",
    "classify_provenance_model",
    "evaluate_run",
    "source_immutability_findings",
]
