"""Unit tests for the single E2E command parser and dispatcher (forward_e2e/execution/cli.py).

CLI tests exercise ``main([...])`` with explicit arguments and inspect exit
codes and console output. Focused helper tests cover parser declarations,
recovery path resolution, and daemon lifecycle behavior.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import io
import json
import os
import signal
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forward_e2e.execution.cli import (
    ALLOWED_WITH_FROM,
    LOCK_IDENTITY_FLAGS,
    SEMANTIC_FLAGS,
    SUBCOMMANDS,
    _with_inner_dockerd,
    _load_execution_manifest,
    _resolve_recovery_target,
    parse_e2e_args,
    build_parser,
    cmd_run,
    main,
)
from forward_e2e.execution.delivery import DeliveryStatus
from forward_e2e.execution.errors import UsageError
from forward_e2e.execution.executor import grade_run_package
from forward_e2e.execution.runpackage import run_stage_dir
from forward_e2e.execution.runlock import write_run_lock
from forward_e2e.suite.supervisor import DinDSupervisor
from forward_e2e.suite.runtime import SuiteRuntimeError
from tests.unit.runner.support.packages import make_baseline_lock, setup_baseline_package

#: Two distinct, syntactically valid full 40-hex commit SHAs.
GONKA_SHA = "e86e4899bd8cf52d1ad4766c811f65230b2f9296"
CONTRACTS_SHA = "7497304e5dc6bf48accdd8c91549bc22de6997fc"

GONKA_REPO = "https://github.com/product-science/gonka.git"
CONTRACTS_REPO = "https://github.com/product-science/gonka24-smart-contract.git"

#: One acceptable value per semantic flag, used to prove that ``--from``
#: refuses each of them individually.
SEMANTIC_FLAG_VALUES = {
    "--gonka-repo": GONKA_REPO,
    "--gonka-path": "/input/gonka",
    "--gonka-sha": GONKA_SHA,
    "--contracts-repo": CONTRACTS_REPO,
    "--contracts-path": "/input/contracts",
    "--contracts-sha": CONTRACTS_SHA,
    "--profile": "smoke",
    "--scenario": "lock-exact-e",
    "--runner-image": "ghcr.io/product-science/a8-runner:latest",
}


class E2ECliTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-cli-")
        self.root = Path(self.tmp_dir.name)
        self.workspace = self.root / "workspace"
        self.output = self.root / "output"
        self.workspace.mkdir(parents=True)
        self.output.mkdir(parents=True)
        # Shell credentials and output defaults must not affect offline fixtures.
        environment = patch.dict(os.environ, {
            "E2E_CREDENTIAL_FILE": "",
            "E2E_OUTPUT_DIR": str(self.output),
            "E2E_WORKSPACE_DIR": str(self.workspace),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def tearDown(self):
        self.tmp_dir.cleanup()

    # -- helpers -------------------------------------------------------
    def _run_cli(self, argv):
        """Call ``main(argv)`` and return ``(exit_code, stdout, stderr)``."""
        stderr = io.StringIO()
        stdout = io.StringIO()
        with patch("sys.stderr", stderr), patch("sys.stdout", stdout):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_ordinary_run_uses_live_pinned_checkouts_without_creating_bundles(self):
        argv = self._plan_argv({"--workspace": str(self.workspace)})
        argv[0] = "run"
        _, args = parse_e2e_args(argv)
        seen = {}

        def fake_plan(request, *, emit):
            self.assertFalse(request.create_bundles)
            root = request.worktree_root
            for role in ("gonka", "contracts"):
                (root / role).mkdir(parents=True)
            seen["source_root"] = root
            return SimpleNamespace(
                lock=make_baseline_lock(),
                lock_path=self.root / "plan" / "run.lock.json",
                package_dir=self.root / "plan",
                gonka_source=SimpleNamespace(worktree=root / "gonka"),
                contracts_source=SimpleNamespace(worktree=root / "contracts"),
            )

        def fake_execute(request, *, emit):
            self.assertTrue(request.pre_acquired_sources["gonka"].worktree.is_dir())
            self.assertTrue(request.pre_acquired_sources["contracts"].worktree.is_dir())
            return 0

        with patch("forward_e2e.execution.planner.build_plan", side_effect=fake_plan), \
                patch("forward_e2e.execution.cli._with_inner_dockerd", side_effect=lambda action, **_: action()), \
                patch("forward_e2e.execution.executor.execute_plan", side_effect=fake_execute):
            self.assertEqual(cmd_run(args, "run", emit=lambda _: None), 0)
        self.assertFalse(seen["source_root"].exists())

    def _plan_argv(self, overrides=None):
        """A complete, valid ``plan`` argv that individual tests can corrupt.

        ``overrides`` maps a flag to a replacement value; ``None`` drops the
        flag entirely so that a required input can be left out on purpose.
        """
        values = {
            "--gonka-repo": GONKA_REPO,
            "--gonka-sha": GONKA_SHA,
            "--contracts-repo": CONTRACTS_REPO,
            "--contracts-sha": CONTRACTS_SHA,
            "--profile": "smoke",
            "--output": str(self.output / "package"),
        }
        values.update(overrides or {})
        argv = ["plan"]
        for flag, value in values.items():
            if value is None:
                continue
            argv.extend([flag, value])
        return argv

    def _fake_plan_result(self):
        package_dir = self.output / "package"
        return SimpleNamespace(
            lock=SimpleNamespace(plan_id="plan-0123456789abcdef", scenarios=["lock-exact-e"]),
            lock_path=package_dir / "run.lock.json",
            package_dir=package_dir,
            tasks=["lock-exact-e"],
        )

    def _assert_source_rejected(self, argv, expected_substring):
        """A bad source value must fail at validation time, before any work."""
        with patch("forward_e2e.execution.planner.build_plan") as build_plan, patch.object(
            DinDSupervisor, "start_dockerd"
        ) as start_dockerd:
            code, _stdout, stderr = self._run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("[INVALID_SOURCE_SPEC]", stderr)
        self.assertIn(expected_substring, stderr)
        build_plan.assert_not_called()
        start_dockerd.assert_not_called()

    def test_help_for_every_subcommand_exits_successfully(self):
        for command in SUBCOMMANDS:
            with self.subTest(command=command):
                code, stdout, stderr = self._run_cli([command, "--help"])
                self.assertEqual(code, 0, stderr)
                self.assertIn("usage:", stdout)

    def test_unsafe_plan_ids_are_rejected_before_planning_or_starting_docker(self):
        for plan_id in ("../escape", "/absolute", "nested/id", "", "x" * 66):
            for command in ("plan", "run"):
                with self.subTest(plan_id=plan_id, command=command):
                    argv = self._plan_argv({"--plan-id": plan_id})
                    argv[0] = command
                    with patch("forward_e2e.execution.planner.build_plan") as planner, patch.object(
                        DinDSupervisor, "start_dockerd"
                    ) as daemon:
                        code, _, stderr = self._run_cli(argv)
                    self.assertEqual(code, 2, stderr)
                    self.assertIn("Invalid plan id", stderr)
                    planner.assert_not_called()
                    daemon.assert_not_called()

    def test_an_unsafe_plan_id_in_a_hash_valid_lock_is_rejected_before_docker(self):
        lock = make_baseline_lock(scenarios=["lock-exact-e"])
        # Change only the plan identity, then seal a new envelope as the producer does.
        lock.plan_id = "../escape"
        lock_path = self.root / "unsafe" / "run.lock.json"
        write_run_lock(lock, lock_path)
        with patch.object(DinDSupervisor, "start_dockerd") as daemon:
            code, _, stderr = self._run_cli(["run", "--from", str(lock_path)])
        self.assertEqual(code, 2, stderr)
        self.assertIn("Invalid plan id", stderr)
        daemon.assert_not_called()

    def test_recovery_falls_back_to_layout_when_one_manifest_fact_is_missing_or_invalid(self):
        pkg, _, _, manifest = setup_baseline_package(
            self.root / "e2e-baseline-run", scenarios=["lock-exact-e"]
        )
        stage = run_stage_dir(self.workspace, manifest.run_id)
        stage.mkdir(parents=True)
        (stage / "run.lock.json").write_bytes((pkg / "run.lock.json").read_bytes())
        _, args = parse_e2e_args([
            "recover", "--run", str(pkg), "--workspace", str(self.workspace)
        ])
        for mutation in ("missing_hash", "invalid_notes"):
            with self.subTest(mutation=mutation):
                payload = manifest.to_dict()
                if mutation == "missing_hash":
                    del payload["lock_sha256"]
                else:
                    payload["notes"] = None
                (pkg / "execution-manifest.json").write_text(json.dumps(payload))
                run_id, workspace = _resolve_recovery_target(args, emit=lambda _: None)
                self.assertEqual(run_id, manifest.run_id)
                self.assertEqual(workspace, self.workspace / run_id / "suite")

    def test_recovery_manifest_lookup_does_not_read_an_external_symlink(self):
        """Recovery must not take its run identity from a linked external manifest. All fixtures are synthetic. No network, Docker, or live chain calls."""
        pkg, _, _, _ = setup_baseline_package(
            self.root / "e2e-linked-manifest", scenarios=["go-query-error-classification"]
        )
        manifest_path = pkg / "execution-manifest.json"
        external = self.root / "external-execution-manifest.json"
        external.write_bytes(manifest_path.read_bytes())
        manifest_path.unlink()
        manifest_path.symlink_to(external)

        self.assertIsNone(_load_execution_manifest(pkg))

    def test_repeated_daemon_failures_preserve_diagnostics_for_both_attempts(self):
        destination = self.output / "bootstrap-failures" / "plan-repeat"
        with patch("forward_e2e.suite.supervisor.DinDSupervisor") as factory:
            supervisor = factory.return_value
            supervisor.log_file = self.workspace / "dockerd.log"
            supervisor.data_root = self.workspace / "docker"
            supervisor.socket_path = self.workspace / "docker.sock"
            for failure in ("first", "second"):
                supervisor.log_file.write_text(failure)
                supervisor.start_dockerd.side_effect = SuiteRuntimeError(failure)
                code = _with_inner_dockerd(
                    lambda: self.fail("execution must not start"),
                    workspace_dir=self.workspace, diagnostics_dir=destination,
                    emit=lambda _: None,
                )
                self.assertEqual(code, 1)
        attempts = list(destination.iterdir())
        self.assertEqual(len(attempts), 2)
        self.assertEqual({(p / "dockerd.log").read_text() for p in attempts}, {"first", "second"})
        for attempt in attempts:
            payload = json.loads((attempt / "diagnostic.json").read_text())
            self.assertEqual(payload["error"], (attempt / "dockerd.log").read_text())

    def test_cancellation_during_failed_bootstrap_keeps_the_signal_exit_code_and_restores_handlers(self):
        original = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        # A regression in production cleanup must not leak handlers into later tests.
        for sig, handler in original.items():
            self.addCleanup(signal.signal, sig, handler)
        for sig, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=sig), patch("forward_e2e.suite.supervisor.DinDSupervisor") as factory:
                def fail_after_signal(**kwargs):
                    signal.getsignal(sig)(sig, None)
                    raise SuiteRuntimeError("startup timed out")
                factory.return_value.start_dockerd.side_effect = fail_after_signal
                code = _with_inner_dockerd(
                    lambda: self.fail("execution must not start"),
                    workspace_dir=self.workspace, emit=lambda _: None,
                )
                self.assertEqual(code, expected)
                for restored_sig, handler in original.items():
                    self.assertEqual(signal.getsignal(restored_sig), handler)

    # -- full SHA accepted, everything else refused ---------------------
    def test_full_forty_hex_sha_is_accepted_for_each_source_role(self):
        with patch(
            "forward_e2e.execution.planner.build_plan", return_value=self._fake_plan_result()
        ) as build_plan, patch.object(DinDSupervisor, "start_dockerd") as start_dockerd:
            code, _stdout, stderr = self._run_cli(self._plan_argv())
        self.assertEqual(code, 0, msg=stderr)
        build_plan.assert_called_once()
        start_dockerd.assert_not_called()
        request = build_plan.call_args.args[0]
        self.assertEqual(request.gonka.commit_sha, GONKA_SHA)
        self.assertEqual(request.contracts.commit_sha, CONTRACTS_SHA)
        self.assertEqual(request.gonka.repo_url, GONKA_REPO)
        self.assertEqual(request.contracts.repo_url, CONTRACTS_REPO)

    def test_uppercase_full_sha_is_accepted_and_normalised_for_both_roles(self):
        argv = self._plan_argv(
            {"--gonka-sha": GONKA_SHA.upper(), "--contracts-sha": CONTRACTS_SHA.upper()}
        )
        with patch(
            "forward_e2e.execution.planner.build_plan", return_value=self._fake_plan_result()
        ) as build_plan:
            code, _stdout, stderr = self._run_cli(argv)
        self.assertEqual(code, 0, msg=stderr)
        request = build_plan.call_args.args[0]
        self.assertEqual(request.gonka.commit_sha, GONKA_SHA)
        self.assertEqual(request.contracts.commit_sha, CONTRACTS_SHA)

    def test_branch_name_is_rejected_for_every_source_role(self):
        for flag in ("--gonka-sha", "--contracts-sha"):
            with self.subTest(flag=flag):
                self._assert_source_rejected(
                    self._plan_argv({flag: "main"}),
                    "does not accept the symbolic revision",
                )

    def test_tag_name_is_rejected_for_every_source_role(self):
        for flag in ("--gonka-sha", "--contracts-sha"):
            with self.subTest(flag=flag):
                self._assert_source_rejected(
                    self._plan_argv({flag: "v1.2.3"}),
                    "must be exactly 40 hexadecimal characters",
                )

    def test_head_is_rejected_for_every_source_role(self):
        for flag in ("--gonka-sha", "--contracts-sha"):
            with self.subTest(flag=flag):
                self._assert_source_rejected(
                    self._plan_argv({flag: "HEAD"}),
                    "does not accept the symbolic revision",
                )

    def test_short_sha_is_rejected_for_every_source_role(self):
        for flag, sha in (("--gonka-sha", GONKA_SHA), ("--contracts-sha", CONTRACTS_SHA)):
            with self.subTest(flag=flag):
                self._assert_source_rejected(
                    self._plan_argv({flag: sha[:12]}),
                    "too short (abbreviated SHAs are refused)",
                )

    def test_head_tilde_one_revision_expression_is_rejected(self):
        for flag in ("--gonka-sha", "--contracts-sha"):
            with self.subTest(flag=flag):
                self._assert_source_rejected(
                    self._plan_argv({flag: "HEAD~1"}),
                    "does not accept revision expressions",
                )

    def test_upstream_tracking_revision_expression_is_rejected(self):
        for flag in ("--gonka-sha", "--contracts-sha"):
            with self.subTest(flag=flag):
                self._assert_source_rejected(
                    self._plan_argv({flag: "main@{upstream}"}),
                    "does not accept revision expressions",
                )

    def test_full_ref_name_is_rejected_instead_of_being_resolved(self):
        self._assert_source_rejected(
            self._plan_argv({"--gonka-sha": "refs/heads/main"}),
            "does not accept a ref name",
        )

    # -- missing and contradictory sources ------------------------------
    def test_missing_source_for_a_required_role_is_a_clear_failure(self):
        for role, flags in (
            ("gonka", ("--gonka-repo",)),
            ("contracts", ("--contracts-repo",)),
        ):
            with self.subTest(role=role):
                argv = self._plan_argv({flag: None for flag in flags})
                with patch("forward_e2e.execution.planner.build_plan") as build_plan, patch.object(
                    DinDSupervisor, "start_dockerd"
                ) as start_dockerd:
                    code, _stdout, stderr = self._run_cli(argv)
                self.assertEqual(code, 2)
                self.assertIn("[INVALID_SOURCE_SPEC]", stderr)
                self.assertIn(f"{role} source is missing", stderr)
                self.assertIn(f"--{role}-repo", stderr)
                self.assertIn(f"--{role}-path", stderr)
                build_plan.assert_not_called()
                start_dockerd.assert_not_called()

    def test_missing_sha_for_a_supplied_repository_is_a_clear_failure(self):
        argv = self._plan_argv({"--gonka-sha": None})
        with patch("forward_e2e.execution.planner.build_plan") as build_plan:
            code, _stdout, stderr = self._run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("[INVALID_SOURCE_SPEC]", stderr)
        self.assertIn("--gonka-sha is required", stderr)
        build_plan.assert_not_called()

    def test_repo_and_path_for_the_same_role_are_mutually_exclusive(self):
        cases = (
            ("gonka", {"--gonka-path": str(self.root / "gonka")}),
            ("contracts", {"--contracts-path": str(self.root / "contracts")}),
        )
        for role, extra in cases:
            with self.subTest(role=role):
                with patch("forward_e2e.execution.planner.build_plan") as build_plan, patch.object(
                    DinDSupervisor, "start_dockerd"
                ) as start_dockerd:
                    code, _stdout, stderr = self._run_cli(self._plan_argv(extra))
                self.assertEqual(code, 2)
                self.assertIn("[INVALID_SOURCE_SPEC]", stderr)
                self.assertIn(
                    f"--{role}-repo and --{role}-path are mutually exclusive", stderr
                )
                build_plan.assert_not_called()
                start_dockerd.assert_not_called()

    def test_profile_and_explicit_scenarios_cannot_be_combined(self):
        argv = self._plan_argv() + ["--scenario", "lock-exact-e"]
        with patch("forward_e2e.execution.planner.resolve_runner_image") as resolve_image, patch(
            "forward_e2e.execution.planner.SourceAcquirer"
        ) as acquirer, patch.object(DinDSupervisor, "start_dockerd") as start_dockerd:
            code, _stdout, stderr = self._run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("[INVALID_SELECTION]", stderr)
        self.assertIn("Cannot combine --profile with --scenario", stderr)
        resolve_image.assert_not_called()
        acquirer.assert_not_called()
        start_dockerd.assert_not_called()

    def test_neither_profile_nor_scenario_is_refused_before_any_acquisition(self):
        argv = self._plan_argv({"--profile": None})
        with patch("forward_e2e.execution.planner.resolve_runner_image") as resolve_image, patch(
            "forward_e2e.execution.planner.SourceAcquirer"
        ) as acquirer:
            code, _stdout, stderr = self._run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("[INVALID_SELECTION]", stderr)
        self.assertIn("Either --profile or --scenario must be specified", stderr)
        resolve_image.assert_not_called()
        acquirer.assert_not_called()

    # -- `--from` replays a decision, it never re-opens it ---------------
    def test_run_and_rerun_from_lock_reject_every_semantic_override_flag(self):
        lock_path = str(self.root / "package" / "run.lock.json")
        for subcommand in ("run", "rerun"):
            for flag, value in SEMANTIC_FLAG_VALUES.items():
                with self.subTest(subcommand=subcommand, flag=flag):
                    argv = [
                        subcommand,
                        "--from",
                        lock_path,
                        flag,
                        value,
                    ]
                    with patch("forward_e2e.execution.cli.load_run_lock") as load_lock, patch(
                        "forward_e2e.execution.cli._with_inner_dockerd"
                    ) as with_dockerd, patch.object(
                        DinDSupervisor, "start_dockerd"
                    ) as start_dockerd:
                        code, _stdout, stderr = self._run_cli(argv)
                    self.assertEqual(code, 2)
                    self.assertIn("[LOCK_OVERRIDE_REJECTED]", stderr)
                    self.assertIn("--from executes a saved plan exactly", stderr)
                    self.assertIn(flag, stderr)
                    self.assertIn("rejected_flags", stderr)
                    self.assertIn("allowed_with_from", stderr)
                    load_lock.assert_not_called()
                    with_dockerd.assert_not_called()
                    start_dockerd.assert_not_called()

    def test_a_plan_id_given_with_from_is_refused_as_a_lock_override_before_the_lock_is_read(self):
        # Before this check existed the flag parsed and was then never read on
        # the replay path, so the operator believed the plan had been renamed.
        lock_path = str(self.root / "package" / "run.lock.json")
        for subcommand in ("run", "rerun"):
            with self.subTest(subcommand=subcommand):
                argv = [subcommand, "--from", lock_path, "--plan-id", "renamed-plan"]
                with patch("forward_e2e.execution.cli.load_run_lock") as load_lock, patch(
                    "forward_e2e.execution.cli._with_inner_dockerd"
                ) as with_dockerd, patch.object(
                    DinDSupervisor, "start_dockerd"
                ) as start_dockerd:
                    code, _stdout, stderr = self._run_cli(argv)
                self.assertEqual(code, 2, stderr)
                self.assertIn("[LOCK_OVERRIDE_REJECTED]", stderr)
                self.assertIn("the plan id comes from the lock", stderr)
                self.assertIn("--plan-id", stderr)
                self.assertIn("allowed_with_from", stderr)
                self.assertNotIn("--plan-id", ALLOWED_WITH_FROM)
                load_lock.assert_not_called()
                with_dockerd.assert_not_called()
                start_dockerd.assert_not_called()

    def test_plan_id_stays_out_of_the_semantic_flag_set_but_is_a_lock_identity_flag(self):
        # The semantic set is the list of proof-changing flags; a plan id does
        # not change what is proven, so the refusal lives in a separate list.
        self.assertNotIn("--plan-id", {flag for _, flag in SEMANTIC_FLAGS})
        self.assertEqual(LOCK_IDENTITY_FLAGS, (("plan_id", "--plan-id"),))

    def test_plan_and_run_without_from_still_pass_an_explicit_plan_id_to_the_planner(self):
        fake_result = self._fake_plan_result()
        # `run` hands the already-acquired checkouts to the executor; the fake
        # only needs the attributes that are read before that hand-over.
        fake_result.gonka_source = SimpleNamespace(worktree=self.root / "gonka")
        fake_result.contracts_source = SimpleNamespace(worktree=self.root / "contracts")
        for command in ("plan", "run"):
            with self.subTest(command=command):
                argv = self._plan_argv({"--plan-id": "plan-chosen-by-hand"})
                argv[0] = command
                with patch(
                    "forward_e2e.execution.planner.build_plan", return_value=fake_result,
                ) as build_plan, patch(
                    "forward_e2e.execution.cli._execute_selected_run", return_value=0
                ), patch.object(DinDSupervisor, "start_dockerd") as start_dockerd:
                    code, _stdout, stderr = self._run_cli(argv)
                self.assertEqual(code, 0, stderr)
                build_plan.assert_called_once()
                self.assertEqual(build_plan.call_args.args[0].plan_id, "plan-chosen-by-hand")
                start_dockerd.assert_not_called()

    def test_run_and_rerun_forward_operational_flags_and_return_the_executor_exit_code(self):
        credential_file = self.root / "token.txt"
        credential_file.write_text("synthetic-token-value", encoding="utf-8")
        lock_path = self.root / "package" / "run.lock.json"
        lock = make_baseline_lock()
        write_run_lock(lock, lock_path)
        for command in ("run", "rerun"):
            with self.subTest(command=command):
                argv = [
                    command, "--from", str(lock_path),
                    "--run-id", "run-0001",
                    "--output", str(self.output / "runs"),
                    "--workspace", str(self.workspace),
                    "--runtime-root", str(self.root / "runtime"),
                    "--credential-file", str(credential_file),
                    "--credential-username", "fixture-user",
                    "--parent-run-id", "run-0000",
                ]
                # Run the real CLI/daemon wrapper; isolate execution and Docker boundaries.
                with patch(
                    "forward_e2e.execution.executor.execute_plan", return_value=1
                ) as execute, patch.object(
                    DinDSupervisor, "start_dockerd"
                ) as start_dockerd, patch.object(
                    DinDSupervisor, "stop_dockerd", return_value=True
                ) as stop_dockerd:
                    code, _stdout, stderr = self._run_cli(argv)
                self.assertEqual(code, 1, msg=stderr)
                execute.assert_called_once()
                request = execute.call_args.args[0]
                self.assertEqual(request.lock.lock_sha256, lock.lock_sha256)
                self.assertEqual(request.lock_path, lock_path)
                self.assertEqual(request.package_dir, lock_path.parent.resolve())
                self.assertEqual(request.run_id, "run-0001")
                self.assertEqual(request.output_dir, self.output / "runs")
                self.assertEqual(request.workspace_dir, self.workspace)
                self.assertEqual(request.runtime_root, self.root / "runtime")
                self.assertEqual(request.credential.secret_file, credential_file)
                self.assertEqual(request.credential.username, "fixture-user")
                self.assertEqual(request.parent_run_id, "run-0000")
                self.assertEqual(request.command, command)
                self.assertTrue(request.replay)
                self.assertFalse(request.keep_resources)
                start_dockerd.assert_called_once()
                stop_dockerd.assert_called_once()

    def test_rerun_without_from_lock_is_a_usage_error(self):
        with patch("forward_e2e.execution.cli._with_inner_dockerd") as with_dockerd, patch.object(
            DinDSupervisor, "start_dockerd"
        ) as start_dockerd:
            code, _stdout, stderr = self._run_cli(["rerun", "--profile", "smoke"])
        self.assertEqual(code, 2)
        self.assertIn("[USAGE]", stderr)
        self.assertIn("rerun requires --from", stderr)
        with_dockerd.assert_not_called()
        start_dockerd.assert_not_called()

    def test_keep_resources_is_refused_inside_the_runner_container(self):
        with patch("forward_e2e.execution.cli._with_inner_dockerd") as with_dockerd, patch.object(
            DinDSupervisor, "start_dockerd"
        ) as start_dockerd:
            code, _stdout, stderr = self._run_cli(
                [
                    "run",
                    "--from",
                    str(self.root / "package" / "run.lock.json"),
                    "--keep-resources",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("[USAGE]", stderr)
        self.assertIn("--keep-resources is not supported", stderr)
        with_dockerd.assert_not_called()
        start_dockerd.assert_not_called()

    def test_a_daemon_bootstrap_crash_writes_a_durable_diagnostic_package(self):
        diagnostic_dir = self.output / "bootstrap-failures" / "run-0003"
        daemon_log = self.workspace / "dockerd.log"
        secret = "ghp_" + "C" * 32
        daemon_log.write_text(
            f"Initializing buildkit\npanic: broken bbolt page; auth={secret}\n", encoding="utf-8"
        )

        with patch("forward_e2e.suite.supervisor.DinDSupervisor") as supervisor_type:
            supervisor = supervisor_type.return_value
            supervisor.log_file = daemon_log
            supervisor.data_root = Path("/var/lib/docker")
            supervisor.socket_path = Path("/var/run/docker.sock")
            supervisor.start_dockerd.side_effect = SuiteRuntimeError(
                f"dockerd exited with code 2; auth={secret}"
            )
            messages = []

            code = _with_inner_dockerd(
                lambda: self.fail("execution must not start after bootstrap failure"),
                workspace_dir=self.workspace,
                diagnostics_dir=diagnostic_dir,
                emit=messages.append,
            )

        self.assertEqual(code, 1)
        diagnostic_dir, = diagnostic_dir.iterdir()
        safe_log = (diagnostic_dir / "dockerd.log").read_text(encoding="utf-8")
        self.assertIn("panic: broken bbolt page", safe_log)
        self.assertNotIn(secret, safe_log)
        diagnostic = json.loads(
            (diagnostic_dir / "diagnostic.json").read_text(encoding="utf-8")
        )
        self.assertEqual(diagnostic["phase"], "bootstrap")
        self.assertIn("dockerd exited with code 2", diagnostic["error"])
        self.assertNotIn(secret, diagnostic["error"])
        self.assertTrue(any("***REDACTED***" in message for message in messages))
        self.assertTrue(any("Daemon bootstrap diagnostics:" in message for message in messages))

    def test_a_successful_run_allows_a_full_minute_for_graceful_daemon_shutdown(self):
        with patch("forward_e2e.suite.supervisor.DinDSupervisor") as supervisor_type:
            supervisor = supervisor_type.return_value
            supervisor.start_dockerd.return_value = None
            supervisor.stop_dockerd.return_value = True

            code = _with_inner_dockerd(
                lambda: 0,
                workspace_dir=self.workspace,
                diagnostics_dir=self.output / "unused",
                emit=lambda _message: None,
            )

        self.assertEqual(code, 0)
        supervisor.stop_dockerd.assert_called_once_with(timeout_seconds=60.0)

    # -- output identity -------------------------------------------------
    def test_existing_lock_at_the_output_location_is_refused_before_anything_starts(self):
        package_dir = self.output / "package"
        package_dir.mkdir(parents=True)
        (package_dir / "run.lock.json").write_text("{}", encoding="utf-8")
        with patch("forward_e2e.execution.planner.resolve_runner_image") as resolve_image, patch(
            "forward_e2e.execution.planner.SourceAcquirer"
        ) as acquirer, patch.object(DinDSupervisor, "start_dockerd") as start_dockerd:
            code, _stdout, stderr = self._run_cli(self._plan_argv())
        self.assertEqual(code, 2)
        self.assertIn("[OUTPUT_COLLISION]", stderr)
        self.assertIn("A plan already exists at this output location", stderr)
        resolve_image.assert_not_called()
        acquirer.assert_not_called()
        start_dockerd.assert_not_called()

    def test_non_empty_output_directory_is_refused_before_anything_is_fetched(self):
        package_dir = self.output / "package"
        package_dir.mkdir(parents=True)
        (package_dir / "leftover.txt").write_text("stale", encoding="utf-8")
        with patch("forward_e2e.execution.planner.resolve_runner_image") as resolve_image, patch(
            "forward_e2e.execution.planner.SourceAcquirer"
        ) as acquirer, patch.object(DinDSupervisor, "start_dockerd") as start_dockerd:
            code, _stdout, stderr = self._run_cli(self._plan_argv())
        self.assertEqual(code, 2)
        self.assertIn("[OUTPUT_COLLISION]", stderr)
        self.assertIn("The --output directory is not empty", stderr)
        resolve_image.assert_not_called()
        acquirer.assert_not_called()
        start_dockerd.assert_not_called()

    def test_plan_without_output_is_refused(self):
        argv = self._plan_argv({"--output": None})
        with patch("forward_e2e.execution.planner.build_plan") as build_plan:
            code, _stdout, stderr = self._run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("[USAGE]", stderr)
        self.assertIn("--output is required", stderr)
        build_plan.assert_not_called()

    # -- no inner dockerd for the read-only subcommands -------------------
    def test_list_does_not_start_the_inner_dockerd(self):
        with patch.object(DinDSupervisor, "start_dockerd") as start_dockerd:
            code, stdout, stderr = self._run_cli(["list"])
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("=== E2E catalog ===", stdout)
        start_dockerd.assert_not_called()

    def test_plan_does_not_start_the_inner_dockerd(self):
        with patch(
            "forward_e2e.execution.planner.build_plan", return_value=self._fake_plan_result()
        ), patch.object(DinDSupervisor, "start_dockerd") as start_dockerd:
            code, _stdout, stderr = self._run_cli(self._plan_argv())
        self.assertEqual(code, 0, msg=stderr)
        start_dockerd.assert_not_called()

    def test_report_does_not_start_the_inner_dockerd(self):
        argv = [
            "report",
            "--run",
            "no-such-run",
            "--workspace",
            str(self.workspace),
            "--output",
            str(self.output),
        ]
        with patch.object(DinDSupervisor, "start_dockerd") as start_dockerd:
            code, _stdout, stderr = self._run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("[USAGE]", stderr)
        self.assertIn("The requested run was not found", stderr)
        start_dockerd.assert_not_called()

    def test_recover_does_not_start_the_inner_dockerd(self):
        run_id = "run-20240101-000000"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(
            stage_dir,
            scenarios=["go-query-error-classification"],
            run_id=run_id,
            delivery_status=DeliveryStatus.COMPLETED,
        )

        argv = [
            "recover",
            "--run",
            run_id,
            "--workspace",
            str(self.workspace),
            "--output",
            str(self.output),
        ]
        with patch(
            "forward_e2e.execution.executor.grade_run_package", wraps=grade_run_package
        ) as spy_grader, patch.object(
            DinDSupervisor, "start_dockerd"
        ) as start_dockerd:
            code, _stdout, stderr = self._run_cli(argv)
        self.assertEqual(code, 0, msg=stderr)
        spy_grader.assert_called_once_with(self.output / run_id)
        start_dockerd.assert_not_called()

    def test_recover_on_incomplete_package_returns_failure_and_does_not_start_dockerd(self):
        run_id = "run-20240101-000000"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(
            stage_dir,
            scenarios=["go-query-error-classification"],
            run_id=run_id,
            delivery_status=DeliveryStatus.COMPLETED,
        )
        (stage_dir / "execution-manifest.json").unlink()

        argv = [
            "recover",
            "--run",
            run_id,
            "--workspace",
            str(self.workspace),
            "--output",
            str(self.output),
        ]
        with patch.object(
            DinDSupervisor, "start_dockerd"
        ) as start_dockerd:
            code, stdout, stderr = self._run_cli(argv)
        self.assertEqual(code, 1, msg=f"stdout={stdout}\nstderr={stderr}")
        start_dockerd.assert_not_called()

    # -- the documented examples must match the parser --------------------
    def test_build_parser_exposes_every_documented_subcommand(self):
        self.assertEqual(SUBCOMMANDS, ("list", "plan", "run", "rerun", "report", "recover"))
        for subcommand in SUBCOMMANDS:
            with self.subTest(subcommand=subcommand):
                parser = build_parser(subcommand)
                self.assertEqual(parser.prog, f"run-e2e {subcommand}")

    def test_build_parser_exposes_the_documented_flags_of_each_subcommand(self):
        expected = {
            "list": {"--json"},
            "plan": {
                "--gonka-repo",
                "--gonka-path",
                "--gonka-sha",
                "--contracts-repo",
                "--contracts-path",
                "--contracts-sha",
                "--profile",
                "--scenario",
                "--output",
                "--workspace",
                "--runtime-root",
                "--runner-image",
                "--credential-file",
                "--credential-username",
                "--plan-id",
            },
            "run": {
                "--gonka-repo",
                "--gonka-path",
                "--gonka-sha",
                "--contracts-repo",
                "--contracts-path",
                "--contracts-sha",
                "--profile",
                "--scenario",
                "--output",
                "--workspace",
                "--runtime-root",
                "--runner-image",
                "--credential-file",
                "--credential-username",
                "--run-id",
                "--keep-resources",
                "--plan-id",
                "--from",
                "--parent-run-id",
            },
            "report": {"--run", "--output", "--workspace"},
            "recover": {"--run", "--output", "--workspace"},
        }
        expected["rerun"] = set(expected["run"])
        for subcommand, flags in expected.items():
            with self.subTest(subcommand=subcommand):
                options = self._option_strings(build_parser(subcommand))
                self.assertTrue(
                    flags.issubset(options),
                    msg=f"missing from {subcommand}: {sorted(flags - options)}",
                )

    def test_plan_parser_does_not_expose_the_execution_only_flags(self):
        options = self._option_strings(build_parser("plan"))
        for flag in ("--from", "--run-id", "--parent-run-id", "--keep-resources"):
            with self.subTest(flag=flag):
                self.assertNotIn(flag, options)

    def test_report_and_recover_require_the_run_selector(self):
        for subcommand in ("report", "recover"):
            with self.subTest(subcommand=subcommand):
                parser = build_parser(subcommand)
                run_action = [a for a in parser._actions if "--run" in a.option_strings]
                self.assertEqual(len(run_action), 1)
                self.assertTrue(run_action[0].required)

    def test_build_parser_rejects_an_unknown_subcommand(self):
        with self.assertRaises(UsageError):
            build_parser("deploy")

    def test_unknown_command_is_a_usage_error_naming_the_available_commands(self):
        code, _stdout, stderr = self._run_cli(["deploy"])
        self.assertEqual(code, 2)
        self.assertIn("[USAGE]", stderr)
        self.assertIn("Unknown command 'deploy'", stderr)
        for subcommand in SUBCOMMANDS:
            self.assertIn(subcommand, stderr)

    def test_no_command_at_all_is_a_usage_error(self):
        code, _stdout, stderr = self._run_cli([])
        self.assertEqual(code, 2)
        self.assertIn("[USAGE]", stderr)
        self.assertIn("A command is required", stderr)

    def test_semantic_flags_match_the_required_set_and_exist_in_the_run_parser(self):
        expected = {(flag[2:].replace("-", "_"), flag) for flag in SEMANTIC_FLAG_VALUES}
        self.assertEqual(set(SEMANTIC_FLAGS), expected)
        parser = build_parser("run")
        options = self._option_strings(parser)
        destinations = {action.dest for action in parser._actions}
        for attr, flag in SEMANTIC_FLAGS:
            with self.subTest(flag=flag):
                self.assertIn(flag, options)
                self.assertIn(attr, destinations)

    # -- secrets never travel through the console -------------------------
    def test_credential_file_value_never_appears_in_the_output(self):
        secret = "ghp-do-not-print-me-0123456789"
        credential_file = self.root / "token.txt"
        credential_file.write_text(secret, encoding="utf-8")
        package_dir = self.output / "package"
        package_dir.mkdir(parents=True)
        (package_dir / "leftover.txt").write_text("stale", encoding="utf-8")
        argv = self._plan_argv() + ["--credential-file", str(credential_file)]
        with patch("forward_e2e.execution.planner.resolve_runner_image") as resolve_image, patch(
            "forward_e2e.execution.planner.SourceAcquirer"
        ) as acquirer, patch.object(DinDSupervisor, "start_dockerd") as start_dockerd:
            code, stdout, stderr = self._run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("[OUTPUT_COLLISION]", stderr)
        self.assertNotIn(secret, stderr)
        self.assertNotIn(secret, stdout)
        resolve_image.assert_not_called()
        acquirer.assert_not_called()
        start_dockerd.assert_not_called()

    def test_missing_credential_file_is_reported_without_inventing_a_secret(self):
        missing = self.root / "absent-token.txt"
        argv = self._plan_argv() + ["--credential-file", str(missing)]
        with patch("forward_e2e.execution.planner.build_plan") as build_plan:
            code, stdout, stderr = self._run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("[INVALID_SOURCE_SPEC]", stderr)
        self.assertIn("Credential file does not exist", stderr)
        self.assertNotIn("ghp-", stderr)
        self.assertNotIn("ghp-", stdout)
        build_plan.assert_not_called()

    # -- small utility ----------------------------------------------------
    @staticmethod
    def _option_strings(parser):
        return {
            option
            for action in parser._actions
            for option in action.option_strings
        }


if __name__ == "__main__":
    unittest.main()
