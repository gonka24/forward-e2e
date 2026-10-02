# Published acceptance evidence

> [!NOTE]
> **Historical Provenance Notice**
> This repository was prepared for public release from a verified clean snapshot starting with a single commit.
> References in historical reviews, evidence manifests, and matrices to intermediate Git commit
> hashes (e.g., `83076ee`, `a6bbc6f`), pull request numbers (e.g., `PR #11`, `PR #13`, `PR #24`),
> and local execution environments refer to the project's private development archive.
> These records are retained to preserve cryptographic provenance, audit trails, and exact verification
> boundaries. Historical PASS outcomes validate specific test matrices under their recorded conditions
> and do not constitute an automated certification of future releases. New releases and verification
> evidence must be generated directly from public commits.

These files preserve historical acceptance results; they do not certify a new
contract build or production deployment. Current coverage and proof limits are
summarized in the [coverage matrix](../a8-final-coverage-matrix.md).

Local workstation paths were anonymized for publication. Paths such as
`/home/runner/a8-runtime/...` and `./workspace/...` are placeholders for historical
artifact locations, not instructions to fetch artifacts from a CI runner.
Repository and pull-request URLs (including development forks) identify the actual source used for a run and
must not be replaced with unrelated upstream URLs.

In `a8-matrix-source-register-20260910.json`, each top-level file entry's `sha256`
hashes the current published file bytes. These hashes were refreshed after path
anonymization and normalized to lowercase hexadecimal. Nested
hashes identify the original raw artifacts, source files, or Wasm binaries; they
are retained and are not hashes of the anonymized path strings. Verifying those
raw hashes requires the original artifacts. Entries whose keys are not JSON
filenames are historical source or run metadata, not additional local files;
any hashes within those records still refer to their original artifacts.
