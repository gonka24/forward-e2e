import org.gradle.api.tasks.testing.TestDescriptor
import org.gradle.api.tasks.testing.TestListener
import org.gradle.api.tasks.testing.TestResult
import org.gradle.api.tasks.testing.logging.TestExceptionFormat
import java.nio.file.Files
import java.security.MessageDigest

// Marketplace Testermint harness.
//
// Boundary (see README.md): nothing here is compiled *into* Gonka and no Gonka
// file is copied, patched or shadowed. The upstream Testermint main classes and
// their actual runtime classpath are produced by a separate Gradle run on the
// unmodified selected Gonka checkout (task `a8ExportClasspath` from
// gradle/out-of-tree.init.gradle.kts) and arrive here only as the file named
// by `-Pa8.upstreamClasspathFile`. Upstream's `src/test/kotlin/TestermintTest.kt`
// is the single upstream test-source input; it is copied byte-identically after
// a sha256 check. No other upstream test source is ever compiled.
plugins {
    // Same Kotlin plugin version as upstream testermint/build.gradle.kts, so the
    // harness compiles against upstream classes with the same compiler/stdlib.
    kotlin("jvm") version "2.0.10"
}

group = "io.gonka.forward.e2e"
version = "1.0"

repositories {
    mavenCentral()
}

fun a8Property(name: String): String =
    providers.gradleProperty(name).orNull?.trim()?.takeIf { it.isNotEmpty() }
        ?: throw GradleException(
            "Gradle property $name is required by the Testermint harness " +
                "(pass -P$name=...; see harness/testermint/README.md)"
        )

/**
 * Reads the exported upstream classpath: one absolute path per line, main
 * output directories first, then upstream `runtimeClasspath`. A missing or
 * empty file is an error, never an empty classpath: compiling the scenarios
 * against "whatever happens to be resolvable" would no longer prove they ran
 * against the selected Gonka's Testermint.
 */
fun upstreamClasspathEntries(): List<File> {
    val listFile = File(a8Property("a8.upstreamClasspathFile"))
    if (!listFile.isAbsolute) {
        throw GradleException("a8.upstreamClasspathFile must be an absolute path: $listFile")
    }
    if (!listFile.isFile) {
        throw GradleException("a8.upstreamClasspathFile does not exist or is not a file: $listFile")
    }
    val entries = listFile.readLines(Charsets.UTF_8).map { it.trim() }.filter { it.isNotEmpty() }
    if (entries.isEmpty()) {
        throw GradleException("a8.upstreamClasspathFile is empty: $listFile")
    }
    return entries.map { line ->
        val entry = File(line)
        if (!entry.isAbsolute) {
            throw GradleException("upstream classpath entry is not absolute ($listFile): $line")
        }
        if (!entry.exists()) {
            throw GradleException("upstream classpath entry does not exist ($listFile): $line")
        }
        entry
    }
}

dependencies {
    // Upstream Testermint main classes + upstream's resolved runtimeClasspath.
    // Deliberately *no* coordinates for upstream's own libraries (gson, fuel,
    // docker-java, tinylog, jackson, ...): redeclaring them could resolve a
    // different version than the one upstream was compiled and tested with.
    // Resolved lazily so `help`/`tasks` work without the property.
    testImplementation(files(provider { upstreamClasspathEntries() }))

    // Upstream's own testImplementation set (not part of its main
    // runtimeClasspath), at upstream's versions.
    testImplementation(kotlin("test"))
    testImplementation("org.assertj:assertj-core:3.26.3")
}

kotlin {
    // Same toolchain as upstream testermint.
    jvmToolchain(21)
}

val upstreamTestSrcDir = layout.buildDirectory.dir("upstream-test-src")

/**
 * Fails before compilation if the classpath file does not actually carry the
 * upstream main classes, or carries upstream *test* classes. The latter would
 * let a stale upstream TestermintTest (or any other upstream test class) win
 * over the sha256-checked copy compiled here.
 */
val a8VerifyUpstreamClasspath by tasks.registering {
    group = "verification"
    description = "Checks the exported upstream Testermint classpath before compiling the harness."
    doLast {
        val entries = upstreamClasspathEntries()
        val directories = entries.filter { it.isDirectory }
        if (directories.none { File(it, "com/productscience/LocalInferencePair.class").isFile }) {
            throw GradleException(
                "no upstream classpath directory contains com/productscience/LocalInferencePair.class; " +
                    "the classpath file must list upstream testermint main output first"
            )
        }
        val withUpstreamTests = directories.filter {
            File(it, "TestermintTest.class").exists() || File(it, "LogTestWatcher.class").exists()
        }
        if (withUpstreamTests.isNotEmpty()) {
            throw GradleException(
                "upstream test classes must never be on the harness classpath: $withUpstreamTests"
            )
        }
    }
}

