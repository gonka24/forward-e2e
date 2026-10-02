# A8 Marketplace Testermint harness

A standalone Gradle project holding the Marketplace acceptance scenarios
(`MarketplaceContractAcceptanceTests`) and their helpers. It runs against the
**unmodified** Testermint of the selected Gonka commit.

## The boundary

Nothing is compiled into Gonka, and no Gonka file is copied over, patched or
shadowed. The old runner did all three: it wrote the scenarios into
`<gonka>/testermint/src/test/kotlin`, replaced `DockerGroup.kt` and
`LocalInferencePair.kt`, and created `gradlew.a8-linux` in the checkout. That
turned the selected SHA into a "prepared" tree, so the result no longer proved
anything about the SHA itself. Now:

| Input | Where it comes from | How it is pinned |
|---|---|---|
| Scenario code (`src/test/kotlin/*.kt`) | this directory (runner image) | hashed by the runner (`external-harness/harness-inputs.json`) |
| Upstream Testermint main classes + runtime jars | a separate Gradle run on `<gonka>/testermint` | `-Pa8.upstreamClasspathFile` (exported list, copied into evidence) |
| Upstream `TestermintTest.kt` (the scenarios' base class) | `<gonka>/testermint/src/test/kotlin/TestermintTest.kt`, read only | `-Pa8.upstreamTestermintTest` + `-Pa8.upstreamTestermintTestSha256`; copied byte-identically into `<buildDir>/upstream-test-src/` |
| Required upstream API | [`required-upstream-api.json`](required-upstream-api.json) | checked by the runner against the upstream sources **before** any network is created |

No other upstream test source is compiled. The only upstream test-source
helper the scenarios call, `ensureGenesisSpendableForDevshard`, is reproduced
verbatim in `A8UpstreamTestSupport.kt` with its upstream origin.
`a8VerifyUpstreamClasspath` fails the build if the classpath does not contain
the upstream main classes, or if it contains upstream test classes.

API stop/start does not go through Testermint any more.
`A8ApiContainerControl.kt` calls the runner's `container_control.py`. The
`stop` command checks the run's ownership label and saves the container ID
before it stops anything. The `start` command restarts that same container by
ID and waits for `admin/v1/config`. Upstream `restartApiContainer()` can't be
used because it only lists running containers.

The network state lives under `GONKA_REPO_ROOT=<W>/network-root`, never in the
snapshot. The upstream `getRepoRoot()` honours that variable. The extra
Compose files `a8-ownership.yml`, `a8-nats.yml` and `a8-b3-genesis.yml` (B3
only) are runner-generated under `<W>/network-root/local-test-net/`.

## Builds the runner performs

Both builds use the selected Gonka's own Gradle through its wrapper jar. No
wrapper script is copied, so no `gradlew.a8-linux` appears:

```
java -cp <gonka>/testermint/gradle/wrapper/gradle-wrapper.jar \
     org.gradle.wrapper.GradleWrapperMain <args>
```

`GradleWrapperMain` reads `gradle-wrapper.properties` next to the jar, so
both builds run on the Gradle version that Gonka pins.

Common arguments for every build:

```
--no-daemon -I ops/a8/harness/testermint/gradle/a8-out-of-tree.init.gradle.kts
-Pa8.outRoot=<W>/...  --project-cache-dir <W>/gradle-project-cache/<build>
-Pkotlin.project.persistent.dir=<W>/kotlin/<build>   (GRADLE_USER_HOME=<W>/gradle-home)
```

1. **Upstream classpath export:** `--project-dir <gonka>/testermint
   -Pa8.outRoot=<W>/upstream-build -Pa8.classpathFile=<W>/testermint-classpath.txt a8ExportClasspath`.
   It writes the main output dirs (classes, then resources) followed by
   `runtimeClasspath`, one absolute path per line. It also writes
   `<classpathFile>.json` with the Gradle, JVM and project version facts.
2. **Harness test:** `--project-dir <runner>/ops/a8/harness/testermint
   -Pa8.outRoot=<W>/harness-build -Pa8.upstreamClasspathFile=<W>/testermint-classpath.txt
   -Pa8.upstreamTestermintTest=<gonka>/testermint/src/test/kotlin/TestermintTest.kt
   -Pa8.upstreamTestermintTestSha256=<sha256> test --tests "<Class.method>" -DexcludeTags=unstable,exclude`.

The init script puts every project's build directory under `a8.outRoot`:

- root project: `<outRoot>/<rootProject.name>`
- subproject: `<outRoot>/<rootProject.name>/<path>`

That gives `<W>/upstream-build/testermint`, `<outRoot>/mock_server` and
`<W>/harness-build/a8-marketplace-testermint-harness`. JUnit XML is written to
`<W>/harness-build/a8-marketplace-testermint-harness/test-results/test/`. The
test JVM runs in `.../a8-marketplace-testermint-harness/test-work/`, because
Testermint writes relative paths such as `logs/` and `reboot.txt`.

The script can't move `.gradle/` or `.kotlin/`, which is why the runner passes
`--project-cache-dir` and `kotlin.project.persistent.dir` itself. If anything
still escapes into a snapshot, the runner's post-run source-snapshot comparison
catches it and fails the run.

A selected test that does not exist fails the run. This is enforced by
`failOnNoMatchingTests = true`, plus a check that at least one test actually
executed.

## Environment read by the Kotlin code

`A8_PYTHON`, `A8_HARNESS`, `A8_MARKETPLACE_DIR`, `A8_CONTEXT`, `A8_RUN_ID`,
`A8_DEAL_WASM`, `A8_FACTORY_WASM`, `A8_CW20_WASM`, `A8_CALLER_WASM`,
`A8_CONTAINER_CONTROL`, `A8_CONTAINER_CONTROL_STATE_DIR`. Upstream Testermint
reads `GONKA_REPO_ROOT`. A missing variable fails with a message naming it.
