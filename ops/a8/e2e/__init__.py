"""E2E runner: explicit full-SHA source selection and reproducible replay.

This subpackage implements the public ``E2E`` runner on top of the existing
A8 orchestration machinery.  It deliberately lives inside ``ops/a8`` so that
no risky mass rename of the internal modules is required; the *public* name
of the tool is E2E and is exposed through ``ops/e2e/Run-E2E.ps1`` and
``ops/e2e/run-e2e.sh``.

Module map
----------
``errors``        Typed error hierarchy with stable machine-readable codes.
``gitio``         Safe ``git`` invocation, credential isolation, redaction.
``sources``       Source specs, strict full-SHA validation, acquisition,
                  self-contained bundles, submodule/LFS handling.
``runlock``       ``run.lock.json`` envelope plus build/execution manifests.
``compat``        Compatibility adapters: family detection from *measured*
                  target sources, build recipes, runtime expectations and
                  allow-listed test-only patches.
``runner_image``  Runner image identity resolution and lock enforcement.
``builder``       Executes build recipes and records real build provenance.
``planner``       Assembles a portable plan (lock + source package).
``executor``      Executes a plan, fresh network per run, replay support.
``cli``           The single argument parser shared by both host wrappers.

Nothing in this subpackage starts the inner Docker daemon by itself; that
remains the responsibility of the container entrypoint for ``run``/``rerun``.
"""

from __future__ import annotations

E2E_TOOL_NAME = "e2e"

__all__ = ["E2E_TOOL_NAME"]
