"""Low-level A8 runtime environment controller: prepare, probe, run, cleanup.

Ensures:
- Preflight guards: native POSIX filesystem check (rejects /mnt/c and symlinks), mount probe (2 cycles), tool versions (cosmwasm-check 2.2.2).
- Immutable snapshots in $HOME/a8-runtime/<run_id>/
- Single-use snapshots (one native selector = one snapshot)
- Re-use of fixed Git bundles across the suite
- Global flock protection via exclusive.lock
- Full ownership verification for containers, shared volumes (genesis/join postgres-data), and networks (chain-public)
- Preservation of diagnostics and raw logs prior to destruction
- Refusal to touch foreign/unverified containers, networks, or volumes
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

try:
    from .lock import RuntimeLock
    from .evidence_model import EVIDENCE_MODEL_IMMUTABLE
except (ImportError, ValueError):
    _repo_root = Path(__file__).resolve().parent.parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    from ops.a8.lock import RuntimeLock
    from ops.a8.evidence_model import EVIDENCE_MODEL_IMMUTABLE


RUN_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,65}$")
POSTGRES_DATA_VOLUMES = (
    "genesis_postgres-data",
    "join1_postgres-data",
    "join2_postgres-data",
)


class RuntimeErrorA8(RuntimeError):
    """Base exception for runtime environment errors."""


def get_default_runtime_root() -> Path:
    if "A8_WORKSPACE_DIR" in os.environ:
        return Path(os.environ["A8_WORKSPACE_DIR"]).resolve() / "runtime"
    return Path.home() / "a8-runtime"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def run_cmd(
    argv: Sequence[str],
    cwd: Optional[Path] = None,
    timeout: Optional[float] = None,
    check: bool = True,
    env: Optional[Mapping[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    """Execute command safely as an argv array (never through shell)."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            text=True,
            capture_output=True,
            env=merged_env,
        )
        if check and proc.returncode != 0:
            cmd_str = " ".join(argv)
            raise RuntimeErrorA8(
                f"Command failed (exit {proc.returncode}): {cmd_str}\n"
                f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
            )
        return proc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeErrorA8(f"Command timed out after {timeout}s: {' '.join(argv)}") from exc
    except FileNotFoundError as exc:
        raise RuntimeErrorA8(f"Executable not found: {argv[0]}") from exc


def check_native_filesystem(path: Path) -> None:
    """Ensure path is on a native Linux filesystem, not /input, /out, /mnt/c, or non-POSIX mounts."""
    resolved = Path(path).resolve()
    resolved_str = str(resolved)

    # Strictly forbid running Testermint inside host bind mounts (/input, /out)
    if resolved_str == "/input" or resolved_str.startswith("/input/"):
        raise RuntimeErrorA8(
            f"Path {resolved} is inside host input mount /input. "
            "Testermint live execution must run inside native Linux workspace volume (/workspace)."
        )
    if resolved_str == "/out" or resolved_str.startswith("/out/"):
        raise RuntimeErrorA8(
            f"Path {resolved} is inside host output mount /out. "
            "Host output mount is strictly for exported artifacts, not runtime execution."
        )

    # Strictly forbid Windows DrvFs / NTFS mounts
    if resolved_str.startswith("/mnt/c") or resolved_str.startswith("/mnt/C"):
        raise RuntimeErrorA8(
            f"Path {resolved} is on Windows /mnt/c mount. "
            "A8 live execution requires a native Linux filesystem."
        )

    # Check if path itself is a symlink resolving to forbidden mounts
    p = Path(path)
    if p.is_symlink():
        target = p.readlink()
        target_str = str(target)
        if target_str.startswith(("/mnt/", "/input", "/out")):
            raise RuntimeErrorA8(f"Path {path} is a symlink pointing to non-native mount: {target}")

    # Check /proc/mounts if available to detect non-POSIX filesystems
    proc_mounts = Path("/proc/mounts")
    if proc_mounts.is_file():
        try:
            for line in proc_mounts.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    mount_point, fs_type = parts[1], parts[2]
                    if resolved_str == mount_point or resolved_str.startswith(mount_point.rstrip("/") + "/"):
                        if fs_type in ("cifs", "smbfs", "9p", "vboxsf", "drvfs", "fuse.sshfs"):
                            raise RuntimeErrorA8(
                                f"Path {resolved} is mounted on unsupported filesystem type {fs_type!r}. "
                                "A8 live execution requires a native POSIX Linux filesystem."
                            )
        except Exception as exc:
            if isinstance(exc, RuntimeErrorA8):
                raise
            pass


