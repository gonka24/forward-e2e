"""Shared Git process fake for offline source acquisition tests.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from pathlib import Path
import hashlib
import subprocess
import re

PINNED_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f001234567"


def _completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=list(argv), returncode=returncode, stdout=stdout, stderr=stderr)


class FakeGitRunner:
    """Stand-in for ``subprocess.run`` injected via ``GitClient(runner=...)``.

    It is called exactly the way the production client calls it::

        runner(argv, cwd=..., env=..., capture_output=True, text=True, timeout=...)

    and answers with a real :class:`subprocess.CompletedProcess`. No git
    process is ever created.
    """

    SUBCOMMANDS = frozenset(
        {
            "init",
            "fetch",
            "cat-file",
            "rev-parse",
            "update-ref",
            "bundle",
            "ls-remote",
            "clone",
            "checkout",
            "ls-tree",
            "config",
        }
    )

    def __init__(
        self,
        *,
        head_sha=None,
        object_types=("commit",),
        ls_tree_output="",
        clone_contents=None,
        config_output="",
        snapshots=None,
        bundle_bytes=b"SYNTHETIC BUNDLE\n",
        fetch_returncode=0,
        fetch_stderr="",
    ):
        # An explicit override injects a wrong observed HEAD; normal HEADs
        # come from successful checkouts of commits present in the cloned bundle.
        self.head_sha = head_sha
        self._object_types = list(object_types)
        self.ls_tree_output = ls_tree_output
        self.clone_contents = dict(clone_contents or {})
        self.config_output = config_output
        self.snapshots = dict(snapshots or {})
        self.bundle_bytes = bundle_bytes
        self.fetch_returncode = fetch_returncode
        self.fetch_stderr = fetch_stderr
        self.repository_refs = {}
        self.repository_heads = {}
        self.bundles = {}
        self.calls = []
        self.envs = []

    # -- helpers -------------------------------------------------------
    def parse_command(self, argv, cwd=None):
        """Parse only global options used by the tested client; reject other forms."""
        repo = Path(cwd) if cwd is not None else Path.cwd()
        index = 1
        while index < len(argv) and argv[index].startswith("-"):
            option = argv[index]
            if option not in ("-C", "-c") or index + 1 >= len(argv):
                raise AssertionError(f"Unsupported Git global option: {option}")
            value = argv[index + 1]
            if option == "-C":
                repo = repo / value
            elif "=" not in value:
                raise AssertionError("Git -c requires a key=value argument")
            index += 2
        if index >= len(argv) or argv[index] not in self.SUBCOMMANDS:
            raise AssertionError(f"Unsupported Git command: {argv[index:]}")
        return repo.resolve(), argv[index], argv[index + 1:]

    def subcommand(self, argv):
        return self.parse_command(argv)[1]

    def register_bundle(self, path, refs):
        """Declare an existing fixture bundle, without accepting arbitrary files."""
        path = Path(path).resolve()
        self.bundles[path] = (path.read_bytes(), dict(refs))

    def _bundle_refs(self, path):
        path = Path(path).resolve()
        record = self.bundles.get(path)
        if record is None or not path.is_file() or path.read_bytes() != record[0]:
            return None
        return record[1]

    def _next_object_type(self):
        if len(self._object_types) > 1:
            return self._object_types.pop(0)
        return self._object_types[0]

    # -- the injected callable -----------------------------------------
    def __call__(self, argv, cwd=None, env=None, capture_output=False, text=False, timeout=None):
        result = self._run(argv, cwd=cwd, env=env)
        for name in ("stdout", "stderr"):
            value = getattr(result, name)
            if text:
                value = value.replace("\r\n", "\n").replace("\r", "\n")
            else:
                value = value.encode("utf-8", errors="surrogateescape")
            setattr(result, name, value)
        return result

    def _run(self, argv, cwd=None, env=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.envs.append(dict(env or {}))
        repo, sub, args = self.parse_command(argv, cwd)

        if sub == "fetch":
            return _completed(argv, self.fetch_returncode, "", self.fetch_stderr)

        if sub == "cat-file":
            obj_type = self._next_object_type()
            if obj_type is None:
                return _completed(argv, 128, "", "fatal: Not a valid object name")
            return _completed(argv, 0, obj_type + "\n")

        if sub == "rev-parse":
            if args == ["--git-dir"]:
                return _completed(argv, 0, ".git\n")
            if args == ["HEAD"]:
                head = self.head_sha if self.head_sha is not None else self.repository_heads.get(repo)
                if head is None:
                    return _completed(argv, 128, stderr="HEAD is not set")
                return _completed(argv, 0, head + "\n")
            if len(args) == 1 and re.fullmatch(r"[0-9a-fA-F]{40}\^\{commit\}", args[0]):
                return _completed(argv, 0, args[0][:-len("^{commit}")].lower() + "\n")
            if (
                len(args) == 2
                and args[0] == "--verify"
                and re.fullmatch(r"[0-9a-fA-F]{40}\^\{tree\}", args[1])
            ):
                sha = args[1][:-len("^{tree}")].lower()
                tree = hashlib.sha1(f"tree:{sha}".encode("utf-8")).hexdigest()
                return _completed(argv, 0, tree + "\n")
            raise AssertionError(f"Unsupported rev-parse arguments: {args}")

        if sub == "update-ref":
            if len(args) != 2:
                raise AssertionError("Unsupported update-ref arguments")
            self.repository_refs.setdefault(repo, {})[args[0]] = args[1].lower()
            return _completed(argv, 0)

        if sub == "bundle":
            if len(args) == 3 and args[0] == "create":
                target = repo / args[1]
                ref = args[2]
                refs = self.repository_refs.get(repo, {})
                if ref not in refs:
                    return _completed(argv, 128, stderr="unknown bundle ref")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.bundle_bytes)
                self.register_bundle(target, {ref: refs[ref]})
                return _completed(argv, 0)
            if len(args) == 2 and args[0] == "verify":
                valid = self._bundle_refs(repo / args[1]) is not None
                return _completed(argv, 0 if valid else 128,
                                  stderr="" if valid else "missing or unknown bundle")
            raise AssertionError(f"Unsupported bundle operation: {args}")

        if sub == "ls-remote":
            if len(args) != 1:
                raise AssertionError("Unsupported ls-remote arguments")
            refs = self._bundle_refs(repo / args[0])
            if refs is None:
                return _completed(argv, 128, stderr="missing or unknown bundle")
            return _completed(argv, 0, "".join(
                f"{sha}\t{ref}\n" for ref, sha in sorted(refs.items())
            ))

        if sub == "clone":
            if len(args) < 2:
                raise AssertionError("Clone requires a source bundle and destination")
            # Only the client's supported form is modelled: optional flags,
            # followed by exactly two paths. Never silently ignore an option typo.
            if (any(option not in ("--no-checkout", "--quiet") for option in args[:-2])
                    or any(path.startswith("-") for path in args[-2:])):
                raise AssertionError(f"Unsupported clone arguments: {args}")
            src_path = (repo / args[-2]).resolve()
            refs = self._bundle_refs(src_path)
            if refs is None and src_path.is_dir():
                refs = self.repository_refs.get(src_path)
            if refs is None:
                return _completed(argv, 128, stderr="missing or unknown bundle")
            target = repo / args[-1]
            target.mkdir(parents=True, exist_ok=True)
            (target / ".git").mkdir(exist_ok=True)
            self.repository_refs[target.resolve()] = dict(refs)
            self.repository_heads.pop(target.resolve(), None)
            for relative, text_content in self.clone_contents.items():
                destination = target.joinpath(*relative.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(text_content, encoding="utf-8")
            return _completed(argv, 0)

        if sub == "ls-tree":
            if args != ["-rz", "HEAD"]:
                raise AssertionError(f"Unsupported ls-tree arguments: {args}")
            snapshot = self.snapshots.get(self.repository_heads.get(repo), {})
            return _completed(argv, 0, snapshot.get("ls_tree_output", self.ls_tree_output))

        if sub == "config":
            if args == ["--null", "--no-includes", "--file", str(repo / ".gitmodules"), "--list"]:
                snapshot = self.snapshots.get(self.repository_heads.get(repo), {})
                return _completed(argv, snapshot.get("config_returncode", 0),
                                  snapshot.get("config_output", self.config_output))
            if args != ["--get", "remote.origin.url"]:
                raise AssertionError(f"Unsupported config arguments: {args}")
            # No origin is configured in any fake repository.
            return _completed(argv, 1)

        if sub == "checkout":
            if len(args) < 2 or args[:-1] not in (["--detach"], ["--quiet", "--detach"]):
                raise AssertionError(f"Unsupported checkout arguments: {args}")
            sha = args[-1].lower()
            # The fake models selected ref tips, not a complete Git object graph.
            if sha not in self.repository_refs.get(repo, {}).values():
                return _completed(argv, 128, stderr="commit is absent from the cloned bundle")
            self.repository_heads[repo] = sha
            for relative, contents in self.snapshots.get(sha, {}).get("contents", {}).items():
                destination = repo / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(contents, encoding="utf-8", errors="surrogateescape")
            return _completed(argv, 0)
        if sub == "init":
            return _completed(argv, 0)
        raise AssertionError(f"Unhandled Git command: {sub}")
