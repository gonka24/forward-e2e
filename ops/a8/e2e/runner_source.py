"""Git identity baked into the runner image from its selected commit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

from .errors import IntegrityError

SOURCE_FILENAME = "runner-source.json"
_SHA = re.compile(r"[0-9a-f]{40}")


def read_runner_source(root: Path) -> dict[str, str]:
    path = root / SOURCE_FILENAME
    if path.is_symlink():
        raise IntegrityError("Runner source identity must not be a symlink")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IntegrityError("Runner source identity is missing or invalid; rebuild from a full SHA") from exc
    if not isinstance(body, dict) or body.get("schema_version") != 1:
        raise IntegrityError("Unsupported runner source identity")
    for key in ("commit_sha", "tree_sha"):
        if not isinstance(body.get(key), str) or not _SHA.fullmatch(body[key]):
            raise IntegrityError("Runner source identity requires full Git SHAs", {"field": key})
    if not isinstance(body.get("repo_url"), str) or not body["repo_url"].startswith("https://"):
        raise IntegrityError("Runner source identity requires an HTTPS repository URL")
    return {key: body[key] for key in ("repo_url", "commit_sha", "tree_sha")}


def bake_runner_source(root: Path, expected_commit: str, repo_url: str) -> None:
    """Measure the fetched Git objects, rather than trusting a build label."""
    if not _SHA.fullmatch(expected_commit):
        raise IntegrityError("Runner build requires a full lowercase 40-hex commit SHA")
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    commit = git("rev-parse", "HEAD")
    if commit != expected_commit or git("status", "--porcelain", "--untracked-files=all", "--ignored"):
        raise IntegrityError("Runner build checkout must be pristine at the selected commit")
    body = {"schema_version": 1, "repo_url": repo_url, "commit_sha": commit,
            "tree_sha": git("rev-parse", "HEAD^{tree}")}
    (root / SOURCE_FILENAME).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    read_runner_source(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--repo-url", required=True)
    args = parser.parse_args()
    bake_runner_source(Path.cwd(), args.expected_commit, args.repo_url)
