# Local Git integration tests

This suite requires Git on the host. It is separate from the offline unit tests
under `tests/unit/runner`, whose external process boundaries are mocked. CI runs it
as a distinct step in `python-offline-runner` on a native Linux checkout.

Run from the repository root:

```bash
python3 -B -m unittest discover -s tests/integration/local_sources -p 'test_*.py' -v
```

The tests create temporary repositories and verify that acquisition selects the
requested commit without changing a dirty user checkout. They do not access
remote repositories, Docker, or a live chain. Missing Git is an explicit skip
(`@unittest.skipUnless(shutil.which("git"), ...)`); unexpected Git command
failures fail the test.

Each fixture clears inherited `GIT_*` overrides for both setup and acquisition,
disables host configuration and templates (empty `GIT_CONFIG_GLOBAL` /
`GIT_CONFIG_SYSTEM` files, an empty `GIT_TEMPLATE_DIR`), and creates SHA-1
repositories (`git init --object-format=sha1`). The preservation checks
compare file contents, HEAD, full porcelain status
(`git status --porcelain=v1 --untracked-files=all`), and staged index entries
before and after acquisition.

## Host Git caveat

The code under test does not inherit the fixture's `GIT_CONFIG_SYSTEM`:
`GitClient` in `forward_e2e/execution/gitio.py` deliberately drops every
inherited `GIT_CONFIG*` override (adding back only its own empty
`credential.helper` entry), points `GIT_CONFIG_GLOBAL` at `os.devnull`, and
intentionally lets Git read its normal system config path. A host Git whose
**system** config sets `safe.bareRepository = explicit` (some vendor builds
do) therefore makes the fetch into the runner-owned bare scratch repository
fail with "cannot use bare repository", and one or more tests error out.
That is a property of that host Git, not of the code; a stock Git, which is
what CI uses, passes. Do not loosen `GitClient` to work around it.
See [`docs/development.md`](../../../docs/development.md) §2.
