"""The handover object between the E2E layer and the existing suite runner.

The suite orchestrator requires an explicit E2E context. The core runner
modules must not import the ``forward_e2e.execution`` package: the E2E layer builds on top
of them, so a reverse dependency would risk an import cycle. The orchestrator
and adapters therefore use the context through duck typing.

This module deliberately has no imports from the rest of ``forward_e2e``: it is a
data carrier whose attributes and methods define the handover contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class E2ERunContext:
    """Everything the legacy suite runner needs to honour an E2E plan.

    Attributes
    ----------
    harness_script:
        Absolute path to the acceptance harness *of the runner image*. When
        set, the adapter uses this path; otherwise it resolves the harness
        relative to its own runner root. Both paths keep the harness independent
        of the marketplace checkout, so the code under test cannot choose its
        own judge.
    gonka_requested_sha:
        The commit the user selected. Under the immutable-source model it is
        also, exactly, the commit that is built and that the running binary
        must report. There is no second, "prepared" commit any more: the old
        runner committed a test overlay on top of the selection and then had
        to explain why the two differed.
    expected_gonka_sha / expected_marketplace_sha:
        Values for the harness' pristine-snapshot guard. The harness refuses to
        start when either snapshot is not exactly that commit, and measures both
        again after the last scenario (``source-immutability.json``).
    work_root:
        Where the harness keeps outputs for one live task (project caches,
        build directories, the Testermint network root). It
        is outside both snapshots by construction; ``None`` lets the harness
        use its documented default next to the Gonka snapshot.
    gradle_user_home:
        Run-scoped distribution/dependency cache shared with the build stage.
        Project outputs remain task-local and Gradle build cache is disabled.
    testermint_harness_dir:
        The runner-owned external Kotlin project that holds the marketplace
        scenarios. It always comes from the runner image, never from Gonka.
    expected_runtime:
        Field name to expected value for ``inferenced version --long``, measured
        from the selected sources by the compatibility adapter.
    evidence_model:
        The identifier the harness must stamp into every ``live-context.json``
        it writes. It is a literal here and not an import because this module
        must stay free of ``forward_e2e`` imports; the value is pinned against
        ``forward_e2e/suite/evidence_model.py`` by a test.
    """

    #: Must equal ``forward_e2e.suite.evidence_model.EVIDENCE_MODEL_IMMUTABLE``.
    E2E_EVIDENCE_MODEL = "a8.evidence/e2e-immutable-source/2"

    harness_script: Optional[Path] = None

    #: The verified A9 release manifest produced by the build stage. Handing it
    #: to the harness makes the suite deploy *these* artefacts instead of
    #: starting its own second release build, which used ``HEAD`` rather than
    #: the requested commit and produced binaries that no manifest described.
    a9_manifest_path: Optional[Path] = None

    gonka_requested_sha: Optional[str] = None
    contracts_requested_sha: Optional[str] = None

    expected_gonka_sha: Optional[str] = None
    expected_marketplace_sha: Optional[str] = None
    work_root: Optional[Path] = None
    #: Run-scoped tool/dependency cache; task outputs remain under work_root.
    gradle_user_home: Optional[Path] = None
    testermint_harness_dir: Optional[Path] = None
    expected_runtime: Dict[str, str] = field(default_factory=dict)
    evidence_model: str = E2E_EVIDENCE_MODEL

    adapter_id: Optional[str] = None
    contracts_adapter_id: Optional[str] = None
    plan_id: Optional[str] = None
    lock_sha256: Optional[str] = None
    runner_image_id: Optional[str] = None
    runner_version: Optional[str] = None

    #: Free-form provenance copied verbatim into the suite evidence.
    provenance: Dict[str, Any] = field(default_factory=dict)

    def harness_argv_extras(self) -> List[str]:
        """Extra ``run-live`` flags that bind the harness to this plan.

        Exactly the flags of the run-live contract: ``--manifest``,
        ``--expected-gonka-sha``, ``--expected-marketplace-sha``,
        ``--work-root``, ``--testermint-harness-dir``, ``--expected-runtime``
        and ``--evidence-model``. Nothing that could permit a modified tree
        (overlay directory, prepared SHA, allowed test paths) exists any more.
        """
        argv: List[str] = []
        if self.a9_manifest_path is not None:
            argv += ["--manifest", str(self.a9_manifest_path)]
        if self.expected_gonka_sha:
            argv += ["--expected-gonka-sha", self.expected_gonka_sha]
        if self.expected_marketplace_sha:
            argv += ["--expected-marketplace-sha", self.expected_marketplace_sha]
        if self.work_root is not None:
            argv += ["--work-root", str(self.work_root)]
        if self.gradle_user_home is not None:
            argv += ["--gradle-user-home", str(self.gradle_user_home)]
        if self.testermint_harness_dir is not None:
            argv += ["--testermint-harness-dir", str(self.testermint_harness_dir)]
        for field_name, value in sorted(self.expected_runtime.items()):
            argv += ["--expected-runtime", f"{field_name}={value}"]
        if self.evidence_model:
            argv += ["--evidence-model", self.evidence_model]
        return argv

    def to_dict(self) -> Dict[str, Any]:
        return {
            "harness_script": str(self.harness_script) if self.harness_script else None,
            "a9_manifest_path": str(self.a9_manifest_path) if self.a9_manifest_path else None,
            "gonka_requested_sha": self.gonka_requested_sha,
            "contracts_requested_sha": self.contracts_requested_sha,
            "expected_gonka_sha": self.expected_gonka_sha,
            "expected_marketplace_sha": self.expected_marketplace_sha,
            "work_root": str(self.work_root) if self.work_root else None,
            "gradle_user_home": str(self.gradle_user_home) if self.gradle_user_home else None,
            "testermint_harness_dir": (
                str(self.testermint_harness_dir) if self.testermint_harness_dir else None
            ),
            "expected_runtime": dict(self.expected_runtime),
            "evidence_model": self.evidence_model,
            "adapter_id": self.adapter_id,
            "contracts_adapter_id": self.contracts_adapter_id,
            "plan_id": self.plan_id,
            "lock_sha256": self.lock_sha256,
            "runner_image_id": self.runner_image_id,
            "runner_version": self.runner_version,
            "provenance": dict(self.provenance),
        }


__all__ = ["E2ERunContext"]
