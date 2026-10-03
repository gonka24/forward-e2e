"""Unified delivery lifecycle service for the E2E runner.

This module is the single owner of:
- Initiating delivery attempts (recording IN_PROGRESS status).
- Safe pre-export destination marking.
- Exporting packages with atomic publication of individual files.
- Finalizing attempt records and updating delivery manifests.
- Safe failure recording and ledger preservation without masking primary errors.

Separation of Concerns:
- Immutable execution manifests and sealed evidence are preserved intact.
- Staged delivery ledger (``<stage>/delivery.json``) maintains the authoritative
  chronological history of all attempts (initial and subsequent recoveries).
- Destination delivery manifest (``<destination>/delivery.json``) reflects the
  attempt history and the delivery status specific to that destination.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from .errors import E2EError, ExportConflict, PathSafetyError
from .file_safety import (
    atomic_write_bytes, ensure_directory_safe, open_checked_directory,
    read_checked_file_bytes,
)
from .runlock import (
    DELIVERY_MANIFEST_FILENAME,
    DeliveryAttempt,
    DeliveryManifest,
    DeliveryStatus,
    utc_now_iso,
)
from .runpackage import REQUIRED_DOCUMENTS, assert_export_paths_disjoint, export_run_package
from .sources import assert_no_symlinks_in_path


class DeliveryError(E2EError):
    """Base error for delivery operations."""

    code = "DELIVERY_ERROR"


class CorruptDeliveryLedgerError(DeliveryError):
    """The existing delivery ledger in staging is corrupted or unreadable."""

    code = "DELIVERY_LEDGER_CORRUPTED"


class DeliveryBusyError(DeliveryError):
    """Another delivery owns this staging package's ledger."""

    code = "DELIVERY_BUSY"


@contextmanager
def _delivery_lock(stage_dir: Path) -> Iterator[None]:
    """Serialize the whole ledger transaction, including initial delivery.

    Lock the durable directory inode so no mutable lock file enters the exported
    evidence. Closing the descriptor releases the lock even after an exception.
    Offline recovery does not acquire the Docker runtime lock.
    """
    assert_no_symlinks_in_path(stage_dir, what="Delivery staging directory")
    with open_checked_directory(stage_dir, what="Delivery staging directory") as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeliveryBusyError(
                "Another delivery is active for this run; retry after it finishes.",
                {"stage_dir": str(stage_dir)},
            ) from exc
        yield


def _assert_destination_identity(stage_dir: Path, run_dir: Path, run_id: str) -> None:
    """Do not replace a foreign ledger even when export will reject its files.

    Compare every identity document present in a partial destination. Matching
    run IDs alone cannot establish that two packages have the same provenance.
    """
    for name in REQUIRED_DOCUMENTS:
        destination = run_dir / name
        source = stage_dir / name
        assert_no_symlinks_in_path(destination, what="Delivery destination identity")
        if destination.exists():
            assert_no_symlinks_in_path(source, what="Delivery source identity")
            if (
                not destination.is_file()
                or not source.is_file()
                or read_checked_file_bytes(destination) != read_checked_file_bytes(source)
            ):
                raise ExportConflict(
                    "Destination contains a different run package.",
                    {"path": str(destination)},
                )
    status_path = run_dir / "status.json"
    assert_no_symlinks_in_path(status_path, what="Delivery destination status")
    if status_path.exists():
        try:
            status = json.loads(read_checked_file_bytes(status_path).decode("utf-8"))
            if not isinstance(status, dict) or status.get("run_id") != run_id:
                raise ValueError("status belongs to another run")
        except (E2EError, OSError, UnicodeDecodeError, ValueError) as exc:
            raise ExportConflict(
                "Destination contains an unidentified run status.",
                {"path": str(status_path)},
            ) from exc
    ledger = run_dir / DELIVERY_MANIFEST_FILENAME
    assert_no_symlinks_in_path(ledger, what="Delivery destination ledger")
    if ledger.exists():
        try:
            existing = DeliveryManifest.from_dict(
                json.loads(read_checked_file_bytes(ledger).decode("utf-8"))
            )
        except Exception as exc:
            raise ExportConflict("Destination delivery ledger cannot be identified.", {"path": str(ledger)}) from exc
        if existing.run_id != run_id:
            raise ExportConflict("Destination delivery ledger belongs to another run.", {"path": str(ledger)})


