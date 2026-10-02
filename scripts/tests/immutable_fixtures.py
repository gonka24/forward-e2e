"""Synthetic fixtures for the immutable-source harness tests.

Why real Git repositories: ``ops/a8/source_snapshot.py`` measures a snapshot by
running ``git`` (HEAD, tree, index, status including ignored files, gitlinks).
Mocking Git would test the mock, so these fixtures create throw-away local
repositories in a temporary directory. Git is run with the global and system
configuration disabled and never touches a remote.

Where the fixtures reproduce upstream Gonka, the text is copied verbatim from
Gonka ``c33c9eaa5bc40c53b564159b5e1534bbfdab8a08`` (the reference SHA of the
external harness), so the real ``required-upstream-api.json`` patterns match
them. The B3 genesis documents reproduce cosmos-sdk v0.53 ``x/genutil``
``AddGenesisAccount`` output (BaseAccount with ``@type``, ``pub_key: null``,
string ``account_number``/``sequence``, balances sorted by address, supply as a
sorted coin list with string amounts); no recorded genesis exists under
``docs/reviews/evidence``.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    from scripts.tests.support import a8, completed, env_pairs
except ImportError:  # pragma: no cover - direct discovery from scripts/tests
    from support import a8, completed, env_pairs

harness = a8.external_harness

# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "a8 fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "a8 fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2025-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2025-01-01T00:00:00Z",
}


def git(root: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(_GIT_ENV)
    result = subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {root}: {result.stderr}")
    return result.stdout.strip()


def write_files(root: Path, files: Mapping[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


def commit_repo(root: Path, files: Mapping[str, str | bytes], message: str = "fixture") -> str:
    """Create (or extend) a local repository at ``root`` and return HEAD."""
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        git(root, "init", "-q")
    write_files(root, files)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# Upstream Gonka (verbatim excerpts at c33c9eaa)
# ---------------------------------------------------------------------------

# testermint/src/main/kotlin/DockerGroup.kt:69 and :887-890 at c33c9eaa.
UPSTREAM_GET_REPO_ROOT = (
    "fun getRepoRoot(): String {\n"
    "    // Allow an explicit override so worktrees / additional checkouts (e.g.\n"
    "    // gonka-2) can run tests without renaming their directory.\n"
    '    System.getenv("GONKA_REPO_ROOT")?.takeIf { it.isNotBlank() }?.let { return it }\n'
    '    return System.getProperty("user.dir")\n'
    "}\n"
)
UPSTREAM_LOCAL_TEST_NET_DIR = 'const val LOCAL_TEST_NET_DIR = "local-test-net"\n'
# local-test-net/docker-compose-base.yml:23 at c33c9eaa.
UPSTREAM_COMPOSE_BASE = (
    "services:\n"
    "  api:\n"
    "    container_name: ${KEY_NAME}-api\n"
    "    volumes:\n"
    "      - ./prod-local/${KEY_NAME}:/root/.inference\n"
)
#: The API symbols the synthetic Gonka satisfies, taken from the real spec.
API_SYMBOL_IDS = ("getRepoRoot.GONKA_REPO_ROOT", "LOCAL_TEST_NET_DIR", "compose.api.container_name")

GONKA_DOCKER_GROUP = "testermint/src/main/kotlin/DockerGroup.kt"


def gonka_files() -> dict[str, str | bytes]:
    return {
        ".gitignore": "build/\n.gradle/\n",
        GONKA_DOCKER_GROUP: "package com.productscience\n\n"
        + UPSTREAM_LOCAL_TEST_NET_DIR
        + "\n"
        + UPSTREAM_GET_REPO_ROOT,
        "testermint/src/test/kotlin/TestermintTest.kt": "import org.junit.jupiter.api.TestInstance\n",
        "testermint/src/main/resources/mappings/health.json": '{"request": {"url": "/health"}}\n',
        "testermint/src/main/resources/alternative-mappings/a.template.json": "{}\n",
        "testermint/gradle/wrapper/gradle-wrapper.jar": b"PK\x03\x04synthetic-wrapper",
        "testermint/gradle/wrapper/gradle-wrapper.properties": (
            "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.8-bin.zip\n"
        ),
        "local-test-net/docker-compose-base.yml": UPSTREAM_COMPOSE_BASE,
        "local-test-net/docker-compose.dns.yml": "services:\n  dns:\n    image: coredns/coredns\n",
        "local-test-net/dns/Corefile": ".:53 {\n    forward . 8.8.8.8\n}\n",
        "inference-chain/test_genesis_overrides.json": "{}\n",
        "inference-chain/scripts/init-docker-genesis.sh": "#!/bin/sh\nset -e\necho synthetic upstream genesis\n",
    }


def marketplace_files() -> dict[str, str | bytes]:
    return {
        ".gitignore": "target/\n",
        "Cargo.toml": '[workspace]\nmembers = []\nresolver = "2"\n',
        # A decoy harness copy inside the target: run-live must never use it.
        "scripts/a8_acceptance.py": "raise SystemExit('decoy harness')\n",
    }


def write_harness_dir(root: Path, *, symbol_ids=API_SYMBOL_IDS) -> Path:
    """An external harness project whose API spec is a subset of the real one."""
    real = json.loads((harness.DEFAULT_TESTERMINT_HARNESS_DIR / "required-upstream-api.json").read_text("utf-8"))
    by_id = {item["id"]: item for item in real["symbols"]}
    missing = [symbol for symbol in symbol_ids if symbol not in by_id]
    if missing:
        raise AssertionError(f"real required-upstream-api.json no longer declares {missing}")
    spec = {
        "schema": real["schema"],
        "upstream_reference_sha": real["upstream_reference_sha"],
        "symbols": [by_id[symbol] for symbol in symbol_ids],
    }
    write_files(
        root,
        {
            "required-upstream-api.json": json.dumps(spec, indent=2) + "\n",
            "settings.gradle.kts": f'rootProject.name = "{harness.HARNESS_ROOT_PROJECT_NAME}"\n',
            "build.gradle.kts": "plugins { kotlin(\"jvm\") }\n",
            "gradle/a8-out-of-tree.init.gradle.kts": "// synthetic init script\n",
            "src/test/kotlin/MarketplaceContractAcceptanceTests.kt": "class MarketplaceContractAcceptanceTests\n",
        },
    )
    return root


# ---------------------------------------------------------------------------
# B3 genesis (cosmos-sdk v0.53 x/genutil AddGenesisAccount shape)
# ---------------------------------------------------------------------------

GENESIS_ADDRESS = "gonka1qyqszqgpqyqszqgpqyqszqgpqyqszqgp7wzkd8"
POOL_ADDRESS = "gonka1zg69v7yszg69v7yszg69v7yszg69v7ys8xdv96"
B3_COMMAND = (
    f"inferenced genesis add-genesis-account {harness.B3_FOREIGN_ADDRESS} "
    f"{harness.B3_FOREIGN_AMOUNT}{harness.B3_FOREIGN_DENOM} --keyring-backend test"
)


def _base_account(address: str) -> dict[str, Any]:
    return {
        "@type": "/cosmos.auth.v1beta1.BaseAccount",
        "address": address,
        "pub_key": None,
        "account_number": "0",
        "sequence": "0",
    }


def genesis_before_b3() -> dict[str, Any]:
    """Genesis right after the POOL account was added (provisioner step order)."""
    return {
        "app_name": "inferenced",
        "app_version": "",
        "genesis_time": "2025-01-01T00:00:00Z",
        "chain_id": "gonka-mainnet",
        "initial_height": 1,
        "app_hash": None,
        "app_state": {
            "auth": {
                "params": {
                    "max_memo_characters": "256",
                    "tx_sig_limit": "7",
                    "tx_size_cost_per_byte": "10",
                    "sig_verify_cost_ed25519": "590",
                    "sig_verify_cost_secp256k1": "1000",
                },
                "accounts": [_base_account(GENESIS_ADDRESS), _base_account(POOL_ADDRESS)],
            },
            "bank": {
                "params": {"send_enabled": [], "default_send_enabled": True},
                "balances": [
                    {"address": GENESIS_ADDRESS, "coins": [{"denom": "ngonka", "amount": "2000000000"}]},
                    {"address": POOL_ADDRESS, "coins": [{"denom": "ngonka", "amount": "160000000000000000"}]},
                ],
                "supply": [{"denom": "ngonka", "amount": "160000002000000000"}],
                "denom_metadata": [],
                "send_enabled": [],
            },
            "genutil": {"gen_txs": []},
        },
    }


def genesis_after_b3(before: Mapping[str, Any] | None = None) -> dict[str, Any]:
    after = copy.deepcopy(dict(before or genesis_before_b3()))
    bank = after["app_state"]["bank"]
    after["app_state"]["auth"]["accounts"].append(_base_account(harness.B3_FOREIGN_ADDRESS))
    bank["balances"].append(
        {
            "address": harness.B3_FOREIGN_ADDRESS,
            "coins": [{"denom": harness.B3_FOREIGN_DENOM, "amount": str(harness.B3_FOREIGN_AMOUNT)}],
        }
    )
    bank["balances"].sort(key=lambda entry: entry["address"])
    bank["supply"].append({"denom": harness.B3_FOREIGN_DENOM, "amount": str(harness.B3_FOREIGN_AMOUNT)})
    bank["supply"].sort(key=lambda coin: coin["denom"])
    return after


def genesis_final(after: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """After gentx collection: gen_txs filled, the B3 fixture untouched."""
    final = copy.deepcopy(dict(after or genesis_after_b3()))
    final["app_state"]["genutil"]["gen_txs"] = [{"body": {"messages": [{"@type": "/cosmos.staking.v1beta1.MsgCreateValidator"}]}}]
    return final


def _dump(value: Any) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def write_b3_provision(
    provision_dir: Path,
    *,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    final: Mapping[str, Any] | None = None,
    derived_from: str | None = None,
    command: str = B3_COMMAND,
    omit: tuple[str, ...] = (),
) -> None:
    """What ``a8-genesis-provision.sh`` leaves under ``$STATE_DIR/a8-provision``."""
    before = before if before is not None else genesis_before_b3()
    after = after if after is not None else genesis_after_b3(before)
    final = final if final is not None else genesis_final(after)
    provision_dir.mkdir(parents=True, exist_ok=True)
    documents = {
        "genesis-before-b3.json": _dump(before),
        "genesis-after-b3.json": _dump(after),
        "genesis-final.json": _dump(final),
    }
    for name, data in documents.items():
        if name not in omit:
            (provision_dir / name).write_bytes(data)
    record = {
        "schema": "a8.b3-provision/1",
        "derived_from_sha256": derived_from or harness.GENESIS_PROVISIONER_UPSTREAM_SHA256,
        "command": command,
        "address": harness.B3_FOREIGN_ADDRESS,
        "denom": harness.B3_FOREIGN_DENOM,
        "amount": str(harness.B3_FOREIGN_AMOUNT),
        "genesis_validate": "passed",
        "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in documents.items()},
    }
    if "b3-provision.json" not in omit:
        (provision_dir / "b3-provision.json").write_bytes(_dump(record))


# ---------------------------------------------------------------------------
# JUnit (Gradle JUnit Platform XML report shape)
# ---------------------------------------------------------------------------


def junit_xml(test_name: str, *, outcome: str = "passed") -> str:
    cls, method = harness.split_test_name(test_name)
    body = {
        "passed": "",
        "failed": '    <failure message="boom" type="java.lang.AssertionError">boom</failure>\n',
        "skipped": "    <skipped/>\n",
    }[outcome]
    close = "/>" if not body else ">\n" + body + "  </testcase>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="{cls}" tests="1" skipped="{int(outcome == "skipped")}" '
        f'failures="{int(outcome == "failed")}" errors="0" timestamp="2025-01-01T00:00:00" '
        'hostname="runner" time="12.5">\n'
        "  <properties/>\n"
        f'  <testcase name="{method}()" classname="{cls}" time="12.5"{close}\n'
        "  <system-out><![CDATA[]]></system-out>\n"
        "  <system-err><![CDATA[]]></system-err>\n"
        "</testsuite>\n"
    )


# ---------------------------------------------------------------------------
# Scripted command runner
# ---------------------------------------------------------------------------


def _flag(argv, prefix: str) -> str:
    for item in argv:
        if item.startswith(prefix):
            return item[len(prefix):]
    raise AssertionError(f"{prefix} missing from {argv}")


class ScriptedRunner:
    """Plays cargo and Gradle for run-live, producing what the real tools write.

    Every product it writes goes where the real tool would write it given the
    argv the harness built -- so a harness that pointed Gradle at the snapshot
    would make this runner write into the snapshot, and the after-snapshot
    would catch it.
    """

    def __init__(
        self,
        *,
        junit_outcome: str = "passed",
        junit_test: str | None = None,
        write_junit: bool = True,
        write_context: bool = True,
        test_returncode: int = 0,
        during_test: Callable[[dict[str, str]], None] | None = None,
        during_build: Callable[[], None] | None = None,
        b3_writer: Callable[[Path], None] | None = None,
    ):
        self.calls: list[list[str]] = []
        self.junit_outcome = junit_outcome
        self.junit_test = junit_test
        self.write_junit = write_junit
        self.write_context = write_context
        self.test_returncode = test_returncode
        self.during_test = during_test
        self.during_build = during_build
        self.b3_writer = b3_writer

    def run(self, argv, *, input_text=None, timeout=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["cargo", "build"]:
            release = Path(argv[argv.index("--target-dir") + 1]) / "wasm32-unknown-unknown" / "release"
            release.mkdir(parents=True, exist_ok=True)
            (release / "a8_caller.wasm").write_bytes(b"\x00asm-caller")
            (release / "a8_cw20.wasm").write_bytes(b"\x00asm-cw20")
            return completed()
        if "a8ExportClasspath" in argv:
            classpath = Path(_flag(argv, "-Pa8.classpathFile="))
            classpath.parent.mkdir(parents=True, exist_ok=True)
            classpath.write_text("/w/gradle-home/caches/kotlin-stdlib-2.0.0.jar\n/w/upstream-build/classes\n", "utf-8")
            classpath.with_name(classpath.name + ".json").write_text(
                json.dumps({"schema": "a8.testermint-classpath/1", "entries": 2}) + "\n", "utf-8"
            )
            return completed("BUILD SUCCESSFUL")
        if "testClasses" in argv:
            if self.during_build is not None:
                self.during_build()
            return completed("BUILD SUCCESSFUL")
        if "--tests" in argv:
            env = env_pairs(argv)
            test_name = argv[argv.index("--tests") + 1]
            out_root = Path(_flag(argv, "-Pa8.outRoot="))
            if self.write_junit:
                results = out_root / harness.HARNESS_ROOT_PROJECT_NAME / "test-results" / "test"
                results.mkdir(parents=True, exist_ok=True)
                (results / "TEST-MarketplaceContractAcceptanceTests.xml").write_text(
                    junit_xml(self.junit_test or test_name, outcome=self.junit_outcome), "utf-8"
                )
            if self.write_context:
                Path(env["A8_CONTEXT"]).write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0.0",
                            "kind": "gonka-marketplace-a8-live-context",
                            "run_id": env["A8_RUN_ID"],
                            "level": "live_network",
                            "source": {
                                "evidence_model": env[a8.ENV_EVIDENCE_MODEL],
                                "gonka_source_sha": env[a8.ENV_EXPECTED_GONKA_SHA],
                                "gonka_sha": env[a8.ENV_EXPECTED_GONKA_SHA],
                                "protobuf_sha": env[a8.ENV_EXPECTED_PROTO_SHA],
                                "runtime": {"gonka_source_sha": env[a8.ENV_EXPECTED_GONKA_SHA]},
                            },
                        }
                    ),
                    "utf-8",
                )
            if self.b3_writer is not None:
                self.b3_writer(Path(env["GONKA_REPO_ROOT"]) / harness.PROVISION_DIR_IN_PROD_LOCAL)
            if self.during_test is not None:
                self.during_test(env)
            return completed("BUILD SUCCESSFUL" if not self.test_returncode else "FAILED", self.test_returncode)
        raise AssertionError(f"unexpected command: {argv}")
