"""Unit tests for A8 runtime safety guards, snapshot isolation, and cleanup verification.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

from forward_e2e.suite.lock import RuntimeLock
from forward_e2e.suite.runtime import (
    RUN_ID_REGEX,
    SuiteRuntimeError,
    RuntimeSnapshot,
    get_git_clean_head,
    perform_runtime_cleanup,
    prepare_runtime_snapshot,
)


class RuntimeGuardsTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-guards-")
        self.root = Path(self.tmp_dir.name)
        self.runtime_root = self.root / "runtime"
        self.marketplace = self.root / "marketplace"
        self.gonka = self.root / "gonka"

        self.marketplace.mkdir(parents=True)
        self.gonka.mkdir(parents=True)
        self.runtime_root.mkdir(parents=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_run_id_regex_validation(self):
        valid = ["c1-exact-e-001", "run_123", "regression-01-funded-claim", "A8-Test-42"]
        for vid in valid:
            self.assertTrue(RUN_ID_REGEX.match(vid), f"Expected {vid} to be valid RunId")
            snap = RuntimeSnapshot(self.runtime_root, vid)
            self.assertEqual(snap.run_id, vid)

        invalid = [
            "c1 exact e",           # space
            "c1/exact",             # slash
            "c1:exact",             # colon
            "run_тест",             # non-ascii
            "",                     # empty
            "a" * 66,               # > 65 characters
        ]
        for ivid in invalid:
            self.assertFalse(RUN_ID_REGEX.match(ivid) and len(ivid) <= 65, f"Expected {ivid} to be invalid")
            with self.assertRaises(SuiteRuntimeError):
                RuntimeSnapshot(self.runtime_root, ivid)

    @patch("forward_e2e.suite.runtime.run_cmd")
    def test_git_clean_head_requires_clean_working_tree(self, mock_run):
        # Fake git repo
        (self.marketplace / ".git").mkdir()

        # Case 1: Clean
        mock_run.side_effect = [
            MagicMock(stdout="a" * 40, returncode=0),  # rev-parse HEAD
            MagicMock(stdout="", returncode=0),        # status --porcelain
        ]
        head = get_git_clean_head(self.marketplace)
        self.assertEqual(head, "a" * 40)

        # Case 2: Dirty
        mock_run.side_effect = [
            MagicMock(stdout="a" * 40, returncode=0),
            MagicMock(stdout=" M src/contract.rs", returncode=0),
        ]
        with self.assertRaisesRegex(SuiteRuntimeError, "is dirty"):
            get_git_clean_head(self.marketplace)

    def test_existing_snapshot_blocks_prepare_single_use_invariant(self):
        run_id = "test-run-001"
        snap_dir = self.runtime_root / run_id
        snap_dir.mkdir(parents=True)

        with self.assertRaisesRegex(SuiteRuntimeError, "already exists. Refusing to overwrite"):
            prepare_runtime_snapshot(
                marketplace_source=self.marketplace,
                gonka_source=self.gonka,
                run_id=run_id,
                runtime_root=self.runtime_root,
            )

    @patch("forward_e2e.suite.runtime.run_cmd")
    def test_cleanup_touches_only_proven_owned_containers(self, mock_run):
        """Cleanup must leave foreign containers untouched."""
        snap = RuntimeSnapshot(self.runtime_root, "test-run-cleanup")
        snap.run_dir.mkdir(parents=True)
        snap.gonka_dir.mkdir(parents=True)
        snap.cleanup_evidence_dir.mkdir(parents=True)

        owned_dir = str(snap.gonka_dir.resolve())
        foreign_dir = "/home/otheruser/some-other-project"

        fake_inspect = [
            {
                "Id": "owned1234567890",
                "Name": "/genesis-node",
                "Config": {
                    "Labels": {
                        "com.docker.compose.project.working_dir": owned_dir,
                        "com.docker.compose.service": "chain-node",
                    }
                },
                "State": {"Status": "running"},
            },
            {
                "Id": "foreign987654321",
                "Name": "/other-app-db",
                "Config": {
                    "Labels": {
                        "com.docker.compose.project.working_dir": foreign_dir,
                        "com.docker.compose.service": "postgres",
                    }
                },
                "State": {"Status": "running"},
            },
        ]

        # Dispatch by exact argv so an added foreign container or obsolete
        # inspect call cannot consume a successful response meant for another action.
        responses = {
            ("docker", "ps", "-a", "--format", "{{.ID}}"): "owned1234567890\nforeign987654321",
            ("docker", "inspect", "owned1234567890", "foreign987654321"): json.dumps(fake_inspect),
            ("docker", "logs", "--tail", "1000", "owned1234567890"): "genesis node log output",
            ("docker", "stop", "-t", "10", "owned1234567890"): "",
            ("docker", "rm", "owned1234567890"): "",
        }

        def fake_run(cmd, **kwargs):
            self.assertIn(tuple(cmd), responses)
            if cmd[1] in ("stop", "rm"):
                # Ownership and diagnostics must be durable before destruction.
                ownership = json.loads(
                    (snap.cleanup_evidence_dir / "ownership.json").read_text(encoding="utf-8")
                )
                self.assertEqual([c["id"] for c in ownership["owned_containers"]], ["owned1234567"])
                self.assertEqual(
                    (snap.cleanup_evidence_dir / "container-logs" / "genesis-node.log").read_text(encoding="utf-8"),
                    "genesis node log output",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout=responses[tuple(cmd)], stderr="")

        mock_run.side_effect = fake_run

        lock = RuntimeLock(self.runtime_root / "exclusive.lock")
        res = perform_runtime_cleanup(snap, keep_resources=False, lock=lock)

        self.assertEqual(res["status"], "CLEANED")
        self.assertTrue(res["ownership_verified"])
        # Only the owned container was removed
        self.assertEqual(res["removed_containers"], ["owned1234567"])
        self.assertEqual(mock_run.call_args_list, [
            call(list(command), check=False) for command in responses
        ])
        # Diagnostics preserved
        ownership_file = snap.cleanup_evidence_dir / "ownership.json"
        self.assertTrue(ownership_file.is_file())
        completed_file = snap.cleanup_evidence_dir / "completed.json"
        self.assertTrue(completed_file.is_file())

    @patch("forward_e2e.suite.runtime.run_cmd")
    def test_cleanup_removes_named_and_anonymous_volumes_once_before_removing_the_network(self, mock_run):
        snap = RuntimeSnapshot(self.runtime_root, "test-run-vols")
        snap.run_dir.mkdir(parents=True)
        snap.gonka_dir.mkdir(parents=True)
        snap.cleanup_evidence_dir.mkdir(parents=True)

        fake_inspect = [
            {
                "Id": "cid_postgres",
                "Name": "/postgres",
                "Config": {
                    "Labels": {
                        "com.docker.compose.project.working_dir": str(snap.gonka_dir.resolve()),
                        "com.docker.compose.service": "postgres",
                    }
                },
                "Mounts": [
                    {"Type": "volume", "Name": "genesis_postgres-data"},
                    {"Type": "volume", "Name": "a" * 64},
                ],
                "NetworkSettings": {
                    "Networks": {
                        "chain-public": {}
                    }
                },
                "State": {"Status": "running"},
            }
        ]

        # Docker inspect includes a Name even for anonymous volumes. Model the
        # daemon's removal semantics so rm -v followed by volume rm fails.
        volumes = {"genesis_postgres-data", "a" * 64}

        def docker_boundary(argv, **kwargs):
            command = argv[1:]
            if command == ["ps", "-a", "--format", "{{.ID}}"]:
                return subprocess.CompletedProcess(argv, 0, "cid_postgres", "")
            if command == ["inspect", "cid_postgres"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps(fake_inspect), "")
            if command == ["logs", "--tail", "1000", "cid_postgres"]:
                return subprocess.CompletedProcess(argv, 0, "db log", "")
            if command == ["stop", "-t", "10", "cid_postgres"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if command in (["rm", "cid_postgres"], ["rm", "-v", "cid_postgres"]):
                if "-v" in command:
                    volumes.discard("a" * 64)
                return subprocess.CompletedProcess(argv, 0, "", "")
            if command[:2] == ["volume", "rm"] and len(command) == 3:
                volume = command[2]
                if volume not in volumes:
                    return subprocess.CompletedProcess(argv, 1, "", "no such volume")
                volumes.remove(volume)
                return subprocess.CompletedProcess(argv, 0, "", "")
            if command == ["network", "rm", "chain-public"]:
                self.assertFalse(volumes)
                return subprocess.CompletedProcess(argv, 0, "", "")
            self.fail(f"Unexpected Docker command: {argv}")

        mock_run.side_effect = docker_boundary

        lock = RuntimeLock(self.runtime_root / "exclusive.lock")
        res = perform_runtime_cleanup(snap, keep_resources=False, lock=lock)
        self.assertEqual(res["status"], "CLEANED")
        self.assertEqual(set(res["removed_volumes"]), {"genesis_postgres-data", "a" * 64})
        self.assertIn("chain-public", res["removed_networks"])

    @patch("forward_e2e.suite.runtime.run_cmd")
    def test_cleanup_aborts_without_removing_containers_if_discovery_fails(self, mock_run):
        snap = RuntimeSnapshot(self.runtime_root, "test-run-disc-fail")
        snap.run_dir.mkdir(parents=True)
        snap.gonka_dir.mkdir(parents=True)
        snap.cleanup_evidence_dir.mkdir(parents=True)

        fake_inspect = [
            {
                "Id": "cid_broken",
                "Name": "/broken",
                "Config": {
                    "Labels": {
                        "com.docker.compose.project.working_dir": str(snap.gonka_dir.resolve()),
                    }
                },
                "Mounts": "corrupted_non_list_mounts",
                "State": {"Status": "running"},
            }
        ]

        mock_run.side_effect = [
            MagicMock(stdout="cid_broken", returncode=0),
            MagicMock(stdout=json.dumps(fake_inspect), returncode=0),
        ]

        lock = RuntimeLock(self.runtime_root / "exclusive.lock")
        res = perform_runtime_cleanup(snap, keep_resources=False, lock=lock)
        self.assertEqual(res["status"], "FAILED")
        self.assertEqual(res["removed_containers"], [])
        self.assertTrue((snap.cleanup_evidence_dir / "ownership.json").is_file())

    @patch("forward_e2e.suite.runtime.run_cmd")
    def test_cleanup_keeps_containers_when_logs_cannot_be_saved(self, mock_run):
        snap = RuntimeSnapshot(self.runtime_root, "test-run-log-fail")
        snap.run_dir.mkdir(parents=True)
        snap.gonka_dir.mkdir(parents=True)
        owned = [{
            "Id": "cid_log_fail",
            "Name": "/genesis-node",
            "Config": {"Labels": {
                "io.gonka.a8.run-id": snap.run_id,
                "com.docker.compose.project.working_dir": str(snap.gonka_dir.resolve()),
            }},
            "Mounts": [],
            "NetworkSettings": {"Networks": {}},
            "State": {"Status": "running"},
        }]

        def docker_boundary(argv, **kwargs):
            command = argv[1:]
            if command == ["ps", "-a", "--format", "{{.ID}}"]:
                return subprocess.CompletedProcess(argv, 0, "cid_log_fail", "")
            if command == ["inspect", "cid_log_fail"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps(owned), "")
            if command == ["logs", "--tail", "1000", "cid_log_fail"]:
                return subprocess.CompletedProcess(argv, 1, "", "log driver unavailable")
            if command[0] in ("stop", "rm"):
                return subprocess.CompletedProcess(argv, 0, "", "")
            self.fail(f"Unexpected Docker command: {argv}")

        mock_run.side_effect = docker_boundary
        res = perform_runtime_cleanup(snap, keep_resources=False,
                                      lock=RuntimeLock(self.runtime_root / "exclusive.lock"))
        self.assertEqual(res["status"], "FAILED")
        self.assertEqual(res["removed_containers"], [])
        self.assertFalse(any(c.args[0][1] in ("stop", "rm") for c in mock_run.call_args_list))
        self.assertTrue((snap.cleanup_evidence_dir / "ownership.json").is_file())
        self.assertTrue((snap.cleanup_evidence_dir / "completed.json").is_file())

    @patch("forward_e2e.suite.runtime.run_cmd")
    def test_cleanup_fails_closed_on_inspect_error(self, mock_run):
        snap = RuntimeSnapshot(self.runtime_root, "test-run-inspect-err")
        snap.run_dir.mkdir(parents=True)
        snap.gonka_dir.mkdir(parents=True)
        snap.cleanup_evidence_dir.mkdir(parents=True)

        # ps -a succeeds, but inspect fails
        mock_run.side_effect = [
            MagicMock(stdout="some_cid", returncode=0),
            MagicMock(stdout="", stderr="Docker daemon error", returncode=1),
        ]

        lock = RuntimeLock(self.runtime_root / "exclusive.lock")
        res = perform_runtime_cleanup(snap, keep_resources=False, lock=lock)
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("Failed to inspect containers", res["error"])

    def test_keep_resources_skips_cleanup(self):
        snap = RuntimeSnapshot(self.runtime_root, "test-run-keep")
        snap.run_dir.mkdir(parents=True)
        snap.cleanup_evidence_dir.mkdir(parents=True)

        res = perform_runtime_cleanup(snap, keep_resources=True)
        self.assertEqual(res["status"], "KEPT")
        self.assertTrue((snap.cleanup_evidence_dir / "ownership.json").is_file())

    @patch("forward_e2e.suite.runtime.run_cmd")
    def test_cleanup_requires_consistent_ownership_label_and_compose_working_dir(
        self, mock_run
    ):
        """Immutable-source compose runs under snapshot.run_dir/network/local-test-net with io.gonka.a8.run-id."""
        snap = RuntimeSnapshot(self.runtime_root, "test-run-external-net")
        snap.run_dir.mkdir(parents=True)
        snap.gonka_dir.mkdir(parents=True)
        snap.cleanup_evidence_dir.mkdir(parents=True)
        external_net_dir = snap.run_dir / "network" / "local-test-net"
        external_net_dir.mkdir(parents=True)

        fake_inspect = [
            {
                "Id": "by_label_1234567890",
                "Name": "/api-node",
                "Config": {
                    "Labels": {
                        "io.gonka.a8.run-id": snap.run_id,
                        "com.docker.compose.project.working_dir": str(external_net_dir.resolve()),
                        "com.docker.compose.service": "api",
                    }
                },
                "State": {"Status": "running"},
            },
            {
                "Id": "by_netdir_123456789",
                "Name": "/chain-node",
                "Config": {
                    "Labels": {
                        "com.docker.compose.project.working_dir": str(external_net_dir.resolve()),
                        "com.docker.compose.service": "node",
                    }
                },
                "State": {"Status": "running"},
            },
            {
                "Id": "foreign_run_1234567",
                "Name": "/other-run-node",
                "Config": {
                    "Labels": {
                        "io.gonka.a8.run-id": "other-run-id",
                        "com.docker.compose.project.working_dir": str(external_net_dir.resolve()),
                        "com.docker.compose.service": "node",
                    }
                },
                "State": {"Status": "running"},
            },
            {
                "Id": "foreign_path_123456",
                "Name": "/foreign-path-node",
                "Config": {
                    "Labels": {
                        "io.gonka.a8.run-id": snap.run_id,
                        "com.docker.compose.project.working_dir": "/elsewhere/workdir",
                        "com.docker.compose.service": "node",
                    }
                },
                "State": {"Status": "running"},
            },
        ]

        def docker_boundary(argv, **kwargs):
            command = argv[1:]
            if command == ["ps", "-a", "--format", "{{.ID}}"]:
                return subprocess.CompletedProcess(
                    argv, 0, "by_label_1234567890\nby_netdir_123456789\nforeign_run_1234567\nforeign_path_123456", ""
                )
            if command[0] == "inspect":
                return subprocess.CompletedProcess(argv, 0, json.dumps(fake_inspect), "")
            if command[0] in ("logs", "stop", "rm"):
                return subprocess.CompletedProcess(argv, 0, "", "")
            self.fail(f"Unexpected Docker command: {argv}")

        mock_run.side_effect = docker_boundary
        lock = RuntimeLock(self.runtime_root / "exclusive.lock")
        res = perform_runtime_cleanup(snap, keep_resources=False, lock=lock)

        self.assertEqual(res["status"], "CLEANED")
        self.assertEqual(res["removed_containers"], ["by_label_123", "by_netdir_12"])

    @patch("forward_e2e.suite.runtime.run_cmd")
    def test_shared_direct_source_path_alone_does_not_transfer_container_ownership(self, mock_run):
        snap = RuntimeSnapshot(self.runtime_root, "test-run-direct-source")
        snap.run_dir.mkdir(parents=True)
        # In E2E direct-source mode, every task uses this same suite checkout.
        snap.gonka_dir = self.gonka.resolve()
        containers = [
            {
                "Id": "owned_direct_123456",
                "Name": "/owned-node",
                "Config": {"Labels": {
                    "io.gonka.a8.run-id": snap.run_id,
                    "com.docker.compose.project.working_dir": str(snap.gonka_dir),
                }},
                "State": {"Status": "running"},
            },
            {
                "Id": "unlabelled_direct_123456",
                "Name": "/unlabelled-node",
                "Config": {"Labels": {
                    "com.docker.compose.project.working_dir": str(snap.gonka_dir),
                }},
                "State": {"Status": "running"},
            },
        ]

        def docker_boundary(argv, **kwargs):
            command = argv[1:]
            if command == ["ps", "-a", "--format", "{{.ID}}"]:
                return subprocess.CompletedProcess(argv, 0,
                                                   "owned_direct_123456\nunlabelled_direct_123456", "")
            if command[0] == "inspect":
                return subprocess.CompletedProcess(argv, 0, json.dumps(containers), "")
            if command[0] in ("logs", "stop", "rm"):
                return subprocess.CompletedProcess(argv, 0, "", "")
            self.fail(f"Unexpected Docker command: {argv}")

        mock_run.side_effect = docker_boundary
        res = perform_runtime_cleanup(snap, keep_resources=False,
                                      lock=RuntimeLock(self.runtime_root / "exclusive.lock"))
        self.assertEqual(res["status"], "CLEANED")
        self.assertEqual(res["removed_containers"], ["owned_direct"])


if __name__ == "__main__":
    unittest.main()
