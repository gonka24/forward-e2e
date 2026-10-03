#!/usr/bin/env python3
"""Stop and restart one owned Testermint container by its recorded ID.

Why this exists
---------------
Six Marketplace scenarios stop a pair's API container and start it again. The
old overlay did that with a helper patched into Gonka's ``DockerGroup.kt``. In
the immutable-source model Gonka is never patched, so the external Kotlin
harness calls this controller instead (``A8_CONTAINER_CONTROL``).

The controller exists to make "the same container came back" a fact rather
than an assumption:

* ``stop`` inspects the container by name, refuses it unless it is running and
  carries this run's ownership label (``io.gonka.a8.run-id``), and records its
  immutable ID, image, labels and compose project in a state file *before*
  stopping it -- so a crash between the two leaves evidence, not a guess.
* ``start`` never resolves a name again. It inspects the recorded ID, refuses
  when the ID or ownership differ from the record (a recreated or foreign
  container must not pass for the stopped one), starts it, and waits a bounded
  time for it to run and, optionally, for an HTTP readiness URL to answer 2xx.

Every refusal prints one JSON line ``{"error": CODE, ...}`` on stdout and exits
1. Codes: CONTAINER_NOT_FOUND, CONTAINER_NOT_OWNED, CONTAINER_ID_MISMATCH,
CONTAINER_NOT_RUNNING, CONTAINER_NOT_READY, STATE_INVALID.

Standard library only; Docker is driven through its CLI. The command runner,
the HTTP probe, the clock and the sleep are injectable for offline tests.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

STATE_SCHEMA = "a8.container-control/1"
OWNERSHIP_LABEL = "io.gonka.a8.run-id"
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

CONTAINER_NOT_FOUND = "CONTAINER_NOT_FOUND"
CONTAINER_NOT_OWNED = "CONTAINER_NOT_OWNED"
CONTAINER_ID_MISMATCH = "CONTAINER_ID_MISMATCH"
CONTAINER_NOT_RUNNING = "CONTAINER_NOT_RUNNING"
CONTAINER_NOT_READY = "CONTAINER_NOT_READY"
STATE_INVALID = "STATE_INVALID"

DEFAULT_READY_TIMEOUT_SECONDS = 180
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DOCKER_TIMEOUT_SECONDS = 120

CommandRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
HttpProbe = Callable[[str, float], int]


class ControlError(RuntimeError):
    """A refusal or failure with a stable code."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_json(self) -> Dict[str, Any]:
        return {"error": self.code, "message": str(self), **self.details}


def default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv), capture_output=True, text=True, check=False, timeout=DOCKER_TIMEOUT_SECONDS
    )


