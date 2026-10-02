"""Safe ``git`` invocation for the E2E runner.

Design rules enforced here (all of them are review requirements):

* Every command is an ``argv`` list. The shell is never used, so a path or a
  URL taken from JSON can never become a command.
* Every exit code is checked. There is no "best effort" git call: callers get
  either a :class:`~ops.a8.e2e.errors.GitCommandError` or a completed process.
* Credentials never appear in a URL, in ``argv``, in a log line, in the lock,
  in a bundle or in evidence. A private source is authenticated through an
  ``GIT_ASKPASS`` helper that reads a mounted secret file at call time.
* Nothing is ever fetched interactively: ``GIT_TERMINAL_PROMPT=0`` guarantees
  a hard failure instead of a hanging prompt.
* Git LFS smudging is disabled during acquisition; LFS pointers are detected
  and reported explicitly instead of being silently materialised as pointer
  text (which would be an incomplete source snapshot).

The module is import-safe, and constructing the helpers performs no I/O.
The askpass script is created in a caller supplied directory only when an
environment allowing credentials is requested.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import Dict, List, Mapping, Optional, Sequence

from .errors import GitCommandError, SourceSpecError

# ``userinfo`` in a URL is the classic way tokens leak into logs and locks.
_URL_USERINFO_RE = re.compile(r"\A[a-zA-Z][a-zA-Z0-9+.\-]*://[^/@]*@")
_ALLOWED_URL_SCHEMES = ("https://",)
_SSH_SCP_RE = re.compile(r"\A[A-Za-z0-9._\-]+@[A-Za-z0-9._\-]+:")

REDACTED = "***REDACTED***"

# Patterns that look like a credential in free text. Used for defence in depth
# when a git error message is attached to an exception or written to a log.
# Each format names its own replacement: a capture group may be a label to
# preserve, not a secret. Mask the whole Authorization value, including schemes
# such as Bearer or Basic, without consuming the following diagnostic line.
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(r"(?i)\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"(?i)(authorization[^\S\r\n]*:[^\S\r\n]*)[^\r\n]+"), r"\g<1>" + REDACTED),
    (re.compile(r"(?i)(x-access-token:)[^@\s]+"), r"\g<1>" + REDACTED),
    (re.compile(r"://[^/\s@]+@"), "://" + REDACTED + "@"),
)


def redact(text: Optional[str]) -> str:
    """Remove anything that plausibly carries a secret from ``text``."""
    if not text:
        return ""
    cleaned = text
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def redact_argv(argv: Sequence[str]) -> List[str]:
    return [redact(str(a)) for a in argv]


def validate_remote_url(url: str) -> str:
    """Validate a remote repository URL.

    Only ``https://`` is accepted. ``ssh``/``scp`` forms are rejected because
    they would require host key and agent state that the container must not
    inherit, and ``file://``/local paths must be passed through the dedicated
    ``--*-path`` flag so that they are recorded as a local source.

    A URL that embeds ``user:password@`` is rejected outright: that is exactly
    the "token in the URL" failure mode the runner must never allow.
    Query strings and fragments are also rejected because they can carry tokens.
    """
    if not isinstance(url, str) or not url.strip():
        raise SourceSpecError("Repository URL must be a non-empty string")
    candidate = url.strip()
    if candidate != url:
        raise SourceSpecError(
            "Repository URL must not carry leading or trailing whitespace",
        )
    if _URL_USERINFO_RE.match(candidate):
        raise SourceSpecError(
            "Repository URL must not embed credentials. Use --credentials-file instead.",
            {"url": REDACTED},
        )
    if _SSH_SCP_RE.match(candidate):
        raise SourceSpecError(
            "SSH/SCP style remotes are not supported. Use an https:// URL, "
            "or pass a local clone with the matching --*-path flag.",
        )
    if not candidate.startswith(_ALLOWED_URL_SCHEMES):
        raise SourceSpecError(
            "Repository URL must start with https://",
        )
    if "?" in candidate or "#" in candidate:
        raise SourceSpecError(
            "Repository URL must not contain a query or fragment; use --credentials-file for authentication."
        )
    if any(ch in candidate for ch in ("\n", "\r", "\x00")):
        raise SourceSpecError("Repository URL must not contain control characters")
    return candidate


@dataclass(frozen=True)
class CredentialProvider:
    """A mounted secret used for private repositories.

    ``secret_file`` is read by the askpass helper *at git invocation time*.
    The runner itself never reads the value, never logs it and never copies
    it into the workspace, the lock or the evidence.
    """

    secret_file: Path
    username: str = "x-access-token"

    def validate(self) -> None:
        path = Path(self.secret_file)
        if not path.is_file():
            raise SourceSpecError(
                "Credential file does not exist or is not a regular file",
                {"path": str(path)},
            )
        if not self.username or any(c in self.username for c in ("\n", "\r", "\x00")):
            raise SourceSpecError("Credential username is invalid")


_ASKPASS_TEMPLATE = """#!/bin/sh
# Generated by the E2E runner. Prints a username or a token on demand so that
# the secret never appears in argv, in a URL or in a log line.
case "$1" in
  *[Uu]sername*) printf '%s' "$E2E_GIT_USERNAME" ;;
  *) cat "$E2E_GIT_SECRET_FILE" ;;
