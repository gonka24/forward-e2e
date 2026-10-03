"""Test package for Forward E2E runner and tooling."""

import os
import tempfile

# On macOS, TMPDIR defaults to /var/folders/... where /var is a symlink to
# /private/var. The runner's O_NOFOLLOW directory walker refuses symlinked
# ancestor components unless the temporary root is resolved first.
_tempdir = tempfile.gettempdir()
if os.path.islink("/var") and _tempdir.startswith("/var/"):
    _resolved_tempdir = os.path.realpath(_tempdir)
    os.environ["TMPDIR"] = _resolved_tempdir
    tempfile.tempdir = _resolved_tempdir
