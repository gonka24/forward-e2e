"""Shared ownership evidence validation for execution and offline reporting."""

from pathlib import Path
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .builder import verify_running_images
from .errors import BuildProvenanceError, IntegrityError
from .evidence import EvidenceRequirement
from .runlock import BuildManifest
from .sources import resolve_within


def verify_ownership_evidence(
    suite_dir: Path,
    *,
    requirements: Sequence[EvidenceRequirement],
    build: BuildManifest,
    runtime_external_images: Optional[Mapping[str, str]] = None,
    platform: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Read each native task's raw ownership record and verify its image IDs.

    A stored comparison summary cannot replace the files that establish which
    containers actually ran. Boundary tasks never acquire this obligation.
    """
    containers: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    for requirement in requirements:
        if not requirement.requires_live_context:
            continue
        relative = f"runs/{requirement.run_id}/cleanup-evidence/ownership.json"
        ownership = resolve_within(suite_dir, relative, what="task ownership evidence")
        if not ownership.is_file():
            missing.append({
                "task_id": requirement.task_id,
                "run_id": requirement.run_id,
                "expected_path": relative,
                "reason": "the task recorded no cleanup-evidence/ownership.json",
            })
            continue
        try:
            payload = json.loads(ownership.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise IntegrityError(
                "Task ownership evidence is unreadable",
                {"task_id": requirement.task_id, "path": relative},
            ) from exc
        if not isinstance(payload, Mapping):
            raise BuildProvenanceError(
                "Task ownership evidence must be a JSON object",
                {"task_id": requirement.task_id, "path": relative},
            )
        # Directory placement alone cannot bind a copied document to this task.
        if payload.get("run_id") != requirement.run_id:
            raise BuildProvenanceError(
                "Ownership evidence belongs to a different or unspecified task run",
                {"task_id": requirement.task_id, "path": relative, "field": "run_id",
                 "expected": requirement.run_id, "actual": payload.get("run_id")},
            )
        if payload.get("ownership_verified") is not True:
            raise BuildProvenanceError(
                "The producer did not confirm container ownership",
                {"task_id": requirement.task_id, "path": relative, "field": "ownership_verified"},
            )
        entries = payload.get("owned_containers")
        if not isinstance(entries, list) or not entries:
            raise BuildProvenanceError(
                "Native task ownership evidence must identify the containers that ran",
                {"task_id": requirement.task_id, "path": relative},
            )
        running = {}
        for index, entry in enumerate(entries):
            field = None
            reason = None
            if not isinstance(entry, Mapping):
                field, reason = "owned_containers", "entry must be an object"
            elif not isinstance(entry.get("name"), str) or not entry["name"].strip():
                field, reason = "name", "must be a nonempty string"
            elif not isinstance(entry.get("image"), str) or not entry["image"].strip():
                field, reason = "image", "must be a nonempty image ID"
            elif entry["name"] in running:
                field, reason = "name", "duplicate container name"
            if reason is not None:
                raise BuildProvenanceError(
                    "Every owned container must have a unique name and an image ID",
                    {"task_id": requirement.task_id, "path": relative,
                     "entry_index": index, "field": field, "reason": reason},
                )
            running[entry["name"]] = {
                "image": entry["image"],
                "image_reference": str(entry.get("image_reference") or ""),
            }
        containers.append(
            {
                "path": relative,
                "findings": verify_running_images(
                    expected_images=build.images,
                    running=running,
                    runtime_external_images=runtime_external_images,
                    runtime_dependencies=build.runtime_dependencies,
                    platform=platform,
                ),
            }
        )
    return containers, missing
