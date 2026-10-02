# Third-party licensing scope

The root BUSL grant covers only original Gonka24-authored material for which the
Licensor holds the necessary rights. It does not replace third-party terms.

- `packages/gonka-proto/proto/vendor/` preserves upstream protobuf definitions.
  Their headers, the package [NOTICE](packages/gonka-proto/NOTICE), and the pinned
  [provenance](https://github.com/gonka24/forward-contracts/blob/d637eea5432506d60c90c1d8436c67b93802d829/packages/gonka-proto/proto/PROVENANCE.toml) identify the sources.
- `packages/gonka-proto/src/generated/` contains generated bindings derived from
  that snapshot. Generation does not remove applicable upstream terms.
- Upstream-derived A8 harness material must be tracked to its individual source
  revision. `A8UpstreamTestSupport.kt` identifies its verbatim helper source as
  Gonka commit `c33c9eaa5bc40c53b564159b5e1534bbfdab8a08`,
  `testermint/src/test/kotlin/DevshardTestSupport.kt` lines 340–364. The genesis
  provisioner identifies the source sequence and its SHA-256 in its header;
  both refer to `inference-chain/scripts/init-docker-genesis.sh` at that same
  commit. At that commit, GitHub reports `LICENSE.md` blob
  `c65ef755460ee6fedad9669216df820f5c0e29ed` (14,883 bytes), and the complete
  recursive tree contains no separate Appendix A or Genesis Code Reference
  file. The license text defines Genesis Code by that reference but the pinned
  source tree does not expose its file list; the verbatim Kotlin helper has no
  per-file license header. The Go boundary fixtures are project-authored test
  support and must not be treated as verbatim upstream code without separate
  evidence.
  `licenses/Gonka-pinned.txt` is the copy of `LICENSE.md` recorded for Gonka
  commit `379bebced638aeb5e6077bfd51c986f898443832` in the protobuf provenance;
  that pin alone does not establish the license text or file-specific scope at
  `c33c9eaa5bc40c53b564159b5e1534bbfdab8a08`. Verify the license at each source
  revision and confirm which files are designated as Genesis Code before
  release. Until that scope is confirmed, do not claim that this copy of the
  license conclusively covers every derived harness file.
- Apache-2.0 terms are reproduced in [licenses/Apache-2.0.txt](licenses/Apache-2.0.txt).
  The vendored Google protobuf and GoGo files retain their complete BSD notices
  in their source headers. Preserve the applicable notices in redistributed
  source and binary distributions as required by those terms.
- Cargo dependencies retain their own package licenses. The root license does
  not relicense them.

Do not remove notices or edit vendored bytes to update project licensing.
Distribution of mixed or derived material remains subject to the applicable
source licenses; a project-wide license label is not a substitute for them.
