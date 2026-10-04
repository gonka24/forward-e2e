"""Prove that a materialised source snapshot is exactly its selected commit.

Why this module exists
----------------------
The runner used to *change* the Gonka checkout before building it: it copied a
test overlay into the tree, committed the result as a "prepared" commit and then
allowed a list of paths to differ from the selected SHA. A PASS therefore said
something about a tree nobody had selected.

The immutable-source model forbids that. Both product commits are materialised
from Git objects and must stay byte-for-byte what the selected commit says, from
before the first build step until after the last scenario. This module is the
one place that measures that fact. It is called:

* by the E2E layer (``forward_e2e/execution``) before the build, after the build and after
  execution, and
* by ``scripts/acceptance_harness.py run-live`` around the Testermint run, which loads
  this file by path because the ``forward_e2e`` package is not importable there.

One implementation for both paths is deliberate (see AGENTS.md, "house style"):
two copies of an immutability check would drift, and the weaker one would win.

Constraints
-----------
Standard library only and **no relative or ``ops.*`` imports**, so the file can
be loaded with ``importlib`` from a bare path. This is pinned by a test.

What is measured
----------------
``capture`` records, for one snapshot root:

* ``head`` and ``tree`` -- ``HEAD`` and ``HEAD^{tree}``;
* ``index_matches_head`` -- whether the index lists exactly the blobs of
  ``HEAD`` (a rewritten index could otherwise hide a changed file);
* ``tracked_digest`` -- SHA-256 over path, mode and the SHA-256 of the *bytes on
  disk* of every tracked file, so a change that Git's stat cache would miss is
  still seen;
* ``status`` -- ``git status`` including untracked **and ignored** files, so an
  added file, a build output or a planted binary is visible even when a
  ``.gitignore`` would normally hide it;
* ``submodules`` -- every gitlink with the commit ``HEAD`` pins and a recursive
  snapshot of the checked-out commit, including ignored files;
* ``forbidden`` -- named paths left by the old runner that are absent from the
  selected commit (for example its untracked ``gradlew.a8-linux`` copy).

Nothing here writes into the snapshot: Git is run with optional locks disabled
and without fsmonitor, so even ``git status`` does not refresh the index.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

SNAPSHOT_SCHEMA = "a8.source-snapshot/1"

#: Paths left by the retired runner when they are absent from the selected
#: commit. A tracked file at one of these paths belongs to that commit and must
#: not be rejected merely because an older overlay used the same name.
FORBIDDEN_SNAPSHOT_PATHS = (
    "testermint/gradlew.a8-linux",
    "local-test-net/docker-compose.genesis-a8-b3-foreign-denom.yml",
    "inference-chain/app/a8faults/plan.go",
    "inference-chain/app/legacy.go",
    "testermint/src/test/kotlin/MarketplaceContractAcceptanceTests.kt",
    ".docker-context",
)

# Error codes. They are stable strings; callers map them to their own error
# classes (``forward_e2e/execution/errors.py``) or to harness exit codes.
CODE_NOT_A_REPOSITORY = "SOURCE_NOT_A_REPOSITORY"
CODE_HEAD_MISMATCH = "SOURCE_HEAD_MISMATCH"
CODE_TREE_DIRTY = "SOURCE_TREE_DIRTY"
CODE_INDEX_MISMATCH = "SOURCE_INDEX_MISMATCH"
CODE_SUBMODULE_DRIFT = "SOURCE_SUBMODULE_DRIFT"
CODE_FORBIDDEN_FILE = "SOURCE_FORBIDDEN_FILE"
CODE_SNAPSHOT_MUTATED = "SOURCE_SNAPSHOT_MUTATED"
CODE_SNAPSHOT_MALFORMED = "SOURCE_SNAPSHOT_MALFORMED"

GitRunner = Callable[[Path, Sequence[str]], bytes]


class SourceSnapshotError(RuntimeError):
    """A snapshot is not, or is no longer, exactly its selected commit."""

    def __init__(self, message: str, *, code: str, details: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.details: Dict[str, Any] = dict(details or {})


def _git_environment() -> Dict[str, str]:
    env = dict(os.environ)
    # ``git status`` would otherwise refresh and rewrite .git/index. The index
    # is not a tracked file, but a measurement that writes is not a
    # measurement we want to reason about.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_INDEX_FILE", None)
    return env


def default_git_runner(root: Path, args: Sequence[str]) -> bytes:
    """Run one read-only Git command in ``root`` and return raw stdout."""
    completed = subprocess.run(
        [
            "git", "-c", f"safe.directory={root}",
            "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *args,
        ],
        cwd=str(root),
        env=_git_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise SourceSnapshotError(
            f"git {' '.join(args)} failed in {root}",
            code=CODE_NOT_A_REPOSITORY,
            details={"stderr": completed.stderr.decode("utf-8", "replace")[-2000:]},
        )
    return completed.stdout


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "strict").strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_ls_entries(raw: bytes, *, stage_format: bool) -> Dict[str, Dict[str, str]]:
    """Parse ``git ls-files -s -z`` or ``git ls-tree -r -z`` output.

    ``ls-files -s``: ``<mode> <object> <stage>\\t<path>``
    ``ls-tree -r``: ``<mode> <type> <object>\\t<path>``
    """
    entries: Dict[str, Dict[str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, _, path_bytes = record.partition(b"\t")
        fields = meta.decode("ascii").split()
        path = path_bytes.decode("utf-8", "surrogateescape")
        if stage_format:
            mode, obj, _stage = fields[0], fields[1], fields[2]
        else:
            mode, _kind, obj = fields[0], fields[1], fields[2]
        entries[path] = {"mode": mode, "object": obj}
    return entries


def _parse_status(raw: bytes) -> List[str]:
    """``git status --porcelain=v1 -z`` records, verbatim, in order."""
    return [record.decode("utf-8", "surrogateescape") for record in raw.split(b"\0") if record]


def capture(
    root: Path, *, git: Optional[GitRunner] = None,
    _ancestors: frozenset[Path] = frozenset(),
) -> Dict[str, Any]:
    """Measure one snapshot root. Reads only; never writes into ``root``."""
    run = git or default_git_runner
    root = Path(root)
    if not (root / ".git").exists():
        raise SourceSnapshotError(
            f"{root} is not a materialised Git snapshot (no .git)",
            code=CODE_NOT_A_REPOSITORY,
            details={"root": str(root)},
        )
    resolved_root = root.resolve(strict=True)
    if resolved_root in _ancestors:
        raise SourceSnapshotError(
            f"Submodule path cycles back to {resolved_root}",
            code=CODE_SUBMODULE_DRIFT,
        )
    ancestors = _ancestors | {resolved_root}
    head = _text(run(root, ["rev-parse", "--verify", "HEAD^{commit}"]))
    tree = _text(run(root, ["rev-parse", "--verify", "HEAD^{tree}"]))
    index_entries = _parse_ls_entries(run(root, ["ls-files", "-s", "-z"]), stage_format=True)
    head_entries = _parse_ls_entries(run(root, ["ls-tree", "-r", "-z", "HEAD"]), stage_format=False)
    status = _parse_status(
        run(
            root,
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
                "--ignore-submodules=none",
            ],
        )
    )

    digest = hashlib.sha256()
    missing: List[str] = []
    submodules: List[Dict[str, Any]] = []
    for path in sorted(head_entries):
        entry = head_entries[path]
        mode = entry["mode"]
        if mode == "160000":
            checked_out: Optional[str] = None
            sub_snapshot: Optional[Dict[str, Any]] = None
            sub_root = root / path
            if sub_root.is_symlink():
                raise SourceSnapshotError(
                    f"Submodule path is a symlink: {sub_root}",
                    code=CODE_SUBMODULE_DRIFT,
                )
            if (sub_root / ".git").exists():
                sub_snapshot = capture(sub_root, git=run, _ancestors=ancestors)
                checked_out = str(sub_snapshot["head"])
            submodules.append(
                {
                    "path": path,
                    "pinned": entry["object"],
                    "checked_out": checked_out,
                    "materialized": checked_out is not None,
                    "snapshot": sub_snapshot,
                }
            )
            nested_digest = sub_snapshot["tracked_digest"] if sub_snapshot else None
            digest.update(
                f"gitlink\0{path}\0{entry['object']}\0{checked_out}\0{nested_digest}\n".encode(
                    "utf-8", "surrogateescape"
                )
            )
            continue
        file_path = root / path
        if mode == "120000":
            # A symlink is compared by its target text, never followed.
            try:
                content = os.readlink(file_path).encode("utf-8", "surrogateescape")
                value = hashlib.sha256(content).hexdigest()
            except OSError:
                missing.append(path)
                value = "missing"
        elif file_path.is_file() and not file_path.is_symlink():
            value = _sha256_file(file_path)
        else:
            missing.append(path)
            value = "missing"
        digest.update(f"{mode}\0{path}\0{value}\n".encode("utf-8", "surrogateescape"))

    forbidden = [
        rel for rel in FORBIDDEN_SNAPSHOT_PATHS
        if not any(path == rel or path.startswith(rel + "/") for path in head_entries)
        and os.path.lexists(root / rel)
    ]
    return {
        "schema": SNAPSHOT_SCHEMA,
        "head": head,
        "tree": tree,
        "index_matches_head": index_entries == head_entries,
        "tracked_files": len(head_entries),
        "tracked_digest": digest.hexdigest(),
        "missing_tracked": missing,
        "status": status,
        "submodules": submodules,
        "forbidden": forbidden,
    }


def _require_fingerprint(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema") != SNAPSHOT_SCHEMA:
        raise SourceSnapshotError(
            f"{what} is not an {SNAPSHOT_SCHEMA} fingerprint",
            code=CODE_SNAPSHOT_MALFORMED,
        )
    for key in (
        "head", "tree", "index_matches_head", "tracked_files", "tracked_digest",
        "missing_tracked", "status", "submodules", "forbidden",
    ):
        if key not in value:
            raise SourceSnapshotError(
                f"{what} has no {key!r}; absence is never agreement",
                code=CODE_SNAPSHOT_MALFORMED,
                details={"missing": key},
            )
    return value


def pristine_violations(fingerprint: Mapping[str, Any], *, expected_sha: str) -> List[Dict[str, Any]]:
    """Every reason ``fingerprint`` is not exactly ``expected_sha``. Empty = pristine."""
    fp = _require_fingerprint(fingerprint, "snapshot")
    problems: List[Dict[str, Any]] = []
    if str(fp["head"]).lower() != str(expected_sha).lower():
        problems.append({"code": CODE_HEAD_MISMATCH, "expected": expected_sha, "actual": fp["head"]})
    if not fp.get("index_matches_head", False):
        problems.append({"code": CODE_INDEX_MISMATCH})
    if fp.get("missing_tracked"):
        problems.append({"code": CODE_TREE_DIRTY, "missing_tracked": list(fp["missing_tracked"])})
    if fp["status"]:
        problems.append({"code": CODE_TREE_DIRTY, "status": list(fp["status"])})
    for sub in fp["submodules"]:
        if not sub.get("materialized") or str(sub.get("checked_out")).lower() != str(sub.get("pinned")).lower():
            problems.append({"code": CODE_SUBMODULE_DRIFT, "submodule": dict(sub)})
        nested = sub.get("snapshot")
        if sub.get("materialized") and not isinstance(nested, Mapping):
            problems.append({"code": CODE_SNAPSHOT_MALFORMED, "submodule": dict(sub)})
        elif isinstance(nested, Mapping):
            for problem in pristine_violations(nested, expected_sha=str(sub.get("pinned"))):
                problems.append({"code": problem["code"], "submodule": sub.get("path"), "nested": problem})
    if fp["forbidden"]:
        problems.append({"code": CODE_FORBIDDEN_FILE, "paths": list(fp["forbidden"])})
    return problems


def assert_pristine(fingerprint: Mapping[str, Any], *, expected_sha: str, label: str = "source") -> None:
    """Raise unless the snapshot is exactly ``expected_sha`` with nothing added."""
    problems = pristine_violations(fingerprint, expected_sha=expected_sha)
    if problems:
        raise SourceSnapshotError(
            f"The {label} snapshot is not exactly commit {expected_sha}: "
            + ", ".join(sorted({p["code"] for p in problems})),
            code=problems[0]["code"],
            details={"label": label, "problems": problems},
        )


#: Fields whose equality proves "unchanged". ``tracked_files`` is implied by
#: ``tracked_digest`` but kept so a report can say *how* it changed.
_COMPARED_FIELDS = (
    "head",
    "tree",
    "index_matches_head",
    "tracked_files",
    "tracked_digest",
    "missing_tracked",
    "status",
    "submodules",
    "forbidden",
)


def differences(before: Mapping[str, Any], after: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Field-level differences between two fingerprints of the same root."""
    b = _require_fingerprint(before, "before-snapshot")
    a = _require_fingerprint(after, "after-snapshot")
    return [
        {"field": key, "before": b.get(key), "after": a.get(key)}
        for key in _COMPARED_FIELDS
        if b.get(key) != a.get(key)
    ]


