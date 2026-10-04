"""Run Go classification tests with Gonka's pinned Docker builder, no blockchain.

Usage: python3 scripts/run_go_boundary.py GONKA_LINUX_CHECKOUT NEW_OUTPUT_DIR
           [--expected-gonka-sha SHA] [--harness-dir DIR]

The output retains Go JSON even when tests fail. This is NOT Wasm ABI proof:
the proof level is "Go classification and JSON roundtrip, not FFI".

Immutable-source model
----------------------
The probes used to be copied into Gonka (``inference-chain/app/a8faults``) and
compiled there. They now live in the runner-owned module
``harness/go_boundary`` and Gonka is only ever *read*:

* Gonka is the read-only Docker build context; the harness module is a second,
  named build context (``--build-context harness=...``). The generated
  Dockerfile is written to a temporary directory outside both.
* The harness ``go.mod`` template is completed inside the build from the
  selected Gonka's ``go.mod`` (``go`` directive, a local replace of
  ``github.com/productscience/inference`` to ``../inference-chain`` and a copy
  of every Gonka replace directive) and Gonka's ``go.sum`` is copied next to
  it. ``go mod edit`` and ``go test -mod=mod`` run on the harness module only.
* Gonka's ``go.mod``/``go.sum`` are hashed on the host and inside the build,
  before and after; the snapshot is measured with ``forward_e2e/suite/source_snapshot.py``
  before and after. Any change, and any dependency version that differs from
  Gonka's own module graph, makes the report FAIL.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

RUNNER_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HARNESS_DIR = RUNNER_ROOT / "harness" / "go_boundary"
SOURCE_SNAPSHOT_PATH = RUNNER_ROOT / "forward_e2e" / "suite" / "source_snapshot.py"
LEVEL = "Go classification and JSON roundtrip, not FFI"
REQUIRED_TESTS = ("TestToQuerierResultClassifiesVMSystemErrors", "TestStrictPlanValidation")
INFERENCE_MODULE = "github.com/productscience/inference"
INFERENCE_PSEUDO_VERSION = "v0.0.0-00010101000000-000000000000"
PINNED_BUILDER = "golang:1.24.2-alpine3.21"
GONKA_GO_FILES = ("inference-chain/go.mod", "inference-chain/go.sum")


class BoundaryError(SystemExit):
    """A refusal before or around the Docker build (exit 1 with a message)."""


def load_source_snapshot():
    name = "a8_source_snapshot"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SOURCE_SNAPSHOT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def gonka_go_hashes(root: Path) -> Dict[str, Optional[str]]:
    return {rel: (sha256_file(root / rel) if (root / rel).is_file() else None) for rel in GONKA_GO_FILES}


def _strip_comment(line: str) -> str:
    return line.split("//", 1)[0].strip()


def parse_go_mod(text: str) -> Dict[str, Any]:
    """The ``go``/``toolchain`` directives and every replace directive of a go.mod."""
    go_version = None
    toolchain = None
    replaces: List[Tuple[str, str]] = []
    in_replace_block = False
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line:
            continue
        if in_replace_block:
            if line == ")":
                in_replace_block = False
                continue
            replaces.append(_parse_replace(line))
            continue
        if line.startswith("replace"):
            rest = line[len("replace"):].strip()
            if rest == "(":
                in_replace_block = True
            else:
                replaces.append(_parse_replace(rest))
        elif line.startswith("go ") and go_version is None:
            go_version = line.split()[1]
        elif line.startswith("toolchain "):
            toolchain = line.split()[1]
    return {"go": go_version, "toolchain": toolchain, "replaces": replaces}


def _parse_replace(spec: str) -> Tuple[str, str]:
    old, sep, new = spec.partition("=>")
    if not sep:
        raise BoundaryError(f"malformed replace directive in Gonka go.mod: {spec!r}")
    old_parts, new_parts = old.split(), new.split()
    if not 1 <= len(old_parts) <= 2 or not 1 <= len(new_parts) <= 2:
        raise BoundaryError(f"malformed replace directive in Gonka go.mod: {spec!r}")
    return "@".join(old_parts), "@".join(new_parts)


def replace_flags(parsed: Mapping[str, Any]) -> List[str]:
    """``go mod edit`` flags adapting a copy of Gonka's go.mod for the harness."""
    flags = [
        "-module=github.com/gonka24/forward-e2e-go-boundary",
        f"-require={INFERENCE_MODULE}@{INFERENCE_PSEUDO_VERSION}",
        f"-replace={INFERENCE_MODULE}=../inference-chain",
    ]
    if parsed.get("go"):
        flags.append(f"-go={parsed['go']}")
    if parsed.get("toolchain"):
        flags.append(f"-toolchain={parsed['toolchain']}")
    for old, new in parsed.get("replaces", []):
        if old.split("@")[0] == INFERENCE_MODULE:
            raise BoundaryError("Gonka go.mod replaces its own module; refusing to guess")
        if new.startswith("./") or new.startswith("../"):
            # A local replacement is relative to inference-chain in Gonka.
            new = "../inference-chain/" + new
        flags.append(f"-replace={old}={new}")
    for flag in flags:
        if "'" in flag:
            raise BoundaryError(f"unsafe go.mod edit flag: {flag!r}")
    return flags


