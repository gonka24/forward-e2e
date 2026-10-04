"""Which evidence each *selected* task has to produce, and who must produce it.

Why this module exists
----------------------
A run proves different things depending on what was selected. A ``NATIVE`` task
starts the real chain, deploys the production contracts and writes a
``live-context.json`` describing what it deployed. A ``GO_BOUNDARY``,
``WASM_ABI`` or ``CONTRACT_TEST`` task never starts a chain at all: it compiles
and runs a bounded probe and leaves its own artefacts behind.

Requiring deployment evidence from a boundary task is therefore not a strict
check, it is a wrong one -- it fails a run that did exactly what it was asked to
do. Requiring it from a native task is mandatory, and the presence of *another*
task's evidence may never stand in for it.

The rule is keyed on the task's **proof level**, which the catalog already
defines and which the planner already records in the lock
(``lock.selection["proof_levels"]``). Nothing here re-decides the selection: it
only reads what was decided, so the online post-run checks and the offline
reader cannot drift apart.

This module is pure: it reads documents and filesystem entries, never runs a
build, a container or a network call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..suite.models import ProofLevel, make_task_run_id
from .deployment import SOURCE_IMMUTABILITY_FILENAME
from .errors import E2EError
from .file_safety import read_checked_file_bytes

#: Proof levels whose tasks start the real network. Only these can be expected
#: to leave a ``live-context.json`` behind, because only these have a live
#: context at all.
LIVE_CONTEXT_PROOF_LEVELS = frozenset({ProofLevel.NATIVE.value})

#: Proof levels whose tasks deploy the production contracts. Deployment hashes
#: are only meaningful for these; a boundary probe deploys nothing.
DEPLOYMENT_PROOF_LEVELS = frozenset({ProofLevel.NATIVE.value})

#: Where the orchestrator puts one task's evidence inside a suite directory.
SUITE_RUNS_DIRNAME = "runs"

#: The file a native task's harness writes with the observed runtime and the
#: hashes of what it deployed.
LIVE_CONTEXT_FILENAME = "live-context.json"

#: Only this retired model predates per-task source-immutability evidence.
#: Kept literal to leave this pure module independent of the lock format.
HISTORICAL_SOURCE_MODEL = "historical-prepared-build"


class EvidencePolicyError(ValueError):
    """The selection cannot be read, so no honest requirement can be derived."""


@dataclass(frozen=True)
class SelectedTask:
    """One task the lock says this run is about."""

    task_id: str
    ordinal: int
    proof_level: str

    def run_id(self, suite_id: str) -> str:
        """The per-task directory name the orchestrator will use."""
        return make_task_run_id(suite_id, self.ordinal, self.task_id)


@dataclass(frozen=True)
class EvidenceRequirement:
    """What one selected task must leave behind for the run to be provable."""

    task_id: str
    ordinal: int
    proof_level: str
    run_id: str
    requires_live_context: bool
    requires_deployment_evidence: bool
    #: Files the catalog declares for this task, relative to its run directory.
    #: Recorded for the report; their integrity is audited by the suite reporter,
    #: which owns the artifact index.
    expected_artifacts: Tuple[str, ...] = ()
    #: A live task of an immutable-source run must also leave the harness'
    #: before/after measurement of both source snapshots
    #: (``source-immutability.json``) next to its live context. Without it the
    #: task cannot show the tested trees were the selected, unmodified commits.
    requires_source_immutability: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "ordinal": self.ordinal,
            "proof_level": self.proof_level,
            "run_id": self.run_id,
            "requires_live_context": self.requires_live_context,
            "requires_deployment_evidence": self.requires_deployment_evidence,
            "requires_source_immutability": self.requires_source_immutability,
            "expected_artifacts": list(self.expected_artifacts),
        }

    def evidence_dir(self, suite_dir: Path) -> Path:
        return Path(suite_dir) / SUITE_RUNS_DIRNAME / self.run_id

    def live_context_candidates(self, suite_dir: Path) -> Tuple[Path, ...]:
        """Every place the producer is known to put this task's live context.

        The harness is given ``--evidence-dir <snapshot>/evidence`` and appends
        the run id itself (``adapters.py``, ``runtime.RuntimeSnapshot``), and
        the collector copies each file at its path *relative to the snapshot
        run directory* (``collector.py``). The real exported location is
        therefore ``runs/<run-id>/evidence/<run-id>/live-context.json`` -- one
        level deeper than the run directory, and with the run id repeated.

        The other two entries are the layouts the suite reporter has always
        tolerated as well (``reporter.py``): a harness that wrote straight into
        the evidence directory, and a flattened export. They are listed
        explicitly, in order, instead of being found by a recursive glob: a
        task that produced nothing must never be rescued by a *different*
        task's file elsewhere in the suite.
        """
        base = self.evidence_dir(suite_dir)
        return (
            base / "evidence" / self.run_id / LIVE_CONTEXT_FILENAME,
            base / "evidence" / LIVE_CONTEXT_FILENAME,
            base / LIVE_CONTEXT_FILENAME,
        )

    def live_context_path(self, suite_dir: Path) -> Path:
        """The first candidate that exists, or the canonical one if none does.

        Returning the canonical path when nothing is there keeps the error
        message pointing at where the file was supposed to be written.
        """
        candidates = self.live_context_candidates(suite_dir)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0]

    def source_immutability_path(self, suite_dir: Path) -> Path:
        """Where this task's ``source-immutability.json`` must be.

        The harness writes it into the same evidence directory as the live
        context (``scripts/acceptance_harness.py run-live``), so it is looked for
        next to *this task's* live context and nowhere else: another task's
        measurement can never answer for this one.
        """
        return self.live_context_path(suite_dir).parent / SOURCE_IMMUTABILITY_FILENAME


def _proof_level_of(value: Any) -> str:
    """Normalise a recorded proof level, refusing anything unknown.

    An unknown level must not silently become "no requirements": that is the
    exact shape of a check that disappears when a new task type is added.
    """
    text = str(value or "").strip()
    try:
        return ProofLevel(text).value
    except ValueError as exc:
        raise EvidencePolicyError(
            f"Unknown proof level {text!r} in the selection; this runner cannot decide "
            "what evidence that task owes."
        ) from exc


def selected_tasks_from_lock(lock: Any) -> List[SelectedTask]:
    """The tasks this lock selected, in the order they will be executed.

    The lock is the only immutable statement of what was selected, so it is the
    default source. ``selection.scenarios`` is already ordered by the catalog
    (``planner._selection_section``), and every resolver assigns ordinals
    ``1..n`` in exactly that order, which is what ``make_task_run_id`` uses.
    """
    selection = getattr(lock, "selection", None)
    if not isinstance(selection, Mapping):
        raise EvidencePolicyError("The lock has no selection section")
    scenarios = selection.get("scenarios")
    if not isinstance(scenarios, Sequence) or isinstance(scenarios, (str, bytes)):
        raise EvidencePolicyError("The lock's selection has no scenario list")
    levels = selection.get("proof_levels")
    levels_map = dict(levels) if isinstance(levels, Mapping) else {}
    tasks: List[SelectedTask] = []
    for index, task_id in enumerate(scenarios):
        name = str(task_id)
        if name not in levels_map:
            raise EvidencePolicyError(
                f"The lock selected {name!r} but records no proof level for it, so the "
                "evidence it owes cannot be determined."
            )
        tasks.append(
            SelectedTask(
                task_id=name,
                ordinal=index + 1,
                proof_level=_proof_level_of(levels_map[name]),
            )
        )
    if not tasks:
        raise EvidencePolicyError("The lock selected no tasks at all")
    return tasks


def tasks_from_lock(lock: Any) -> "List[Any]":
    """Construct the ordered list of TaskPlan objects from the lock and catalog.

    The planner already resolved the user's profile or scenario selection into
    canonical task IDs, catalog order, proof levels, and timeout bounds. This
    function reconstructs the TaskPlan objects for execution without re-running
    any selection policy, and refuses if the catalog or plan is incompatible.
    """
    from ..suite.catalog import _CATALOG_ORDER, get_task_by_id
    from ..suite.models import TaskPlan

    selected = selected_tasks_from_lock(lock)
    order_map = {tid: idx for idx, tid in enumerate(_CATALOG_ORDER)}
    limits = getattr(lock, "limits", None)
    ttm = limits.get("task_timeout_minutes") if isinstance(limits, Mapping) else None
    sts = limits.get("stage_timeout_seconds") if isinstance(limits, Mapping) else None
    gtm = limits.get("gradle_timeout_minutes") if isinstance(limits, Mapping) else None

    result: List[TaskPlan] = []
    last_idx = -1
    for s in selected:
        cat_task = get_task_by_id(s.task_id)
        if cat_task is None or cat_task.task_id != s.task_id or s.task_id not in order_map:
            raise EvidencePolicyError(
                f"The lock selected {s.task_id!r}, which is not a canonical task in this runner's catalog."
            )
        idx = order_map[s.task_id]
        if idx <= last_idx:
            raise EvidencePolicyError(
                "The lock's scenario list is not ordered by the catalog or contains duplicates."
            )
        last_idx = idx
        if cat_task.proof_level.value != s.proof_level:
            raise EvidencePolicyError(
                f"The lock records proof level {s.proof_level!r} for {s.task_id!r}, "
                f"but the catalog defines {cat_task.proof_level.value!r}."
            )
        tp = TaskPlan.from_dict(cat_task.to_dict())
        tp.ordinal = s.ordinal
        if isinstance(ttm, Mapping) and s.task_id in ttm:
            tp.timeout_minutes = int(ttm[s.task_id])
        if isinstance(sts, Mapping) and s.task_id in sts:
            tp.stage_timeout_seconds = int(sts[s.task_id])
        if isinstance(gtm, Mapping) and s.task_id in gtm and gtm[s.task_id] is not None:
            tp.gradle_timeout_minutes = int(gtm[s.task_id])
        result.append(tp)
    return result


def selected_tasks_from_suite_plan(payload: Mapping[str, Any]) -> List[SelectedTask]:
    """The tasks the suite actually planned, read from its own ``suite-plan.json``.

    This is the producer's record, including the ordinals it really used, so it
    is preferred over re-deriving them whenever the file exists.
    """
    tasks_raw = payload.get("tasks")
    if not isinstance(tasks_raw, Sequence) or isinstance(tasks_raw, (str, bytes)):
        raise EvidencePolicyError("suite-plan.json has no task list")
    tasks: List[SelectedTask] = []
    for entry in tasks_raw:
        if not isinstance(entry, Mapping):
            raise EvidencePolicyError("suite-plan.json contains a malformed task entry")
        ordinal = entry.get("ordinal")
        # Coercion would silently change the directory we search for evidence;
        # bool is also excluded despite being an int subclass in Python.
        if type(ordinal) is not int or ordinal <= 0:
            raise EvidencePolicyError(
                f"suite-plan.json task {entry.get('task_id')!r} has invalid ordinal "
                f"{ordinal!r}; expected a positive integer"
            )
        tasks.append(
            SelectedTask(
                task_id=str(entry.get("task_id") or ""),
                ordinal=ordinal,
                proof_level=_proof_level_of(entry.get("proof_level")),
            )
        )
    if not tasks:
        raise EvidencePolicyError("suite-plan.json planned no tasks")
    return tasks


def catalog_expected_artifacts(task_id: str) -> Tuple[str, ...]:
    """What the catalog says this task leaves behind, if this runner knows it.

    The catalog is pinned by hash in the lock, so when it is available it is the
    same catalog that planned the run. A task this runner does not know is not
    an error here: the requirement that matters (live context / deployment) comes
    from the recorded proof level, not from this list.
    """
    from ..suite.catalog import get_task_by_id

    task = get_task_by_id(task_id)
    if task is None:
        return ()
    return tuple(str(name) for name in task.expected_artifacts)


def requirements_for(
    tasks: Sequence[SelectedTask],
    *,
    suite_id: str,
    build_produced_production_wasm: bool,
    immutable_source: bool = True,
) -> List[EvidenceRequirement]:
    """Turn selected tasks into the evidence each of them owes.

    ``build_produced_production_wasm`` is the *only* build-side input: a native
    task can only be asked which contracts it deployed if this build actually
    produced production contracts to deploy. It can never turn a boundary task
    into one that owes deployment evidence, which is the bug this replaces.

    ``immutable_source`` is true for every package this runner produces; it is
    false only when a *historical* prepared-build package is re-read, whose
    harness never wrote a ``source-immutability.json`` and which is graded as
    historical evidence anyway. It defaults to the strict value so that a
    caller who forgets it asks for more evidence, never less.
    """
    requirements: List[EvidenceRequirement] = []
    for task in tasks:
        live = task.proof_level in LIVE_CONTEXT_PROOF_LEVELS
        deployment = (
            task.proof_level in DEPLOYMENT_PROOF_LEVELS
            and bool(build_produced_production_wasm)
        )
        requirements.append(
            EvidenceRequirement(
                task_id=task.task_id,
                ordinal=task.ordinal,
                proof_level=task.proof_level,
                run_id=task.run_id(suite_id),
                requires_live_context=live,
                requires_deployment_evidence=deployment,
                expected_artifacts=catalog_expected_artifacts(task.task_id),
                requires_source_immutability=live and bool(immutable_source),
            )
        )
    return requirements


def lock_is_immutable_source(lock: Any) -> bool:
    """Whether ``lock`` belongs to the immutable-source model.

    Only a lock that positively says it is historical relaxes the duty; an
    object without a provenance model (a synthetic stand-in, a future format)
    keeps the strict default.
    """
    model = getattr(lock, "provenance_model", None)
    return str(model) != HISTORICAL_SOURCE_MODEL


def requirements_from_documents(
    *,
    lock: Any,
    suite_id: str,
    build_produced_production_wasm: bool,
    suite_plan: Optional[Mapping[str, Any]] = None,
) -> Tuple[List[EvidenceRequirement], List[Dict[str, Any]]]:
    """Requirements plus any disagreement between the lock and the suite plan.

    Both documents are read on purpose. The lock says what was *selected*; the
    suite plan says what the suite *planned*. If they disagree, that is itself a
    finding -- a suite that quietly ran a different set of tasks must not be
    graded against the set nobody ran.
    """
    from_lock = selected_tasks_from_lock(lock)
    disagreements: List[Dict[str, Any]] = []
    chosen = from_lock
    if suite_plan is not None:
        from_plan = selected_tasks_from_suite_plan(suite_plan)
        lock_ids = [task.task_id for task in from_lock]
        plan_ids = [task.task_id for task in from_plan]
        if lock_ids != plan_ids:
            disagreements.append(
                {
                    "code": "SELECTION_DISAGREEMENT",
                    "message": (
                        "The suite planned a different set of tasks than the lock selected."
                    ),
                    "lock_selection": lock_ids,
                    "suite_plan_selection": plan_ids,
                }
            )
        for planned in from_plan:
            match = next((t for t in from_lock if t.task_id == planned.task_id), None)
            if match is not None and match.proof_level != planned.proof_level:
                disagreements.append(
                    {
                        "code": "PROOF_LEVEL_DISAGREEMENT",
                        "message": "The suite plan and the lock disagree on a proof level.",
                        "task_id": planned.task_id,
                        "lock_proof_level": match.proof_level,
                        "suite_plan_proof_level": planned.proof_level,
                    }
                )
        # The ordinals the suite really used decide the directory names, so the
        # plan wins for *locating* evidence even while the disagreement stands.
        chosen = from_plan
    return (
        requirements_for(
            chosen,
            suite_id=suite_id,
            build_produced_production_wasm=build_produced_production_wasm,
            immutable_source=lock_is_immutable_source(lock),
        ),
        disagreements,
    )


def load_suite_plan(suite_dir: Path, *, raw_bytes: Optional[bytes] = None) -> Optional[Dict[str, Any]]:
    """Read the suite's own plan, or ``None`` if the suite never wrote one."""
    path = Path(suite_dir) / "suite-plan.json"
    if raw_bytes is None and not path.is_file():
        return None
    try:
        data = raw_bytes if raw_bytes is not None else read_checked_file_bytes(path)
        payload = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, E2EError) as exc:
        raise EvidencePolicyError(f"suite-plan.json is unreadable: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise EvidencePolicyError("suite-plan.json is not a JSON object")
    return dict(payload)


#: A task whose evidence is complete for its proof level.
EVIDENCE_SATISFIED = "SATISFIED"
#: A task that owed evidence and did not leave it.
EVIDENCE_MISSING = "MISSING"
#: A task that owes no evidence of this kind at all (boundary probes).
EVIDENCE_NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass
class TaskEvidence:
    """What was actually found for one task, against what it owed."""

    requirement: EvidenceRequirement
    live_context_path: Optional[str] = None
    live_context_present: bool = False
    source_immutability_path: Optional[str] = None
    source_immutability_present: bool = False
    status: str = "PENDING"
    problems: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = self.requirement.to_dict()
        payload.update(
            {
                "live_context_path": self.live_context_path,
                "live_context_present": self.live_context_present,
                "source_immutability_path": self.source_immutability_path,
                "source_immutability_present": self.source_immutability_present,
                "status": self.status,
                "problems": list(self.problems),
            }
        )
        return payload


def _check_source_immutability_present(
    requirement: EvidenceRequirement, root: Path, evidence: TaskEvidence
) -> None:
    """Verify that a task owing immutable-source proof recorded source-immutability.json."""
    imm_path = requirement.source_immutability_path(root)
    if imm_path.is_file() and not imm_path.is_symlink():
        evidence.source_immutability_present = True
        evidence.source_immutability_path = str(imm_path.relative_to(root))
        try:
            content = read_checked_file_bytes(imm_path).decode("utf-8")
            if not content.strip():
                raise ValueError(f"{imm_path.name} is empty")
            data = json.loads(content)
            if not isinstance(data, Mapping):
                raise ValueError(f"{imm_path.name} root must be a JSON object")
        except (E2EError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            evidence.status = EVIDENCE_MISSING
            evidence.problems.append(
                {
                    "code": "DOCUMENT_UNREADABLE",
                    "message": f"{imm_path.name} is present but unreadable: {exc}",
                    "expected_path": str(imm_path.relative_to(root)),
                    "searched_paths": [str(imm_path.relative_to(root))],
                }
            )
        return

    evidence.status = EVIDENCE_MISSING
    evidence.problems.append(
        {
            "code": "SOURCE_IMMUTABILITY_MISSING",
            "message": (
                "An immutable-source live task must record before/after source "
                "snapshot measurements in source-immutability.json next to its "
                "live-context.json."
            ),
            "expected_path": str(imm_path.relative_to(root)),
            "searched_paths": [str(imm_path.relative_to(root))],
        }
    )


def locate_task_evidence(
    requirements: Sequence[EvidenceRequirement], *, suite_dir: Path
) -> List[TaskEvidence]:
    """Find each task's own evidence in its own directory.

    Deliberately *not* a recursive glob: a native task that produced nothing
    must not be rescued by another task's ``live-context.json`` sitting
    elsewhere in the same suite.
    """
    found: List[TaskEvidence] = []
    root = Path(suite_dir)
    for requirement in requirements:
        evidence = TaskEvidence(requirement=requirement)
        path = requirement.live_context_path(root)
        evidence.live_context_present = path.is_file()
        if evidence.live_context_present:
            evidence.live_context_path = str(path.relative_to(root))
        if not requirement.requires_live_context:
            evidence.status = (
                EVIDENCE_SATISFIED
                if evidence.live_context_present
                else EVIDENCE_NOT_APPLICABLE
            )
        elif evidence.live_context_present:
            is_valid = True
            err_code = "DOCUMENT_UNREADABLE"
            err_msg = ""
            try:
                content = read_checked_file_bytes(path).decode("utf-8")
                if not content.strip():
                    is_valid = False
                    err_msg = f"{path.name} is empty"
                else:
                    data = json.loads(content)
                    if not isinstance(data, Mapping):
                        is_valid = False
                        err_msg = f"{path.name} root must be a JSON object"
                    elif not data.get("source") or not isinstance(data.get("source"), Mapping):
                        is_valid = False
                        err_code = "SCHEMA_UNSUPPORTED"
                        err_msg = f"{path.name} missing non-empty 'source' provenance dictionary"
            except (E2EError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                is_valid = False
                err_msg = f"{path.name} is present but unreadable: {exc}"

            if is_valid:
                evidence.status = EVIDENCE_SATISFIED
                if requirement.requires_source_immutability:
                    _check_source_immutability_present(requirement, root, evidence)
            else:
                evidence.status = EVIDENCE_MISSING
                evidence.problems.append(
                    {
                        "code": err_code,
                        "message": err_msg,
                        "expected_path": str(path.relative_to(root)),
                        "searched_paths": [str(path.relative_to(root))],
                    }
                )
        else:
            evidence.status = EVIDENCE_MISSING
            evidence.problems.append(
                {
                    "code": "LIVE_CONTEXT_MISSING",
                    "message": (
                        "A native task must record the runtime it observed and the "
                        "contracts it deployed; no live-context.json was found in its "
                        "own evidence directory."
                    ),
                    "expected_path": str(path.relative_to(root)),
                    # Naming every place that was looked at is what turns "the
                    # file is missing" into a statement a reviewer can check,
                    # rather than one they have to trust.
                    "searched_paths": [
                        str(candidate.relative_to(root))
                        for candidate in requirement.live_context_candidates(root)
                    ],
                }
            )
            if requirement.requires_source_immutability:
                _check_source_immutability_present(requirement, root, evidence)
        found.append(evidence)
    return found


def unclaimed_live_contexts(
    requirements: Sequence[EvidenceRequirement], *, suite_dir: Path
) -> List[str]:
    """Live contexts that belong to no selected task.

    An unexpected live context is not proof of anything and must not be counted
    as one; it is reported so a reviewer can see the suite produced evidence
    nobody asked for. Every location a selected task is allowed to write to
    counts as claimed -- otherwise the layout the producer actually uses would
    be reported as an intruder in every single run.
    """
    root = Path(suite_dir)
    claimed = {
        str(candidate)
        for requirement in requirements
        for candidate in requirement.live_context_candidates(root)
    }
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob(LIVE_CONTEXT_FILENAME)
        if str(path) not in claimed
    )


__all__ = [
    "DEPLOYMENT_PROOF_LEVELS",
    "EVIDENCE_MISSING",
    "EVIDENCE_NOT_APPLICABLE",
    "EVIDENCE_SATISFIED",
    "EvidencePolicyError",
    "EvidenceRequirement",
    "LIVE_CONTEXT_FILENAME",
    "LIVE_CONTEXT_PROOF_LEVELS",
    "SUITE_RUNS_DIRNAME",
    "SelectedTask",
    "TaskEvidence",
    "catalog_expected_artifacts",
    "load_suite_plan",
    "locate_task_evidence",
    "requirements_for",
    "requirements_from_documents",
    "selected_tasks_from_lock",
    "selected_tasks_from_suite_plan",
    "tasks_from_lock",
    "unclaimed_live_contexts",
]
