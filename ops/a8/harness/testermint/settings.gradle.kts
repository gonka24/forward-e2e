// Marketplace A8 Testermint harness: a standalone Gradle build that lives in
// the *runner* tree, never in the Gonka checkout under test. The upstream
// Testermint main classes it tests against are compiled from the unmodified
// selected Gonka commit by a separate Gradle invocation and handed over as a
// classpath file (see README.md and gradle/a8-out-of-tree.init.gradle.kts).
plugins {
    // Same toolchain resolver and version as upstream testermint/settings.gradle.kts,
    // so `jvmToolchain(21)` resolves identically in both builds.
    id("org.gradle.toolchains.foojay-resolver-convention") version "0.8.0"
}

rootProject.name = "a8-marketplace-testermint-harness"