@dataclass
class DeliveryResult:
    """Result of a delivery attempt (initial run or recovery)."""

    success: bool
    stage_dir: Path
    run_dir: Path
    graded_root: Path
    manifest: Optional[DeliveryManifest] = None
    export_failure: Optional[BaseException] = None
    ledger_failure: Optional[BaseException] = None


class DeliveryService:
    """Service governing delivery attempts and ledger transitions."""

    @classmethod
    def _execute_attempt(
        cls,
        stage_dir: Path,
        run_dir: Path,
        delivery_m: DeliveryManifest,
        attempt: DeliveryAttempt,
        *,
        is_recovery: bool,
        emit: Optional[Callable[[str], None]] = None,
        exporter: Optional[Callable[..., Any]] = None,
    ) -> DeliveryResult:
        """Core attempt executor shared by initial delivery and recovery."""
        say = emit or (lambda message: None)
        delivery_stage_path = stage_dir / DELIVERY_MANIFEST_FILENAME
        graded_root = stage_dir
        export_failure: Optional[BaseException] = None
        ledger_failure: Optional[BaseException] = None
        destination_marked = False

        what = "Recovery run directory" if is_recovery else "Run output directory"
        try:
            assert_no_symlinks_in_path(run_dir, what=what)
            ensure_directory_safe(run_dir, what=what)
            _assert_destination_identity(stage_dir, run_dir, delivery_m.run_id)

            dest_in_progress = copy.deepcopy(delivery_m)
            dest_in_progress.status = DeliveryStatus.IN_PROGRESS
            dest_in_progress.write(run_dir / DELIVERY_MANIFEST_FILENAME, package_root=run_dir)
            destination_marked = True

            actual_exporter = exporter if exporter is not None else export_run_package
            actual_exporter(stage_dir, run_dir, emit=say)

            now_done = utc_now_iso()
            attempt.status = DeliveryStatus.COMPLETED
            attempt.completed_at_utc = now_done
            attempt.error = None

            if not is_recovery:
                delivery_m.status = DeliveryStatus.COMPLETED

            try:
                delivery_m.write(delivery_stage_path, package_root=stage_dir)
            except Exception as stage_err:  # noqa: BLE001 - preserve the ledger error in DeliveryResult
                ledger_failure = stage_err
                say(f"WARNING: Failed to update delivery ledger in staging: {stage_err}")
                # Keep the destination's durable IN_PROGRESS marker. A later
                # offline report must not call this two-ledger delivery PASSED.
                return DeliveryResult(
                    success=False,
                    stage_dir=stage_dir,
                    run_dir=run_dir,
                    graded_root=run_dir,
                    manifest=dest_in_progress,
                    ledger_failure=ledger_failure,
                )

            dest_delivery = copy.deepcopy(delivery_m)
            dest_delivery.status = DeliveryStatus.COMPLETED
            dest_delivery.write(run_dir / DELIVERY_MANIFEST_FILENAME, package_root=run_dir)

            return DeliveryResult(
                success=True,
                stage_dir=stage_dir,
                run_dir=run_dir,
                graded_root=run_dir,
                manifest=dest_delivery,
                ledger_failure=ledger_failure,
            )
        except Exception as exc:  # noqa: BLE001 - export failure must not crash runner
            export_failure = exc
            if not is_recovery:
                say(
                    f"WARNING: the run could not be exported to {run_dir}: {exc}. "
                    "The durable copy is intact; `recover` can export it again."
                )
            now_fail = utc_now_iso()
            attempt.status = DeliveryStatus.FAILED
            attempt.completed_at_utc = now_fail
            attempt.error = str(exc)

            if not is_recovery:
                delivery_m.status = DeliveryStatus.FAILED

            try:
                delivery_m.write(delivery_stage_path, package_root=stage_dir)
            except Exception as stage_err:  # noqa: BLE001 - ledger error must not mask export failure
                ledger_failure = stage_err
                say(f"WARNING: Failed to update delivery ledger in staging: {stage_err}")

            try:
                assert_no_symlinks_in_path(run_dir, what=what)
                if destination_marked and (run_dir / DELIVERY_MANIFEST_FILENAME).is_file():
                    dest_fail = copy.deepcopy(delivery_m)
                    dest_fail.status = DeliveryStatus.FAILED
                    dest_fail.write(run_dir / DELIVERY_MANIFEST_FILENAME, package_root=run_dir)
            except Exception:
                pass

            return DeliveryResult(
                success=False,
                stage_dir=stage_dir,
                run_dir=run_dir,
                graded_root=graded_root,
                manifest=delivery_m,
                export_failure=export_failure,
                ledger_failure=ledger_failure,
            )

    @classmethod
    def deliver_initial(
        cls,
        stage_dir: Path,
        run_dir: Path,
        delivery: DeliveryManifest,
        *,
        emit: Optional[Callable[[str], None]] = None,
        exporter: Optional[Callable[..., Any]] = None,
    ) -> DeliveryResult:
        """Execute the initial delivery of a freshly executed run."""
        assert_export_paths_disjoint(stage_dir, run_dir)
        with _delivery_lock(stage_dir):
            if not delivery.attempts:
                delivery.attempts.append(
                    DeliveryAttempt(
                        attempt_number=1,
                        command="run",
                        started_at_utc=utc_now_iso(),
                        destination=str(run_dir),
                        status=DeliveryStatus.IN_PROGRESS,
                    )
                )
            attempt = delivery.attempts[0]
            # Persist the initial marker under the same lock as finalization;
            # otherwise it could overwrite an active recovery's ledger.
            delivery.write(stage_dir / DELIVERY_MANIFEST_FILENAME, package_root=stage_dir)
            return cls._execute_attempt(
                stage_dir,
                run_dir,
                delivery,
                attempt,
                is_recovery=False,
                emit=emit,
                exporter=exporter,
            )

    @classmethod
    def deliver_recovery(
        cls,
        stage_dir: Path,
        run_dir: Path,
        *,
        suite_id: str,
        emit: Optional[Callable[[str], None]] = None,
        exporter: Optional[Callable[..., Any]] = None,
    ) -> DeliveryResult:
        """Execute a recovery delivery from an existing durable staging directory."""
        assert_export_paths_disjoint(stage_dir, run_dir)
        with _delivery_lock(stage_dir):
            delivery_stage_path = stage_dir / DELIVERY_MANIFEST_FILENAME

            assert_no_symlinks_in_path(run_dir, what="Recovery run directory")

            if delivery_stage_path.is_symlink():
                raise PathSafetyError(
                    f"Delivery manifest in staging is a symlink: {delivery_stage_path}",
                    {"path": str(delivery_stage_path)},
                )

            delivery_m: Optional[DeliveryManifest] = None
            if delivery_stage_path.is_file():
                try:
                    delivery_m = DeliveryManifest.from_dict(
                        json.loads(read_checked_file_bytes(delivery_stage_path).decode("utf-8"))
                    )
                except Exception as exc:
                    raise CorruptDeliveryLedgerError(
                        f"Existing delivery manifest in staging is unreadable or corrupted: {exc}",
                        {"path": str(delivery_stage_path), "error": str(exc)},
                    ) from exc

            if delivery_m is None:
                delivery_m = DeliveryManifest(run_id=suite_id, status=DeliveryStatus.FAILED)
            elif delivery_m.run_id != suite_id:
                raise CorruptDeliveryLedgerError(
                    "Staging delivery ledger belongs to another run.",
                    {"path": str(delivery_stage_path), "expected_run_id": suite_id,
                     "ledger_run_id": delivery_m.run_id},
                )
            attempt = DeliveryAttempt(
                attempt_number=len(delivery_m.attempts) + 1,
                command="recover",
                started_at_utc=utc_now_iso(),
                destination=str(run_dir),
                status=DeliveryStatus.IN_PROGRESS,
            )
            delivery_m.attempts.append(attempt)

            # Record IN_PROGRESS in staging
            delivery_m.write(delivery_stage_path, package_root=stage_dir)

            result = cls._execute_attempt(
                stage_dir,
                run_dir,
                delivery_m,
                attempt,
                is_recovery=True,
                emit=emit,
                exporter=exporter,
            )
            # status.json is operational and changes during a run, so the bulk
            # exporter never treats it as sealed evidence. A recovered package
            # needs its own final status alongside the delivery ledger.
            status_path = stage_dir / "status.json"
            if result.success and status_path.is_file():
                try:
                    status = json.loads(read_checked_file_bytes(status_path).decode("utf-8"))
                    status["export_status"] = "COMPLETED"
                    status["updated_at_utc"] = utc_now_iso()
                    content = (json.dumps(status, indent=2, sort_keys=True) + "\n").encode("utf-8")
                    atomic_write_bytes(run_dir / "status.json", content, package_root=run_dir)
                    atomic_write_bytes(status_path, content, package_root=stage_dir)
                except (OSError, ValueError, TypeError, E2EError) as exc:
                    (emit or (lambda _: None))(f"warning: recovered status could not be published: {exc}")
            return result