def generate_dockerfile(gonka_dockerfile: str, flags: Sequence[str]) -> str:
    prefix = gonka_dockerfile.split("ARG LDFLAGS", 1)[0]
    if prefix == gonka_dockerfile or PINNED_BUILDER not in prefix:
        raise BoundaryError("Unexpected pinned builder layout")
    edit = " ".join(f"'{flag}'" for flag in flags)
    run_filter = "|".join(REQUIRED_TESTS)
    return prefix + f'''
FROM builder AS go-boundary
COPY --from=harness . /app/go-boundary/
WORKDIR /app/go-boundary
RUN --mount=type=cache,id=go-build-cache,target=/root/.cache/go-build \\
    --mount=type=cache,id=go-mod-cache,target=/go/pkg/mod \\
    mkdir -p /boundary-evidence; \\
    sha256sum /app/inference-chain/go.mod /app/inference-chain/go.sum > /boundary-evidence/gonka-go-before.sha256; \\
    cp /app/inference-chain/go.mod ./go.mod; \\
    go mod edit {edit} > /boundary-evidence/go-mod-edit.log 2>&1; \\
    cp /app/inference-chain/go.sum ./go.sum; \\
    (cd /app/inference-chain && go list -mod=readonly -m all) > /boundary-evidence/gonka-modules.txt 2>&1; \\
    go test -mod=mod -tags=muslc -count=1 -json ./query_faults \\
      -run '{run_filter}' \\
      > /boundary-evidence/go-test.json 2>&1; \\
    echo $? > /boundary-evidence/exit-code; \\
    go list -mod=mod -m all > /boundary-evidence/harness-modules.txt 2>&1; \\
    cp go.mod /boundary-evidence/harness-go.mod; \\
    sha256sum /app/inference-chain/go.mod /app/inference-chain/go.sum > /boundary-evidence/gonka-go-after.sha256
FROM scratch AS boundary-evidence
COPY --from=go-boundary /boundary-evidence/ /
'''


def parse_modules(text: str) -> Dict[str, str]:
    """``go list -m all`` -> {module: version}; the main module has no version."""
    modules: Dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith("go:"):
            # "mod version" or "mod version => replacement [version]": the
            # whole selection, replacement included, must agree.
            modules[parts[0]] = " ".join(parts[1:])
    return modules


def dependency_version_differences(gonka: Mapping[str, str], harness: Mapping[str, str]) -> List[Dict[str, str]]:
    return [
        {"module": module, "gonka": gonka[module], "harness": version}
        for module, version in sorted(harness.items())
        if module in gonka and gonka[module] != version
    ]


def parse_sha_file(text: str) -> Dict[str, str]:
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            result[parts[1].replace("/app/", "")] = parts[0]
    return result


def harness_file_hashes(harness_dir: Path) -> Dict[str, str]:
    result = {}
    for path in sorted(Path(harness_dir).rglob("*")):
        if path.is_symlink():
            raise BoundaryError(f"harness module contains a symlink: {path}")
        if path.is_file():
            result[path.relative_to(harness_dir).as_posix()] = sha256_file(path)
    return result


def validate_paths(root: Path, output: Path, harness_dir: Path) -> None:
    """Keep writable evidence separate from both immutable source trees."""
    if str(root).startswith("/mnt/"):
        raise BoundaryError("Use a clean Linux checkout; do not build from /mnt/c")
    for candidate, label in ((output, "output"), (harness_dir, "harness")):
        try:
            candidate.relative_to(root)
        except ValueError:
            pass
        else:
            raise BoundaryError(f"{label} {candidate} must be outside the Gonka snapshot {root}")
    try:
        output.relative_to(RUNNER_ROOT)
    except ValueError:
        pass
    else:
        raise BoundaryError(f"output {output} must be outside the runner checkout {RUNNER_ROOT}")
    # A custom harness may be outside the runner checkout. Prevent evidence
    # from being created in it (or from replacing an ancestor containing it).
    if output == harness_dir or output in harness_dir.parents or harness_dir in output.parents:
        raise BoundaryError(f"output {output} and harness {harness_dir} must not overlap")


