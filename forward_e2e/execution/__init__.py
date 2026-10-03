"""E2E runner: explicit full-SHA source selection and reproducible replay.

This subpackage implements the public ``E2E`` runner on top of the suite
runner in ``forward_e2e.suite``.  It lives next to it, as
``forward_e2e.execution``, and the dependency is one-way: this package may
import the suite, the suite never imports this package (see AGENTS.md, "The
import direction is one-way").  The *public* name of the tool is E2E and is
exposed through ``ops/e2e/Run-E2E.ps1`` and ``ops/e2e/run-e2e.sh``.

Module map
----------
``errors``        Typed error hierarchy with stable machine-readable codes.
``gitio``         Safe ``git`` invocation, credential isolation, redaction.
``sources``       Source specs, strict full-SHA validation, acquisition,
                  self-contained bundles, submodule/LFS handling.
``runlock``       ``run.lock.json`` envelope plus build/execution manifests.
``compat``        Compatibility adapters: family detection from *measured*
                  target sources, build recipes and runtime expectations.
                  There is deliberately no patch or allow-listed-path
                  mechanism: the selected commits are built as they are.
``runner_image``  Runner image identity resolution and lock enforcement.
``builder``       Executes build recipes and records real build provenance.
``planner``       Assembles a portable plan (lock + source package).
``executor``      Executes a plan, fresh network per run, replay support.
``cli``           The single argument parser shared by both host wrappers.

Only ``run``/``rerun`` start the private inner Docker daemon, through
``_with_inner_dockerd`` in ``cli`` (``DinDSupervisor`` in the suite); planning,
listing and reporting never do.
"""

from __future__ import annotations

E2E_TOOL_NAME = "e2e"

__all__ = ["E2E_TOOL_NAME"]