def perform_mount_probe(runtime_root: Path) -> None:
    """Execute two mount/write/chmod/read/remove cycles to ensure POSIX filesystem integrity."""
    check_native_filesystem(runtime_root)
    probe_dir = runtime_root / ".probe-tmp"
    probe_dir.mkdir(parents=True, exist_ok=True)
    probe_file = probe_dir / "probe.bin"

    for cycle in (1, 2):
        try:
            # Write
            test_bytes = f"a8-mount-probe-cycle-{cycle}".encode("utf-8")
            probe_file.write_bytes(test_bytes)
            # Chmod
            probe_file.chmod(0o755)
            # Stat check
            st = probe_file.stat()
            if (st.st_mode & 0o777) != 0o755:
                raise RuntimeErrorA8(
                    f"Mount probe cycle {cycle} failed: permissions mismatch {oct(st.st_mode)}"
                )
            # Read back
            if probe_file.read_bytes() != test_bytes:
                raise RuntimeErrorA8(f"Mount probe cycle {cycle} failed: data readback mismatch")
            # Remove
            probe_file.unlink()
        except Exception as exc:
            if probe_file.exists():
                probe_file.unlink(missing_ok=True)
            raise RuntimeErrorA8(f"Mount probe failed on {runtime_root}: {exc}") from exc

    probe_dir.rmdir()


def verify_toolchain_versions(check_cosmwasm: bool = False) -> Dict[str, str]:
    """Verify toolchain tools and versions."""
    versions: Dict[str, str] = {}

    git_res = run_cmd(["git", "--version"], check=False)
    if git_res.returncode != 0:
        raise RuntimeErrorA8("Toolchain error: git is required but not available")
    versions["git"] = git_res.stdout.strip()

    py_res = run_cmd([sys.executable, "--version"], check=False)
    if py_res.returncode != 0:
        raise RuntimeErrorA8("Toolchain error: python3 is required")
    versions["python"] = py_res.stdout.strip()

    if check_cosmwasm:
        cw_res = run_cmd(["cosmwasm-check", "--version"], check=False)
        match = re.search(r"\b2\.2\.2\b", cw_res.stdout)
        if cw_res.returncode != 0 or not match:
            raise RuntimeErrorA8(
                f"Toolchain error: cosmwasm-check 2.2.2 is required. Got: {cw_res.stdout.strip() or cw_res.stderr.strip()}"
            )
        versions["cosmwasm-check"] = cw_res.stdout.strip()

    return versions