/**
 * Copies upstream `testermint/src/test/kotlin/TestermintTest.kt` byte-for-byte
 * into the build directory after verifying its sha256. The scenarios extend
 * `TestermintTest`, so they need upstream's exact lifecycle/logging base class,
 * but the harness must not compile upstream's test tree to get it.
 */
val a8CopyUpstreamTestermintTest by tasks.registering {
    group = "build"
    description = "Copies the sha256-pinned upstream TestermintTest.kt into the harness test sources."
    outputs.dir(upstreamTestSrcDir)
    outputs.upToDateWhen { false }
    doLast {
        val source = File(a8Property("a8.upstreamTestermintTest"))
        val expected = a8Property("a8.upstreamTestermintTestSha256").lowercase()
        if (!Regex("[0-9a-f]{64}").matches(expected)) {
            throw GradleException("a8.upstreamTestermintTestSha256 must be 64 hex characters: $expected")
        }
        if (!source.isAbsolute) {
            throw GradleException("a8.upstreamTestermintTest must be an absolute path: $source")
        }
        if (source.name != "TestermintTest.kt") {
            throw GradleException("a8.upstreamTestermintTest must name TestermintTest.kt: $source")
        }
        if (Files.isSymbolicLink(source.toPath()) || !source.isFile) {
            throw GradleException("a8.upstreamTestermintTest must be a regular file: $source")
        }
        val bytes = source.readBytes()
        val actual = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
        if (actual != expected) {
            throw GradleException(
                "upstream TestermintTest.kt sha256 mismatch: expected $expected, got $actual ($source)"
            )
        }
        val targetDir = upstreamTestSrcDir.get().asFile
        targetDir.deleteRecursively()
        if (!targetDir.mkdirs()) {
            throw GradleException("cannot create $targetDir")
        }
        File(targetDir, "TestermintTest.kt").writeBytes(bytes)
    }
}

kotlin {
    sourceSets.named("test") {
        kotlin.srcDir(upstreamTestSrcDir)
    }
}

tasks.named("compileTestKotlin") {
    dependsOn(a8CopyUpstreamTestermintTest, a8VerifyUpstreamClasspath)
}

tasks.withType<JavaExec>().configureEach {
    systemProperty("java.net.preferIPv6Addresses", "true")
}

tasks.test {
    // A selected scenario that does not exist must fail the run, never pass
    // as "0 tests". Upstream sets this to false for its matrix tooling.
    filter {
        isFailOnNoMatchingTests = true
    }

    outputs.upToDateWhen { false }
    // Same tag handling as upstream testermint/build.gradle.kts.
    useJUnitPlatform {
        val includeTags = System.getProperty("includeTags")?.trim()
        val excludeTags = System.getProperty("excludeTags")?.trim()
        if (!includeTags.isNullOrEmpty()) {
            val tags = includeTags.split(",").map { it.trim() }.filter { it.isNotEmpty() }
            if (tags.isNotEmpty()) {
                includeTags(*tags.toTypedArray())
            }
        }
        if (!excludeTags.isNullOrEmpty()) {
            val tags = excludeTags.split(",").map { it.trim() }.filter { it.isNotEmpty() }
            if (tags.isNotEmpty()) {
                excludeTags(*tags.toTypedArray())
            }
        }
    }
    systemProperty("java.net.preferIPv6Addresses", "true")

    // Upstream Testermint writes relative paths (tinylog `logs/`, `reboot.txt`).
    // Run the JVM inside the build directory so none of that lands in the
    // runner tree or in a source snapshot.
    val testWorkDir = layout.buildDirectory.dir("test-work")
    workingDir(testWorkDir)
    doFirst {
        testWorkDir.get().asFile.mkdirs()
    }

    // The Test JVM inherits the build's environment; forward the harness
    // contract explicitly as well so a daemon started earlier cannot leave a
    // stale value (the runner also uses --no-daemon).
    environment(
        System.getenv().filterKeys { it.startsWith("E2E_") || it.startsWith("A8_") || it == "GONKA_REPO_ROOT" }
    )

    reports {
        junitXml.required.set(true)
        html.required.set(true)
    }
    testLogging {
        events("started", "passed", "skipped", "failed")
        exceptionFormat = TestExceptionFormat.FULL
        showStandardStreams = true
    }

    // Tag filtering can also select zero tests without tripping
    // failOnNoMatchingTests. Absence is never a pass.
    var executedTests = 0L
    addTestListener(object : TestListener {
        override fun beforeSuite(suite: TestDescriptor) {}
        override fun beforeTest(testDescriptor: TestDescriptor) {}
        override fun afterTest(testDescriptor: TestDescriptor, result: TestResult) {}
        override fun afterSuite(suite: TestDescriptor, result: TestResult) {
            if (suite.parent == null) executedTests = result.testCount
        }
    })
    doLast {
        if (executedTests == 0L) {
            throw GradleException("Testermint harness executed no tests; the selected test is missing")
        }
    }
}
