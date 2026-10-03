"""Low-level safe atomic filesystem operations for the E2E runner.

This module is the single owner of safe temporary file allocation, path
safety verification, exclusive creation flags, symlink defense, and atomic
publication.

Two atomic publication operations are supported:
1. Byte writing (``atomic_write_bytes``): writes through an exclusive temporary
   sibling, replacing mutable documents by default. ``replace=False`` publishes
   immutable documents without overwriting existing entries.
2. File copying (``atomic_publish_file``): copies evidence or build artifacts
   with streaming SHA-256 verification and permission preservation. Existing
   destinations are replaced by default; replace=False refuses existing entries.

Destination operations are anchored to open directory descriptors on the POSIX
runner. Replacing a parent path cannot redirect publication or cleanup. These
helpers do not prevent another writer from moving or modifying directory entries
it owns; detected substitutions are rejected before publication.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import stat
from typing import BinaryIO, Iterator, Optional, Union
import uuid

from .errors import ExportConflict, IntegrityError, PathSafetyError
from .sources import SYSTEM_SYMLINK_ALLOWLIST, assert_no_symlinks_in_path

CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Calculate the SHA-256 digest of a file, streaming in 1MB chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _regular_source_file(source: Path) -> Iterator[tuple[BinaryIO, os.stat_result]]:
    """Read one regular source inode without following a replaced path component."""
    with _destination_directory(source, create=False, what="Export source file") as directory_fd:
        try:
            fd = os.open(
                source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise PathSafetyError("Export source file is a symlink or unsafe", {"path": str(source)}) from exc
            raise
        try:
            source_stat = os.fstat(fd)
            if not stat.S_ISREG(source_stat.st_mode):
                raise PathSafetyError("Export source is not a regular file", {"path": str(source)})
            with os.fdopen(fd, "rb", closefd=False) as stream:
                yield stream, source_stat
        finally:
            os.close(fd)


def sha256_checked_file(source: Union[str, Path]) -> str:
    """Hash a regular file only while its checked pathname names that inode."""
    source_path = Path(source)
    digest = hashlib.sha256()
    with _regular_source_file(source_path) as (stream, original):
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
        _assert_checked_source_unchanged(source_path, stream, original)
    return digest.hexdigest()


def sha256_size_checked_file(source: Union[str, Path]) -> tuple[str, int]:
    """Hash and size the same checked regular inode in one read."""
    source_path = Path(source)
    digest = hashlib.sha256()
    size = 0
    with _regular_source_file(source_path) as (stream, original):
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            size += len(chunk)
        _assert_checked_source_unchanged(source_path, stream, original)
        if size != original.st_size:
            raise IntegrityError("File size changed while reading", {"path": str(source_path)})
    return digest.hexdigest(), size


def size_checked_file(source: Union[str, Path]) -> int:
    """Read a regular file's size while its checked pathname names that inode."""
    source_path = Path(source)
    with _regular_source_file(source_path) as (stream, original):
        _assert_checked_source_unchanged(source_path, stream, original)
        return original.st_size


def read_checked_file_bytes(source: Union[str, Path]) -> bytes:
    """Read a package document from one regular inode and reject path changes."""
    source_path = Path(source)
    with _regular_source_file(source_path) as (stream, original):
        data = stream.read()
        _assert_checked_source_unchanged(source_path, stream, original)
        return data


