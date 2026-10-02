# Local Git integration tests

This suite requires Git on the host. It is separate from the offline unit tests
under `ops/a8/tests`, whose external process boundaries are mocked. CI runs it
as a distinct step in `python-offline-runner` on a native Linux checkout.

Run from the repository root:

```bash
python3 -B -m unittest discover -s ops/a8/integration_tests -v
```

The tests create temporary repositories and verify that acquisition selects the
requested commit without changing a dirty user checkout. They do not access
remote repositories, Docker, or a live chain. Missing Git is an explicit skip;
unexpected Git command failures fail the test.

Each fixture clears inherited `GIT_*` overrides for both setup and acquisition,
disables host configuration and templates, and creates SHA-1 repositories.
The preservation checks compare file contents, HEAD, full porcelain status,
and staged index entries before and after acquisition.
