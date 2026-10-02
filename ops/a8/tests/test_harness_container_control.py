"""Tests for ops/a8/harness/container_control.py, the API stop/start controller.

The controller replaces the stop/restart helper that used to be patched into
Gonka's DockerGroup.kt. What it must prove: the container it starts is the very
container it stopped (same immutable ID), it only ever touches containers that
carry this run's ownership label, a tampered state file is refused, and a
container that does not become ready within the bound is a failure.

The module is loaded by path because ``ops/a8/harness`` is runner payload, not a
package. Only the Docker CLI, the HTTP probe, the clock and sleep are replaced;
the controller itself is real.

Positive ``docker inspect`` fixtures reproduce the Docker Engine
``ContainerInspect`` JSON (an array holding one object with ``Id``, ``Name``,
``Image``, ``State.Running``, ``Config.Image`` and ``Config.Labels`` as
Docker 24-27 print it); every negative fixture changes exactly one fact of it.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "harness" / "container_control.py"
_SPEC = importlib.util.spec_from_file_location("a8_container_control_under_test", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
cc = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cc
_SPEC.loader.exec_module(cc)

RUN_ID = "20260925-a8-run"
CONTAINER_ID = "3f4e2b1c9a8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f"
OTHER_ID = "9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f3f4e2b1c9a8d7e6f5a4b3c2d1e0f"


def inspect_object(*, running: bool = True, run_id: str | None = RUN_ID, container_id: str = CONTAINER_ID):
    """One element of ``docker inspect --type container join1-api`` output."""
    labels = {
        "com.docker.compose.config-hash": "b7c4d1a2e3f4",
        "com.docker.compose.container-number": "1",
        "com.docker.compose.oneoff": "False",
        "com.docker.compose.project": "join1",
        "com.docker.compose.service": "api",
        "com.docker.compose.version": "2.29.1",
    }
    if run_id is not None:
        labels[cc.OWNERSHIP_LABEL] = run_id
    return {
        "Id": container_id,
        "Created": "2026-09-25T10:00:00.000000000Z",
        "Name": "/join1-api",
        "Image": "sha256:5d0da3dc976460b72c77d94c8a1ad043720b0416bfc16c52c45d4847e53fadb6",
        "State": {
            "Status": "running" if running else "exited",
            "Running": running,
            "Paused": False,
            "Restarting": False,
            "OOMKilled": False,
            "Dead": False,
            "Pid": 4242 if running else 0,
            "ExitCode": 0,
        },
        "Config": {"Image": "ghcr.io/product-science/api", "Labels": labels},
    }


class FakeDocker:
    """Answers the docker CLI from a mutable container table."""

    def __init__(self, containers_by_ref):
        self.by_ref = containers_by_ref
        self.calls = []
        self.start_makes_running = True

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            info = self.by_ref.get(argv[-1])
            if info is None:
                return subprocess.CompletedProcess(argv, 1, "[]", "Error: No such container")
            return subprocess.CompletedProcess(argv, 0, json.dumps([info]), "")
        if argv[:2] == ["docker", "stop"]:
            self.by_ref[argv[2]]["State"]["Running"] = False
            self.by_ref[argv[2]]["State"]["Status"] = "exited"
            return subprocess.CompletedProcess(argv, 0, argv[2] + "\n", "")
        if argv[:2] == ["docker", "start"]:
            if self.start_makes_running:
                self.by_ref[argv[2]]["State"]["Running"] = True
                self.by_ref[argv[2]]["State"]["Status"] = "running"
            return subprocess.CompletedProcess(argv, 0, argv[2] + "\n", "")
        raise AssertionError(f"unexpected docker call {argv}")


class Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class ContainerControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="a8-cc-")
        self.addCleanup(self.tmp.cleanup)
        self.state_file = Path(self.tmp.name) / "container-control" / "join1-api-1.json"
        self.clock = Clock()

    def controller(self, docker, *, http_status=200):
        return cc.ContainerController(
            runner=docker,
            http_probe=lambda url, timeout: http_status,
            sleep=self.clock.sleep,
            monotonic=self.clock.monotonic,
            now=lambda: "2026-09-25T10:00:00Z",
            poll_interval=1.0,
        )

    def shared_docker(self, info):
        # The name and the ID resolve to the same container object.
        return FakeDocker({"join1-api": info, CONTAINER_ID: info})

    def test_stopping_and_starting_the_same_owned_container_by_its_recorded_id_succeeds(self):
        docker = self.shared_docker(inspect_object())
        controller = self.controller(docker)

        stopped = controller.stop(container_name="join1-api", run_id=RUN_ID, state_file=self.state_file)
        started = controller.start(
            state_file=self.state_file, run_id=RUN_ID, ready_url="http://join1-api:9200/health", ready_timeout_seconds=30
        )

        self.assertEqual(stopped["container_id"], CONTAINER_ID)
        self.assertEqual(stopped["compose_project"], "join1")
        self.assertEqual(started["container_id"], CONTAINER_ID)
        self.assertTrue(started["ready"])
        self.assertIn(["docker", "stop", CONTAINER_ID], docker.calls)
        self.assertIn(["docker", "start", CONTAINER_ID], docker.calls)
        # After the stop, the controller never resolves the name again.
        stop_index = docker.calls.index(["docker", "stop", CONTAINER_ID])
        self.assertNotIn("join1-api", [call[-1] for call in docker.calls[stop_index:]])
        recorded = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(recorded["schema"], cc.STATE_SCHEMA)
        self.assertEqual(recorded["labels"][cc.OWNERSHIP_LABEL], RUN_ID)
        self.assertEqual(recorded["started_at"], "2026-09-25T10:00:00Z")

    def test_the_state_is_written_before_docker_stop_is_issued(self):
        docker = self.shared_docker(inspect_object())
        seen = {}
        original = docker.__call__

        def spying(argv):
            if list(argv)[:2] == ["docker", "stop"]:
                seen["state_existed"] = self.state_file.is_file()
            return original(argv)

        self.controller(spying).stop(container_name="join1-api", run_id=RUN_ID, state_file=self.state_file)
        self.assertTrue(seen["state_existed"])

    def test_a_container_labelled_for_another_run_is_refused_and_never_stopped(self):
        docker = self.shared_docker(inspect_object(run_id="some-other-run"))
        with self.assertRaises(cc.ControlError) as cm:
            self.controller(docker).stop(container_name="join1-api", run_id=RUN_ID, state_file=self.state_file)
        self.assertEqual(cm.exception.code, cc.CONTAINER_NOT_OWNED)
        self.assertFalse(any(call[1] == "stop" for call in docker.calls))
        self.assertFalse(self.state_file.exists())

    def test_a_container_without_any_ownership_label_is_refused(self):
        docker = self.shared_docker(inspect_object(run_id=None))
        with self.assertRaises(cc.ControlError) as cm:
            self.controller(docker).stop(container_name="join1-api", run_id=RUN_ID, state_file=self.state_file)
        self.assertEqual(cm.exception.code, cc.CONTAINER_NOT_OWNED)

    def test_a_missing_container_is_reported_as_not_found(self):
        docker = FakeDocker({})
        with self.assertRaises(cc.ControlError) as cm:
            self.controller(docker).stop(container_name="join1-api", run_id=RUN_ID, state_file=self.state_file)
        self.assertEqual(cm.exception.code, cc.CONTAINER_NOT_FOUND)

    def test_a_container_that_is_already_stopped_is_not_recorded_as_stopped_by_us(self):
        docker = self.shared_docker(inspect_object(running=False))
        with self.assertRaises(cc.ControlError) as cm:
            self.controller(docker).stop(container_name="join1-api", run_id=RUN_ID, state_file=self.state_file)
        self.assertEqual(cm.exception.code, cc.CONTAINER_NOT_RUNNING)
        self.assertFalse(self.state_file.exists())

    def test_start_refuses_when_the_recorded_id_now_inspects_as_a_different_container(self):
        docker = self.shared_docker(inspect_object())
        controller = self.controller(docker)
        controller.stop(container_name="join1-api", run_id=RUN_ID, state_file=self.state_file)
        replaced = copy.deepcopy(docker.by_ref[CONTAINER_ID])
        replaced["Id"] = OTHER_ID
        docker.by_ref[CONTAINER_ID] = replaced

        with self.assertRaises(cc.ControlError) as cm:
            controller.start(state_file=self.state_file, run_id=RUN_ID)
        self.assertEqual(cm.exception.code, cc.CONTAINER_ID_MISMATCH)
        self.assertFalse(any(call[1] == "start" for call in docker.calls))

    def test_start_refuses_a_state_file_whose_container_id_was_tampered_with(self):
        docker = self.shared_docker(inspect_object())
        controller = self.controller(docker)
        controller.stop(container_name="join1-api", run_id=RUN_ID, state_file=self.state_file)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        state["container_id"] = OTHER_ID
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaises(cc.ControlError) as cm:
            controller.start(state_file=self.state_file, run_id=RUN_ID)
        self.assertEqual(cm.exception.code, cc.CONTAINER_NOT_FOUND)
        self.assertFalse(any(call[1] == "start" for call in docker.calls))

    def test_start_refuses_a_state_file_recorded_for_another_run(self):
        docker = self.shared_docker(inspect_object())
        controller = self.controller(docker)
        controller.stop(container_name="join1-api", run_id=RUN_ID, state_file=self.state_file)

        with self.assertRaises(cc.ControlError) as cm:
            controller.start(state_file=self.state_file, run_id="some-other-run")
        self.assertEqual(cm.exception.code, cc.CONTAINER_NOT_OWNED)

    def test_start_rejects_non_finite_or_non_positive_timeout_before_docker_start(self):
        for timeout in (float("nan"), float("inf"), float("-inf"), 0.0, -1.0):
            with self.subTest(timeout=timeout):
                docker = self.shared_docker(inspect_object())
                controller = self.controller(docker)
                controller.stop(
                    container_name="join1-api", run_id=RUN_ID, state_file=self.state_file
                )
                docker.calls.clear()

                with self.assertRaises(cc.ControlError) as cm:
                    controller.start(
                        state_file=self.state_file,
                        run_id=RUN_ID,
                        ready_timeout_seconds=timeout,
                    )

                self.assertEqual(cm.exception.code, cc.STATE_INVALID)
                self.assertFalse(any(call[1] == "start" for call in docker.calls))
                self.state_file.unlink()

    def test_start_refuses_when_the_container_lost_its_ownership_label_while_stopped(self):
        docker = self.shared_docker(inspect_object())
        controller = self.controller(docker)
        controller.stop(container_name="join1-api", run_id=RUN_ID, state_file=self.state_file)
        del docker.by_ref[CONTAINER_ID]["Config"]["Labels"][cc.OWNERSHIP_LABEL]

        with self.assertRaises(cc.ControlError) as cm:
            controller.start(state_file=self.state_file, run_id=RUN_ID)
        self.assertEqual(cm.exception.code, cc.CONTAINER_NOT_OWNED)

    def test_a_state_file_missing_a_mandatory_field_is_invalid(self):
        docker = self.shared_docker(inspect_object())
        controller = self.controller(docker)
        controller.stop(container_name="join1-api", run_id=RUN_ID, state_file=self.state_file)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        del state["stopped_at"]
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaises(cc.ControlError) as cm:
            controller.start(state_file=self.state_file, run_id=RUN_ID)
        self.assertEqual(cm.exception.code, cc.STATE_INVALID)

    def test_an_api_that_never_answers_2xx_within_the_bound_fails_as_not_ready(self):
        docker = self.shared_docker(inspect_object())
        controller = self.controller(docker, http_status=503)
        controller.stop(container_name="join1-api", run_id=RUN_ID, state_file=self.state_file)

        with self.assertRaises(cc.ControlError) as cm:
            controller.start(
                state_file=self.state_file, run_id=RUN_ID, ready_url="http://join1-api:9200/health", ready_timeout_seconds=5
            )
        self.assertEqual(cm.exception.code, cc.CONTAINER_NOT_READY)
        self.assertLessEqual(self.clock.value, 6.0)
        self.assertFalse(json.loads(self.state_file.read_text(encoding="utf-8"))["ready"])

    def test_a_container_that_never_reaches_running_within_the_bound_fails(self):
        docker = self.shared_docker(inspect_object())
        docker.start_makes_running = False
        controller = self.controller(docker)
        controller.stop(container_name="join1-api", run_id=RUN_ID, state_file=self.state_file)

        with self.assertRaises(cc.ControlError) as cm:
            controller.start(state_file=self.state_file, run_id=RUN_ID, ready_timeout_seconds=3)
        self.assertEqual(cm.exception.code, cc.CONTAINER_NOT_RUNNING)

    def test_the_cli_prints_one_json_error_line_and_exits_one_on_refusal(self):
        docker = self.shared_docker(inspect_object(run_id="some-other-run"))
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cc.main(
                ["stop", "--container-name", "join1-api", "--run-id", RUN_ID, "--state-file", str(self.state_file)],
                controller=self.controller(docker),
            )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue().strip())["error"], cc.CONTAINER_NOT_OWNED)

    def test_the_controller_module_uses_only_the_standard_library(self):
        import ast

        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                imported.add((node.module or "").split(".")[0])
        stdlib = set(sys.stdlib_module_names) | {"__future__"}
        self.assertLessEqual(imported, stdlib)


if __name__ == "__main__":
    unittest.main()
