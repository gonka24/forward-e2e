"""Artifact collector and integrity checker for A8 evidence.

Collects raw evidence using a strict allowlist to prevent copying Git clones,
Gradle cache, node keyrings, private keys, or mnemonics.
Validates paths against directory traversal and symlink escapes.
Generates artifact-index.json with SHA256 checksums (self-excluded).
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import tempfile
from typing import Any, BinaryIO, Dict, Iterator, List, Sequence, Tuple
import uuid

from .models import ArtifactEntry
from .runtime import RuntimeSnapshot, sha256_file


# Strict allowlist of relative glob patterns within snapshot.run_dir
ALLOWED_ARTIFACT_PATTERNS: Sequence[str] = (
    "identity.json",
    "started.json",
    "result.json",
    "launcher.log",
    "evidence/live-context.json",
    "evidence/*/live-context.json",
    "evidence/context.json",
    "evidence/*/context.json",
    "evidence/testermint.log",
    "evidence/*/testermint.log",
    "evidence/a9-release/build-manifest.json",
    "evidence/*/a9-release/build-manifest.json",
    "evidence/a9-release/*.wasm",
    "evidence/*/a9-release/*.wasm",
    "evidence/build.log",
    "evidence/*/build.log",
    "evidence/report.json",
    "evidence/*/report.json",
    "evidence/go-boundary/report.json",
    "evidence/*/go-boundary/report.json",
    "evidence/go-boundary/build.log",
    "evidence/*/go-boundary/build.log",
    "evidence/go-boundary/raw/go-test.json",
    "evidence/*/go-boundary/raw/go-test.json",
    "evidence/go-boundary/raw/exit-code",
    "evidence/*/go-boundary/raw/exit-code",
    # The producer (scripts/run_a8_go_boundary.py) exports the buildx stage
    # straight into ``<task evidence>/raw``, one level above the patterns above,
    # so the two files the catalog declares mandatory for GO_BOUNDARY
    # (``raw/go-test.json``, ``raw/exit-code``) were never copied into the
    # suite and the reporter could only ever report them as missing. These
    # patterns name exactly those two declared files -- no directory is opened
    # up, and the machine-readable test output stops being dropped.
    "evidence/raw/go-test.json",
    "evidence/*/raw/go-test.json",
    "evidence/raw/exit-code",
    "evidence/*/raw/exit-code",
    "evidence/abi.json",
    "evidence/*/abi.json",
    "evidence/a8_query_boundary.wasm",
    "evidence/*/a8_query_boundary.wasm",
    "evidence/wasm-build.log",
    "evidence/*/wasm-build.log",
    "evidence/node-probe.log",
    "evidence/*/node-probe.log",
    "evidence/ct-*.log",
    "evidence/*/ct-*.log",
    "junit/TEST-*.xml",
    "cleanup-evidence/ownership.json",
    "cleanup-evidence/completed.json",
    "cleanup-evidence/container-logs/*.log",
    # Immutable-source evidence written by ``scripts/a8_acceptance.py
    # run-live`` into the task evidence directory (contract §4). Each pattern
    # names a declared file or a declared single-level directory of one kind;
    # nothing broader is opened up. ``source-immutability.json``,
    # ``external-harness/harness-inputs.json`` and
    # ``network/network-manifest.json`` are hashed into ``live-context.json``
    # and re-hashed by the verifier, so dropping them here would turn every
    # live task into INCOMPLETE.
    "evidence/source-immutability.json",
    "evidence/*/source-immutability.json",
    "evidence/external-harness/api-compat.json",
    "evidence/*/external-harness/api-compat.json",
    "evidence/external-harness/upstream-classpath.log",
    "evidence/*/external-harness/upstream-classpath.log",
    "evidence/external-harness/testermint-classpath.txt",
    "evidence/*/external-harness/testermint-classpath.txt",
    "evidence/external-harness/testermint-classpath.txt.json",
    "evidence/*/external-harness/testermint-classpath.txt.json",
    "evidence/external-harness/harness-inputs.json",
    "evidence/*/external-harness/harness-inputs.json",
    "evidence/external-harness/harness-build.log",
    "evidence/*/external-harness/harness-build.log",
    "evidence/testermint-junit/*.xml",
    "evidence/*/testermint-junit/*.xml",
    "evidence/network/network-manifest.json",
    "evidence/*/network/network-manifest.json",
    "evidence/genesis/genesis-before-b3.json",
    "evidence/*/genesis/genesis-before-b3.json",
    "evidence/genesis/genesis-after-b3.json",
    "evidence/*/genesis/genesis-after-b3.json",
    "evidence/genesis/genesis-final.json",
    "evidence/*/genesis/genesis-final.json",
    "evidence/genesis/b3-provision.json",
    "evidence/*/genesis/b3-provision.json",
    "evidence/genesis/b3-genesis-verification.json",
    "evidence/*/genesis/b3-genesis-verification.json",
    "evidence/container-control/*.json",
    "evidence/*/container-control/*.json",
)

# Explicit denylist to guard against accidental sensitive leak
DENIED_NAMES: Sequence[str] = (
    ".git",
    "node_key",
    "priv_validator_key",
    "keyring-test",
    "keyring",
    "id_rsa",
    "mnemonic",
    "target",
    ".gradle",
    "prod-local",
)


class CollectorSecurityError(RuntimeError):
    """Raised when an artifact violates path boundary or security policy."""

    code = "COLLECTOR_UNSAFE_PATH"
    exit_code = 1


class ArtifactIndexError(RuntimeError):
    """An existing index cannot be safely merged without losing evidence."""

    code = "ARTIFACT_INDEX_INVALID"
    exit_code = 1


def classify_artifact_kind(rel_path: str) -> str:
    if "junit" in rel_path:
        return "JUNIT"
    elif "cleanup-evidence" in rel_path:
        return "CLEANUP_DIAGNOSTIC"
    elif "go-boundary" in rel_path or "ct-" in rel_path or "abi" in rel_path:
        return "BOUNDARY_EVIDENCE"
    elif rel_path.endswith(".log"):
        return "LOG"
    elif rel_path.endswith(".json"):
        return "METADATA_JSON"
    elif rel_path.endswith(".wasm"):
        return "WASM_BINARY"
    return "EVIDENCE_RAW"


def is_path_safe(base_dir: Path, target_path: Path) -> bool:
    """Reject traversal, denied names, and every symlink below the trusted root."""
    try:
        relative = target_path.relative_to(base_dir)
        if base_dir.is_symlink() or ".." in relative.parts:
            return False
        current = base_dir
        for part in relative.parts:
            current = current / part
            lowered = part.casefold()
            denied_name = any(
                lowered == denied or lowered.startswith(denied + ".")
                for denied in DENIED_NAMES
            )
            if denied_name or current.is_symlink():
                return False
        target_path.resolve().relative_to(base_dir.resolve())
        return True
    except (ValueError, RuntimeError, OSError):
        return False


def _open_anchored_directory(directory: Path) -> int:
    """Open every ancestor from the filesystem root without following links."""
    absolute = Path(os.path.abspath(directory))
    flags = os.O_RDONLY | os.O_DIRECTORY
    current_fd = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            child_fd = os.open(
                component, flags | os.O_NOFOLLOW, dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_regular_source(base_dir: Path, target_path: Path) -> BinaryIO:
    """Open the checked source without following a substituted path component.

    The runner executes on POSIX. Windows unit tests retain the earlier path
    checks because Windows Python does not expose dir_fd/O_NOFOLLOW semantics.
    """
    relative = target_path.relative_to(base_dir)
    if os.name != "posix":
        return target_path.open("rb")

    directory_fd = _open_anchored_directory(base_dir)
    try:
        for component in relative.parts[:-1]:
            child_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child_fd
        source_fd = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            os.close(source_fd)
            raise CollectorSecurityError(f"Source is not a regular file: {target_path}")
        try:
            return os.fdopen(source_fd, "rb")
        except BaseException:
            os.close(source_fd)
            raise
    finally:
        os.close(directory_fd)


def _sha256_stream(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    source.seek(0)
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def sha256_checked_artifact(base_dir: Path, target_path: Path) -> str:
    """Hash one regular inode that still occupies its checked suite/runtime path."""
    if not is_path_safe(base_dir, target_path):
        raise CollectorSecurityError(f"Security: unsafe artifact path: {target_path}")
    try:
        with _open_regular_source(base_dir, target_path) as source:
            original = os.fstat(source.fileno())
            digest = _sha256_stream(source)
            if not is_path_safe(base_dir, target_path):
                raise CollectorSecurityError(f"Security: artifact path changed: {target_path}")
            current = os.stat(target_path, follow_symlinks=False)
            if not os.path.samestat(original, current):
                raise CollectorSecurityError(f"Security: artifact inode changed: {target_path}")
            return digest
    except OSError as exc:
        raise CollectorSecurityError(f"Security: artifact path could not be read: {target_path}: {exc}") from exc


@contextmanager
def _open_destination_directory(base_dir: Path, parent: Path) -> Iterator[int]:
    """Anchor destination operations to directories opened without symlinks."""
    relative = parent.relative_to(base_dir)
    directory_fd = _open_anchored_directory(base_dir)
    try:
        for component in relative.parts:
            try:
                os.mkdir(component, dir_fd=directory_fd)
            except FileExistsError:
                pass
            child_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child_fd
        yield directory_fd
    finally:
        os.close(directory_fd)


def _copy_artifact(
    source: BinaryIO, dest_path: Path, suite_dir: Path, rel_path: str, *, replace: bool = True,
) -> Tuple[str, int]:
    """Publish a checked source, returning the digest and size of copied bytes."""
    if os.name != "posix":
        # The actual runner is POSIX; keep the Windows unit-test path functional.
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=dest_path.parent, prefix=".artifact-", delete=False) as stream:
                temporary = Path(stream.name)
                src_sha = _sha256_stream(source)
                source.seek(0)
                shutil.copyfileobj(source, stream)
            dest_sha = sha256_file(temporary)
            if src_sha != dest_sha or _sha256_stream(source) != dest_sha:
                raise CollectorSecurityError(f"Integrity mismatch after copy: {rel_path} ({src_sha} != {dest_sha})")
            size = temporary.stat().st_size
            if replace:
                os.replace(temporary, dest_path)
            else:
                os.link(temporary, dest_path)
            return dest_sha, size
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    with _open_destination_directory(suite_dir, dest_path.parent) as directory_fd:
        name = dest_path.name
        try:
            existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise CollectorSecurityError(f"Security: rejected non-file destination: {dest_path}")
        if existing is not None and not replace:
            raise CollectorSecurityError(f"Security: destination already exists: {dest_path}")
        temporary_name = f".artifact-{uuid.uuid4().hex}"
        temp_fd = os.open(
            temporary_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory_fd,
        )
        original = os.fstat(temp_fd)
        try:
            with os.fdopen(temp_fd, "w+b") as stream:
                src_sha = _sha256_stream(source)
                source.seek(0)
                shutil.copyfileobj(source, stream)
                stream.flush()
                dest_sha = _sha256_stream(stream)
                if src_sha != dest_sha or _sha256_stream(source) != dest_sha:
                    raise CollectorSecurityError(
                        f"Integrity mismatch after copy: {rel_path} ({src_sha} != {dest_sha})"
                    )
                size = os.fstat(stream.fileno()).st_size
                os.fsync(stream.fileno())
                if not is_path_safe(suite_dir, dest_path):
                    raise CollectorSecurityError(f"Security: rejected changed destination: {dest_path}")
                current = os.stat(dest_path.parent, follow_symlinks=False)
                if not os.path.samestat(os.fstat(directory_fd), current):
                    raise CollectorSecurityError(f"Security: destination parent changed: {dest_path.parent}")
                candidate = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
                if not os.path.samestat(original, candidate):
                    raise CollectorSecurityError(f"Security: artifact temporary file changed: {dest_path}")
                if replace:
                    os.replace(
                        temporary_name, name,
                        src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    )
                else:
                    try:
                        os.link(
                            temporary_name, name,
                            src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except FileExistsError as exc:
                        raise CollectorSecurityError(
                            f"Security: destination appeared during recovery: {dest_path}"
                        ) from exc
            return dest_sha, size
        finally:
            try:
                current = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
                if os.path.samestat(original, current):
                    os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def copy_checked_runtime_file(
    source_root: Path, source_path: Path, destination_root: Path, destination: Path,
) -> None:
    """Copy one runtime file through checked source and destination descriptors."""
    if not is_path_safe(source_root, source_path):
        raise CollectorSecurityError(f"Security: unsafe runtime source: {source_path}")
    if not is_path_safe(destination_root, destination):
        raise CollectorSecurityError(f"Security: unsafe runtime destination: {destination}")
    try:
        source_stream = _open_regular_source(source_root, source_path)
        with source_stream:
            _copy_artifact(
                source_stream, destination, destination_root,
                destination.relative_to(destination_root).as_posix(),
            )
    except OSError as exc:
        raise CollectorSecurityError(
            f"Security: runtime source could not be copied: {source_path}: {exc}"
        ) from exc


def copy_runtime_artifact(
    source_root: Path, source_path: Path, suite_dir: Path, run_id: str, task_id: str,
) -> ArtifactEntry:
    """Recover one allowlisted runtime artifact without replacing suite evidence."""
    if not is_path_safe(source_root, source_path):
        raise CollectorSecurityError(f"Security: unsafe runtime artifact source: {source_path}")
    relative = source_path.relative_to(source_root).as_posix()
    destination = suite_dir / "runs" / run_id / relative
    if not is_path_safe(suite_dir, destination):
        raise CollectorSecurityError(f"Security: unsafe recovery destination: {destination}")
    try:
        source_stream = _open_regular_source(source_root, source_path)
        with source_stream:
            digest, size = _copy_artifact(
                source_stream, destination, suite_dir, relative, replace=False,
            )
    except OSError as exc:
        raise CollectorSecurityError(f"Security: runtime artifact could not be copied: {source_path}: {exc}") from exc
    return ArtifactEntry(
        relative_path=f"runs/{run_id}/{relative}",
        size_bytes=size,
        sha256=digest,
        run_id=run_id,
        task_id=task_id,
        artifact_kind=classify_artifact_kind(relative),
    )


def collect_snapshot_artifacts(
    snapshot: RuntimeSnapshot,
    task_id: str,
    output_suite_dir: Path,
) -> Tuple[List[ArtifactEntry], List[str]]:
    """Collects allowlisted artifacts from snapshot.run_dir into output_suite_dir/runs/<run_id>.

    Returns: (exported_artifact_entries, errors)
    """
    exported: List[ArtifactEntry] = []
    errors: List[str] = []

    suite_run_dir = output_suite_dir / "runs" / snapshot.run_id
    if not is_path_safe(output_suite_dir, suite_run_dir):
        return [], [f"Security: rejected unsafe destination: {suite_run_dir}"]
    try:
        if os.name == "posix":
            with _open_destination_directory(output_suite_dir, suite_run_dir):
                pass
        else:
            suite_run_dir.mkdir(parents=True, exist_ok=True)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as exc:
        return [], [f"Security: rejected changed destination: {suite_run_dir}: {exc}"]

    source_root = snapshot.run_dir
    if source_root.is_symlink():
        return [], [f"Security: rejected symlink source root: {source_root}"]

    # Find candidate files matching allowlist patterns
    candidate_files: List[Path] = []
    for pattern in ALLOWED_ARTIFACT_PATTERNS:
        matches = list(source_root.glob(pattern))
        candidate_files.extend(matches)

    # De-duplicate candidate files
    seen_rel: set[str] = set()

    for src_path in candidate_files:
        if not is_path_safe(source_root, src_path):
            errors.append(f"Security: rejected unsafe or symlink escape path: {src_path}")
            continue

        if not src_path.is_file():
            continue

        rel_path = src_path.relative_to(source_root).as_posix()
        if rel_path in seen_rel:
            continue
        seen_rel.add(rel_path)

        try:
            source_stream = _open_regular_source(source_root, src_path)
        except (OSError, CollectorSecurityError) as exc:
            errors.append(f"Security: rejected changed or unsafe source: {src_path}: {exc}")
            continue

        dest_path = suite_run_dir / rel_path
        with source_stream:
            if not is_path_safe(output_suite_dir, dest_path):
                errors.append(f"Security: rejected unsafe destination: {dest_path}")
                continue
            if dest_path.exists() and not dest_path.is_file():
                errors.append(f"Security: rejected non-file destination: {dest_path}")
                continue
            try:
                dest_sha, size = _copy_artifact(source_stream, dest_path, output_suite_dir, rel_path)
            except CollectorSecurityError as exc:
                errors.append(str(exc))
                continue
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as exc:
                errors.append(f"Security: rejected changed destination: {dest_path}: {exc}")
                continue

        entry = ArtifactEntry(
            relative_path=f"runs/{snapshot.run_id}/{rel_path}",
            size_bytes=size,
            sha256=dest_sha,
            run_id=snapshot.run_id,
            task_id=task_id,
            artifact_kind=classify_artifact_kind(rel_path),
        )
        exported.append(entry)

    return exported, errors


def update_artifact_index(
    suite_dir: Path,
    new_entries: Sequence[ArtifactEntry],
) -> Path:
    """Updates artifact-index.json in suite_dir with new entries.

    Does NOT hash artifact-index.json into itself.
    """
    index_file = suite_dir / "artifact-index.json"
    if not is_path_safe(suite_dir, index_file):
        raise CollectorSecurityError(f"Unsafe artifact index path: {index_file}")
    if os.name == "posix":
        try:
            with _open_destination_directory(suite_dir, suite_dir) as directory_fd:
                return _update_artifact_index(suite_dir, new_entries, directory_fd)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as exc:
            raise CollectorSecurityError(f"Unsafe artifact index directory: {suite_dir}: {exc}") from exc
    return _update_artifact_index(suite_dir, new_entries, None)


def validate_artifact_index_record(artifact: Any) -> str:
    """Apply the same schema and portable path rules to old and new entries."""
    if not isinstance(artifact, dict):
        raise ValueError("Artifact must be an object")
    key = artifact.get("relative_path")
    if not isinstance(key, str) or not key:
        raise ValueError("Artifact is missing its relative path")
    for field in ("sha256", "run_id", "task_id", "artifact_kind"):
        if not isinstance(artifact.get(field), str) or not artifact[field]:
            raise ValueError(f"Artifact is missing a non-empty {field}")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", artifact["sha256"]):
        raise ValueError("Artifact sha256 must contain 64 hexadecimal characters")
    if type(artifact.get("size_bytes")) is not int or artifact["size_bytes"] < 0:
        raise ValueError("Artifact size_bytes must be a non-negative integer")
    posix_path = PurePosixPath(key)
    windows_path = PureWindowsPath(key)
    if (
        key == "." or "\\" in key or posix_path.is_absolute()
        or windows_path.drive or ".." in posix_path.parts
        or posix_path.as_posix() != key
    ):
        raise ValueError("Artifact path must be a normalized portable relative file path")
    return key


def _update_artifact_index(
    suite_dir: Path,
    new_entries: Sequence[ArtifactEntry],
    directory_fd: int | None,
) -> Path:
    index_file = suite_dir / "artifact-index.json"
    existing_index: Dict[str, Dict[str, Any]] = {}

    index_mode = 0o644
    if directory_fd is not None:
        try:
            index_fd = os.open(index_file.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                               dir_fd=directory_fd)
        except FileNotFoundError:
            index_fd = None
        if index_fd is not None:
            with os.fdopen(index_fd, "r", encoding="utf-8") as stream:
                index_stat = os.fstat(stream.fileno())
                if not stat.S_ISREG(index_stat.st_mode):
                    raise CollectorSecurityError(f"Unsafe artifact index file: {index_file}")
                index_mode = stat.S_IMODE(index_stat.st_mode)
                raw_text = stream.read()
        else:
            raw_text = None
    else:
        raw_text = index_file.read_text(encoding="utf-8") if index_file.is_file() else None
        if raw_text is not None:
            index_mode = stat.S_IMODE(index_file.stat().st_mode)

    if raw_text is not None:
        try:
            raw = json.loads(raw_text)
            if not isinstance(raw, dict) or not isinstance(raw.get("artifacts"), list):
                raise ValueError("Expected an artifacts array")
            if raw.get("schema_version") != "1.0.0":
                raise ValueError("Unsupported or missing artifact index schema_version")
            if type(raw.get("total_artifacts")) is not int or raw["total_artifacts"] != len(raw["artifacts"]):
                raise ValueError("Artifact count does not match the artifacts array")
            for artifact in raw["artifacts"]:
                key = validate_artifact_index_record(artifact)
                if key in existing_index:
                    raise ValueError("Duplicate artifact path")
                existing_index[key] = artifact
        except (ValueError, OSError) as exc:
            raise ArtifactIndexError(f"Cannot merge {index_file}: {exc}") from exc

    seen_new: set[str] = set()
    for entry in new_entries:
        try:
            record = entry.to_dict()
            key = validate_artifact_index_record(record)
            if key in seen_new:
                raise ValueError("Duplicate new artifact path")
            seen_new.add(key)
        except (AttributeError, ValueError, TypeError) as exc:
            raise ArtifactIndexError(f"Cannot add entry to {index_file}: {exc}") from exc
        existing_index[key] = record

    sorted_list = [existing_index[k] for k in sorted(existing_index.keys())]
    doc = {
        "schema_version": "1.0.0",
        "total_artifacts": len(sorted_list),
        "artifacts": sorted_list,
    }
    # Evidence indexes are exported for host-side review. Preserve an existing
    # access mode; a new index must remain readable outside a root-run container.
    content = json.dumps(doc, indent=2) + "\n"
    if directory_fd is None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=suite_dir,
                                             prefix=".artifact-index-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                os.fchmod(stream.fileno(), index_mode)
            os.replace(temporary, index_file)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return index_file

    temporary_name = f".artifact-index-{uuid.uuid4().hex}"
    temp_fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o600, dir_fd=directory_fd)
    original = os.fstat(temp_fd)
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), index_mode)
            os.fsync(stream.fileno())
            if not is_path_safe(suite_dir, index_file):
                raise CollectorSecurityError(f"Unsafe artifact index path: {index_file}")
            current = os.stat(suite_dir, follow_symlinks=False)
            if not os.path.samestat(os.fstat(directory_fd), current):
                raise CollectorSecurityError(f"Artifact index directory changed: {suite_dir}")
            candidate = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
            if not os.path.samestat(original, candidate):
                raise CollectorSecurityError(f"Artifact index temporary file changed: {suite_dir}")
            os.replace(temporary_name, index_file.name,
                       src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        try:
            current = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
            if os.path.samestat(original, current):
                os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
    return index_file
