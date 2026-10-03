# External Testermint Harness (`harness/testermint`)

A standalone Gradle project holding the Marketplace acceptance scenarios
(`MarketplaceContractAcceptanceTests`) and their helpers. It runs against the
**unmodified** Testermint of the selected Gonka commit.

## The boundary

Nothing is compiled into Gonka, and no Gonka file is copied over, patched or
shadowed:

| Input | Where it comes from | How it is pinned |
|---|---|---|
| Scenario code (`src/test/kotlin/*.kt`) | this directory (runner image) | hashed by the runner (`external-harness/harness-inputs.json` and `lock.external_tests`) |
| Upstream Testermint main classes + runtime jars | a separate Gradle run on `<gonka>/testermint` | `-Pa8.upstreamClasspathFile` (exported list, copied into evidence) |
| Upstream `TestermintTest.kt` (the scenarios' base class) | `<gonka>/testermint/src/test/kotlin/TestermintTest.kt`, read only | `-Pa8.upstreamTestermintTest` + `-Pa8.upstreamTestermintTestSha256`; copied byte-identically into `<buildDir>/upstream-test-src/` |
| Required upstream API | [`required-upstream-api.json`](required-upstream-api.json) | checked by the runner against the upstream sources **before** any network is created |

No other upstream test source is compiled. The only upstream test-source
helper the scenarios call, `ensureGenesisSpendableForDevshard`, is reproduced
verbatim in [`src/test/kotlin/UpstreamTestSupport.kt`](src/test/kotlin/UpstreamTestSupport.kt)
with its upstream origin. `a8VerifyUpstreamClasspath` fails the build if the
classpath does not contain the upstream main classes, or if it contains upstream
test classes.

API stop/start goes through [`src/test/kotlin/ApiContainerControl.kt`](src/test/kotlin/ApiContainerControl.kt),
which calls [`harness/container_control.py`](../container_control.py). The `stop`
command checks the run's ownership label (`io.gonka.a8.run-id`) and saves the
container ID before it stops anything. The `start` command restarts that same
container by ID and waits for `admin/v1/config`.

The network state lives under `GONKA_REPO_ROOT=<W>/network-root`, never in the
source snapshot. The extra Compose files (`ownership.yml`, `nats.yml`, and
`foreign-native-genesis.yml` for `foreign-native-preservation` /
`b3-foreign-native` only) are copied from [`harness/network/`](../network/) into
`<W>/network-root/local-test-net/`.

## Builds the runner performs

Both builds use the selected Gonka's own Gradle through its wrapper jar:

```text
java -cp <gonka>/testermint/gradle/wrapper/gradle-wrapper.jar \
     org.gradle.wrapper.GradleWrapperMain <args>
```

Common arguments for every build:

```text
--no-daemon -I harness/testermint/gradle/out-of-tree.init.gradle.kts
-Pa8.outRoot=<W>/...  --project-cache-dir <W>/gradle-project-cache/<build>
-Pkotlin.project.persistent.dir=<W>/kotlin/<build>   (GRADLE_USER_HOME=<W>/gradle-home)
```

1. **Upstream classpath export:** `--project-dir <gonka>/testermint
   -Pa8.outRoot=<W>/upstream-build -Pa8.classpathFile=<W>/testermint-classpath.txt a8ExportClasspath`.
2. **Harness test:** `--project-dir <runner>/harness/testermint
   -Pa8.outRoot=<W>/harness-build -Pa8.upstreamClasspathFile=<W>/testermint-classpath.txt
   -Pa8.upstreamTestermintTest=<gonka>/testermint/src/test/kotlin/TestermintTest.kt
   -Pa8.upstreamTestermintTestSha256=<sha256> test --tests "<Class.method>" -DexcludeTags=unstable,exclude`.

## Environment read by the Kotlin code

The Kotlin process accepts `E2E_*` variables as primary and `A8_*` as fallback
(`E2E_PYTHON` / `A8_PYTHON`, `E2E_HARNESS` / `A8_HARNESS`,
`E2E_MARKETPLACE_DIR` / `A8_MARKETPLACE_DIR`, `E2E_CONTEXT` / `A8_CONTEXT`,
`E2E_RUN_ID` / `A8_RUN_ID`, `E2E_DEAL_WASM` / `A8_DEAL_WASM`,
`E2E_FACTORY_WASM` / `A8_FACTORY_WASM`, `E2E_CW20_WASM` / `A8_CW20_WASM`,
`E2E_CALLER_WASM` / `A8_CALLER_WASM`,
`E2E_CONTAINER_CONTROL` / `A8_CONTAINER_CONTROL`,
`E2E_CONTAINER_CONTROL_STATE_DIR` / `A8_CONTAINER_CONTROL_STATE_DIR`).
Upstream Testermint reads `GONKA_REPO_ROOT`. See
[`docs/migration.md`](../../docs/migration.md) for the complete environment variable table.
