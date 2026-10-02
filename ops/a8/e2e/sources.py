"""Typed source specifications and reproducible source acquisition.

The user selects a repository (remote URL or local path) and *always* a full
40 hex character commit SHA. This module is where that contract is enforced
and where the selected commit is turned into a self-contained, portable
artefact that can be replayed on a different host without the original
checkout and without the original remote.

Acquisition pipeline (identical for remote and local inputs)
------------------------------------------------------------
1. Create a private scratch repository inside the container workspace.
2. Try to fetch the exact object. If the server refuses a direct object
   fetch, fall back to fetching ordinary refs and then look for the object.
   A failed fetch never degrades into "use whatever we got".
3. Assert that the object exists **and is a commit** (a tag object, a tree or
   a blob is rejected).
4. Pin it under a deterministic ref and write a self-contained git bundle.
5. Verify the bundle, then clone it and detach exactly onto the SHA.
6. Reject incomplete snapshots (Git LFS), and materialise submodules at their
   pinned gitlink SHAs, recording each of them in the lock.

Nothing here reads the working tree of a local input: only committed objects
are ever transferred, so a dirty or differently-checked-out worktree cannot
contaminate the run, and the user's files are never modified.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import io
from pathlib import Path, PurePosixPath
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .errors import (
    IntegrityError,
    PathSafetyError,
    SourceAcquisitionError,
    SourceSpecError,
    UnsupportedSourceError,
)
from .gitio import GitClient, validate_remote_url

FULL_SHA_RE = re.compile(r"\A[0-9a-fA-F]{40}\Z")

#: Values that users habitually pass and that are explicitly refused. They are
#: listed so that the error message can explain *why* rather than only saying
#: "does not match the pattern".
_NAMED_REVISIONS = {
    "head",
    "orig_head",
    "fetch_head",
    "merge_head",
    "main",
    "master",
    "trunk",
    "develop",
    "latest",
}

_REVISION_OPERATORS = ("~", "^", ":", "@{", "..", "*", "?", "[", "]")

GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})

#: Deterministic ref written into every bundle. Replay looks for exactly this
#: ref, so a bundle cannot smuggle in an unexpected branch layout.
BUNDLE_REF_PREFIX = "refs/e2e/source"


def bundle_ref(role: str) -> str:
    return f"{BUNDLE_REF_PREFIX}/{role}"


# ----------------------------------------------------------------------------
# strict SHA validation
# ----------------------------------------------------------------------------
def normalize_full_sha(raw: Any, *, flag: str) -> str:
    """Validate a user supplied revision and return the canonical lowercase SHA.

    Accepts **only** ``[0-9a-fA-F]{40}``. Branch names, tags, ``HEAD``,
    ``<sha>~1``, abbreviated SHAs, full refs and empty values are rejected.
    There is deliberately no resolver and no fallback to a default branch: the
    user is expected to look the SHA up first and then pass it.
    """
    if raw is None:
        raise SourceSpecError(
            f"{flag} is required: pass the full 40 character commit SHA",
            {"flag": flag},
        )
    if not isinstance(raw, str):
        raise SourceSpecError(f"{flag} must be a string", {"flag": flag})
    value = raw.strip()
    if not value:
        raise SourceSpecError(
            f"{flag} must not be empty. Look up the full commit SHA first.",
            {"flag": flag},
        )
    if value != raw:
        raise SourceSpecError(
            f"{flag} must not contain surrounding whitespace",
            {"flag": flag, "value": raw},
        )

    lowered = value.lower()
    if lowered in _NAMED_REVISIONS:
        raise SourceSpecError(
            f"{flag} does not accept the symbolic revision {value!r}. "
            "Resolve it to a full 40 character commit SHA yourself and pass that.",
            {"flag": flag, "value": value},
        )
    if value.startswith("refs/") or lowered.startswith("origin/"):
        raise SourceSpecError(
            f"{flag} does not accept a ref name ({value!r}). Pass the full commit SHA.",
            {"flag": flag, "value": value},
        )
    if any(op in value for op in _REVISION_OPERATORS):
        raise SourceSpecError(
            f"{flag} does not accept revision expressions ({value!r}). "
            "Pass a plain full commit SHA.",
            {"flag": flag, "value": value},
        )
    if FULL_SHA_RE.match(value) is None:
        hint = "too short (abbreviated SHAs are refused)" if len(value) < 40 else (
            "too long" if len(value) > 40 else "not hexadecimal"
        )
        raise SourceSpecError(
            f"{flag} must be exactly 40 hexadecimal characters; got {len(value)} "
            f"characters which is {hint}.",
            {"flag": flag, "value": value, "length": len(value)},
        )
    return lowered


def is_full_sha(value: Any) -> bool:
    return isinstance(value, str) and FULL_SHA_RE.match(value) is not None


# ----------------------------------------------------------------------------
# path safety
# ----------------------------------------------------------------------------
def safe_relative_path(relative: str, *, what: str = "path") -> PurePosixPath:
    """Validate a relative POSIX path taken from a JSON document."""
    if not isinstance(relative, str) or not relative.strip():
        raise PathSafetyError(f"{what} must be a non-empty relative path")
    candidate = relative.strip().replace("\\", "/")
    pure = PurePosixPath(candidate)
    if pure.is_absolute() or candidate.startswith("/"):
        raise PathSafetyError(f"{what} must be relative", {"path": relative})
    if re.match(r"\A[A-Za-z]:", candidate):
        raise PathSafetyError(f"{what} must not be a drive-qualified path", {"path": relative})
    if any(part in ("..", "") for part in pure.parts):
        raise PathSafetyError(f"{what} must not traverse parent directories", {"path": relative})
    if "\x00" in candidate:
        raise PathSafetyError(f"{what} must not contain NUL bytes")
    return pure


def resolve_within(base: Path, relative: str, *, what: str = "path") -> Path:
    """Join ``relative`` onto ``base`` and prove the result stays inside it.

    Both the lexical form and the fully resolved form are checked, so neither
    ``..`` segments nor a symlink planted inside the package can escape.
    """
    pure = safe_relative_path(relative, what=what)
    base_resolved = Path(base).resolve()
    candidate = base_resolved.joinpath(*pure.parts)
    resolved = candidate.resolve() if candidate.exists() else _resolve_nonexistent(candidate)
    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise PathSafetyError(
            f"{what} escapes its permitted root",
            {"path": relative, "root": str(base_resolved)},
        ) from exc
    _assert_no_symlink_between(base_resolved, candidate, what=what)
    return candidate


def _resolve_nonexistent(candidate: Path) -> Path:
    """Resolve the closest existing ancestor and re-append the missing tail."""
    existing = candidate
    tail: List[str] = []
    while not existing.exists() and existing != existing.parent:
        tail.append(existing.name)
        existing = existing.parent
    return existing.resolve().joinpath(*reversed(tail))


def _assert_no_symlink_between(base: Path, candidate: Path, *, what: str) -> None:
    current = candidate
    while True:
        if current.is_symlink():
            raise PathSafetyError(
                f"{what} traverses a symlink, which is not allowed inside a source package",
                {"path": str(current)},
            )
        if current == base or current == current.parent:
            return
        current = current.parent


SYSTEM_SYMLINK_ALLOWLIST = frozenset(
    {
        Path("/var"),
        Path("/tmp"),
        Path("/etc"),
        Path("/private/var"),
        Path("/private/tmp"),
        Path("/private/etc"),
    }
)


def assert_no_symlinks_in_path(path: str | Path, *, what: str = "path") -> None:
    """Ensure that neither path nor any of its ancestor directories is a symlink.

    Verifies both lexical components and absolute chain (anchored to cwd if relative),
    rejecting any symlink component. System symlinks (/var, /tmp, /etc on macOS)
    are exempted to allow standard temp directories.
    """
    p = Path(path)

    # Anchor without resolving: resolution would hide symlink components.
    full = p if p.is_absolute() else (Path.cwd() / p)
    curr = full
    while curr != curr.parent:
        if curr in SYSTEM_SYMLINK_ALLOWLIST:
            curr = curr.parent
            continue
        if curr.is_symlink():
            raise PathSafetyError(
                f"{what} traverses a symlink, which is not allowed",
                {"path": str(curr)},
            )
        curr = curr.parent


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ----------------------------------------------------------------------------
# source specs
# ----------------------------------------------------------------------------
class SourceKind(str, Enum):
    REMOTE = "remote"
    LOCAL = "local"


@dataclass(frozen=True)
class SourceSpec:
    """A fully determined choice of "which code" for one role."""

    role: str
    kind: SourceKind
    commit_sha: str
    repo_url: Optional[str] = None
    local_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "kind": self.kind.value,
            "commit_sha": self.commit_sha,
            "repo_url": self.repo_url,
            "local_path": self.local_path,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceSpec":
        kind = SourceKind(str(data["kind"]))
        return cls(
            role=str(data["role"]),
            kind=kind,
            commit_sha=normalize_full_sha(data.get("commit_sha"), flag=f"{data.get('role')}.commit_sha"),
            repo_url=data.get("repo_url"),
            local_path=data.get("local_path"),
        )

    @property
    def origin(self) -> str:
        return self.repo_url if self.kind is SourceKind.REMOTE else str(self.local_path)


def build_source_spec(
    *,
    role: str,
    repo: Optional[str],
    path: Optional[str],
    sha: Optional[str],
    repo_flag: str,
    path_flag: str,
    sha_flag: str,
) -> SourceSpec:
    """Turn one pair of CLI flags into a validated :class:`SourceSpec`."""
    has_repo = bool(repo and str(repo).strip())
    has_path = bool(path and str(path).strip())
    if has_repo and has_path:
        raise SourceSpecError(
            f"{repo_flag} and {path_flag} are mutually exclusive; choose exactly one source for {role}",
            {"role": role},
        )
    if not has_repo and not has_path:
        raise SourceSpecError(
            f"{role} source is missing: pass either {repo_flag} or {path_flag}",
            {"role": role},
        )
    commit = normalize_full_sha(sha, flag=sha_flag)
    if has_repo:
        return SourceSpec(
            role=role,
            kind=SourceKind.REMOTE,
            commit_sha=commit,
            repo_url=validate_remote_url(str(repo)),
        )
    local = Path(str(path)).expanduser()
    return SourceSpec(
        role=role,
        kind=SourceKind.LOCAL,
        commit_sha=commit,
        local_path=str(local),
    )


# ----------------------------------------------------------------------------
# commit links
# ----------------------------------------------------------------------------
def github_commit_url(repo_url: Optional[str], sha: str) -> Optional[str]:
    """Build ``https://github.com/<owner>/<repo>/commit/<sha>`` when possible.

    Returns ``None`` for anything that is not recognisably GitHub. A commit
    URL is a convenience link, never a proof that the commit is published:
    callers must not treat a non-``None`` result as evidence of availability.
    """
    if not repo_url or not is_full_sha(sha):
        return None
    try:
        parts = urlsplit(repo_url)
    except ValueError:
        return None
    if parts.scheme not in ("https", "http"):
        return None
    if parts.hostname is None or parts.hostname.lower() not in GITHUB_HOSTS:
        return None
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) < 2:
        return None
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not owner or not repo:
        return None
    return f"https://github.com/{owner}/{repo}/commit/{sha.lower()}"


def detect_local_origin(git: GitClient, repo_path: Path) -> Optional[str]:
    """Return the confirmed ``origin`` URL of a local repository, or ``None``.

    When the origin is unknown, unset, or not an https remote the function
    returns ``None`` so the lock records ``null``. An origin URL is never
    invented.
    """
    proc = git.run(
        [*git.local_safe_directory_args(repo_path), "-C", str(repo_path),
         "config", "--get", "remote.origin.url"],
        check=False,
        timeout=60.0,
    )
    if proc.returncode != 0:
        return None
    value = (proc.stdout or "").strip()
    if not value:
        return None
    try:
        return validate_remote_url(value)
    except SourceSpecError:
        return None


# ----------------------------------------------------------------------------
# acquisition results
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class SubmoduleRecord:
    path: str
    url: str
    commit_sha: str
    bundle_relpath: str
    bundle_sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "url": self.url,
            "commit_sha": self.commit_sha,
            "bundle_relpath": self.bundle_relpath,
            "bundle_sha256": self.bundle_sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SubmoduleRecord":
        return cls(
            path=str(data["path"]),
            url=str(data["url"]),
            commit_sha=normalize_full_sha(data.get("commit_sha"), flag="submodule.commit_sha"),
            bundle_relpath=str(data["bundle_relpath"]),
            bundle_sha256=str(data["bundle_sha256"]),
        )


@dataclass
class AcquiredSource:
    """A committed snapshot plus everything needed to replay it elsewhere."""

    spec: SourceSpec
    worktree: Path
    bundle_path: Path
    bundle_relpath: str
    bundle_sha256: str
    bundle_ref: str
    commit_url: Optional[str]
    resolved_origin_url: Optional[str]
    submodules: List[SubmoduleRecord] = field(default_factory=list)
    fetch_strategy: str = "object"

    def to_lock_record(self) -> Dict[str, Any]:
        return {
            "role": self.spec.role,
            "source_kind": self.spec.kind.value,
            "repo_url": self.spec.repo_url,
            "local_path_hint": self.spec.local_path,
            "resolved_origin_url": self.resolved_origin_url,
            "commit_sha": self.spec.commit_sha,
            "commit_url": self.commit_url,
            "bundle_relpath": self.bundle_relpath,
            "bundle_sha256": self.bundle_sha256,
            "bundle_ref": self.bundle_ref,
            "fetch_strategy": self.fetch_strategy,
            "submodules": [s.to_dict() for s in self.submodules],
        }


# ----------------------------------------------------------------------------
# acquisition
# ----------------------------------------------------------------------------
class SourceAcquirer:
    """Fetches pinned commits and turns them into portable bundles."""

    def __init__(
        self,
        git: GitClient,
        *,
        package_dir: Path,
        scratch_dir: Path,
        max_submodule_depth: int = 2,
    ):
        self.git = git
        self.package_dir = Path(package_dir)
        self.scratch_dir = Path(scratch_dir)
        self.max_submodule_depth = max_submodule_depth

    # -- public API ----------------------------------------------------
    def acquire(
        self, spec: SourceSpec, *, worktree_root: Path, create_bundle: bool = True
    ) -> AcquiredSource:
        origin, allow_credentials = self._origin_for(spec)
        scratch = self.scratch_dir / f"{spec.role}-objects"
        if scratch.exists():
            raise SourceAcquisitionError(
                "Scratch object store already exists; refusing to reuse it",
                {"path": str(scratch)},
            )
        self.git.init_repo(scratch, bare=True)

        strategy = self._fetch_commit(
            scratch, origin, spec.commit_sha,
            allow_credentials=allow_credentials, role=spec.role,
        )
        self._assert_commit_object(scratch, spec.commit_sha, spec)

        ref = bundle_ref(spec.role)
        self.git.run(["-C", str(scratch), "update-ref", ref, spec.commit_sha], timeout=120.0)

        worktree = Path(worktree_root) / spec.role
        if create_bundle:
            bundle_rel = f"bundles/{spec.role}.bundle"
            bundle_path = resolve_within(self.package_dir, bundle_rel, what="bundle path")
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_bundle(scratch, bundle_path, ref)
            self._verify_bundle(bundle_path, ref, spec.commit_sha)
            self.materialise(bundle_path, worktree, ref, spec.commit_sha)
            bundle_sha256 = sha256_path(bundle_path)
        else:
            bundle_rel = ""
            bundle_path = scratch
            self.materialise(scratch, worktree, ref, spec.commit_sha)
            bundle_sha256 = ""

        self._reject_lfs(worktree, spec)
        submodules = self._materialise_submodules(
            worktree, spec, depth=0, allow_credentials=allow_credentials,
            create_bundle=create_bundle,
        )

        resolved_origin = (
            spec.repo_url if spec.kind is SourceKind.REMOTE
            else detect_local_origin(self.git, Path(str(spec.local_path)))
        )
        return AcquiredSource(
            spec=spec,
            worktree=worktree,
            bundle_path=bundle_path,
            bundle_relpath=bundle_rel,
            bundle_sha256=bundle_sha256,
            bundle_ref=ref,
            commit_url=github_commit_url(resolved_origin, spec.commit_sha),
            resolved_origin_url=resolved_origin,
            submodules=submodules,
            fetch_strategy=strategy,
        )

    def materialise(self, bundle_path: Path, worktree: Path, ref: str, sha: str) -> Path:
        """Clone ``bundle_path`` into ``worktree`` and detach exactly onto ``sha``."""
        worktree = Path(worktree)
        if worktree.exists():
            raise SourceAcquisitionError(
                "Refusing to overwrite an existing checkout directory",
                {"path": str(worktree)},
            )
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self.git.run(
            [
                "-c",
                "remote.origin.fetch=+refs/*:refs/*",
                "clone",
                "--no-checkout",
                "--quiet",
                str(bundle_path),
                str(worktree),
            ],
            timeout=900.0,
        )
        self.git.run(["-C", str(worktree), "checkout", "--quiet", "--detach", sha], timeout=900.0)
        head = self.git.stdout(["-C", str(worktree), "rev-parse", "HEAD"], timeout=120.0)
        if head.lower() != sha.lower():
            raise IntegrityError(
                "Checked out HEAD does not match the pinned commit",
                {"expected": sha, "actual": head, "path": str(worktree)},
            )
        return worktree

    def verify_stored_bundle(
        self,
        record: Mapping[str, Any],
        *,
        package_root: Path,
    ) -> Path:
        """Validate one bundle entry of a stored source package.

        Used by replay: the hash, the ref and the commit inside the bundle are
        all checked before anything is cloned from it.
        """
        rel = str(record.get("bundle_relpath") or "")
        bundle_path = resolve_within(package_root, rel, what="bundle_relpath")
        if not bundle_path.is_file():
            raise IntegrityError(
                "Source package is missing a bundle referenced by the lock",
                {"bundle_relpath": rel},
            )
        expected_hash = str(record.get("bundle_sha256") or "")
        actual_hash = sha256_path(bundle_path)
        if not expected_hash or actual_hash != expected_hash:
            raise IntegrityError(
                "Stored bundle hash does not match the lock; the package was tampered with",
                {"bundle_relpath": rel, "expected": expected_hash, "actual": actual_hash},
            )
        ref = record.get("bundle_ref")
        if not isinstance(ref, str) or not ref:
            raise IntegrityError(
                "Stored source bundle_ref is missing; the pinned bundle ref cannot be verified",
                {"bundle_relpath": rel},
            )
        sha = normalize_full_sha(record.get("commit_sha"), flag="lock.commit_sha")
        self._verify_bundle(bundle_path, ref, sha)
        return bundle_path

    # -- internals -----------------------------------------------------
    def _origin_for(self, spec: SourceSpec) -> Tuple[str, bool]:
        if spec.kind is SourceKind.REMOTE:
            return validate_remote_url(str(spec.repo_url)), True
        local = Path(str(spec.local_path))
        if not local.exists():
            raise SourceSpecError(
                "Local source path does not exist inside the runner container. "
                "Mount it read-only and pass the container path.",
                {"role": spec.role, "path": str(local)},
            )
        if not local.is_dir():
            raise SourceSpecError(
                "Local source path is not a directory",
                {"role": spec.role, "path": str(local)},
            )
        self._assert_usable_local_repo(local, spec)
        return str(local.resolve()), False

    def _assert_usable_local_repo(self, local: Path, spec: SourceSpec) -> None:
        """Detect a git worktree whose ``.git`` pointer is unusable here.

        A Windows worktree stores ``gitdir: C:\\...`` inside ``.git``. That
        host path does not exist in the container, so the pointer must never
        be carried across. The host wrapper prepares a bundle instead; this
        error explains exactly that.
        """
        git_entry = local / ".git"
        probe = self.git.run(
            [*self.git.local_safe_directory_args(local), "-C", str(local),
             "rev-parse", "--git-dir"],
            check=False,
            timeout=60.0,
        )
        if probe.returncode == 0:
            return
        if git_entry.is_file():
            raise SourceSpecError(
                "The local path is a Git worktree whose common gitdir is not reachable here. "
                "Run through the host wrapper (ops/e2e/Run-E2E.ps1 or ops/e2e/run-e2e.sh); "
                "it exports a self-contained bundle for the requested SHA instead of "
                "carrying the host .git pointer into the container.",
                {"role": spec.role, "path": str(local)},
            )
        raise SourceSpecError(
            "The local path is not a usable Git repository",
            {"role": spec.role, "path": str(local)},
        )

    def _fetch_commit(
        self,
        scratch: Path,
        origin: str,
        sha: str,
        *,
        allow_credentials: bool,
        role: str,
    ) -> str:
        """Fetch exactly ``sha`` into ``scratch``; never accept another version."""
        direct = self.git.run(
            ["-C", str(scratch), "fetch", "--quiet", "--no-tags", origin, sha],
            check=False,
            allow_credentials=allow_credentials,
            timeout=1800.0,
        )
        if direct.returncode == 0 and self.git.has_commit(scratch, sha):
            return "object"

        # The server refused an object fetch (the usual reason is that the SHA
        # is not the tip of a branch and uploadpack.allowReachableSHA1InWant is
        # off). Fall back to ordinary refs, then look the object up again.
        refspecs = [
            "+refs/heads/*:refs/remotes/origin/*",
            "+refs/tags/*:refs/tags/*",
            "+refs/pull/*/head:refs/remotes/origin/pr/*",
        ]
        for attempt, extra in ((1, refspecs[:2]), (2, refspecs)):
            proc = self.git.run(
                ["-C", str(scratch), "fetch", "--quiet", origin, *extra],
                check=False,
                allow_credentials=allow_credentials,
                timeout=3600.0,
            )
            if proc.returncode != 0 and attempt == 2:
                raise SourceAcquisitionError(
                    "Could not fetch the repository. The requested commit was not obtained "
                    "and the runner refuses to continue with any other version.",
                    {
                        "role": role,
                        "commit_sha": sha,
                        "origin": origin,
                        "exit_code": proc.returncode,
                    },
                )
            if self.git.has_commit(scratch, sha):
                return "refs" if attempt == 1 else "refs+pull"
        raise SourceAcquisitionError(
            "The repository was reachable but does not contain the requested commit. "
            "No other revision is substituted.",
            {"role": role, "commit_sha": sha, "origin": origin},
        )

    def _assert_commit_object(self, scratch: Path, sha: str, spec: SourceSpec) -> None:
        obj_type = self.git.object_type(scratch, sha)
        if obj_type is None:
            raise SourceAcquisitionError(
                "The requested object is absent after fetching",
                {"role": spec.role, "commit_sha": sha},
            )
        if obj_type != "commit":
            raise SourceSpecError(
                f"The requested SHA is a {obj_type} object, not a commit. "
                "Pass the full SHA of a commit (an annotated tag object is not accepted).",
                {"role": spec.role, "commit_sha": sha, "object_type": obj_type},
            )
        # Defence in depth: prove that the abbreviation-free full id round-trips.
        resolved = self.git.stdout(
            ["-C", str(scratch), "rev-parse", f"{sha}^{{commit}}"], timeout=120.0
        )
        if resolved.lower() != sha.lower():
            raise SourceAcquisitionError(
                "The resolved commit id differs from the requested SHA",
                {"requested": sha, "resolved": resolved},
            )

    def _write_bundle(self, scratch: Path, bundle_path: Path, ref: str) -> None:
        if bundle_path.exists():
            raise SourceAcquisitionError(
                "Refusing to overwrite an existing bundle",
                {"path": str(bundle_path)},
            )
        self.git.run(
            ["-C", str(scratch), "bundle", "create", str(bundle_path), ref],
            timeout=1800.0,
        )

    def _verify_bundle(self, bundle_path: Path, ref: str, sha: str) -> None:
        """Verify a bundle in a runner-owned empty bare repository.

        ``git bundle verify`` is repository-aware: it checks a bundle's
        prerequisites against the current object database. A dedicated empty
        bare repository supplies the required Git context and fails closed if
        the bundle is not self-contained.
        """
        verifier = self.scratch_dir / "bundle-verifier.git"
        if not verifier.exists():
            self.git.init_repo(verifier, bare=True)
        self.git.run(
            ["-C", str(verifier), "bundle", "verify", str(bundle_path)],
            timeout=900.0,
        )
        listing = self.git.stdout(["ls-remote", str(bundle_path)], timeout=300.0)
        found: Dict[str, str] = {}
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                found[parts[1]] = parts[0].lower()
        if ref and ref not in found:
            raise IntegrityError(
                "Bundle does not carry the expected pinned ref",
                {"expected_ref": ref, "refs": sorted(found)},
            )
        if ref and found[ref] != sha.lower():
            raise IntegrityError(
                "Bundle ref points at a different commit than the lock records",
                {"ref": ref, "expected": sha, "actual": found[ref]},
            )

    def _reject_lfs(self, worktree: Path, spec: SourceSpec) -> None:
        """Refuse a snapshot whose content is only reachable through Git LFS."""
        offenders: List[str] = []
        for candidate in worktree.rglob(".gitattributes"):
            if candidate.is_file() and "filter=lfs" in candidate.read_text(
                encoding="utf-8", errors="replace"
            ):
                rel = candidate.relative_to(worktree).as_posix()
                offenders.append(rel)
        if offenders:
            raise UnsupportedSourceError(
                "The selected commit uses Git LFS. The runner will not accept an "
                "incomplete source snapshot made of LFS pointer files, and it does not "
                "silently download LFS objects. This source is unsupported.",
                {"role": spec.role, "commit_sha": spec.commit_sha, "gitattributes": offenders[:10]},
            )

    def _read_gitmodules(self, worktree: Path) -> Dict[str, str]:
        path = resolve_within(worktree, ".gitmodules", what="submodule configuration")
        if not path.is_file():
            return {}
        # Git config supports quoting and escapes that generic INI parsers do
        # not. NUL records preserve whitespace; includes must not read host files.
        proc = self.git.run(
            ["-C", str(worktree), "config", "--null", "--no-includes",
             "--file", str(path), "--list"],
            check=False,
            preserve_output=True,
            timeout=60.0,
        )
        if proc.returncode != 0:
            raise UnsupportedSourceError(
                "The .gitmodules file of the selected commit cannot be parsed",
                {"exit_code": proc.returncode},
            )
        modules: Dict[str, Dict[str, str]] = {}
        for entry in (proc.stdout or "").split("\x00"):
            key, _, value = entry.partition("\n")
            if not key.startswith("submodule."):
                continue
            section, _, name = key.rpartition(".")
            if name in ("path", "url"):
                modules.setdefault(section, {})[name] = value
        return {
            values["path"]: values.get("url", "")
            for values in modules.values() if values.get("path")
        }

    def _gitlinks(self, worktree: Path) -> List[Tuple[str, str]]:
        listing = self.git.run(
            ["-C", str(worktree), "ls-tree", "-rz", "HEAD"],
            timeout=600.0, preserve_output=True,
        ).stdout
        links: List[Tuple[str, str]] = []
        for line in listing.split("\x00"):
            if not line.startswith("160000 "):
                continue
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) >= 3:
                # Git permits arbitrary filename bytes, but lock JSON is UTF-8.
                # Reject them before fetching rather than fail during lock hashing.
                try:
                    path.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise UnsupportedSourceError(
                        "Submodule path is not valid UTF-8 and cannot be stored in the run lock",
                        {"submodule": ascii(path)},
                    ) from exc
                links.append((path, parts[2].lower()))
        return links

    def _materialise_submodules(
        self,
        worktree: Path,
        spec: SourceSpec,
        *,
        depth: int,
        allow_credentials: bool,
        create_bundle: bool = True,
    ) -> List[SubmoduleRecord]:
        links = self._gitlinks(worktree)
        if not links:
            return []
        if depth >= self.max_submodule_depth:
            raise UnsupportedSourceError(
                "Submodule nesting exceeds the supported depth; this source is unsupported "
                "because the snapshot cannot be captured completely.",
                {"role": spec.role, "depth": depth, "paths": [p for p, _ in links]},
            )
        declared = self._read_gitmodules(worktree)
        records: List[SubmoduleRecord] = []
        for index, (sub_path, sub_sha) in enumerate(sorted(links)):
            # Git paths are identities, not user input to normalise. Otherwise
            # acquisition can trim a parent name while flat-path replay keeps it.
            if safe_relative_path(sub_path, what="submodule path").as_posix() != sub_path:
                raise UnsupportedSourceError(
                    "Submodule path would be changed by path normalisation; "
                    "this source is unsupported",
                    {"role": spec.role, "submodule": sub_path},
                )
            url = declared.get(sub_path, "")
            if not url:
                raise UnsupportedSourceError(
                    "A submodule gitlink has no URL in .gitmodules, so its content cannot "
                    "be captured. The source snapshot would be incomplete.",
                    {"role": spec.role, "submodule": sub_path},
                )
            if url.startswith("./") or url.startswith("../"):
                raise UnsupportedSourceError(
                    "Relative submodule URLs are not supported because they depend on the "
                    "hosting location rather than on the pinned content.",
                    {"role": spec.role, "submodule": sub_path},
                )
            try:
                normalised_url = validate_remote_url(url)
            except SourceSpecError as exc:
                raise UnsupportedSourceError(
                    "A submodule uses an unsupported transport or URL form; only https:// submodules "
                    "without embedded credentials, queries, or fragments can be captured reproducibly.",
                    {"role": spec.role, "submodule": sub_path},
                ) from exc

            child_role = f"{spec.role}-sub{index:02d}"
            child_scratch = self.scratch_dir / f"{child_role}-objects"
            self.git.init_repo(child_scratch, bare=True)
            self._fetch_commit(
                child_scratch, normalised_url, sub_sha,
                allow_credentials=allow_credentials, role=child_role,
            )
            child_obj = self.git.object_type(child_scratch, sub_sha)
            if child_obj != "commit":
                raise SourceAcquisitionError(
                    "A submodule gitlink does not resolve to a commit in its repository",
                    {"submodule": sub_path, "commit_sha": sub_sha, "object_type": child_obj},
                )
            child_ref = bundle_ref(child_role)
            self.git.run(
                ["-C", str(child_scratch), "update-ref", child_ref, sub_sha], timeout=120.0
            )
            target = resolve_within(worktree, sub_path, what="submodule path")
            if target.exists() and any(target.iterdir()):
                raise IntegrityError(
                    "Submodule directory is unexpectedly non-empty before materialisation",
                    {"submodule": sub_path},
                )
            if target.exists():
                target.rmdir()
            if create_bundle:
                child_rel = f"bundles/{child_role}.bundle"
                child_bundle = resolve_within(self.package_dir, child_rel, what="submodule bundle")
                child_bundle.parent.mkdir(parents=True, exist_ok=True)
                self._write_bundle(child_scratch, child_bundle, child_ref)
                self._verify_bundle(child_bundle, child_ref, sub_sha)
                self.materialise(child_bundle, target, child_ref, sub_sha)
                child_bundle_sha = sha256_path(child_bundle)
            else:
                child_rel = ""
                self.materialise(child_scratch, target, child_ref, sub_sha)
                child_bundle_sha = ""
            child_spec = SourceSpec(
                role=child_role, kind=SourceKind.REMOTE,
                commit_sha=sub_sha, repo_url=normalised_url,
            )
            self._reject_lfs(target, child_spec)

            records.append(
                SubmoduleRecord(
                    path=sub_path,
                    url=normalised_url,
                    commit_sha=sub_sha,
                    bundle_relpath=child_rel,
                    bundle_sha256=child_bundle_sha,
                )
            )
            # Keep the existing flat lock format, with parent entries before
            # their children so offline replay can restore each checkout in order.
            records.extend(
                replace(child, path=f"{sub_path}/{child.path}")
                for child in self._materialise_submodules(
                    target, child_spec, depth=depth + 1,
                    allow_credentials=allow_credentials,
                    create_bundle=create_bundle,
                )
            )
        return records


def describe_source_for_humans(record: Mapping[str, Any]) -> str:
    """One-line rendering used by ``list``/``plan`` output and by reports."""
    role = record.get("role", "?")
    sha = record.get("commit_sha", "?")
    link = record.get("commit_url")
    origin = record.get("repo_url") or record.get("resolved_origin_url") or record.get("local_path_hint")
    buffer = io.StringIO()
    buffer.write(f"{role}: {sha}")
    if origin:
        buffer.write(f"  ({origin})")
    if link:
        buffer.write(f"\n    {link}")
    else:
        buffer.write("\n    commit link: unknown origin, not recorded")
    return buffer.getvalue()


__all__ = [
    "AcquiredSource",
    "BUNDLE_REF_PREFIX",
    "FULL_SHA_RE",
    "SYSTEM_SYMLINK_ALLOWLIST",
    "SourceAcquirer",
    "SourceKind",
    "SourceSpec",
    "SubmoduleRecord",
    "assert_no_symlinks_in_path",
    "build_source_spec",
    "bundle_ref",
    "describe_source_for_humans",
    "detect_local_origin",
    "github_commit_url",
    "is_full_sha",
    "normalize_full_sha",
    "resolve_within",
    "safe_relative_path",
    "sha256_path",
]
