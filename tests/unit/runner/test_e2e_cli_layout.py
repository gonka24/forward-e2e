"""CLI output directory defaults and run directory layout resolution tests.

Three review findings are pinned down here:

1. Output defaults. ``plan`` still refuses to run without ``--output`` because
   the package location is the entire point of the command, while ``run``
   accepts its absence and falls back to the writable output root rather than
   to the plan package directory, which the wrappers mount read-only at
   ``/input/plan``.
2. Run directory layout. Durable E2E run packages live at ``<output>/<run-id>``
   (with suite evidence at ``<run>/suite/<run-id>``) and are staged at
   ``<workspace>/<run-id>/run``. ``report`` and ``recover`` operate only on
   durable E2E run packages and refuse bare or legacy suite directories.
3. Run identity. ``--run-id`` is restricted to the strictest character set
   (``RUN_ID_RE``, ``forward_e2e.suite.runtime.RUN_ID_REGEX`` and
   ``forward_e2e.suite.orchestrator.SUITE_ID_REGEX``), so a run that can be started can
   also be recovered.

The fixture trees are built with ``suite_export_dir`` so that a change to the
layout breaks the production code and these tests together instead of letting
them drift apart.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from forward_e2e.execution.cli import (
    _candidate_run_dirs,
    _resolve_recovery_target,
    build_parser,
    cmd_plan,
    cmd_recover,
    cmd_report,
    cmd_run,
    default_output_root,
    parse_e2e_args,
)
from forward_e2e.execution.errors import UsageError
from forward_e2e.execution.executor import (
    SUITE_EXPORT_DIRNAME,
    suite_export_dir,
    suite_export_root,
    suite_workspace_root,
)
from forward_e2e.execution.runlock import EXECUTION_MANIFEST_FILENAME, EXECUTION_MANIFEST_SCHEMA, RUN_LOCK_FILENAME
from forward_e2e.execution.runpackage import run_stage_dir
from forward_e2e.suite.supervisor import DinDSupervisor
from forward_e2e.suite.runtime import RUN_ID_REGEX
import tests.unit.runner  # noqa: F401

#: Two distinct, syntactically valid full 40-hex commit SHAs.
GONKA_SHA = "e86e4899bd8cf52d1ad4766c811f65230b2f9296"
CONTRACTS_SHA = "7497304e5dc6bf48accdd8c91549bc22de6997fc"

GONKA_REPO = "https://github.com/product-science/gonka.git"
CONTRACTS_REPO = "https://github.com/product-science/gonka24-smart-contract.git"


class E2ECliOutputDefaultsTests(unittest.TestCase):
    """Finding 1: `run` no longer demands --output, and never writes to /input/plan."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-r10cli-")
        self.root = Path(self.tmp_dir.name)
        self.output_root = self.root / "out"
        self.workspace = self.root / "workspace"
        #: Stands in for the read-only /input/plan bind mount of the wrappers.
        self.read_only_package = self.root / "input" / "plan"
        for directory in (self.output_root, self.workspace, self.read_only_package):
            directory.mkdir(parents=True)
        self.env = patch.dict(
            os.environ,
            {
                "E2E_OUTPUT_DIR": str(self.output_root),
                "E2E_WORKSPACE_DIR": str(self.workspace),
                # The parser defaults --credential-file from the environment;
                # an empty value keeps a developer's real token out of the test.
                "E2E_CREDENTIAL_FILE": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self):
        self.tmp_dir.cleanup()

    # -- helpers -------------------------------------------------------
    def _run_argv(self, extra=None):
        """A complete `run` argv whose only omission is --output."""
        argv = [
            "run",
            "--gonka-repo", GONKA_REPO,
            "--gonka-sha", GONKA_SHA,
            "--contracts-repo", CONTRACTS_REPO,
            "--contracts-sha", CONTRACTS_SHA,
            "--profile", "smoke",
            "--workspace", str(self.workspace),
        ]
        argv.extend(extra or [])
        return argv

    def _plan_argv(self, extra=None):
        argv = [
            "plan",
            "--gonka-repo", GONKA_REPO,
            "--gonka-sha", GONKA_SHA,
            "--contracts-repo", CONTRACTS_REPO,
            "--contracts-sha", CONTRACTS_SHA,
            "--profile", "smoke",
            "--workspace", str(self.workspace),
        ]
        argv.extend(extra or [])
        return argv

    @staticmethod
    def _fake_build_plan(request, *, emit=None):
        """Pretend planning succeeded, reporting the package it was told to use."""
        package_dir = Path(request.output_dir)
        return SimpleNamespace(
            lock=SimpleNamespace(
                plan_id=request.plan_id or "plan-0123456789abcdef",
                scenarios=["lock-exact-e"],
                lock_sha256="a" * 64,
            ),
            lock_path=package_dir / "run.lock.json",
            package_dir=package_dir,
            tasks=["lock-exact-e"],
            gonka_source=SimpleNamespace(worktree=Path(request.worktree_root or package_dir) / "gonka"),
            contracts_source=SimpleNamespace(worktree=Path(request.worktree_root or package_dir) / "contracts"),
        )

    def _invoke_run(self, argv):
        """Drive `cmd_run` through the real parser with the slow parts patched."""
        messages = []
        lifecycle = []
        subcommand, args = parse_e2e_args(argv)
        with patch(
            "forward_e2e.execution.planner.build_plan", side_effect=self._fake_build_plan
        ) as build_plan, patch(
            "forward_e2e.execution.executor.execute_plan",
            side_effect=lambda request, **kwargs: lifecycle.append("execute") or 0,
        ) as execute_plan, patch.object(
            DinDSupervisor, "start_dockerd",
            side_effect=lambda **kwargs: lifecycle.append("start"),
        ) as start_dockerd, patch.object(
            DinDSupervisor, "stop_dockerd",
            side_effect=lambda **kwargs: lifecycle.append("stop") or True,
        ):
            code = cmd_run(args, subcommand, emit=messages.append)
        return SimpleNamespace(
            code=code,
            build_plan=build_plan,
            execute_plan=execute_plan,
            start_dockerd=start_dockerd,
            lifecycle=lifecycle,
            messages=messages,
        )

    @staticmethod
    def _plan_request(outcome):
        return outcome.build_plan.call_args.args[0]

    @staticmethod
    def _execution_request(outcome):
        return outcome.execute_plan.call_args.args[0]

    # -- plan still insists on a destination ----------------------------
    def test_plan_without_output_is_still_a_usage_error_naming_the_missing_destination(self):
        _subcommand, args = parse_e2e_args(self._plan_argv())
        self.assertIsNone(args.output)
        with patch("forward_e2e.execution.planner.build_plan") as build_plan:
            with self.assertRaises(UsageError) as cm:
                cmd_plan(args, emit=lambda message: None)
        self.assertIn("--output is required", str(cm.exception))
        self.assertIn("destination", str(cm.exception))
        build_plan.assert_not_called()

    def test_run_without_output_is_not_a_usage_error_and_still_executes_the_plan(self):
        outcome = self._invoke_run(self._run_argv())
        self.assertEqual(outcome.code, 0)
        outcome.build_plan.assert_called_once()
        outcome.execute_plan.assert_called_once()
        self.assertIsNone(self._execution_request(outcome).run_id)

    # -- where the implicit plan and the run actually land ---------------
    def test_run_without_output_writes_the_implicit_plan_under_the_default_output_root(self):
        outcome = self._invoke_run(self._run_argv())
        request = self._plan_request(outcome)
        self.assertIsNotNone(request.plan_id)
        self.assertEqual(
            Path(request.output_dir),
            self.output_root / "plans" / request.plan_id,
        )

    def test_run_without_output_sends_the_run_itself_to_the_default_runs_directory(self):
        outcome = self._invoke_run(self._run_argv())
        self.assertEqual(
            Path(self._execution_request(outcome).output_dir),
            self.output_root / "runs",
        )

    def test_run_without_output_honours_an_explicit_plan_id_for_the_implicit_package(self):
        outcome = self._invoke_run(self._run_argv(["--plan-id", "plan-chosen-by-hand"]))
        request = self._plan_request(outcome)
        self.assertEqual(request.plan_id, "plan-chosen-by-hand")
        self.assertEqual(
            Path(request.output_dir),
            self.output_root / "plans" / "plan-chosen-by-hand",
        )

    def test_two_consecutive_implicit_plans_do_not_collide(self):
        first = self._plan_request(self._invoke_run(self._run_argv()))
        second = self._plan_request(self._invoke_run(self._run_argv()))
        self.assertIsNotNone(first.plan_id)
        self.assertIsNotNone(second.plan_id)
        self.assertNotEqual(first.plan_id, second.plan_id)
        self.assertNotEqual(Path(first.output_dir), Path(second.output_dir))

    def test_run_never_writes_the_implicit_plan_into_the_run_output_directory(self):
        outcome = self._invoke_run(self._run_argv())
        plan_dir = Path(self._plan_request(outcome).output_dir)
        run_dir = Path(self._execution_request(outcome).output_dir)
        self.assertNotEqual(plan_dir, run_dir)
        self.assertEqual(plan_dir.parent, self.output_root / "plans")
        self.assertEqual(run_dir, self.output_root / "runs")

    @contextlib.contextmanager
    def _patched_from_lock_execution(self):
        """Everything `run --from` needs, except the decisions under test."""
        with patch(
            "forward_e2e.execution.cli.load_run_lock",
            return_value=SimpleNamespace(
                plan_id="plan-0123456789abcdef", lock_sha256="b" * 64
            ),
        ), patch(
            "forward_e2e.execution.cli.resolve_package_root", return_value=self.read_only_package
        ), patch(
            "forward_e2e.execution.executor.execute_plan", return_value=0
        ) as execute_plan, patch.object(
            DinDSupervisor, "start_dockerd"
        ), patch.object(
            DinDSupervisor, "stop_dockerd", return_value=True
        ):
            yield execute_plan

    def test_run_from_a_read_only_plan_package_never_writes_the_run_into_that_package(self):
        lock_path = self.read_only_package / "run.lock.json"
        _subcommand, args = parse_e2e_args(
            ["run", "--from", str(lock_path), "--workspace", str(self.workspace)]
        )
        self.assertIsNone(args.output)
        messages = []
        with self._patched_from_lock_execution() as execute_plan:
            code = cmd_run(args, "run", emit=messages.append)
        self.assertEqual(code, 0)
        request = execute_plan.call_args.args[0]
        self.assertEqual(Path(request.package_dir), self.read_only_package)
        self.assertEqual(Path(request.output_dir), self.output_root / "runs")
        self.assertNotEqual(Path(request.output_dir), self.read_only_package)
        self.assertNotIn(
            self.read_only_package, Path(request.output_dir).parents
        )

    def test_run_with_an_explicit_output_uses_it_verbatim_for_plan_and_run(self):
        chosen = self.root / "chosen-output"
        outcome = self._invoke_run(self._run_argv(["--output", str(chosen)]))
        self.assertEqual(Path(self._plan_request(outcome).output_dir), chosen)
        self.assertEqual(Path(self._execution_request(outcome).output_dir), chosen)

    # -- a run id that could not be recovered is refused up front ----------
    def test_a_dotted_run_id_is_refused_because_recovery_would_refuse_it_later(self):
        dotted = "e2e.20240101.000000"
        # The two checks the id has to survive after this one.
        self.assertIsNone(RUN_ID_REGEX.match(dotted))
        _subcommand, args = parse_e2e_args(
            [
                "run",
                "--from", str(self.read_only_package / "run.lock.json"),
                "--workspace", str(self.workspace),
                "--run-id", dotted,
            ]
        )
        with self._patched_from_lock_execution() as execute_plan:
            with self.assertRaises(UsageError) as cm:
                cmd_run(args, "run", emit=lambda message: None)
        self.assertIn("Invalid --run-id", str(cm.exception))
        self.assertIn("letters, digits, '_' or '-'", str(cm.exception))
        self.assertIn("recovery path checks", str(cm.exception))
        self.assertIn(dotted, str(cm.exception))
        execute_plan.assert_not_called()

    def test_a_run_id_of_letters_digits_underscore_and_dash_survives_every_check(self):
        accepted = "e2e-20240101-000000_abcdef"
        self.assertIsNotNone(RUN_ID_REGEX.match(accepted))
        _subcommand, args = parse_e2e_args(
            [
                "run",
                "--from", str(self.read_only_package / "run.lock.json"),
                "--workspace", str(self.workspace),
                "--run-id", accepted,
            ]
        )
        with self._patched_from_lock_execution() as execute_plan:
            code = cmd_run(args, "run", emit=lambda message: None)
        self.assertEqual(code, 0)
        self.assertEqual(execute_plan.call_args.args[0].run_id, accepted)

    # -- the default root itself -----------------------------------------
    def test_default_output_root_follows_the_environment_variable(self):
        self.assertEqual(default_output_root(), self.output_root)
        with patch.dict(os.environ, {"E2E_OUTPUT_DIR": str(self.root / "elsewhere")}):
            self.assertEqual(default_output_root(), self.root / "elsewhere")

    def test_default_output_root_falls_back_to_the_writable_out_mount(self):
        for value in ({}, {"E2E_OUTPUT_DIR": ""}):
            with self.subTest(environment=value):
                with patch.dict(os.environ, value, clear=True):
                    self.assertEqual(default_output_root(), Path("/out"))

    def test_the_run_parser_leaves_output_optional_and_unset_by_default(self):
        parser = build_parser("run")
        actions = [a for a in parser._actions if "--output" in a.option_strings]
        self.assertEqual(len(actions), 1)
        self.assertFalse(actions[0].required)
        self.assertIsNone(actions[0].default)
        _subcommand, args = parse_e2e_args(self._run_argv())
        self.assertIsNone(args.output)

    def test_run_without_output_starts_the_daemon_before_execution_and_stops_it_afterwards(self):
        outcome = self._invoke_run(self._run_argv())
        outcome.start_dockerd.assert_called_once()
        self.assertEqual(outcome.lifecycle, ["start", "execute", "stop"])
        self.assertEqual(outcome.code, 0)


class E2ECliRunLayoutTests(unittest.TestCase):
    """Finding 2: `report` and `recover` must know the doubly-nested layout."""

    RUN_ID = "e2e-20240101-000000-abcdef"

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-r10cli-")
        self.root = Path(self.tmp_dir.name)
        self.output_root = self.root / "out"
        self.workspace = self.root / "workspace"
        for directory in (self.output_root, self.workspace):
            directory.mkdir(parents=True)
        self.env = patch.dict(
            os.environ,
            {
                "E2E_OUTPUT_DIR": str(self.output_root),
                "E2E_WORKSPACE_DIR": str(self.workspace),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.messages = []

    def tearDown(self):
        self.tmp_dir.cleanup()

    # -- fixture builders -------------------------------------------------
    @staticmethod
    def _write_json(path: Path, payload) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _write_suite(self, suite_dir: Path, suite_id: str) -> Path:
        """A suite directory is recognised by a real, readable suite-plan.json."""
        self._write_json(
            suite_dir / "suite-plan.json",
            {
                "schema_version": "1.0.0",
                "suite_id": suite_id,
                "created_at_utc": "2024-01-01T00:00:00Z",
                "profile": "smoke",
                "requested_scenarios": ["lock-exact-e"],
                "source_identity": {
                    "marketplace_commit_sha": CONTRACTS_SHA,
                    "gonka_commit_sha": GONKA_SHA,
                    "runner_version_hash": "c" * 64,
                    "catalog_version_hash": "d" * 64,
                },
                "tasks": [
                    {
                        "task_id": "lock-exact-e",
                        "ordinal": 1,
                        "proof_level": "NATIVE",
                        "description": "Synthetic task used only as fixture content.",
                        "scenario_selector": "lock-exact-e",
                        "timeout_minutes": 10,
                        "stage_timeout_seconds": 600,
                        "expected_artifacts": [],
                        "expected_checkpoints": [],
                        "coverage_ids": [],
                        "limitations": [],
                    }
                ],
            },
        )
        return suite_dir

    def _write_execution_manifest(
        self,
        run_dir: Path,
        *,
        run_id: str,
        suite_id: str,
        suite_export_relpath,
        suite_workspace_dir,
    ) -> Path:
        return self._write_json(
            run_dir / EXECUTION_MANIFEST_FILENAME,
            {
                "schema_version": EXECUTION_MANIFEST_SCHEMA,
                "lock_sha256": "a" * 64,
                "plan_id": "plan-0123456789abcdef",
                "run_id": run_id,
                "suite_id": suite_id,
                "created_at_utc": "2024-01-01T00:00:00Z",
                "command": "run",
                "fresh_network_state": True,
                "replay_of_plan": None,
                "parent_run_id": None,
                "source_plan_path": None,
                "build_manifest_sha256": None,
                "artifact_index_relpath": suite_export_relpath + "/artifact-index.json",
                "suite_export_relpath": suite_export_relpath,
                "suite_workspace_dir": suite_workspace_dir,
                "runner_image_id": None,
                "notes": [],
            },
        )

    def _make_exported_run(
        self,
        *,
        run_id=None,
        suite_id=None,
        manifest="valid",
        parent=None,
    ) -> Path:
        """A run directory shaped exactly like the executor leaves it behind."""
        run_id = run_id or self.RUN_ID
        suite_id = suite_id or run_id
        run_dir = (parent or (self.output_root / "runs")) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        exported = suite_export_dir(run_dir, suite_id)
        self._write_suite(exported, suite_id)
        workspace_root = suite_workspace_root(self.workspace / run_id)
        if manifest == "valid":
            self._write_execution_manifest(
                run_dir,
                run_id=run_id,
                suite_id=suite_id,
                suite_export_relpath=str(exported.relative_to(run_dir)),
                suite_workspace_dir=str(workspace_root),
            )
        elif manifest == "corrupt":
            (run_dir / EXECUTION_MANIFEST_FILENAME).write_text(
                '{"schema_version": "e2e/execution-manifest/1", "run_id":',
                encoding="utf-8",
            )
        elif manifest != "missing":  # pragma: no cover - guards the fixture itself
            raise AssertionError(f"unknown manifest mode {manifest!r}")
        return run_dir

    def _args(self, run, *, output=None, workspace=None):
        argv = ["report", "--run", str(run), "--workspace", str(workspace or self.workspace)]
        if output is not None:
            argv.extend(["--output", str(output)])
        _subcommand, args = parse_e2e_args(argv)
        return args

    def _emit(self, message):
        self.messages.append(message)

    # -- the executor's helpers describe the layout the CLI must expect ----
    def test_the_layout_helpers_reproduce_the_double_nesting_of_the_orchestrator(self):
        run_dir = self.output_root / "runs" / self.RUN_ID
        self.assertEqual(suite_export_root(run_dir), run_dir / SUITE_EXPORT_DIRNAME)
        self.assertEqual(
            suite_export_dir(run_dir, self.RUN_ID),
            suite_export_root(run_dir) / self.RUN_ID,
        )
        workspace = self.workspace / self.RUN_ID
        self.assertEqual(suite_workspace_root(workspace), workspace / SUITE_EXPORT_DIRNAME)

    # -- _candidate_run_dirs ----------------------------------------------
    def test_candidate_run_dirs_are_the_literal_path_then_both_output_locations(self):
        args = self._args(self.RUN_ID, output=self.output_root)
        self.assertEqual(
            _candidate_run_dirs(args),
            [
                Path(self.RUN_ID),
                self.output_root / self.RUN_ID,
                self.output_root / "runs" / self.RUN_ID,
            ],
        )

    def test_candidate_run_dirs_use_the_default_output_root_when_output_is_absent(self):
        args = self._args(self.RUN_ID)
        self.assertEqual(
            _candidate_run_dirs(args),
            [
                Path(self.RUN_ID),
                self.output_root / self.RUN_ID,
                self.output_root / "runs" / self.RUN_ID,
            ],
        )

    def test_candidate_run_dirs_do_not_repeat_a_path_that_is_reached_twice(self):
        args = self._args(self.output_root / self.RUN_ID, output=self.output_root)
        candidates = _candidate_run_dirs(args)
        self.assertEqual(len(candidates), len(set(candidates)))
        self.assertIn(self.output_root / self.RUN_ID, candidates)

    # -- _resolve_recovery_target -----------------------------------------
    def test_recovery_target_is_taken_from_the_manifest_suite_id_and_workspace_dir(self):
        run_dir = self._make_exported_run(suite_id="suite-20240101-000000")
        suite_id, workspace_root = _resolve_recovery_target(
            self._args(run_dir), emit=self._emit
        )
        self.assertEqual(suite_id, "suite-20240101-000000")
        self.assertEqual(workspace_root, suite_workspace_root(self.workspace / self.RUN_ID))
        self.assertIn(
            f"Recovering suite suite-20240101-000000 as recorded in {run_dir}.",
            self.messages,
        )

    def test_an_empty_directory_named_like_the_run_does_not_shadow_the_real_run(self):
        """Having the right name is not evidence: the leftover must be skipped."""
        shadow = self.output_root / self.RUN_ID
        shadow.mkdir(parents=True)
        run_dir = self._make_exported_run(suite_id="suite-20240101-000000")
        self.assertEqual(run_dir.parent, self.output_root / "runs")
        suite_id, workspace_root = _resolve_recovery_target(
            self._args(self.RUN_ID, output=self.output_root), emit=self._emit
        )
        self.assertEqual(suite_id, "suite-20240101-000000")
        self.assertEqual(workspace_root, suite_workspace_root(self.workspace / self.RUN_ID))
        self.assertTrue(any(str(run_dir) in message for message in self.messages), self.messages)

    def test_an_empty_directory_named_like_the_run_does_not_accept_legacy_staging(self):
        (self.output_root / "runs" / self.RUN_ID).mkdir(parents=True)
        self._write_suite(self.workspace / "suites" / self.RUN_ID, self.RUN_ID)
        with self.assertRaises(UsageError) as cm:
            _resolve_recovery_target(self._args(self.RUN_ID, output=self.output_root), emit=self._emit)
        self.assertIn("requested run was not found", str(cm.exception))

    def test_recovery_target_for_a_bare_run_id_uses_the_durable_run_package_stage(self):
        stage_dir = run_stage_dir(self.workspace, self.RUN_ID)
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / RUN_LOCK_FILENAME).write_text("{}", encoding="utf-8")
        suite_id, workspace_root = _resolve_recovery_target(
            self._args(self.RUN_ID, output=self.output_root), emit=self._emit
        )
        self.assertEqual(suite_id, self.RUN_ID)
        self.assertEqual(workspace_root, suite_workspace_root(self.workspace / self.RUN_ID))

    def test_recovery_target_rejects_the_legacy_flat_and_nested_standalone_staging_locations(self):
        self._write_suite(self.workspace / "suites" / self.RUN_ID, self.RUN_ID)
        self._write_suite(self.workspace / self.RUN_ID / "suite" / "suites" / self.RUN_ID, self.RUN_ID)
        with self.assertRaises(UsageError) as cm:
            _resolve_recovery_target(self._args(self.RUN_ID, output=self.output_root), emit=self._emit)
        self.assertIn("requested run was not found", str(cm.exception))

    def test_an_existing_absolute_run_path_is_never_returned_as_a_suite_id(self):
        """Without the guard this directory would be recovered under its own path."""
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        args = self._args(elsewhere, output=self.output_root)
        with self.assertRaises(UsageError) as cm:
            _resolve_recovery_target(args, emit=self._emit)
        self.assertIn("The requested run was not found", str(cm.exception))
        self.assertEqual(cm.exception.details["run"], str(elsewhere))
        self.assertEqual(self.messages, [])

    # -- the commands on top of the resolvers ------------------------------
    def test_cmd_report_reads_the_exported_suite_directory_named_by_the_manifest(self):
        run_dir = self._make_exported_run(suite_id="suite-20240101-000000")
        reporter = MagicMock()
        reporter.return_value.write_reports.return_value = SimpleNamespace(
            suite_id="suite-20240101-000000",
            overall_status=SimpleNamespace(value="PASSED"),
        )
        with patch(
            "forward_e2e.execution.executor.grade_run_package",
            return_value=MagicMock(exit_code=0, summary_lines=lambda: []),
        ), patch("forward_e2e.suite.reporter.OfflineReporter", reporter), patch.object(
            DinDSupervisor, "start_dockerd"
        ) as start_dockerd:
            code = cmd_report(self._args(run_dir), emit=self._emit)
        self.assertEqual(code, 0)
        self.assertEqual(
            Path(reporter.call_args.args[0]).resolve(),
            suite_export_dir(run_dir, "suite-20240101-000000").resolve(),
        )
        start_dockerd.assert_not_called()

    def test_cmd_report_returns_failure_only_when_the_complete_suite_failed(self):
        from forward_e2e.suite.models import ExecutionStatus
        from tests.unit.runner.support.packages import setup_baseline_package

        for status, expected_code in ((ExecutionStatus.PASSED, 0), (ExecutionStatus.FAILED, 1)):
            with self.subTest(execution_status=status):
                run_dir = self.output_root / status.value / self.RUN_ID
                setup_baseline_package(
                    run_dir, scenarios=["go-query-error-classification"], run_id=self.RUN_ID,
                    execution_status=status,
                )
                code = cmd_report(self._args(run_dir), emit=self._emit)
                self.assertEqual(code, expected_code, msg=self.messages)
                verdict = json.loads((run_dir / "e2e-run-result.json").read_text(encoding="utf-8"))
                self.assertEqual(verdict["status"], status.value)
                self.assertEqual(verdict["suite_status"], status.value)
                self.assertFalse(any(
                    finding["code"].startswith("INCOMPLETE_")
                    for finding in verdict["findings"]
                ), verdict["findings"])

    def test_cmd_report_on_a_bare_suite_without_package_returns_failure_even_if_suite_passed(self):
        run_dir = self._make_exported_run(manifest="missing")
        reporter = MagicMock()
        reporter.return_value.write_reports.return_value = SimpleNamespace(
            suite_id=self.RUN_ID, overall_status=SimpleNamespace(value="PASSED")
        )
        with patch("forward_e2e.suite.reporter.OfflineReporter", reporter):
            code = cmd_report(self._args(run_dir), emit=self._emit)
        self.assertEqual(code, 1)
        self.assertTrue(
            any("INCOMPLETE_EXECUTION_MANIFEST_MISSING" in m for m in self.messages),
            self.messages,
        )

    def test_cmd_report_on_an_unknown_run_raises_before_touching_the_reporter(self):
        reporter = MagicMock()
        with patch("forward_e2e.suite.reporter.OfflineReporter", reporter):
            with self.assertRaises(UsageError) as cm:
                cmd_report(self._args("no-such-run", output=self.output_root), emit=self._emit)
        self.assertIn("No suite evidence was found", str(cm.exception))
        reporter.assert_not_called()

    def _make_stage2_package(self, *, complete: bool = True) -> tuple[Path, Path, Path, str]:
        import shutil
        from forward_e2e.execution.runlock import DeliveryStatus
        from forward_e2e.suite.models import make_task_run_id
        from tests.unit.runner.support.packages import setup_baseline_package

        stage_dir = run_stage_dir(self.workspace, self.RUN_ID)
        setup_baseline_package(
            stage_dir,
            scenarios=["go-query-error-classification"],
            run_id=self.RUN_ID,
            delivery_status=DeliveryStatus.COMPLETED,
        )
        stage_suite = suite_export_dir(stage_dir, self.RUN_ID)
        task_run_id = make_task_run_id(self.RUN_ID, 1, "go-query-error-classification")
        runtime_root = suite_workspace_root(self.workspace / self.RUN_ID) / "runtime"
        runtime_task_dir = runtime_root / "runs" / task_run_id
        if runtime_task_dir.exists():
            shutil.rmtree(runtime_task_dir)
        shutil.copytree(stage_suite / "runs" / task_run_id, runtime_task_dir)

        if not complete:
            for rel_remove in (
                f"runs/{task_run_id}/identity.json",
                f"runs/{task_run_id}/result.json",
                "suite-result.json",
            ):
                target = stage_suite / rel_remove
                if target.exists():
                    target.unlink()
            index_file = stage_suite / "artifact-index.json"
            index_data = json.loads(index_file.read_text(encoding="utf-8"))
            removed_set = {
                f"runs/{task_run_id}/identity.json",
                f"runs/{task_run_id}/result.json",
                "suite-result.json",
            }
            kept = [a for a in index_data["artifacts"] if a["relative_path"] not in removed_set]
            index_data["artifacts"] = kept
            index_data["total_artifacts"] = len(kept)
            self._write_json(index_file, index_data)

        return stage_dir, stage_suite, runtime_root, task_run_id

    def test_cmd_recover_delivers_stage2_run_package_directly_without_calling_legacy_recover_suite(self):
        import forward_e2e.suite.orchestrator as orch_mod
        self.assertFalse(hasattr(orch_mod, "recover_suite"))
        _stage_dir, stage_suite, _runtime_root, task_run_id = self._make_stage2_package(complete=True)

        args = parse_e2e_args(
            [
                "recover",
                "--run", self.RUN_ID,
                "--output", str(self.output_root / "recovered"),
                "--workspace", str(self.workspace),
            ]
        )[1]
        code = cmd_recover(args, emit=self._emit)
        self.assertEqual(code, 0, msg=self.messages)
        recovered_run_dir = self.output_root / "recovered" / self.RUN_ID
        recovered_suite = suite_export_dir(recovered_run_dir, self.RUN_ID)
        self.assertTrue((recovered_suite / "suite-plan.json").is_file())
        self.assertEqual(
            (recovered_suite / "runs" / task_run_id / "identity.json").read_bytes(),
            (stage_suite / "runs" / task_run_id / "identity.json").read_bytes(),
        )
        verdict = json.loads((recovered_run_dir / "e2e-run-result.json").read_text(encoding="utf-8"))
        self.assertEqual(verdict["status"], "PASSED")

    def test_cmd_recover_supplements_missing_runtime_evidence_only_when_task_collection_is_incomplete(self):
        _stage_dir, stage_suite, _runtime_root, task_run_id = self._make_stage2_package(complete=False)
        self.assertFalse((stage_suite / "runs" / task_run_id / "identity.json").exists())

        args = parse_e2e_args(
            [
                "recover",
                "--run", self.RUN_ID,
                "--output", str(self.output_root / "recovered"),
                "--workspace", str(self.workspace),
            ]
        )[1]
        code = cmd_recover(args, emit=self._emit)
        self.assertEqual(code, 0, msg=self.messages)
        self.assertTrue((stage_suite / "runs" / task_run_id / "identity.json").is_file())
        recovered_suite = suite_export_dir(self.output_root / "recovered" / self.RUN_ID, self.RUN_ID)
        self.assertTrue((recovered_suite / "runs" / task_run_id / "identity.json").is_file())

    def test_cmd_recover_indexes_artifact_copied_before_interruption_without_overwriting_file(self):
        from forward_e2e.suite.verifier import verify_suite_artifacts_integrity

        _stage_dir, stage_suite, _runtime_root, task_run_id = self._make_stage2_package(complete=True)
        ident_rel = f"runs/{task_run_id}/identity.json"
        collected_identity = stage_suite / ident_rel
        original_inode = collected_identity.stat().st_ino
        original_bytes = collected_identity.read_bytes()

        # Simulate crash after copying identity.json to suite_dir, before update_artifact_index
        # and before writing result.json / suite-result.json.
        (stage_suite / "runs" / task_run_id / "result.json").unlink()
        (stage_suite / "suite-result.json").unlink()
        index_file = stage_suite / "artifact-index.json"
        index_data = json.loads(index_file.read_text(encoding="utf-8"))
        removed_set = {
            ident_rel,
            f"runs/{task_run_id}/result.json",
            "suite-result.json",
        }
        kept = [a for a in index_data["artifacts"] if a["relative_path"] not in removed_set]
        index_data["artifacts"] = kept
        index_data["total_artifacts"] = len(kept)
        self._write_json(index_file, index_data)

        args = parse_e2e_args(
            [
                "recover",
                "--run", self.RUN_ID,
                "--output", str(self.output_root / "recovered"),
                "--workspace", str(self.workspace),
            ]
        )[1]
        first_code = cmd_recover(args, emit=self._emit)
        self.assertEqual(first_code, 0, msg=self.messages)

        # File on disk in suite_dir must not have been overwritten (same inode & bytes)
        self.assertEqual(collected_identity.stat().st_ino, original_inode)
        self.assertEqual(collected_identity.read_bytes(), original_bytes)

        # Missing entry must now be restored in artifact-index.json
        updated_index = json.loads(index_file.read_text(encoding="utf-8"))
        indexed_rel_paths = {a["relative_path"] for a in updated_index["artifacts"]}
        self.assertIn(ident_rel, indexed_rel_paths)

        # verify_suite_artifacts_integrity must accept the restored index entry
        _valid_count, _total_count, corrupt_or_missing = verify_suite_artifacts_integrity(stage_suite)
        self.assertEqual(corrupt_or_missing, [])

        # Repeat recover must also succeed cleanly
        self.messages.clear()
        second_code = cmd_recover(args, emit=self._emit)
        self.assertEqual(second_code, 0, msg=self.messages)

    def test_cmd_recover_refuses_to_overwrite_collected_evidence_when_runtime_diverges(self):
        _stage_dir, stage_suite, runtime_root, task_run_id = self._make_stage2_package(complete=True)
        collected_identity = stage_suite / "runs" / task_run_id / "identity.json"
        original_bytes = collected_identity.read_bytes()
        original_index_bytes = (stage_suite / "artifact-index.json").read_bytes()

        args = parse_e2e_args(
            [
                "recover",
                "--run", self.RUN_ID,
                "--output", str(self.output_root / "recovered"),
                "--workspace", str(self.workspace),
            ]
        )[1]
        first_code = cmd_recover(args, emit=self._emit)
        self.assertEqual(first_code, 0, msg=self.messages)

        # Mutate the runtime file after suite evidence was already collected and indexed
        runtime_identity = runtime_root / "runs" / task_run_id / "identity.json"
        runtime_identity.write_bytes(b'{"source": "tampered-runtime-artifact"}\n')

        self.messages.clear()
        second_code = cmd_recover(args, emit=self._emit)
        self.assertEqual(second_code, 1)
        self.assertEqual(collected_identity.read_bytes(), original_bytes)
        self.assertEqual((stage_suite / "artifact-index.json").read_bytes(), original_index_bytes)
        self.assertTrue(
            any("conflicts with already-collected suite artifact" in m for m in self.messages),
            self.messages,
        )

    def test_cmd_recover_rejects_package_when_both_collected_and_runtime_identity_diverge_identically_from_pinned_index(self):
        _stage_dir, stage_suite, runtime_root, task_run_id = self._make_stage2_package(complete=True)
        collected_identity = stage_suite / "runs" / task_run_id / "identity.json"
        runtime_identity = runtime_root / "runs" / task_run_id / "identity.json"
        original_index_bytes = (stage_suite / "artifact-index.json").read_bytes()

        # Modify BOTH collected identity.json and runtime identity.json to the exact same bytes
        # while keeping the original pinned artifact-index.json untouched.
        tampered = json.loads(collected_identity.read_text(encoding="utf-8"))
        tampered["tampered_field"] = "identical-in-suite-and-runtime"
        tampered_bytes = (json.dumps(tampered, indent=2) + "\n").encode("utf-8")
        collected_identity.write_bytes(tampered_bytes)
        runtime_identity.write_bytes(tampered_bytes)

        args = parse_e2e_args(
            [
                "recover",
                "--run", self.RUN_ID,
                "--output", str(self.output_root / "recovered"),
                "--workspace", str(self.workspace),
            ]
        )[1]
        code = cmd_recover(args, emit=self._emit)
        self.assertEqual(code, 1)
        self.assertEqual((stage_suite / "artifact-index.json").read_bytes(), original_index_bytes)
        self.assertFalse((self.output_root / "recovered" / self.RUN_ID).exists())
        self.assertTrue(
            any("sha256 mismatch" in m for m in self.messages),
            self.messages,
        )

    def test_cmd_recover_rejects_symlink_destination_file_before_any_write(self):
        _stage_dir, stage_suite, runtime_root, task_run_id = self._make_stage2_package(complete=False)
        outside_file = self.root / "outside_secret.json"
        outside_file.write_text("ORIGINAL_SECRET", encoding="utf-8")

        task_dir = stage_suite / "runs" / task_run_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "identity.json").symlink_to(outside_file)

        args = parse_e2e_args(
            [
                "recover",
                "--run", self.RUN_ID,
                "--output", str(self.output_root / "recovered"),
                "--workspace", str(self.workspace),
            ]
        )[1]
        code = cmd_recover(args, emit=self._emit)
        self.assertEqual(code, 1)
        self.assertEqual(outside_file.read_text(encoding="utf-8"), "ORIGINAL_SECRET")
        self.assertTrue(any(
            "symlink" in message.lower() and str(task_dir / "identity.json") in message
            for message in self.messages
        ), self.messages)

    def test_cmd_recover_rejects_symlink_destination_parent_directory_before_any_write(self):
        _stage_dir, stage_suite, runtime_root, task_run_id = self._make_stage2_package(complete=False)
        outside_dir = self.root / "outside_dir"
        outside_dir.mkdir(parents=True, exist_ok=True)

        runs_dir = stage_suite / "runs"
        if (runs_dir / task_run_id).exists():
            import shutil
            shutil.rmtree(runs_dir / task_run_id)
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / task_run_id).symlink_to(outside_dir)

        args = parse_e2e_args(
            [
                "recover",
                "--run", self.RUN_ID,
                "--output", str(self.output_root / "recovered"),
                "--workspace", str(self.workspace),
            ]
        )[1]
        code = cmd_recover(args, emit=self._emit)
        self.assertEqual(code, 1)
        self.assertFalse((outside_dir / "identity.json").exists())
        self.assertTrue(any(
            "symlink" in message.lower() and str(runs_dir / task_run_id) in message
            for message in self.messages
        ), self.messages)

    def test_cmd_report_verifies_suite_exactly_once_and_offline_reporter_does_not_reverify(self):
        from forward_e2e.suite.reporter import OfflineReporter
        from forward_e2e.suite.verifier import verify_and_recalculate_suite

        stage_dir, _stage_suite, _runtime_root, _task_run_id = self._make_stage2_package(complete=True)
        args = parse_e2e_args(
            [
                "report",
                "--run", str(stage_dir),
                "--output", str(self.output_root),
                "--workspace", str(self.workspace),
            ]
        )[1]
        with patch(
            "forward_e2e.suite.verifier.verify_and_recalculate_suite",
            wraps=verify_and_recalculate_suite,
        ) as spy_verify, patch.object(
            OfflineReporter,
            "write_reports",
            autospec=True,
            side_effect=OfflineReporter.write_reports,
        ) as spy_write:
            code = cmd_report(args, emit=self._emit)

        self.assertEqual(code, 0, msg=self.messages)
        self.assertEqual(spy_verify.call_count, 1)
        self.assertEqual(spy_write.call_count, 1)
        self.assertFalse(spy_write.call_args.kwargs.get("write_suite_result", True))

    def test_loaded_run_package_load_is_pure_and_does_not_invoke_verifier_or_reporter(self):
        from forward_e2e.execution.runpackage import LoadedRunPackage

        stage_dir, _stage_suite, _runtime_root, _task_run_id = self._make_stage2_package(complete=True)
        before_files = {
            str(p.relative_to(stage_dir)): p.read_bytes()
            for p in sorted(stage_dir.rglob("*"))
            if p.is_file()
        }
        with patch(
            "forward_e2e.suite.verifier.verify_and_recalculate_suite",
            side_effect=AssertionError("verify_and_recalculate_suite must not be called in load()"),
        ), patch(
            "forward_e2e.suite.reporter.OfflineReporter",
            side_effect=AssertionError("OfflineReporter must not be instantiated in load()"),
        ):
            package = LoadedRunPackage.load(stage_dir)

        after_files = {
            str(p.relative_to(stage_dir)): p.read_bytes()
            for p in sorted(stage_dir.rglob("*"))
            if p.is_file()
        }
        self.assertEqual(before_files, after_files)
        self.assertIsNone(package._verification)
        self.assertEqual(package.load_findings(), [])

    def test_repeated_access_to_package_getters_and_verify_does_not_duplicate_findings_or_reverify(self):
        from forward_e2e.execution.outcome import evaluate_run
        from forward_e2e.execution.runpackage import LoadedRunPackage, verify_run_package
        from forward_e2e.suite.verifier import verify_and_recalculate_suite

        stage_dir, stage_suite, _runtime_root, task_run_id = self._make_stage2_package(complete=True)
        # Introduce an integrity violation so verification produces non-empty findings
        (stage_suite / "runs" / task_run_id / "identity.json").write_text(
            '{"tampered": true}\n', encoding="utf-8"
        )

        package = LoadedRunPackage.load(stage_dir)
        self.assertEqual(package.load_findings(), [])

        with patch(
            "forward_e2e.suite.verifier.verify_and_recalculate_suite",
            wraps=verify_and_recalculate_suite,
        ) as spy_verify:
            _ = package.task_evidence()
            _ = package.task_evidence()
            _ = package.provenance_findings()
            _ = package.provenance_findings()
            _ = package.suite_status
            self.assertEqual(spy_verify.call_count, 0)
            self.assertEqual(package.load_findings(), [])

            v1 = verify_run_package(package)
            v2 = package.verify()
            o1 = evaluate_run(v1)
            o2 = evaluate_run(package)

            self.assertEqual(spy_verify.call_count, 1)
            self.assertIs(v1, v2)
            self.assertEqual(len(o1.findings), len(o2.findings))
            self.assertEqual(package.load_findings(), [])

    def test_cmd_report_is_idempotent_and_does_not_erase_suite_result_disagreement(self):
        stage_dir, stage_suite, _runtime_root, task_run_id = self._make_stage2_package(complete=True)
        suite_result_path = stage_suite / "suite-result.json"
        original_suite_result_bytes = suite_result_path.read_bytes()

        args = parse_e2e_args(
            [
                "report",
                "--run", str(stage_dir),
                "--output", str(self.output_root),
                "--workspace", str(self.workspace),
            ]
        )[1]
        self.assertEqual(cmd_report(args, emit=self._emit), 0)
        self.assertEqual(cmd_report(args, emit=self._emit), 0)
        self.assertEqual(suite_result_path.read_bytes(), original_suite_result_bytes)

        # Now make the task result FAILED and update artifact-index.json so integrity passes,
        # while leaving stored suite-result.json claiming PASSED -> SUITE_RESULT_DISAGREEMENT.
        task_result_path = stage_suite / "runs" / task_run_id / "result.json"
        task_result_data = json.loads(task_result_path.read_text(encoding="utf-8"))
        task_result_data["execution_status"] = "FAILED"
        self._write_json(task_result_path, task_result_data)

        import hashlib
        index_file = stage_suite / "artifact-index.json"
        index_data = json.loads(index_file.read_text(encoding="utf-8"))
        for entry in index_data["artifacts"]:
            if entry["relative_path"] == f"runs/{task_run_id}/result.json":
                raw = task_result_path.read_bytes()
                entry["sha256"] = hashlib.sha256(raw).hexdigest()
                entry["size_bytes"] = len(raw)
        self._write_json(index_file, index_data)
        stored_disagreeing_bytes = suite_result_path.read_bytes()

        self.messages.clear()
        first_fail_code = cmd_report(args, emit=self._emit)
        first_verdict = json.loads((stage_dir / "e2e-run-result.json").read_text(encoding="utf-8"))
        self.assertEqual(first_fail_code, 1)
        self.assertEqual(first_verdict["status"], "FAILED")
        self.assertIn(
            "SUITE_RESULT_DISAGREEMENT",
            [f["code"] for f in first_verdict["findings"]],
        )
        self.assertEqual(suite_result_path.read_bytes(), stored_disagreeing_bytes)

        # Second cmd_report must still fail with SUITE_RESULT_DISAGREEMENT because
        # suite-result.json was not overwritten during the first report run.
        self.messages.clear()
        second_fail_code = cmd_report(args, emit=self._emit)
        second_verdict = json.loads((stage_dir / "e2e-run-result.json").read_text(encoding="utf-8"))
        self.assertEqual(second_fail_code, 1)
        self.assertEqual(second_verdict["status"], "FAILED")
        self.assertIn(
            "SUITE_RESULT_DISAGREEMENT",
            [f["code"] for f in second_verdict["findings"]],
        )

    def test_grade_run_package_cmd_report_and_cmd_recover_produce_identical_verdicts_and_detect_inter_command_tampering(self):
        from forward_e2e.execution.executor import grade_run_package

        stage_dir, stage_suite, _runtime_root, task_run_id = self._make_stage2_package(complete=True)

        # 1. grade_run_package (used by `run`), `cmd_report`, and `cmd_recover` agree on PASSED
        run_outcome = grade_run_package(stage_dir)
        self.assertEqual(run_outcome.status, "PASSED")
        self.assertEqual(run_outcome.exit_code, 0)

        report_args = parse_e2e_args(
            [
                "report",
                "--run", str(stage_dir),
                "--output", str(self.output_root),
                "--workspace", str(self.workspace),
            ]
        )[1]
        report_exit = cmd_report(report_args, emit=self._emit)
        report_verdict = json.loads((stage_dir / "e2e-run-result.json").read_text(encoding="utf-8"))
        self.assertEqual(report_exit, run_outcome.exit_code)
        self.assertEqual(report_verdict["status"], run_outcome.status)

        recover_args = parse_e2e_args(
            [
                "recover",
                "--run", self.RUN_ID,
                "--output", str(self.output_root / "recovered"),
                "--workspace", str(self.workspace),
            ]
        )[1]
        recover_exit = cmd_recover(recover_args, emit=self._emit)
        recovered_verdict = json.loads(
            (self.output_root / "recovered" / self.RUN_ID / "e2e-run-result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(recover_exit, run_outcome.exit_code)
        self.assertEqual(recovered_verdict["status"], run_outcome.status)

        # 2. Tamper with an indexed evidence file between two separate `report` calls;
        # the second `cmd_report` re-verifies from disk (no cross-command cache) and fails closed.
        (stage_suite / "runs" / task_run_id / "identity.json").write_text(
            '{"tampered_between_reports": true}\n', encoding="utf-8"
        )
        tampered_exit = cmd_report(report_args, emit=self._emit)
        tampered_verdict = json.loads((stage_dir / "e2e-run-result.json").read_text(encoding="utf-8"))
        self.assertEqual(tampered_exit, 1)
        self.assertEqual(tampered_verdict["status"], "FAILED")
        self.assertIn(
            "SUITE_INTEGRITY_VIOLATION",
            [f["code"] for f in tampered_verdict["findings"]],
        )


if __name__ == "__main__":
    unittest.main()
