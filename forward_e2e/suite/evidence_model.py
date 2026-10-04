"""Which rules a piece of evidence was produced under, stated explicitly.

The problem this solves
-----------------------
Several pipelines have written a ``live-context.json`` that looks identical --
same ``schema_version``, same ``kind`` -- but whose fields mean different
things:

* **Legacy (overlay) runs.** The acceptance harness (then
  ``scripts/a8_acceptance.py``) ran against a chain binary that already
  existed. ``gonka_prepared_sha`` meant "the requested Gonka commit plus the
  marketplace test overlay", a *test tree* SHA.
* **E2E prepared-build runs (historical).** The E2E layer (then ``ops/a8/e2e``)
  checked out the pinned Gonka commit, committed the adapter's test-only
  patches on top of it and built the chain from *that* tree.
  ``gonka_prepared_sha`` was the commit the binary was compiled from. A PASS
  therefore described a tree nobody had selected.
* **E2E immutable-source runs (current).** Both product commits are
  materialised from Git objects and are never written to. The chain is built
  from the selected commit itself, the running binary must report exactly that
  commit, and the harness proves with ``source-immutability.json`` that neither
  snapshot changed from before the build until after the last scenario. There
  is no prepared commit at all.

The current runner grades only the immutable-source model. The two older
identifiers remain *recognisable* so that old packages can be read and
classified as historical evidence; recognising a declaration never makes it
acceptable as proof of an unmodified source tree.

Missing declarations and missing runtime facts never select weaker rules.

This module is pure data and policy selection. It reads nothing and writes
nothing.

The identifiers are repeated as literals in ``forward_e2e/execution/context.py``
and ``scripts/acceptance_harness.py`` (both must stay free of
``forward_e2e.suite`` imports); a change here must be made in all three places.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

#: Evidence produced by the legacy overlay pipeline
#: (``scripts/a8_acceptance.py`` against a pre-existing chain). Historical.
EVIDENCE_MODEL_LEGACY = "a8.evidence/legacy-overlay/1"

#: Evidence produced by an E2E run that built the chain from a prepared
#: (patched) commit. Historical: readable and classifiable, never gradable as
#: immutable-source proof.
EVIDENCE_MODEL_E2E = "a8.evidence/e2e-prepared-build/1"

#: Evidence produced by an E2E run that built and tested the selected commits
#: without modifying either source snapshot. The only gradable model.
EVIDENCE_MODEL_IMMUTABLE = "a8.evidence/e2e-immutable-source/2"

#: Identifiers that are recognised but only ever describe historical evidence.
HISTORICAL_EVIDENCE_MODELS = frozenset({EVIDENCE_MODEL_LEGACY, EVIDENCE_MODEL_E2E})

#: Recognized identifiers, including historical declarations rejected by grading.
KNOWN_EVIDENCE_MODELS = frozenset(
    {EVIDENCE_MODEL_LEGACY, EVIDENCE_MODEL_E2E, EVIDENCE_MODEL_IMMUTABLE}
)

#: The field a producer writes it into, in ``live-context.json`` under
#: ``source``, and at the top level of ``identity.json`` and
#: ``e2e-context.json``.
EVIDENCE_MODEL_FIELD = "evidence_model"

#: Provenance classification written into a graded outcome. It answers "what
#: kind of claim can this package make at all", independently of its verdict.
PROVENANCE_MODEL_IMMUTABLE = "immutable-source"
PROVENANCE_MODEL_HISTORICAL_PREPARED = "historical-prepared-build"


class EvidenceModelError(ValueError):
    """The evidence does not say, or says something this runner cannot grade."""

    def __init__(self, message: str, code: str = "EVIDENCE_MODEL_INVALID"):
        super().__init__(message)
        self.code = code


def declared_evidence_model(payload: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The model a document declares, or ``None`` if it declares nothing.

    A value that is present but unknown is never treated as "nothing": that
    would grade tomorrow's evidence by today's weakest rule.
    """
    if not isinstance(payload, Mapping):
        return None
    value = payload.get(EVIDENCE_MODEL_FIELD)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip()
    if text not in KNOWN_EVIDENCE_MODELS:
        raise EvidenceModelError(
            f"Unknown evidence model {text!r}. This runner can recognise "
            f"{sorted(KNOWN_EVIDENCE_MODELS)} and refuses to guess which rules "
            "produced this evidence.",
            code="EVIDENCE_MODEL_UNKNOWN",
        )
    return text


def resolve_evidence_model(
    *,
    declared: Optional[str],
) -> str:
    """Decide which rules apply to one piece of evidence.

    Under the immutable-source runner, all new evidence must declare
    EVIDENCE_MODEL_IMMUTABLE. An undeclared, missing, prepared-build or
    legacy-declared live context cannot be graded by weaker rules and is
    rejected as a downgrade attempt.
    """
    if declared == EVIDENCE_MODEL_IMMUTABLE:
        return EVIDENCE_MODEL_IMMUTABLE
    raise EvidenceModelError(
        "This evidence is part of an immutable-source E2E run package, so it must declare "
        f"{EVIDENCE_MODEL_IMMUTABLE!r}. It declares "
        f"{declared!r} instead, and a run package may not be graded by the "
        "weaker prepared-build or legacy rules.",
        code="EVIDENCE_MODEL_DOWNGRADE",
    )


def is_historical_model(model: Optional[str]) -> bool:
    """True for identifiers that can only describe historical evidence."""
    return model in HISTORICAL_EVIDENCE_MODELS


def requires_observed_selected_commit(model: str) -> bool:
    """Whether the running binary must report the *selected* commit itself.

    True for the immutable-source model: nothing was patched, so the commit the
    binary was compiled from is exactly the commit the user selected.
    """
    return model == EVIDENCE_MODEL_IMMUTABLE


def selected_sha_meaning() -> str:
    """One sentence a report can print so the meaning is clear."""
    return (
        "the selected Gonka commit, built without modification; the running binary "
        "must report exactly this commit"
    )


__all__ = [
    "EVIDENCE_MODEL_E2E",
    "EVIDENCE_MODEL_FIELD",
    "EVIDENCE_MODEL_IMMUTABLE",
    "EVIDENCE_MODEL_LEGACY",
    "EvidenceModelError",
    "HISTORICAL_EVIDENCE_MODELS",
    "KNOWN_EVIDENCE_MODELS",
    "PROVENANCE_MODEL_HISTORICAL_PREPARED",
    "PROVENANCE_MODEL_IMMUTABLE",
    "declared_evidence_model",
    "is_historical_model",
    "requires_observed_selected_commit",
    "resolve_evidence_model",
    "selected_sha_meaning",
]