def get_git_clean_head(repo_dir: Path) -> str:
    """Verify repo directory is a git repository with a clean working tree and return HEAD SHA."""
    repo = Path(repo_dir).resolve()
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        raise RuntimeErrorA8(f"Directory is not a Git repository: {repo}")

    head = run_cmd(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    status = run_cmd(["git", "-C", str(repo), "status", "--porcelain"]).stdout.strip()
    if status:
        raise RuntimeErrorA8(f"Git repository {repo} is dirty; uncommitted changes exist:\n{status}")
    return head


def create_git_bundle(repo_dir: Path, bundle_output: Path) -> str:
    """Create a git bundle containing HEAD of the specified repository and return HEAD SHA."""
    repo = Path(repo_dir).resolve()
    bundle_output = Path(bundle_output).resolve()
    bundle_output.parent.mkdir(parents=True, exist_ok=True)

    # If worktree without reachable common gitdir
    git_file = repo / ".git"
    if git_file.is_file():
        rev_test = run_cmd(["git", "-C", str(repo), "rev-parse", "HEAD"], check=False)
        if rev_test.returncode != 0:
            raise RuntimeErrorA8(
                f"Repository at '{repo}' appears to be a Git worktree whose common gitdir is not accessible inside the container.\n"
                "To run with Git worktrees, please use the host launcher script:\n"
                "  Windows PowerShell: ./ops/e2e/Run-E2E.ps1 run ...\n"
                "  Linux / macOS bash: ./ops/e2e/run-e2e.sh run ...\n"
                "The host launcher automatically prepares Git bundles from host worktrees."
            )

    head = get_git_clean_head(repo)
    if bundle_output.exists():
        bundle_output.unlink()
    run_cmd(["git", "-C", str(repo), "bundle", "create", str(bundle_output), "HEAD"])
    return head


def clone_from_bundle(bundle_path: Path, target_dir: Path, expected_head: str) -> None:
    """Clone repository from bundle into target_dir and verify HEAD matches expected_head."""
    bundle = Path(bundle_path).resolve()
    target = Path(target_dir).resolve()
    if target.exists():
        raise RuntimeErrorA8(f"Clone target directory already exists: {target}")
    run_cmd(["git", "clone", str(bundle), str(target)])
    actual_head = run_cmd(["git", "-C", str(target), "rev-parse", "HEAD"]).stdout.strip()
    if actual_head != expected_head:
        raise RuntimeErrorA8(
            f"Cloned repository HEAD {actual_head} does not match expected bundle HEAD {expected_head}"
        )


def git_tree_sha(repo_dir: Path) -> str:
    """The Git tree object of HEAD in ``repo_dir`` (full 40-hex SHA).

    The tree pins the content of the snapshot independently of commit
    metadata, which is what the immutable-source evidence compares.
    """
    tree = run_cmd(["git", "-C", str(Path(repo_dir).resolve()), "rev-parse", "HEAD^{tree}"]).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise RuntimeErrorA8(f"git rev-parse HEAD^{{tree}} in {repo_dir} returned {tree!r}, not a full tree SHA")
    return tree


class RuntimeSnapshot:
    """Represents an isolated A8 runtime execution directory."""

    def __init__(self, runtime_root: Path, run_id: str):
        if not RUN_ID_REGEX.fullmatch(run_id):
            raise RuntimeErrorA8(
                f"Invalid RunId {run_id!r}. Must match regex {RUN_ID_REGEX.pattern} (max 65 chars)"
            )
        self.runtime_root = Path(runtime_root).resolve()
        self.run_id = run_id
        self.run_dir = self.runtime_root / run_id
        self.marketplace_dir = self.run_dir / "marketplace"
        self.gonka_dir = self.run_dir / "gonka"
        self.evidence_dir = self.run_dir / "evidence"
        self.task_evidence_dir = self.evidence_dir / run_id
        self.junit_dir = self.run_dir / "junit"
        self.cleanup_evidence_dir = self.run_dir / "cleanup-evidence"
        self.identity_file = self.run_dir / "identity.json"
        self.started_file = self.run_dir / "started.json"
        self.result_file = self.run_dir / "result.json"
        self.launcher_log = self.run_dir / "launcher.log"

    def exists(self) -> bool:
        return self.run_dir.exists()


def prepare_runtime_snapshot(
    marketplace_source: Path,
    gonka_source: Path,
    run_id: str,
    runtime_root: Optional[Path] = None,
    lock: Optional[RuntimeLock] = None,
    fixed_marketplace_sha: Optional[str] = None,
    fixed_gonka_sha: Optional[str] = None,
    existing_marketplace_bundle: Optional[Path] = None,
    existing_gonka_bundle: Optional[Path] = None,
    check_cosmwasm: bool = False,
    recorded_gonka_source_sha: Optional[str] = None,
    evidence_model: Optional[str] = None,
    use_direct_sources: bool = False,
) -> RuntimeSnapshot:
    """Prepare a fresh isolated runtime snapshot with preflight guards.

    When ``use_direct_sources=True``, the snapshot points ``marketplace_dir``
    and ``gonka_dir`` directly at the verified immutable checkouts instead of
    creating intermediate Git bundles or cloning per-task copies of the two
    repositories. Per-task isolation of ``run_dir``, ``evidence_dir``,
    ``junit_dir`` and ``cleanup_evidence_dir`` is preserved.

    ``recorded_gonka_source_sha`` names the commit the user selected. Under the
    immutable-source model it must equal the checked-out head; a difference
    would mean some other tree reached this point, and the snapshot is refused
    rather than recorded under two names.

    ``identity.json`` records both commits, both Git tree objects
    (``git rev-parse HEAD^{tree}``) and ``evidence_model`` verbatim, so a
    reader never has to infer which rules produced the snapshot.
    """
    root = Path(runtime_root).resolve() if runtime_root else get_default_runtime_root()

    # Preflight 1: Native filesystem and Mount Probe
    check_native_filesystem(root)
    perform_mount_probe(root)

    # Preflight 2: Toolchain
    verify_toolchain_versions(check_cosmwasm=check_cosmwasm)

    snapshot = RuntimeSnapshot(root, run_id)
    if snapshot.exists():
        raise RuntimeErrorA8(
            f"Snapshot directory {snapshot.run_dir} already exists. Refusing to overwrite. Use a fresh RunId."
        )

    active_lock = lock or RuntimeLock(root / "exclusive.lock")
    with active_lock:
        if not use_direct_sources and fixed_marketplace_sha and existing_marketplace_bundle and existing_marketplace_bundle.is_file():
            m_head = fixed_marketplace_sha
        else:
            m_head = get_git_clean_head(marketplace_source)
            if fixed_marketplace_sha and m_head != fixed_marketplace_sha:
                raise RuntimeErrorA8(
                    f"Marketplace HEAD {m_head} changed from fixed suite SHA {fixed_marketplace_sha}"
                )

        if not use_direct_sources and fixed_gonka_sha and existing_gonka_bundle and existing_gonka_bundle.is_file():
            g_head = fixed_gonka_sha
        else:
            g_head = get_git_clean_head(gonka_source)
            if fixed_gonka_sha and g_head != fixed_gonka_sha:
                raise RuntimeErrorA8(
                    f"Gonka HEAD {g_head} changed from fixed suite SHA {fixed_gonka_sha}"
                )

        snapshot.run_dir.mkdir(parents=True, exist_ok=False)
        snapshot.evidence_dir.mkdir(parents=True, exist_ok=True)
        # NOTE: snapshot.task_evidence_dir is intentionally NOT pre-created here.
        # It must be created by the harness (which enforces exist_ok=False).
        snapshot.junit_dir.mkdir(parents=True, exist_ok=True)
        snapshot.cleanup_evidence_dir.mkdir(parents=True, exist_ok=True)

        m_bundle_sha: Optional[str] = None
        g_bundle_sha: Optional[str] = None
        if use_direct_sources:
            snapshot.marketplace_dir = Path(marketplace_source).resolve()
            snapshot.gonka_dir = Path(gonka_source).resolve()
        else:
            bundles_dir = snapshot.run_dir / "bundles"
            bundles_dir.mkdir(parents=True, exist_ok=True)
            m_bundle = bundles_dir / "marketplace.bundle"
            g_bundle = bundles_dir / "gonka.bundle"

            if existing_marketplace_bundle and existing_marketplace_bundle.is_file():
                shutil.copy2(existing_marketplace_bundle, m_bundle)
            else:
                create_git_bundle(marketplace_source, m_bundle)

            if existing_gonka_bundle and existing_gonka_bundle.is_file():
                shutil.copy2(existing_gonka_bundle, g_bundle)
            else:
                create_git_bundle(gonka_source, g_bundle)

            clone_from_bundle(m_bundle, snapshot.marketplace_dir, m_head)
            clone_from_bundle(g_bundle, snapshot.gonka_dir, g_head)
            m_bundle_sha = sha256_file(m_bundle)
            g_bundle_sha = sha256_file(g_bundle)

        # The commit the user selected is the commit that was cloned. A caller
        # naming a different one is describing a tree that was prepared
        # elsewhere, which the immutable-source model does not allow.
        if recorded_gonka_source_sha and recorded_gonka_source_sha != g_head:
            raise RuntimeErrorA8(
                f"Selected Gonka commit {recorded_gonka_source_sha} differs from the snapshot "
                f"HEAD {g_head}; the immutable-source runner never builds or tests a tree "
                "other than the selected commit"
            )
        gonka_tree_sha = git_tree_sha(snapshot.gonka_dir)
        marketplace_tree_sha = git_tree_sha(snapshot.marketplace_dir)

        identity = {
            "run_id": run_id,
            "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "marketplace_source_sha": m_head,
            "marketplace_commit_sha": m_head,
            "marketplace_tree_sha": marketplace_tree_sha,
            "gonka_source_sha": g_head,
            "gonka_commit_sha": g_head,
            "gonka_checkout_sha": g_head,
            "gonka_tree_sha": gonka_tree_sha,
            # Which rules produced this snapshot, stated rather than inferred.
            # See ops/a8/evidence_model.py.
            "evidence_model": evidence_model or EVIDENCE_MODEL_IMMUTABLE,
            "source_policy": "immutable",
            "source_mode": "direct_immutable" if use_direct_sources else "bundle_clone",
            "marketplace_bundle_sha256": m_bundle_sha,
            "gonka_bundle_sha256": g_bundle_sha,
            "run_dir": str(snapshot.run_dir),
        }
        snapshot.identity_file.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
        return snapshot


def probe_runtime_environment(
    runtime_root: Optional[Path] = None,
    lock: Optional[RuntimeLock] = None,
) -> Dict[str, Any]:
    """Probe system tools, Docker health, mount probe, and ensure no orphaned Testermint resources exist."""
    root = Path(runtime_root).resolve() if runtime_root else get_default_runtime_root()
    active_lock = lock or RuntimeLock(root / "exclusive.lock")

    with active_lock:
        info: Dict[str, Any] = {
            "runtime_root": str(root),
            "probed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "tools": {},
            "mount_probe": "FAIL",
            "docker_status": "UNKNOWN",
            "existing_testermint_containers": [],
            "existing_named_volumes": [],
            "status": "PASS",
        }

        # Mount probe
        try:
            perform_mount_probe(root)
            info["mount_probe"] = "OK"
        except Exception as exc:
            info["mount_probe"] = f"FAIL: {exc}"
            info["status"] = "FAIL"

        # Check tools
        try:
            tools = verify_toolchain_versions()
            info["tools"] = tools
        except Exception as exc:
            info["status"] = "FAIL"
            info["tools_error"] = str(exc)

        # Check docker
        docker_res = run_cmd(["docker", "info"], check=False)
        if docker_res.returncode == 0:
            info["docker_status"] = "OK"
            ps_res = run_cmd(
                ["docker", "ps", "-a", "--filter", "label=com.docker.compose.service=chain-node", "--format", "{{.ID}}|{{.Names}}|{{.Labels}}"],
                check=False,
            )
            containers = []
            if ps_res.returncode == 0 and ps_res.stdout.strip():
                for line in ps_res.stdout.strip().splitlines():
                    parts = line.split("|", 2)
                    containers.append({
                        "id": parts[0],
                        "name": parts[1] if len(parts) > 1 else "",
                        "labels": parts[2] if len(parts) > 2 else "",
                    })
            info["existing_testermint_containers"] = containers
            if containers:
                info["status"] = "WARN_LINGERING_CONTAINERS"

            # Check volumes
            v_res = run_cmd(["docker", "volume", "ls", "--format", "{{.Name}}"], check=False)
            if v_res.returncode == 0 and v_res.stdout.strip():
                vol_names = set(v_res.stdout.strip().splitlines())
                lingering_vols = [v for v in POSTGRES_DATA_VOLUMES if v in vol_names]
                info["existing_named_volumes"] = lingering_vols
                if lingering_vols and info["status"] == "PASS":
                    info["status"] = "WARN_LINGERING_CONTAINERS"
        else:
            info["docker_status"] = f"UNAVAILABLE: {docker_res.stderr.strip()}"
            info["status"] = "FAIL"

        return info


def inspect_owned_containers(snapshot: RuntimeSnapshot) -> List[Dict[str, Any]]:
    """Query Docker for containers with a task-owned path and consistent run label."""
    docker_check = run_cmd(["docker", "ps", "-a", "--format", "{{.ID}}"], check=False)
    if docker_check.returncode != 0:
        raise RuntimeErrorA8(f"Docker ps failed: {docker_check.stderr.strip()}")

    cids = docker_check.stdout.strip().split()
    if not cids:
        return []

    inspect_res = run_cmd(["docker", "inspect", *cids], check=False)
    if inspect_res.returncode != 0:
        raise RuntimeErrorA8(f"Docker inspect failed: {inspect_res.stderr.strip()}")

    try:
        items = json.loads(inspect_res.stdout)
    except Exception as exc:
        raise RuntimeErrorA8(f"Failed to parse Docker inspect output: {exc}")

    owned: List[Dict[str, Any]] = []
    expected_work_dir = snapshot.gonka_dir.resolve()
    expected_run_dir = snapshot.run_dir.resolve()
    expected_harness_work_dir = (expected_work_dir.parent / "a8-work" / snapshot.run_id).resolve()

    for item in items:
        labels = item.get("Config", {}).get("Labels") or {}
        run_label = labels.get("io.gonka.a8.run-id")
        # A run-id label alone is not proof of ownership, and a contradictory
        # label must override a matching working directory. Both are Docker
        # metadata supplied by the container creator, so require consistency.
        if run_label is not None and run_label != snapshot.run_id:
            continue

        work_dir_str = labels.get("com.docker.compose.project.working_dir", "")
        if not isinstance(work_dir_str, str) or not work_dir_str or not Path(work_dir_str).is_absolute():
            continue
        try:
            work_dir_path = Path(work_dir_str).resolve()
        except Exception:
            continue

        # A cloned Gonka directory belongs to this task's run_dir. In direct
        # source mode the same checkout is shared by several tasks, so its path
        # alone cannot establish ownership; require this task's run label.
        if (
            (work_dir_path == expected_work_dir and (
                expected_run_dir in expected_work_dir.parents
                or run_label == snapshot.run_id
            ))
            or expected_run_dir in work_dir_path.parents
            or work_dir_path == expected_harness_work_dir
            or expected_harness_work_dir in work_dir_path.parents
        ):
            owned.append(item)

    return owned


def perform_runtime_cleanup(
    snapshot: RuntimeSnapshot,
    keep_resources: bool = False,
    lock: Optional[RuntimeLock] = None,
) -> Dict[str, Any]:
    """Perform verified, fail-closed cleanup of containers, volumes, and networks.

    Guards:
    1. Proves ownership before stopping or removing.
    2. Saves container logs and ownership.json BEFORE destructive actions.
    3. Removes named shared postgres volumes (genesis_postgres-data, etc.).
    4. Removes chain-public network if empty or owned.
    5. Fails closed if Docker inspection encounters an error.
    """
    cleanup_info: Dict[str, Any] = {
        "run_id": snapshot.run_id,
        "cleanup_started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "keep_resources": keep_resources,
        "ownership_verified": False,
        "owned_containers": [],
        "removed_containers": [],
        "removed_volumes": [],
        "removed_networks": [],
        "container_logs_saved": [],
        "status": "NOT_NEEDED",
    }

    if keep_resources:
        cleanup_info["status"] = "KEPT"
        snapshot.cleanup_evidence_dir.mkdir(parents=True, exist_ok=True)
        (snapshot.cleanup_evidence_dir / "ownership.json").write_text(
            json.dumps(cleanup_info, indent=2) + "\n", encoding="utf-8"
        )
        return cleanup_info

    active_lock = lock or RuntimeLock(snapshot.runtime_root / "exclusive.lock")
    with active_lock:
        try:
            owned = inspect_owned_containers(snapshot)
        except Exception as exc:
            cleanup_info["status"] = "FAILED"
            cleanup_info["error"] = f"Failed to inspect containers for ownership: {exc}"
            snapshot.cleanup_evidence_dir.mkdir(parents=True, exist_ok=True)
            (snapshot.cleanup_evidence_dir / "ownership.json").write_text(
                json.dumps(cleanup_info, indent=2) + "\n", encoding="utf-8"
            )
            return cleanup_info

        cleanup_info["owned_containers"] = [
            {
                "id": c.get("Id", "")[:12],
                "name": c.get("Name", "").lstrip("/"),
                "service": (c.get("Config", {}).get("Labels") or {}).get("com.docker.compose.service"),
                "status": c.get("State", {}).get("Status"),
                # The immutable image id the container actually ran. Recorded so
                # that build provenance ("these images came from the selected
                # sources") stays checkable offline, after the run is over.
                "image": c.get("Image"),
                "image_reference": (c.get("Config", {}) or {}).get("Image"),
            }
            for c in owned
        ]

        snapshot.cleanup_evidence_dir.mkdir(parents=True, exist_ok=True)

        if owned:
            cleanup_info["ownership_verified"] = True

            # Collect proven attached volumes and networks directly from verified full inspect objects
            owned_volumes: set[str] = set()
            owned_networks: set[str] = set()
            discovery_failed = False
            discovery_err = ""

            for c in owned:
                cid = c.get("Id", "")
                mounts = c.get("Mounts")
                if mounts is not None and not isinstance(mounts, list):
                    discovery_failed = True
                    discovery_err = f"Container {cid[:12]} Mounts field is corrupted: {type(mounts)}"
                    break
                for m in (mounts or []):
                    if isinstance(m, dict) and m.get("Type") == "volume" and m.get("Name"):
                        owned_volumes.add(str(m["Name"]))

                net_settings = c.get("NetworkSettings")
                if isinstance(net_settings, dict):
                    networks = net_settings.get("Networks")
                    if isinstance(networks, dict):
                        for net_name in networks.keys():
                            if net_name not in ("bridge", "host", "none"):
                                owned_networks.add(str(net_name))
                    elif networks is not None:
                        discovery_failed = True
                        discovery_err = f"Container {cid[:12]} Networks field is corrupted: {type(networks)}"
                        break

            if discovery_failed:
                cleanup_info["status"] = "FAILED"
                cleanup_info["error"] = f"Discovery error before destructive cleanup: {discovery_err}"
                (snapshot.cleanup_evidence_dir / "ownership.json").write_text(
                    json.dumps(cleanup_info, indent=2) + "\n", encoding="utf-8"
                )
                (snapshot.cleanup_evidence_dir / "completed.json").write_text(
                    json.dumps(cleanup_info, indent=2) + "\n", encoding="utf-8"
                )
                return cleanup_info

            cleanup_info["owned_volumes"] = sorted(owned_volumes)
            cleanup_info["owned_networks"] = sorted(owned_networks)

            # 1. Save container logs before stopping
            logs_dir = snapshot.cleanup_evidence_dir / "container-logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            for c in owned:
                cid = c.get("Id", "")
                cname = c.get("Name", "").lstrip("/") or cid[:12]
                log_file = logs_dir / f"{cname}.log"
                try:
                    proc = run_cmd(["docker", "logs", "--tail", "1000", cid], check=False)
                    log_file.write_text(
                        str(proc.stdout or "") + str(proc.stderr or ""), encoding="utf-8"
                    )
                except (OSError, RuntimeErrorA8) as exc:
                    cleanup_info["status"] = "FAILED"
                    cleanup_info["error"] = f"Could not save logs for owned container {cid[:12]}: {exc}"
                else:
                    if proc.returncode != 0:
                        cleanup_info["status"] = "FAILED"
                        cleanup_info["error"] = (
                            f"Docker logs failed for owned container {cid[:12]}: {proc.stderr}"
                        )
                if cleanup_info["status"] == "FAILED":
                    (snapshot.cleanup_evidence_dir / "ownership.json").write_text(
                        json.dumps(cleanup_info, indent=2) + "\n", encoding="utf-8"
                    )
                    (snapshot.cleanup_evidence_dir / "completed.json").write_text(
                        json.dumps(cleanup_info, indent=2) + "\n", encoding="utf-8"
                    )
                    return cleanup_info
                cleanup_info["container_logs_saved"].append(str(log_file.name))

            # 2. Save ownership.json prior to destructive actions
            (snapshot.cleanup_evidence_dir / "ownership.json").write_text(
                json.dumps(cleanup_info, indent=2) + "\n", encoding="utf-8"
            )

            # 3. Stop and remove owned containers
            cids_to_kill = [c.get("Id") for c in owned if c.get("Id")]
            if cids_to_kill:
                run_cmd(["docker", "stop", "-t", "10", *cids_to_kill], check=False)
                # Volumes are removed explicitly below, including anonymous ones.
                # Using rm -v here would delete those before that recorded step.
                rm_proc = run_cmd(["docker", "rm", *cids_to_kill], check=False)
                if rm_proc.returncode == 0:
                    cleanup_info["removed_containers"] = [c[:12] for c in cids_to_kill]
                else:
                    cleanup_info["status"] = "FAILED"
                    cleanup_info["error"] = rm_proc.stderr
                    (snapshot.cleanup_evidence_dir / "completed.json").write_text(
                        json.dumps(cleanup_info, indent=2) + "\n", encoding="utf-8"
                    )
                    return cleanup_info

            # 4. Remove proven owned volumes (fail closed if any fails)
            if owned_volumes:
                for vol in sorted(owned_volumes):
                    rm_v = run_cmd(["docker", "volume", "rm", vol], check=False)
                    if rm_v.returncode == 0:
                        cleanup_info["removed_volumes"].append(vol)
                    else:
                        cleanup_info["status"] = "FAILED"
                        cleanup_info["error"] = f"Failed to remove proven owned volume {vol}: {rm_v.stderr}"
                        (snapshot.cleanup_evidence_dir / "completed.json").write_text(
                            json.dumps(cleanup_info, indent=2) + "\n", encoding="utf-8"
                        )
                        return cleanup_info

            # 5. Remove proven owned networks (fail closed if any fails)
            if owned_networks:
                for net in sorted(owned_networks):
                    rm_n = run_cmd(["docker", "network", "rm", net], check=False)
                    if rm_n.returncode == 0:
                        cleanup_info["removed_networks"].append(net)
                    else:
                        cleanup_info["status"] = "FAILED"
                        cleanup_info["error"] = f"Failed to remove proven owned network {net}: {rm_n.stderr}"
                        (snapshot.cleanup_evidence_dir / "completed.json").write_text(
                            json.dumps(cleanup_info, indent=2) + "\n", encoding="utf-8"
                        )
                        return cleanup_info

        if cleanup_info["status"] != "FAILED":
            cleanup_info["status"] = "CLEANED" if (cleanup_info["removed_containers"] or cleanup_info["removed_volumes"]) else "NOT_NEEDED"

        (snapshot.cleanup_evidence_dir / "completed.json").write_text(
            json.dumps(cleanup_info, indent=2) + "\n", encoding="utf-8"
        )
        return cleanup_info
