"""Regression tests for the host wrappers ``ops/e2e/run-e2e.sh`` and
``ops/e2e/Run-E2E.ps1``.

The wrappers forward the acceptance CLI verbatim into the runner container and
only rewrite what cannot be rewritten from inside it: the host paths. The
interesting case is ``--run``. ``report``/``recover`` accept either a bare run
id, which already means the same thing inside the container, or a host
directory, which does not: only one host directory is bound for evidence, at
``/out``. A run directory therefore has to be translated into ``/out/<relative>``
against the output root -- the one given with ``--output``, or the one inferred
from the documented ``<output>/runs/<run-id>`` layout -- and a run that does not
live under that root has to be refused rather than silently mounted away.

The Bash wrapper is executed for real, with a fake ``docker`` placed first on
``PATH`` that records the argument vector it was handed. PowerShell execution checks use a fake Docker function when pwsh is available.
Separate source checks only pin selected lines and do not establish runtime
behavior or parity with Bash.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ops.a8.e2e.cli import parse_e2e_args
from ops.a8.tests.real_fixtures import REQUIRED_RECORDED_EVIDENCE

REPO_ROOT = Path(__file__).resolve().parents[3]
BASH_WRAPPER = REPO_ROOT / "ops" / "e2e" / "run-e2e.sh"
POWERSHELL_WRAPPER = REPO_ROOT / "ops" / "e2e" / "Run-E2E.ps1"
COMPOSE_RELPATH = "ops/a8/compose.yaml"
SERVICE = "e2e-runner"

RUN_ID = "20240101-000000-abcdef"


class ComposeNetworkContractTests(unittest.TestCase):
    def test_runner_config_selects_the_default_bridge_for_nested_dockerd(self):
        compose = (REPO_ROOT / COMPOSE_RELPATH).read_text(encoding="utf-8")
        # Check active lines in this service only. This is a static contract
        # for the checked-in block layout, not a runtime DNS probe or YAML parser.
        service = re.search(
            r"^  e2e-runner:[ \t]*(?:#.*)?\n(?:(?:[ \t]*(?:#.*)?| {4}[^\n]*)\n)*",
            compose,
            re.MULTILINE,
        )
        self.assertIsNotNone(service, "the runner service block is missing")
        self.assertRegex(
            service.group(0), r"(?m)^    network_mode: bridge[ \t]*(?:#.*)?$"
        )


#: A ``docker`` that answers image-inspect and Compose-version queries, and
#: records other Compose invocations for Python assertions to validate.
#: Unsupported command families fail; nothing is forwarded to a real daemon.
FAKE_DOCKER = """#!/usr/bin/env bash
set -u

FAKE_ID="sha256:1111111111111111111111111111111111111111111111111111111111111111"
FAKE_DIGEST="registry.invalid/a8-runner@sha256:2222222222222222222222222222222222222222222222222222222222222222"

case "${1:-}" in
    compose)
        if [ "${2:-}" = "version" ]; then
            echo "Docker Compose version v2.99.0-fake"
            exit 0
        fi
        : > "$FAKE_DOCKER_ARGV_FILE"
        for arg in "$@"; do
            printf '%s\\n' "$arg" >> "$FAKE_DOCKER_ARGV_FILE"
        done
        {
            printf 'OUTPUT_DIR=%s\\n' "${OUTPUT_DIR:-}"
            printf 'E2E_RUNNER_IMAGE=%s\\n' "${E2E_RUNNER_IMAGE:-}"
            printf 'E2E_RUNNER_IMAGE_ID=%s\\n' "${E2E_RUNNER_IMAGE_ID:-}"
            printf 'E2E_RUNNER_IMAGE_DIGEST=%s\\n' "${E2E_RUNNER_IMAGE_DIGEST:-}"
            printf 'GONKA_DIR=%s\\n' "${GONKA_DIR:-}"
            printf 'CONTRACTS_DIR=%s\\n' "${CONTRACTS_DIR:-}"
            printf 'E2E_PLAN_DIR=%s\\n' "${E2E_PLAN_DIR:-}"
            printf 'E2E_SECRETS_DIR=%s\\n' "${E2E_SECRETS_DIR:-}"
            printf 'A8_DOCKER_ROOT_VOLUME=%s\\n' "${A8_DOCKER_ROOT_VOLUME:-}"
        } > "$FAKE_DOCKER_ENV_FILE"
        exit 0
        ;;
    image)
        if [ "${2:-}" = "inspect" ]; then
            case "${4:-}" in
                *RepoDigests*)
                    # A moved tag must not be queried again for metadata.
                    [ "${5:-}" = "$FAKE_ID" ] || exit 96
                    printf '%s\\n' "$FAKE_DIGEST" ;;
                *)             printf '%s\\n' "$FAKE_ID" ;;
            esac
            exit 0
        fi
        ;;