esac
"""


class GitClient:
    """A thin, strict wrapper around the ``git`` executable."""

    def __init__(
        self,
        *,
        credentials: Optional[CredentialProvider] = None,
        helper_dir: Optional[Path] = None,
        default_timeout: float = 900.0,
        runner=None,
    ):
        self.credentials = credentials
        self.helper_dir = Path(helper_dir) if helper_dir else None
        self.default_timeout = default_timeout
        # ``runner`` exists purely so that tests can inject a fake process
        # runner. Production always uses subprocess.run.
        self._runner = runner or subprocess.run
        self._askpass_path: Optional[Path] = None

    # ------------------------------------------------------------------
    # environment
    # ------------------------------------------------------------------
    def _ensure_askpass(self) -> Optional[Path]:
        if self.credentials is None:
            return None
        if self._askpass_path is not None:
            return self._askpass_path
        if self.helper_dir is None:
            raise GitCommandError(
                "A credential provider was configured without a helper directory"
            )
        self.credentials.validate()
        self.helper_dir.mkdir(parents=True, exist_ok=True)
        script = self.helper_dir / "e2e-askpass.sh"
        script.write_text(_ASKPASS_TEMPLATE, encoding="utf-8")
        script.chmod(0o700)
        self._askpass_path = script
        return script

    def build_env(
        self,
        extra: Optional[Mapping[str, str]] = None,
        *,
        allow_credentials: bool = False,
    ) -> Dict[str, str]:
        env = os.environ.copy()
        if extra:
            env.update(extra)
        # Environment-supplied configuration can override the disabled global
        # config (including credential helpers and URL rewrites). Let Git read
        # its normal system config path, but discard every inherited override.
        for key in list(env):
            if key == "GIT_CONFIG" or key.startswith("GIT_CONFIG_"):
                del env[key]
        # An empty helper resets the helper list from system/local config. Remote
        # authentication must use our explicit askpass provider, never a host helper.
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = ""
        # Hard fail instead of prompting; a hung prompt inside a container is
        # indistinguishable from a stalled network fetch.
        env["GIT_TERMINAL_PROMPT"] = "0"
        # Never let a developer's global config influence a reproducible run.
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        # LFS content is handled (or explicitly rejected) by the acquisition
        # layer, never implicitly smudged during clone/checkout.
        env["GIT_LFS_SKIP_SMUDGE"] = "1"
        env.pop("GIT_DIR", None)
        env.pop("GIT_WORK_TREE", None)
        env.pop("GIT_INDEX_FILE", None)
        askpass = self._ensure_askpass() if allow_credentials else None
        if askpass is not None and self.credentials is not None:
            env["GIT_ASKPASS"] = str(askpass)
            env["SSH_ASKPASS"] = str(askpass)
            env["E2E_GIT_USERNAME"] = self.credentials.username
            env["E2E_GIT_SECRET_FILE"] = str(Path(self.credentials.secret_file))
        else:
            # Without an enabled provider, make sure no inherited helper can silently
            # pull an arbitrary host credential into the run.
            env["GIT_ASKPASS"] = "/bin/false"
            env["SSH_ASKPASS"] = "/bin/false"
            env.pop("E2E_GIT_USERNAME", None)
            env.pop("E2E_GIT_SECRET_FILE", None)
        return env

    # ------------------------------------------------------------------
    # invocation
    # ------------------------------------------------------------------
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[Path] = None,
        timeout: Optional[float] = None,
        check: bool = True,
        env: Optional[Mapping[str, str]] = None,
        allow_credentials: bool = False,
        preserve_output: bool = False,
    ) -> subprocess.CompletedProcess:
        """Run ``git <args>`` and return the completed process.

        ``allow_credentials`` must be set explicitly for the few commands that
        talk to a remote. Local operations run without the askpass helper so
        that a malformed local path can never trigger a credential read.

        ``preserve_output`` captures bytes and decodes them without universal
        newline conversion, for machine output containing literal Git paths.
        """
        argv = ["git", *[str(a) for a in args]]
        for token in argv:
            if "\x00" in token:
                raise GitCommandError("git argument contains a NUL byte")
        base_env = self.build_env(env, allow_credentials=allow_credentials)
        effective_timeout = self.default_timeout if timeout is None else timeout
        try:
            proc = self._runner(
                argv,
                cwd=str(cwd) if cwd is not None else None,
                env=base_env,
                capture_output=True,
                text=not preserve_output,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCommandError(
                "git command timed out",
                {"argv": redact_argv(argv), "timeout_seconds": effective_timeout},
            ) from exc
        except FileNotFoundError as exc:
            raise GitCommandError(
                "git executable is not available in this environment",
                {"argv": redact_argv(argv)},
            ) from exc
        if preserve_output:
            # Surrogate escapes round-trip non-UTF-8 POSIX filename bytes too.
            proc.stdout = (proc.stdout or b"").decode("utf-8", errors="surrogateescape")
            proc.stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        if check and proc.returncode != 0:
            raise GitCommandError(
                "git command failed",
                {
                    "argv": redact_argv(argv),
                    "exit_code": proc.returncode,
                    "stderr": redact(proc.stderr)[-2000:],
                },
            )
        return proc

    def stdout(self, args: Sequence[str], **kwargs) -> str:
        return (self.run(args, **kwargs).stdout or "").strip()

    # ------------------------------------------------------------------
    # small helpers used across the acquisition pipeline
    # ------------------------------------------------------------------
    def object_type(self, repo: Path, sha: str) -> Optional[str]:
        """Return the git object type of ``sha`` or ``None`` when absent."""
        proc = self.run(
            ["-C", str(repo), "cat-file", "-t", sha],
            check=False,
            timeout=120.0,
        )
        if proc.returncode != 0:
            return None
        return (proc.stdout or "").strip() or None

    def has_commit(self, repo: Path, sha: str) -> bool:
        return self.object_type(repo, sha) == "commit"

    def init_repo(self, path: Path, *, bare: bool = False) -> None:
        path.mkdir(parents=True, exist_ok=True)
        args = ["init", "--quiet"]
        if bare:
            args.append("--bare")
        args.append(str(path))
        self.run(args, timeout=120.0)

    def local_safe_directory_args(self, path: Path) -> List[str]:
        """Narrow ``safe.directory`` grant for one specific read-only path.

        A read-only bind mount is frequently owned by another uid inside the
        container. Granting the exact path (never ``*``) keeps git working
        without weakening the global configuration.
        """
        return ["-c", f"safe.directory={Path(path)}"]


__all__ = [
    "CredentialProvider",
    "GitClient",
    "REDACTED",
    "redact",
    "redact_argv",
    "validate_remote_url",
]