def default_http_probe(url: str, timeout: float) -> int:
    """HTTP status of ``url``; 0 when nothing answered."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local URL
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ContainerController:
    def __init__(
        self,
        *,
        runner: CommandRunner = default_runner,
        http_probe: HttpProbe = default_http_probe,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], str] = utc_now,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ):
        self.runner = runner
        self.http_probe = http_probe
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.poll_interval = poll_interval

    # -- docker helpers ----------------------------------------------------

    def inspect(self, reference: str) -> Optional[Dict[str, Any]]:
        """``docker inspect`` one container; ``None`` when it does not exist."""
        result = self.runner(["docker", "inspect", "--type", "container", reference])
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout or "[]")
        except ValueError as exc:
            raise ControlError(STATE_INVALID, f"docker inspect {reference} returned non-JSON") from exc
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            return None
        return payload[0]

    def _docker(self, *args: str) -> None:
        result = self.runner(["docker", *args])
        if result.returncode != 0:
            raise ControlError(
                CONTAINER_NOT_RUNNING if args[0] == "start" else STATE_INVALID,
                f"docker {' '.join(args)} failed: {(result.stderr or '').strip()[-500:]}",
            )

    @staticmethod
    def _labels(info: Mapping[str, Any]) -> Dict[str, str]:
        config = info.get("Config") if isinstance(info.get("Config"), Mapping) else {}
        labels = config.get("Labels") if isinstance(config.get("Labels"), Mapping) else {}
        return {str(k): str(v) for k, v in labels.items()}

    @staticmethod
    def _running(info: Mapping[str, Any]) -> bool:
        state = info.get("State") if isinstance(info.get("State"), Mapping) else {}
        return state.get("Running") is True

    def _require_owned(self, info: Mapping[str, Any], run_id: str, reference: str) -> Dict[str, str]:
        labels = self._labels(info)
        actual = labels.get(OWNERSHIP_LABEL)
        if actual != run_id:
            raise ControlError(
                CONTAINER_NOT_OWNED,
                f"container {reference} is not owned by run {run_id}",
                container=reference,
                expected_run_id=run_id,
                actual_run_id=actual,
            )
        return labels

    # -- commands ----------------------------------------------------------

    def stop(self, *, container_name: str, run_id: str, state_file: Path) -> Dict[str, Any]:
        if not run_id:
            raise ControlError(STATE_INVALID, "an empty run id proves no ownership")
        state_file = Path(state_file)
        if state_file.exists():
            raise ControlError(STATE_INVALID, f"state file {state_file} already exists", state_file=str(state_file))
        info = self.inspect(container_name)
        if info is None:
            raise ControlError(CONTAINER_NOT_FOUND, f"container {container_name} does not exist", container=container_name)
        labels = self._require_owned(info, run_id, container_name)
        if not self._running(info):
            raise ControlError(CONTAINER_NOT_RUNNING, f"container {container_name} is not running", container=container_name)
        container_id = info.get("Id")
        if not isinstance(container_id, str) or len(container_id) < 12:
            raise ControlError(STATE_INVALID, f"container {container_name} has no usable ID")
        config = info.get("Config") if isinstance(info.get("Config"), Mapping) else {}
        state = {
            "schema": STATE_SCHEMA,
            "container_id": container_id,
            "name": container_name,
            "image": info.get("Image") or config.get("Image"),
            "labels": labels,
            "compose_project": labels.get(COMPOSE_PROJECT_LABEL),
            "run_id": run_id,
            "stopped_at": self.now(),
        }
        # Recorded before the stop, so the identity survives any failure below.
        write_state(state_file, state)
        self._docker("stop", container_id)
        after = self.inspect(container_id)
        if after is None or self._running(after):
            raise ControlError(STATE_INVALID, f"container {container_id} is still running after docker stop")
        state["stopped_confirmed"] = True
        write_state(state_file, state)
        return state

    def start(
        self,
        *,
        state_file: Path,
        run_id: str,
        ready_url: Optional[str] = None,
        ready_timeout_seconds: float = DEFAULT_READY_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        timeout = float(ready_timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ControlError(
                STATE_INVALID,
                "ready timeout must be a finite positive number",
                ready_timeout_seconds=str(ready_timeout_seconds),
            )
        state = read_state(Path(state_file))
        if state.get("run_id") != run_id or state.get("labels", {}).get(OWNERSHIP_LABEL) != run_id:
            raise ControlError(
                CONTAINER_NOT_OWNED,
                f"state file {state_file} does not belong to run {run_id}",
                expected_run_id=run_id,
                recorded_run_id=state.get("run_id"),
            )
        container_id = state["container_id"]
        info = self.inspect(container_id)
        if info is None:
            raise ControlError(CONTAINER_NOT_FOUND, f"recorded container {container_id} no longer exists", container_id=container_id)
        if info.get("Id") != container_id:
            raise ControlError(
                CONTAINER_ID_MISMATCH,
                f"inspected container ID {info.get('Id')} differs from recorded {container_id}",
                recorded=container_id,
                actual=info.get("Id"),
            )
        self._require_owned(info, run_id, container_id)
        self._docker("start", container_id)

        deadline = self.monotonic() + timeout
        running = False
        ready = ready_url is None
        attempts = 0
        while True:
            attempts += 1
            current = self.inspect(container_id)
            if current is not None and current.get("Id") != container_id:
                raise ControlError(CONTAINER_ID_MISMATCH, "container ID changed while starting", recorded=container_id)
            running = current is not None and self._running(current)
            if running and ready_url is not None:
                status = self.http_probe(ready_url, max(1.0, min(10.0, self.poll_interval * 2)))
                ready = 200 <= status < 300
            if running and ready:
                break
            if self.monotonic() >= deadline:
                code = CONTAINER_NOT_RUNNING if not running else CONTAINER_NOT_READY
                state.update({"started_at": self.now(), "ready": False, "ready_attempts": attempts})
                write_state(Path(state_file), state)
                raise ControlError(
                    code,
                    f"container {container_id} not {'running' if not running else 'ready'} "
                    f"within {ready_timeout_seconds}s",
                    container_id=container_id,
                    attempts=attempts,
                )
            self.sleep(self.poll_interval)
        state.update(
            {
                "started_at": self.now(),
                "ready": True,
                "ready_url": ready_url,
                "ready_attempts": attempts,
            }
        )
        write_state(Path(state_file), state)
        return state


def write_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_state(path: Path) -> Dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ControlError(STATE_INVALID, f"state file {path} is missing", state_file=str(path))
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ControlError(STATE_INVALID, f"state file {path} is not JSON") from exc
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
        raise ControlError(STATE_INVALID, f"state file {path} is not {STATE_SCHEMA}")
    for key in ("container_id", "name", "labels", "run_id", "stopped_at"):
        if key not in state:
            raise ControlError(STATE_INVALID, f"state file {path} has no {key!r}", missing=key)
    if not isinstance(state["labels"], dict) or not isinstance(state["container_id"], str):
        raise ControlError(STATE_INVALID, f"state file {path} is malformed")
    return state


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = root.add_subparsers(dest="command", required=True)
    stop = commands.add_parser("stop")
    stop.add_argument("--container-name", required=True)
    stop.add_argument("--run-id", required=True)
    stop.add_argument("--state-file", required=True)
    start = commands.add_parser("start")
    start.add_argument("--state-file", required=True)
    start.add_argument("--run-id", required=True)
    start.add_argument("--ready-url")
    start.add_argument("--ready-timeout-seconds", type=float, default=DEFAULT_READY_TIMEOUT_SECONDS)
    return root


def main(argv: Optional[List[str]] = None, *, controller: Optional[ContainerController] = None) -> int:
    args = parser().parse_args(argv)
    controller = controller or ContainerController()
    try:
        if args.command == "stop":
            state = controller.stop(
                container_name=args.container_name, run_id=args.run_id, state_file=Path(args.state_file)
            )
        else:
            state = controller.start(
                state_file=Path(args.state_file),
                run_id=args.run_id,
                ready_url=args.ready_url,
                ready_timeout_seconds=args.ready_timeout_seconds,
            )
    except ControlError as exc:
        print(json.dumps(exc.as_json(), sort_keys=True))
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": STATE_INVALID, "message": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"status": "ok", "command": args.command, "container_id": state["container_id"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