def _assert_checked_source_unchanged(source: Path, stream: BinaryIO, original: os.stat_result) -> None:
    after = os.fstat(stream.fileno())
    if (
        after.st_size != original.st_size
        or after.st_mtime_ns != original.st_mtime_ns
        or after.st_ctime_ns != original.st_ctime_ns
    ):
        raise IntegrityError("File changed while reading", {"path": str(source)})
    with _destination_directory(source, create=False, what="Checked file source") as directory_fd:
        try:
            current = os.stat(source.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise PathSafetyError("File disappeared while reading", {"path": str(source)}) from exc
        if not os.path.samestat(original, current):
            raise PathSafetyError("File was replaced while reading", {"path": str(source)})


def _verify_package_root_containment(
    target: Path,
    package_root: Optional[Path],
    *,
    what: str = "Destination",
) -> None:
    """Ensure target resolves inside package_root without escaping."""
    if package_root is None:
        return
    root_path = Path(package_root)
    assert_no_symlinks_in_path(root_path, what=f"{what} package root")
    try:
        target.resolve().relative_to(root_path.resolve())
    except ValueError as exc:
        raise PathSafetyError(
            f"{what} escapes package root",
            {"path": str(target), "root": str(root_path)},
        ) from exc


@contextmanager
def _destination_directory(target: Path, *, create: bool, what: str) -> Iterator[int]:
    """Walk from the filesystem root without following user-controlled symlinks."""
    absolute = Path(os.path.abspath(target))
    flags = os.O_RDONLY | os.O_DIRECTORY
    fd = os.open(absolute.anchor, flags)
    try:
        current = Path(absolute.anchor)
        try:
            for component in absolute.parent.parts[1:]:
                current /= component
                if create:
                    try:
                        os.mkdir(component, dir_fd=fd)
                    except FileExistsError:
                        pass
                # Match sources.py's existing exception for system aliases on macOS.
                nofollow = 0 if current in SYSTEM_SYMLINK_ALLOWLIST else os.O_NOFOLLOW
                child_fd = os.open(component, flags | nofollow, dir_fd=fd)
                os.close(fd)
                fd = child_fd
        except OSError as exc:
            raise PathSafetyError(
                f"{what} parent directory is missing or unsafe", {"path": str(target.parent)}
            ) from exc
        yield fd
    finally:
        os.close(fd)


def ensure_directory_safe(directory: Union[str, Path], *, what: str = "Export directory") -> Path:
    """Create and open every directory component without following symlinks."""
    path = Path(directory)
    with _destination_directory(path / ".directory-check", create=True, what=what):
        pass
    return path


@contextmanager
def open_checked_directory(directory: Union[str, Path], *, what: str) -> Iterator[int]:
    """Open an existing directory through checked ancestor descriptors."""
    path = Path(directory)
    with _destination_directory(path / ".directory-check", create=False, what=what) as fd:
        yield fd


def _entry_stat(directory_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _refuse_symlink(directory_fd: int, name: str, *, what: str) -> None:
    entry = _entry_stat(directory_fd, name)
    if entry is not None and stat.S_ISLNK(entry.st_mode):
        raise PathSafetyError(f"{what} is a symlink", {"name": name})


def _allocate_safe_temp_candidate(target: Path, directory_fd: int, *, what: str) -> tuple[int, str]:
    """Exclusively allocate a private sibling; never remove pre-existing entries."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _ in range(10):
        name = f".{target.name}.tmp.{uuid.uuid4().hex}"
        try:
            return os.open(name, flags, 0o600, dir_fd=directory_fd), name
        except FileExistsError:
            _refuse_symlink(directory_fd, name, what=f"Temporary {what.lower()} candidate")
        except OSError as exc:
            raise PathSafetyError(
                f"Cannot allocate temporary {what.lower()}", {"path": str(target.parent / name)}
            ) from exc
    raise PathSafetyError(
        f"Failed to allocate a unique temporary {what.lower()}", {"target": str(target)}
    )


@contextmanager
def _atomic_destination(
    target: Path, *, package_root: Optional[Union[str, Path]], what: str, replace: bool = True,
    mode: Optional[int] = None,
) -> Iterator[BinaryIO]:
    """Own every descriptor and publish only the temporary inode we allocated."""
    assert_no_symlinks_in_path(target, what=what)
    root = Path(package_root) if package_root is not None else None
    _verify_package_root_containment(target, root, what=what)
    with _destination_directory(target, create=True, what=what) as directory_fd:
        _refuse_symlink(directory_fd, target.name, what=what)
        fd, name = _allocate_safe_temp_candidate(target, directory_fd, what=what)
        original = os.fstat(fd)
        try:
            # Keep raw descriptor ownership here even if fdopen or source opening fails.
            with os.fdopen(fd, "wb", closefd=False) as stream:
                yield stream
                stream.flush()
                if mode is not None:
                    existing = _entry_stat(directory_fd, target.name) if replace else None
                    # Keep candidates private while writing. Published documents
                    # retain existing permissions, or use the caller's explicit mode.
                    final_mode = (
                        stat.S_IMODE(existing.st_mode)
                        if existing is not None and stat.S_ISREG(existing.st_mode)
                        else mode
                    )
                    os.fchmod(fd, final_mode)
                os.fsync(fd)
                # Reject a visible parent substitution; all mutations below still use
                # the original descriptor, so a later path swap cannot redirect them.
                with _destination_directory(target, create=False, what=what) as current_fd:
                    if not os.path.samestat(os.fstat(directory_fd), os.fstat(current_fd)):
                        raise PathSafetyError(f"{what} parent directory was replaced", {"path": str(target)})
                _verify_package_root_containment(target, root, what=what)
                entry = _entry_stat(directory_fd, name)
                if entry is None or not os.path.samestat(original, entry):
                    raise PathSafetyError(f"Temporary {what.lower()} was replaced", {"name": name})
                _refuse_symlink(directory_fd, target.name, what=what)
                if replace:
                    os.replace(name, target.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                else:
                    # A hard link publishes the complete sibling inode atomically
                    # and fails if another writer has already claimed the name.
                    try:
                        os.link(name, target.name, src_dir_fd=directory_fd,
                                dst_dir_fd=directory_fd, follow_symlinks=False)
                    except FileExistsError as exc:
                        raise ExportConflict(
                            "Destination appeared during publication", {"path": str(target)}
                        ) from exc
        finally:
            os.close(fd)
            # Cleanup is descriptor-relative too. Do not unlink a substituted entry.
            try:
                entry = _entry_stat(directory_fd, name)
                if entry is not None and os.path.samestat(original, entry):
                    os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass


def atomic_write_bytes(
    target: Union[str, Path],
    data: bytes,
    *,
    package_root: Optional[Union[str, Path]] = None,
    what: str = "Destination",
    replace: bool = True,
    mode: Optional[int] = None,
) -> Path:
    """Publish bytes atomically; replace=False refuses existing destinations.

    With mode set, preserve an existing regular file's permissions on replacement
    and use mode for new files. Otherwise publish with private permissions (0600).
    """
    dest_path = Path(target)
    with _atomic_destination(
        dest_path, package_root=package_root, what=what, replace=replace, mode=mode
    ) as stream:
        stream.write(data)
    return dest_path


def atomic_publish_file(
    source: Union[str, Path],
    destination: Union[str, Path],
    *,
    package_root: Optional[Union[str, Path]] = None,
    what: str = "Export destination file",
    replace: bool = True,
) -> Path:
    """Copy a file atomically with SHA-256 verification, mode and timestamp preservation.

    Replaces existing destinations unless replace=False, which atomically refuses
    collisions with ExportConflict. Failure before publication leaves the destination untouched.
    """
    src_path = Path(source)
    dest_path = Path(destination)
    digest = hashlib.sha256()
    with _atomic_destination(dest_path, package_root=package_root, what=what, replace=replace) as dst:
        with _regular_source_file(src_path) as (src, source_stat):
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
                digest.update(chunk)
            src.seek(0)
            expected = hashlib.sha256()
            for chunk in iter(lambda: src.read(CHUNK_SIZE), b""):
                expected.update(chunk)
            expected_hash = expected.hexdigest()
            actual_hash = digest.hexdigest()
            current_stat = os.fstat(src.fileno())
            with _destination_directory(src_path, create=False, what="Export source file") as directory_fd:
                try:
                    current_entry = os.stat(src_path.name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError as exc:
                    raise PathSafetyError("Export source was removed during copy", {"path": str(src_path)}) from exc
                if not os.path.samestat(source_stat, current_entry):
                    raise PathSafetyError("Export source was replaced during copy", {"path": str(src_path)})
            if (
                actual_hash != expected_hash
                or current_stat.st_size != source_stat.st_size
                or current_stat.st_mtime_ns != source_stat.st_mtime_ns
                or current_stat.st_ctime_ns != source_stat.st_ctime_ns
            ):
                raise IntegrityError(
                    f"Export integrity check failed for {src_path.name}",
                    {"expected": expected_hash, "actual": actual_hash},
                )
            # Flush before setting timestamps: subsequent buffered writes would change mtime.
            # Both metadata and bytes come from the opened source inode.
            dst.flush()
            os.fchmod(dst.fileno(), stat.S_IMODE(source_stat.st_mode))
            os.utime(dst.fileno(), ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    return dest_path