def assert_unchanged(before: Mapping[str, Any], after: Mapping[str, Any], *, label: str = "source") -> None:
    """Raise when anything about the snapshot changed between two measurements."""
    diff = differences(before, after)
    if diff:
        raise SourceSnapshotError(
            f"The {label} snapshot changed during the run: "
            + ", ".join(item["field"] for item in diff),
            code=CODE_SNAPSHOT_MUTATED,
            details={"label": label, "differences": diff},
        )


def immutability_record(
    *,
    label: str,
    expected_sha: str,
    before: Mapping[str, Any],
    after: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """The evidence document for one root. A missing ``after`` is never a pass."""
    violations = pristine_violations(before, expected_sha=expected_sha)
    diff: List[Dict[str, Any]] = []
    if after is None:
        verdict = "INCOMPLETE"
    else:
        diff = differences(before, after)
        violations_after = pristine_violations(after, expected_sha=expected_sha)
        verdict = "UNCHANGED" if not violations and not violations_after and not diff else "VIOLATED"
        violations = violations + [dict(v, phase="after") for v in violations_after]
    if after is None and violations:
        verdict = "VIOLATED"
    return {
        "schema": "a8.source-immutability/1",
        "label": label,
        "expected_sha": expected_sha,
        "before": dict(before),
        "after": dict(after) if after is not None else None,
        "violations": violations,
        "differences": diff,
        "verdict": verdict,
    }


__all__ = [
    "CODE_FORBIDDEN_FILE",
    "CODE_HEAD_MISMATCH",
    "CODE_INDEX_MISMATCH",
    "CODE_NOT_A_REPOSITORY",
    "CODE_SNAPSHOT_MALFORMED",
    "CODE_SNAPSHOT_MUTATED",
    "CODE_SUBMODULE_DRIFT",
    "CODE_TREE_DIRTY",
    "FORBIDDEN_SNAPSHOT_PATHS",
    "SNAPSHOT_SCHEMA",
    "SourceSnapshotError",
    "assert_pristine",
    "assert_unchanged",
    "capture",
    "default_git_runner",
    "differences",
    "immutability_record",
    "pristine_violations",
]
