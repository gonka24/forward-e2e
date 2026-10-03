"""Tying the contracts a run deployed back to the contracts it built.

One function, two callers
-------------------------
The executor applies this check while the evidence is fresh and turns a
mismatch into a failed run. The offline reader applies it again later, from the
stored documents, when ``report`` or ``recover`` grades the same run. If the two
had separate implementations, an offline report could disagree with the run it
is reporting on. Preventing that disagreement is the point of sharing this check.

The function therefore *reports* rather than raises: deciding what a mismatch
means belongs to the caller, not to the comparison.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..suite import verifier as _verifier

#: How the harness names the production contracts inside
#: ``source.a9_contract_sha256``, mapped to the build-manifest role of the same
#: artefact.
DEPLOYED_CONTRACT_ROLES = {
    "deal": "a9-release/wasm/marketplace_deal.wasm",
    "factory": "a9-release/wasm/marketplace_factory.wasm",
}

#: The role under which the release manifest itself is recorded.
A9_MANIFEST_ROLE = "a9-release/build-manifest.json"

#: Prefix identifying production release Wasm artefacts.
A9_RELEASE_ROLE_PREFIX = "a9-release/wasm/"

#: The evidence states a deployment check can end in.
MATCHED = "MATCHED"
INCOMPLETE = "INCOMPLETE"
NOT_REPORTED = "NOT_REPORTED"
MISMATCHED = "MISMATCHED"


def verify_deployed_artifacts(
    payload: Mapping[str, Any],
    *,
    built_wasm: Sequence[Mapping[str, Any]],
    expected_manifest_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare what a live context says it deployed with what the build produced.

    ``payload`` is a parsed ``live-context.json``. Absence is never agreement: a
    build that handed over production artefacts must be answered with their
    hashes, and a silent evidence file is ``INCOMPLETE`` rather than a pass.
    """
    source = payload.get("source") or {}
    if not isinstance(source, Mapping):
        source = {}
    built: Dict[str, List[Any]] = {}
    for item in built_wasm:
        role = item.get("role")
        if role:
            built.setdefault(str(role), []).append(item.get("sha256"))

    def is_sha256(value: Any) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None

    mismatches: List[Dict[str, Any]] = []
    missing: List[str] = []
    checked: Dict[str, str] = {}

    reported_manifest_sha = source.get("a9_manifest_sha256")
    has_production_wasm = any(role in built for role in DEPLOYED_CONTRACT_ROLES.values())
    if has_production_wasm and not expected_manifest_sha256:
        missing.append(A9_MANIFEST_ROLE)
    elif expected_manifest_sha256:
        if not is_sha256(expected_manifest_sha256):
            missing.append(A9_MANIFEST_ROLE)
        elif not isinstance(reported_manifest_sha, str) or not reported_manifest_sha:
            missing.append(A9_MANIFEST_ROLE)
        else:
            checked["a9_manifest_sha256"] = reported_manifest_sha
            if reported_manifest_sha != expected_manifest_sha256:
                mismatches.append(
                    {
                        "artefact": A9_MANIFEST_ROLE,
                        "built": expected_manifest_sha256,
                        "deployed": reported_manifest_sha,
                    }
                )

    reported = source.get("a9_contract_sha256")
    reported_map = reported if isinstance(reported, Mapping) else {}
    for name, role in DEPLOYED_CONTRACT_ROLES.items():
        if role not in built:
            # This build did not produce that contract, so there is nothing to
            # tie the evidence to.
            continue
        built_values = built[role]
        if len(built_values) != 1 or not is_sha256(built_values[0]):
            # A recorded contract without a build hash is missing comparison
            # evidence, and duplicate roles cannot identify one artifact.
            missing.append(role)
            continue
        built_sha = built_values[0]
        deployed_sha = reported_map.get(name)
        if not isinstance(deployed_sha, str) or not deployed_sha:
            missing.append(role)
            continue
        checked[role] = deployed_sha
        if deployed_sha != built_sha:
            mismatches.append(
                {"artefact": role, "built": built_sha, "deployed": deployed_sha}
            )

    if mismatches:
        status = MISMATCHED
    elif missing:
        status = INCOMPLETE
    elif checked:
        status = MATCHED
    else:
        status = NOT_REPORTED
    return {
        "checked": checked,
        "missing": missing,
        "mismatches": mismatches,
        "status": status,
    }


#: Name and schemas of the harness' per-task immutability evidence
#: (``scripts/acceptance_harness.py run-live``), relative to the task's evidence
#: dir. Re-exported from the suite verifier, which owns the one implementation.
SOURCE_IMMUTABILITY_FILENAME = _verifier.SOURCE_IMMUTABILITY_FILENAME
SOURCE_IMMUTABILITY_SET_SCHEMA = _verifier.SOURCE_IMMUTABILITY_SET_SCHEMA
SOURCE_IMMUTABILITY_RECORD_SCHEMA = _verifier.SOURCE_IMMUTABILITY_RECORD_SCHEMA
#: Root labels the harness writes, mapped to the lock role they describe.
SOURCE_IMMUTABILITY_ROOTS = {"gonka": "gonka", "marketplace": "contracts"}


def verify_source_immutability_evidence(
    document: Optional[bytes],
    payload: Optional[Mapping[str, Any]],
    *,
    gonka_sha: str,
    marketplace_sha: Optional[str],
    network_manifest: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Check one task's ``source-immutability.json`` against the plan and its live context.

    ``document`` is the raw file (``None`` when absent) and ``payload`` the
    parsed ``live-context.json`` of the same task. Returns ``{"status":
    MATCHED|INCOMPLETE|MISMATCHED, "document_sha256", "missing",
    "mismatches"}``.

    Why a delegate: the suite verifier grades the same file while reporting a
    suite, and the executor / offline reader grade it while deciding a run.
    Two implementations of one rule would drift, and the weaker one would
    decide some verdicts (AGENTS.md, "house style"). The rule therefore lives
    once, in :func:`forward_e2e.suite.verifier.check_source_immutability_document`; the
    E2E layer may import the suite runner, never the reverse.

    Absence is never agreement: a missing or unreadable document, a missing
    root or a missing verdict is ``INCOMPLETE``; anything that says the
    snapshot changed, names another commit, or carries a field of the retired
    prepared-build model is ``MISMATCHED``.
    """
    return _verifier.check_source_immutability_document(
        document, payload, gonka_sha=gonka_sha, marketplace_sha=marketplace_sha,
        network_manifest=network_manifest,
    )

__all__ = [
    "A9_MANIFEST_ROLE",
    "A9_RELEASE_ROLE_PREFIX",
    "DEPLOYED_CONTRACT_ROLES",
    "INCOMPLETE",
    "MATCHED",
    "MISMATCHED",
    "NOT_REPORTED",
    "SOURCE_IMMUTABILITY_FILENAME",
    "SOURCE_IMMUTABILITY_ROOTS",
    "SOURCE_IMMUTABILITY_SET_SCHEMA",
    "verify_deployed_artifacts",
    "verify_source_immutability_evidence",
]