def snapshot_or_fail(snapshot, root: Path, expected_sha: Optional[str], label: str) -> Dict[str, Any]:
    fingerprint = snapshot.capture(root)
    expected = expected_sha or fingerprint["head"]
    problems = snapshot.pristine_violations(fingerprint, expected_sha=expected)
    if problems:
        raise BoundaryError(
            f"{label} Gonka snapshot is not pristine: "
            + ", ".join(sorted({p["code"] for p in problems}))
        )
    return fingerprint


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    run: Callable[..., "subprocess.CompletedProcess[Any]"] = subprocess.run,
) -> int:
    cli = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    cli.add_argument("gonka")
    cli.add_argument("output")
    cli.add_argument("--expected-gonka-sha")
    cli.add_argument("--harness-dir", default=str(DEFAULT_HARNESS_DIR))
    args = cli.parse_args(argv)
    root = Path(args.gonka).resolve()
    output = Path(args.output).resolve()
    harness_dir = Path(args.harness_dir).resolve()
    validate_paths(root, output, harness_dir)

    snapshot = load_source_snapshot()
    try:
        before = snapshot_or_fail(snapshot, root, args.expected_gonka_sha, "before build:")
    except snapshot.SourceSnapshotError as exc:
        raise BoundaryError(f"Gonka checkout is not a usable snapshot: {exc}") from exc
    sha = before["head"]
    host_before = gonka_go_hashes(root)
    output.mkdir(parents=True, exist_ok=False)

    parsed = parse_go_mod((root / "inference-chain/go.mod").read_text(encoding="utf-8"))
    flags = replace_flags(parsed)
    dockerfile = generate_dockerfile((root / "inference-chain/Dockerfile").read_text(encoding="utf-8"), flags)
    harness_hashes = harness_file_hashes(harness_dir)
    raw = output / "raw"
    with tempfile.TemporaryDirectory(prefix="a8-go-boundary-") as tmp:
        spec = Path(tmp) / "Dockerfile"
        spec.write_text(dockerfile, encoding="utf-8")
        with (output / "build.log").open("w", encoding="utf-8") as log:
            result = run(
                [
                    "docker", "build",
                    "--file", str(spec),
                    "--build-context", f"harness={harness_dir}",
                    "--target", "boundary-evidence",
                    "--output", f"type=local,dest={raw}",
                    str(root),
                ],
                stdout=log, stderr=subprocess.STDOUT, timeout=1800,
            )

    host_after = gonka_go_hashes(root)
    after_problems: List[Dict[str, Any]] = []
    try:
        after = snapshot.capture(root)
        after_problems = snapshot.pristine_violations(after, expected_sha=sha) + [
            dict(item, code=snapshot.CODE_SNAPSHOT_MUTATED) for item in snapshot.differences(before, after)
        ]
    except snapshot.SourceSnapshotError as exc:
        after_problems = [{"code": exc.code, "message": str(exc)}]

    def read(name: str) -> Optional[str]:
        path = raw / name
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None

    code = (read("exit-code") or "").strip() or None
    container_before = parse_sha_file(read("gonka-go-before.sha256") or "")
    container_after = parse_sha_file(read("gonka-go-after.sha256") or "")
    gonka_modules = parse_modules(read("gonka-modules.txt") or "")
    harness_modules = parse_modules(read("harness-modules.txt") or "")
    version_diff = dependency_version_differences(gonka_modules, harness_modules)

    report: Dict[str, Any] = {
        "gonka_sha": sha,
        "gonka_tree_sha": before["tree"],
        "level": LEVEL,
        "docker_exit": result.returncode,
        "go_exit": code,
        "dockerfile_sha256": hashlib.sha256(dockerfile.encode()).hexdigest(),
        "harness_module": {"dir": str(harness_dir), "files_sha256": harness_hashes},
        "go_mod_edit_flags": flags,
        "gonka_go_files": {
            "host_before": host_before,
            "host_after": host_after,
            "container_before": container_before,
            "container_after": container_after,
            "unchanged": host_before == host_after
            and bool(container_before)
            and container_before == container_after,
        },
        "dependency_versions": {
            "gonka_modules": len(gonka_modules),
            "harness_modules": len(harness_modules),
            "differences": version_diff,
            "match": bool(gonka_modules) and bool(harness_modules) and not version_diff,
        },
        "source_snapshot": {
            "before_head": before["head"],
            "before_tree": before["tree"],
            "after_problems": after_problems,
            "verdict": "UNCHANGED" if not after_problems else "VIOLATED",
        },
        "status": "FAIL",
    }
    test_output = read("go-test.json")
    if result.returncode == 0 and code == "0" and test_output is not None:
        events = [json.loads(line) for line in test_output.splitlines() if line.startswith("{")]
        passed = {e.get("Test") for e in events if e.get("Action") == "pass"}
        if (
            set(REQUIRED_TESTS) <= passed
            and report["gonka_go_files"]["unchanged"]
            and report["dependency_versions"]["match"]
            and not after_problems
        ):
            report["status"] = "PASS"
    if test_output is not None:
        report["test_output_sha256"] = hashlib.sha256((raw / "go-test.json").read_bytes()).hexdigest()
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