esac

printf 'fake docker: unexpected invocation: %s\\n' "$*" >&2
exit 97
"""


@unittest.skipUnless(
    bool(shutil.which("bash")) and os.name == "posix",
    "the Bash wrapper can only be exercised on a POSIX host that has bash",
)
class BashWrapperRunTranslationTests(unittest.TestCase):
    """``run-e2e.sh`` rewrites a host run directory into the container mount."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-wrap-")
        self.root = Path(self.tmp_dir.name).resolve()
        # The wrapper derives defaults from its own location, not cwd. Copy
        # its real bytes into a disposable layout to isolate default mounts too.
        self.repo_root = self.root / "repository"
        self.wrapper = self.repo_root / "ops/e2e/run-e2e.sh"
        self.wrapper.parent.mkdir(parents=True)
        shutil.copyfile(BASH_WRAPPER, self.wrapper)
        self.compose_file = self.repo_root / COMPOSE_RELPATH
        self.compose_file.parent.mkdir(parents=True)
        shutil.copyfile(REPO_ROOT / COMPOSE_RELPATH, self.compose_file)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.fake_docker = self.bin_dir / "docker"
        self.fake_docker.write_text(FAKE_DOCKER, encoding="utf-8")
        self.fake_docker.chmod(0o755)
        self.argv_file = self.root / "docker-argv.txt"
        self.env_file = self.root / "docker-env.txt"
        self.bridge_dir = self.root / "bridge"
        self.output_dir = self.root / "out"
        self.run_dir = self.output_dir / "runs" / RUN_ID
        self.run_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    # -- helpers -------------------------------------------------------
    def _invoke(self, *args):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_DOCKER_ARGV_FILE"] = str(self.argv_file)
        env["FAKE_DOCKER_ENV_FILE"] = str(self.env_file)
        # Keep the wrapper's Git bridge out of the checkout.
        env["E2E_BRIDGE_DIR"] = str(self.bridge_dir)
        env.pop("E2E_RUNNER_IMAGE", None)
        return subprocess.run(
            ["bash", str(self.wrapper), *args],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _forwarded_arguments(self, result):
        """Return the complete Docker argument vector, including its header.

        Also proves the wrapper really invoked ``compose run --rm`` against the
        disposable repository's copy of the compose file.
        """
        self.assertEqual(result.returncode, 0, msg=f"stderr: {result.stderr}")
        self.assertTrue(
            self.argv_file.is_file(),
            msg=f"the fake docker was never asked to run the service; stderr: {result.stderr}",
        )
        argv = self.argv_file.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(argv), 8, msg=f"argv too short: {argv}")
        self.assertEqual(argv[0], "compose")
        self.assertEqual(argv[1], "-f")
        self.assertEqual(
            argv[2], str(self.compose_file),
            msg=f"unexpected compose file: {argv[2]}",
        )
        self.assertEqual(argv[3:5], ["run", "--rm"])
        self.assertEqual(argv[5], "-e")
        self.assertTrue(argv[6].startswith("E2E_RUNNER_IMAGE="))
        return argv

    def _container_arguments(self, result):
        argv = self._forwarded_arguments(result)
        self.assertEqual(argv[7], SERVICE)
        return argv[8:]

    def _recorded_environment(self, name):
        self.assertTrue(self.env_file.is_file(), "the fake docker recorded no environment")
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key == name:
                return value
        self.fail(f"{name} was not recorded by the fake docker")

    # -- tests ---------------------------------------------------------
    def test_replaying_a_plan_rejects_an_explicit_runner_image_in_either_argument_order(self):
        plan = self.root / "plan"
        plan.mkdir()
        (plan / "run.lock.json").write_text("{}", encoding="utf-8")
        for command in ("run", "rerun"):
            for flags in (
                ["--from", str(plan), "--runner-image", "a8-runner:local"],
                ["--runner-image=a8-runner:local", f"--from={plan}"],
            ):
                with self.subTest(command=command, flags=flags):
                    result = self._invoke(command, *flags)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("--runner-image cannot be combined with --from", result.stderr)
                    self.assertFalse(self.argv_file.exists())

    def test_full_image_ids_and_registry_digests_do_not_produce_mutable_tag_warnings(self):
        for locator in ("sha256:" + "1" * 64, "registry.invalid/runner@sha256:" + "2" * 64):
            with self.subTest(locator=locator):
                result = self._invoke("list", "--runner-image", locator)
                self.assertEqual(self._container_arguments(result), ["list"])
                self.assertNotIn("mutable tag", result.stderr)
        result = self._invoke("list", "--runner-image", "a8-runner:local")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mutable tag", result.stderr)

    def test_the_inspected_image_is_launched_while_the_original_locator_is_preserved(self):
        result = self._invoke("list", "--runner-image=registry.invalid/runner:changing")
        argv = self._forwarded_arguments(result)
        self.assertEqual(argv[6], "E2E_RUNNER_IMAGE=registry.invalid/runner:changing")
        self.assertEqual(self._recorded_environment("E2E_RUNNER_IMAGE"), "sha256:" + "1" * 64)
        self.assertEqual(
            self._recorded_environment("E2E_RUNNER_IMAGE"),
            self._recorded_environment("E2E_RUNNER_IMAGE_ID"),
        )
        self.assertEqual(self._recorded_environment("E2E_RUNNER_IMAGE_DIGEST"),
                         "registry.invalid/a8-runner@sha256:" + "2" * 64)
        compose = (REPO_ROOT / COMPOSE_RELPATH).read_text(encoding="utf-8")
        self.assertIn("    image: ${E2E_RUNNER_IMAGE:-a8-runner:local}", compose)

    def test_an_output_with_missing_ancestors_is_created_before_path_translation(self):
        output = self.root / "new parent" / "nested" / "output"
        result = self._invoke("report", "--run", RUN_ID, "--output", str(output))
        self.assertEqual(self._container_arguments(result), ["report", "--run", RUN_ID, "--output", "/out"])
        self.assertTrue(output.is_dir())
        self.assertEqual(self._recorded_environment("OUTPUT_DIR"), str(output))

    def test_equals_style_run_and_output_options_translate_like_separate_arguments(self):
        result = self._invoke("report", f"--run={self.run_dir}", f"--output={self.output_dir}",
                              "--docker-root-volume=review-cache")
        forwarded = self._container_arguments(result)
        self.assertEqual(forwarded, ["report", "--run", f"/out/runs/{RUN_ID}", "--output", "/out"])
        parse_e2e_args(forwarded)
        self.assertEqual(self._recorded_environment("A8_DOCKER_ROOT_VOLUME"), "review-cache")

    def test_equals_style_plan_and_credential_paths_preserve_spaces_and_equals_in_values(self):
        plan = self.root / "plan = one"
        plan.mkdir()
        (plan / "run.lock.json").write_text("{}", encoding="utf-8")
        credential = self.root / "synthetic = credential"
        credential.write_text("synthetic", encoding="utf-8")
        result = self._invoke("run", f"--from={plan}", f"--credential-file={credential}",
                              f"--output={self.output_dir}")
        forwarded = self._container_arguments(result)
        self.assertEqual(forwarded, ["run", "--from", "/input/plan/run.lock.json",
                                    "--credential-file", "/run/secrets/e2e/synthetic = credential",
                                    "--output", "/out"])
        parse_e2e_args(forwarded)
        self.assertEqual(self._recorded_environment("E2E_PLAN_DIR"), str(plan))
        self.assertEqual(self._recorded_environment("E2E_SECRETS_DIR"), str(self.root))

    def _install_fake_git(self, *, fail_fetch=False):
        # Only the Git process boundary is replaced; the actual wrapper owns
        # directory allocation, mount selection, and cleanup.
        git = self.bin_dir / "git"
        git.write_text("#!/usr/bin/env bash\n"
                       'if [ "$1" = init ]; then exit 0; fi\n'
                       'case "$3" in\n'
                       f'fetch) exit {23 if fail_fetch else 0} ;;\n'
                       'cat-file) echo commit ;;\n'
                       'update-ref) printf "%s" "$5" > "$2/selected-sha" ;;\n'
                       '*) exit 97 ;;\nesac\n', encoding="utf-8")
        git.chmod(0o755)

    def test_overlapping_local_plans_keep_distinct_bridges_alive_and_clean_only_their_own_directories(self):
        self._install_fake_git()
        self.bridge_dir.mkdir()
        sentinel = self.bridge_dir / "gonka.git" / "unrelated"
        sentinel.parent.mkdir()
        sentinel.write_text("keep", encoding="utf-8")
        # The first fake Compose invocation synchronously starts a second
        # wrapper while its own bridges are still mounted. No timing sleeps.
        hook = r'''
        if [ "${FAKE_NESTED:-}" != 1 ]; then
            first="$GONKA_DIR"
            FAKE_NESTED=1 FAKE_DOCKER_ENV_FILE="$FAKE_DOCKER_ENV_FILE.nested" \
                bash "$FAKE_WRAPPER" plan --gonka-path="$FAKE_SOURCE" \
                --gonka-sha=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
                --output="$OUTPUT_DIR" || exit 91
            [ -d "$first" ] || exit 92
            [ "$(cat "$first/selected-sha")" = aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ] || exit 93
        fi
'''
        self.fake_docker.write_text(FAKE_DOCKER.replace('        : > "$FAKE_DOCKER_ARGV_FILE"',
                                                       hook + '        : > "$FAKE_DOCKER_ARGV_FILE"'), encoding="utf-8")
        with patch.dict(os.environ, {"FAKE_WRAPPER": str(self.wrapper), "FAKE_SOURCE": str(self.root)}):
            result = self._invoke("plan", f"--gonka-path={self.root}", "--gonka-sha=" + "a" * 40,
                                  f"--contracts-path={self.root}", "--contracts-sha=" + "c" * 40,
                                  "--profile=smoke", f"--output={self.output_dir}")
        forwarded = self._container_arguments(result)
        parse_e2e_args(forwarded)
        first = Path(self._recorded_environment("GONKA_DIR"))
        contracts = Path(self._recorded_environment("CONTRACTS_DIR"))
        nested_env = dict(line.split("=", 1) for line in Path(str(self.env_file) + ".nested").read_text().splitlines())
        second = Path(nested_env["GONKA_DIR"])
        self.assertNotEqual(first.parent, second.parent)
        self.assertEqual(first.parent, contracts.parent)
        self.assertFalse(first.parent.exists())
        self.assertFalse(second.parent.exists())
        self.assertEqual(sentinel.read_text(), "keep")

    def test_a_failed_git_fetch_cleans_the_invocations_temporary_bridge(self):
        self._install_fake_git(fail_fetch=True)
        result = self._invoke("plan", "--gonka-path", str(self.root),
                              "--gonka-sha", "a" * 40)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Failed to read Git objects", result.stderr)
        self.assertEqual(list(self.bridge_dir.iterdir()), [])
        self.assertFalse(self.argv_file.exists())

    def test_a_failed_container_preserves_its_exit_code_and_cleans_the_bridge(self):
        self._install_fake_git()
        self.fake_docker.write_text(
            FAKE_DOCKER.replace('        exit 0\n        ;;', '        exit 23\n        ;;'),
            encoding="utf-8",
        )
        result = self._invoke("plan", "--gonka-path", str(self.root),
                              "--gonka-sha", "a" * 40, "--output", str(self.output_dir))
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertFalse(Path(self._recorded_environment("GONKA_DIR")).exists())
        self.assertEqual(list(self.bridge_dir.iterdir()), [])

    def test_swapped_bridge_root_does_not_delete_an_unrelated_session(self):
        self._install_fake_git()
        foreign = self.root / "foreign"
        foreign.mkdir()
        hook = r'''
        original="$E2E_BRIDGE_DIR"
        session="$(basename "$(dirname "$GONKA_DIR")")"
        mv "$original" "$original.moved"
        mkdir -p "$FAKE_FOREIGN/$session"
        printf 'keep' > "$FAKE_FOREIGN/$session/unrelated"
        ln -s "$FAKE_FOREIGN" "$original"
'''
        self.fake_docker.write_text(
            FAKE_DOCKER.replace('        : > "$FAKE_DOCKER_ARGV_FILE"',
                                hook + '        : > "$FAKE_DOCKER_ARGV_FILE"'),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"FAKE_FOREIGN": str(foreign)}):
            result = self._invoke("plan", "--gonka-path", str(self.root),
                                  "--gonka-sha", "a" * 40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bridge directory identity changed", result.stderr)
        self.assertEqual(len(list(foreign.glob("run.*/unrelated"))), 1)
        self.assertEqual(len(list(self.root.glob("bridge.moved/run.*"))), 1)

    def test_no_arguments_still_reaches_the_container_default_command_on_macos_bash(self):
        result = self._invoke()
        self.assertEqual(self._container_arguments(result), [])
        self.assertEqual(self._recorded_environment("OUTPUT_DIR"), str(self.repo_root / "out"))
        self.assertTrue((self.repo_root / "out").is_dir())

    def test_a_nested_suite_keeps_the_enclosing_package_inside_the_evidence_mount(self):
        suite = self.run_dir / "suite" / RUN_ID
        suite.mkdir(parents=True)
        # Marker contents are deliberately not parsed by the host wrapper.
        (self.run_dir / "run.lock.json").write_text("{}", encoding="utf-8")
        for command in ("report", "recover"):
            with self.subTest(command=command):
                result = self._invoke(command, "--run", str(suite))
                self.assertEqual(self._container_arguments(result),
                                 [command, "--run", f"/out/runs/{RUN_ID}/suite/{RUN_ID}"])
                self.assertEqual(self._recorded_environment("OUTPUT_DIR"), str(self.output_dir))

    def test_mount_discovery_stops_at_the_nearest_package_even_when_its_lock_is_missing(self):
        suite = self.run_dir / "suite" / RUN_ID
        suite.mkdir(parents=True)
        (self.run_dir / "execution-manifest.json").write_text("{}", encoding="utf-8")
        (self.output_dir / "run.lock.json").write_text("{}", encoding="utf-8")
        result = self._invoke("report", "--run", str(suite))
        self.assertEqual(self._container_arguments(result),
                         ["report", "--run", f"/out/runs/{RUN_ID}/suite/{RUN_ID}"])
        self.assertEqual(self._recorded_environment("OUTPUT_DIR"), str(self.output_dir))

    def test_named_docker_state_is_host_only_and_exported_to_compose(self):
        result = self._invoke(
            "report", "--run", RUN_ID,
            "--docker-root-volume", "a8-docker-root-test-001",
            "--output", str(self.output_dir),
        )

        forwarded = self._container_arguments(result)
        self.assertEqual(forwarded, ["report", "--run", RUN_ID, "--output", "/out"])
        # The fake Docker checks transport only; the real parser must accept
        # the command that would be executed inside the container.
        parse_e2e_args(forwarded)
        self.assertEqual(self._recorded_environment("OUTPUT_DIR"), str(self.output_dir))
        self.assertEqual(
            self._recorded_environment("A8_DOCKER_ROOT_VOLUME"),
            "a8-docker-root-test-001",
        )

    def test_unsafe_docker_state_name_is_refused_before_compose(self):
        result = self._invoke("list", "--docker-root-volume", "../foreign")

        self.assertEqual(result.returncode, 2, msg=result.stderr)
        self.assertIn("must be a plain Docker volume name", result.stderr)
        self.assertFalse(self.argv_file.exists())

    def test_an_explicit_output_is_bound_and_its_run_directory_is_forwarded_as_a_container_path(self):
        result = self._invoke(
            "report", "--run", str(self.run_dir), "--output", str(self.output_dir)
        )

        self.assertEqual(
            self._container_arguments(result),
            ["report", "--run", f"/out/runs/{RUN_ID}", "--output", "/out"],
        )

        # The container only ever sees /out; the bind source travels in the
        # OUTPUT_DIR variable that ops/a8/compose.yaml consumes.
        self.assertEqual(self._recorded_environment("OUTPUT_DIR"), str(self.output_dir))

    def test_a_run_directory_without_output_infers_the_root_from_the_runs_layout(self):
        result = self._invoke("report", "--run", str(self.run_dir))

        self.assertEqual(
            self._container_arguments(result),
            ["report", "--run", f"/out/runs/{RUN_ID}"],
        )
        # The inferred root is the parent of `runs/`, not the parent of the run.
        self.assertEqual(self._recorded_environment("OUTPUT_DIR"), str(self.output_dir))

    def test_a_bare_run_id_is_forwarded_to_the_container_untouched(self):
        result = self._invoke("report", "--run", RUN_ID, "--output", str(self.output_dir))

        self.assertEqual(
            self._container_arguments(result),
            ["report", "--run", RUN_ID, "--output", "/out"],
        )

    def test_a_run_directory_outside_the_output_directory_is_a_hard_error(self):
        stray = self.root / "elsewhere" / "runs" / RUN_ID
        stray.mkdir(parents=True)

        result = self._invoke("report", "--run", str(stray), "--output", str(self.output_dir))

        self.assertEqual(result.returncode, 2, msg=result.stderr)
        self.assertIn("is outside --output", result.stderr)
        self.assertIn("Only one host directory is mounted for evidence", result.stderr)
        self.assertFalse(
            self.argv_file.exists(),
            "the wrapper must refuse before starting the runner container",
        )

    def test_a_run_directory_that_does_not_exist_is_a_hard_error(self):
        missing = self.output_dir / "runs" / "no-such-run"

        result = self._invoke("report", "--run", str(missing), "--output", str(self.output_dir))

        self.assertEqual(result.returncode, 2, msg=result.stderr)
        self.assertIn("Run directory not found:", result.stderr)
        self.assertFalse(
            self.argv_file.exists(),
            "the wrapper must refuse before starting the runner container",
        )


class PowerShellWrapperSourceStructureTests(unittest.TestCase):
    """Pin selected source lines without claiming executable PowerShell parity.

    Anchoring rejects line-commented copies, but this is not a PowerShell parser:
    block comments, reachability, syntax and runtime behavior are not validated.
    """

    def setUp(self):
        self.source = POWERSHELL_WRAPPER.read_text(encoding="utf-8")

    def _assert_source_line(self, line):
        self.assertRegex(self.source, r"(?m)^[ \t]*" + re.escape(line) + r"[ \t]*$")

    def _assert_code_line(self, line):
        # A trailing comment does not change code; in a diagnostic here-string
        # the same characters are message text, so those checks stay exact.
        self.assertRegex(
            self.source, r"(?m)^[ \t]*" + re.escape(line) + r"[ \t]*(?:#.*)?$"
        )

    def test_the_source_contains_the_run_flag_case_and_argument_index_assignment(self):
        self._assert_code_line("'--run' {")
        self._assert_code_line("$runArgIndex = $forward.Count")

    def test_the_source_contains_container_path_assignments(self):
        self._assert_code_line("$forward[$runArgIndex] = '/out'")
        self._assert_code_line('$forward[$runArgIndex] = "/out/$relative"')
        self._assert_code_line("$forward.Add('--output'); $forward.Add('/out'); $i++")

    def test_the_source_contains_the_runs_layout_condition(self):
        self._assert_code_line("if ((Split-Path -Leaf $runParent) -eq 'runs') {")

    def test_the_source_contains_missing_and_foreign_run_directory_diagnostics(self):
        self._assert_source_line("Run directory not found: $runArgValue")
        self._assert_source_line("--run $runDirHost is outside --output $outputRoot.")
        self._assert_source_line("Only one host directory is mounted for evidence, so both must live under it.")

    def test_the_source_contains_the_docker_volume_case_and_environment_assignment(self):
        self._assert_code_line("'--docker-root-volume' {")
        self._assert_code_line("$env:A8_DOCKER_ROOT_VOLUME = $dockerRootVolume")

    def test_the_source_reads_digest_from_the_inspected_image_id(self):
        self.assertIn(
            "@('image', 'inspect', '--format', '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}', $imageId)",
            self.source,
        )


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell runtime checks require pwsh")
class PowerShellWrapperExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="a8-test-powershell-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.record = self.root / "docker.json"
        self.driver = self.root / "driver.ps1"
        self.driver.write_text(r"""
function global:docker {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'image') {
        if ($args[3] -like '*RepoDigests*') { return '' }
        return ('sha256:' + ('1' * 64))
    }
    if ($args[0] -eq 'compose') {
        @{ argv = @($args); output = $env:OUTPUT_DIR; plan = $env:E2E_PLAN_DIR;
           secrets = $env:E2E_SECRETS_DIR; image = $env:E2E_RUNNER_IMAGE;
           image_id = $env:E2E_RUNNER_IMAGE_ID } | ConvertTo-Json -Depth 4 |
            Set-Content -LiteralPath $env:FAKE_DOCKER_RECORD
        if ($env:FAKE_BUILD_FAILURE -eq '1') { $global:LASTEXITCODE = 23 }
        return
    }
    throw "Unexpected Docker command: $args"
}
$wrapperArgs = @(ConvertFrom-Json $env:FAKE_WRAPPER_ARGS)
& $env:FAKE_WRAPPER @wrapperArgs
exit $LASTEXITCODE
""", encoding="utf-8")

    def invoke(self, *args, build=False, fail_build=False):
        environment = dict(os.environ)
        environment.update({
            "FAKE_DOCKER_RECORD": str(self.record),
            "FAKE_WRAPPER_ARGS": json.dumps(args),
            "FAKE_WRAPPER": str(POWERSHELL_WRAPPER.with_name("Build-Runner.ps1") if build else POWERSHELL_WRAPPER),
            "FAKE_BUILD_FAILURE": "1" if fail_build else "0",
            "OUTPUT_DIR": str(self.root / "out"),
            "E2E_BRIDGE_DIR": str(self.root / "bridge"),
        })
        return subprocess.run(
            [shutil.which("pwsh"), "-NoProfile", "-File", str(self.driver)],
            cwd=self.root, env=environment, capture_output=True, text=True, timeout=30,
        )

    def test_a_nested_suite_keeps_the_enclosing_package_inside_the_evidence_mount(self):
        package = self.root / "out" / "runs" / RUN_ID
        suite = package / "suite" / RUN_ID
        suite.mkdir(parents=True)
        # Only existence is read by the wrapper; grading remains container-owned.
        (package / "run.lock.json").write_text("{}", encoding="utf-8")
        result = self.invoke("report", "--run", str(suite))
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = json.loads(self.record.read_text(encoding="utf-8-sig"))
        self.assertEqual(Path(recorded["output"]), self.root / "out")
        self.assertEqual(recorded["argv"][-3:], ["report", "--run", f"/out/runs/{RUN_ID}/suite/{RUN_ID}"])

    def test_the_windows_wrapper_launches_the_inspected_image_id(self):
        locator = "registry.invalid/runner:changing"
        result = self.invoke("list", "--runner-image", locator)
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = json.loads(self.record.read_text(encoding="utf-8-sig"))
        expected_id = "sha256:" + "1" * 64
        self.assertEqual(recorded["image"], expected_id)
        self.assertEqual(recorded["image_id"], expected_id)
        self.assertEqual(recorded["argv"][-5:],
                         ["--rm", "-e", f"E2E_RUNNER_IMAGE={locator}", SERVICE, "list"])

    def test_an_existing_bridge_directory_is_preserved_and_the_new_session_is_cleaned(self):
        source = self.root / "local-source"
        subprocess.run(["git", "init", "--quiet", str(source)], check=True)
        subprocess.run(
            ["git", "-C", str(source), "-c", "user.name=Test", "-c",
             "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "seed"],
            check=True, capture_output=True,
        )
        sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        existing = self.root / "bridge" / "gonka.git"
        existing.mkdir(parents=True)
        marker = existing / "belongs-to-another-plan.txt"
        marker.write_text("keep", encoding="utf-8")

        result = self.invoke("plan", "--gonka-path", str(source), "--gonka-sha", sha)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list((self.root / "bridge").glob("run.*")), [])

        failed = self.invoke("plan", "--gonka-path", str(source), "--gonka-sha", sha,
                             fail_build=True)
        self.assertEqual(failed.returncode, 23, failed.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list((self.root / "bridge").glob("run.*")), [])

        missing_commit = self.invoke("plan", "--gonka-path", str(source),
                                     "--gonka-sha", "f" * 40)
        self.assertNotEqual(missing_commit.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list((self.root / "bridge").glob("run.*")), [])

    def test_a_bare_lock_filename_mounts_the_current_directory(self):
        (self.root / "run.lock.json").write_text("{}", encoding="utf-8")
        result = self.invoke("run", "--from", "run.lock.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = json.loads(self.record.read_text(encoding="utf-8-sig"))
        self.assertEqual(Path(recorded["plan"]), self.root)
        self.assertEqual(recorded["argv"][-2:], ["--from", "/input/plan/run.lock.json"])

    def test_a_bare_credential_filename_mounts_the_current_directory(self):
        (self.root / "credentials.json").write_text("{}", encoding="utf-8")
        result = self.invoke("plan", "--credential-file", "credentials.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = json.loads(self.record.read_text(encoding="utf-8-sig"))
        self.assertEqual(Path(recorded["secrets"]), self.root)
        self.assertEqual(recorded["argv"][-2:], ["--credential-file", "/run/secrets/e2e/credentials.json"])

    def test_a_missing_flag_value_exits_with_the_usage_error_code(self):
        result = self.invoke("run", "--from")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.record.exists())

    def test_a_failed_image_build_preserves_the_docker_exit_code(self):
        result = self.invoke(build=True, fail_build=True)
        self.assertEqual(result.returncode, 23, result.stderr)


class RunnerBuildContextTests(unittest.TestCase):
    def test_all_binding_recorded_fixtures_are_available_to_the_runner_test_suite(self):
        for name in REQUIRED_RECORDED_EVIDENCE:
            with self.subTest(fixture=name):
                fixture = REPO_ROOT / "docs/reviews/evidence" / name
                self.assertTrue(fixture.is_file(), f"Missing recorded fixture: {fixture}")
                self.assertGreater(fixture.stat().st_size, 0, f"Empty recorded fixture: {fixture}")


if __name__ == "__main__":
    unittest.main()
