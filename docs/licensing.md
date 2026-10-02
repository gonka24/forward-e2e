# Licensing and version dates

The [root LICENSE](../LICENSE) applies BUSL-1.1 to original Gonka24 material,
with Mikita Anikiyevich as Licensor. The Gonka24 authors are listed in the
[README](../README.md#authors); this attribution does not transfer copyright or
establish authority to license another author's contributions. The standard Terms
and Covenants are preserved; the parameters specify a one-calendar-year period
for each version and Apache-2.0 as its Change License.

The source is available for inspection, modification, redistribution and
non-production use under BUSL. The Additional Use Grant also permits commercial
interaction with official deployments, integrations, and Deal instances created
through a Factory deployed by the Licensor. It does not authorize operating a
separate production Marketplace before its Change Date without a separate
license. See the full LICENSE for the controlling terms.

## Version 0.1.0 publication dates

| Version | First public distribution (UTC date) | Change Date | Change License |
| --- | --- | --- | --- |
| 0.1.0 | 2026-09-11 | 2027-09-11 | Apache-2.0 |

These dates are recorded in the [LICENSE](../LICENSE) parameters for version
0.1.0. Later versions receive their own dates; they do not extend this version's
period. This date record is not a build manifest or evidence of deployment.
Exact public source and artifact hashes belong in the release records below;
historical local evidence is not rebound to the new public revision.

## Publication procedure

The Change Date is determined by the first public distribution of each version,
including public development snapshots. A private PR, private test or private
build does not start that period. Do not describe this repository as open source before the applicable
Change Date; use source-available.

For every public release, publish a record alongside its artifacts containing:

- version and exact source commit;
- first public distribution date in UTC, including an earlier public development
  distribution of the same revision where applicable;
- the explicit Change Date in YYYY-MM-DD, one calendar year later;
- hashes of the distributed LICENSE and release artifacts.

Maintain records for public development revisions as well as release tags.
Publish the records in release metadata or a later registry commit, avoiding a
self-referential source hash. Preserve old tags, artifacts and records. A new
release, renaming, or republication must not extend the date of already
distributed material. Later changes have their own dates; earlier versions
remain available under Apache-2.0 after their dates. A downstream copy or
modification does not restart the period for the original material.

For example, a version first made public on 2026-10-01 changes on 2027-10-01;
a different version first made public on 2026-12-15 changes on 2027-12-15.
These are examples, not actual publication dates. A February 29 publication
changes on February 28 of the following year.

Official deployment records should identify the chain ID, Factory address and
code checksums so users can identify deployments covered by the Additional Use
Grant. Normal use of the Factory to create a Deal is expressly permitted.

## Scope and previous permissions

[Third-party materials](../THIRD_PARTY.md) retain their original terms, including
those applicable to generated bindings. Root Cargo metadata describes original
code; the mixed gonka-proto package points to the scoped license file instead
of asserting that every vendored/generated file is BUSL.

This change does not revoke any Apache-2.0 or other permissions previously
granted. Private history cleanup does not withdraw licenses already received.
Contributions can only be included under terms that their rights holders permit;
listing the Licensor does not transfer another contributor's copyright.

The Additional Use Grant is project-specific legal text. Obtain legal review of
its scope and the date mechanism before public release. The date records above
are a maintainer procedure, not an automated gate in the build tooling.

Sources: [BUSL-1.1](https://spdx.org/licenses/BUSL-1.1.html),
[licensor guidance](https://mariadb.com/bsl-faq-adopting/).
