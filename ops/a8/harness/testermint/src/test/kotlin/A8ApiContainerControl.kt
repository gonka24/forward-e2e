import com.productscience.LocalInferencePair
import com.productscience.SERVER_TYPE_ADMIN
import com.productscience.logSection
import java.io.File
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * Stops and restarts a pair's dAPI container through the runner-owned
 * controller (`ops/a8/harness/container_control.py`) instead of through
 * Testermint.
 *
 * Why this is not `LocalInferencePair.stopApiContainer()/restartApiContainer()`:
 * upstream `restartApiContainer()` resolves the container through
 * `getRawContainers()`, which lists *running* containers only, so it cannot
 * find an API it has just stopped. The old runner patched that inside Gonka's
 * `LocalInferencePair.kt`; the immutable-source runner must not change any
 * Gonka file, so the stop/start pair moves out of the JVM entirely.
 *
 * The controller is also stricter than the old code: `stop` refuses a
 * container that does not carry this run's ownership label and records the
 * container ID in a state file *before* stopping it; `start` then starts the
 * container with exactly that saved ID (never a name lookup), so a different
 * container that later reuses the `<pair>-api` name can never be started by
 * mistake. The state files are JSON evidence under `container-control/`.
 */
internal class A8StoppedApiContainer internal constructor(
    val pair: LocalInferencePair,
    val containerName: String,
    val stateFile: File,
) {
    @Volatile
    private var started = false

    /**
     * Starts the very container [LocalInferencePair.a8StopApiContainer]
     * stopped (by its saved ID) and waits until its admin API answers.
     */
    fun startSameContainer() {
        check(!started) { "A8 API container $containerName was already restarted from $stateFile" }
        check(stateFile.isFile) {
            "A8 container-control state file for $containerName is missing: $stateFile"
        }
        val readyUrl = a8ApiReadyUrl(pair)
        logSection("A8 container-control: start $containerName (state ${stateFile.name})")
        runA8ContainerControl(
            phase = "container-control start $containerName",
            timeoutSeconds = A8_START_PROCESS_TIMEOUT_SECONDS,
            "start",
            "--state-file", stateFile.absolutePath,
            "--run-id", a8RequiredEnv("A8_RUN_ID"),
            "--ready-url", readyUrl,
            "--ready-timeout-seconds", A8_READY_TIMEOUT_SECONDS.toString(),
        )
        started = true
        // The controller already proved HTTP 2xx on the admin config endpoint.
        // Read it once more through Testermint's own client so the scenario
        // continues only when the same call path it uses afterwards works.
        a8AwaitApiConfig(pair)
    }
}

/**
 * Stops this pair's `<pair>-api` container through the external controller and
 * returns the handle that [A8StoppedApiContainer.startSameContainer] must use.
 * Scenarios that never restart simply drop the handle; the container is torn
 * down with the rest of the run by the runner's ownership cleanup.
 */
internal fun LocalInferencePair.a8StopApiContainer(): A8StoppedApiContainer {
    // Same derivation as upstream TestermintContainers.getApi(): "$name-api"
    // with Docker's leading '/' removed.
    val containerName = "${name.trimStart('/')}-api"
    val stateDir = File(a8RequiredEnv("A8_CONTAINER_CONTROL_STATE_DIR"))
    check(stateDir.isDirectory || stateDir.mkdirs()) {
        "A8_CONTAINER_CONTROL_STATE_DIR cannot be created: $stateDir"
    }
    val sequence = A8_CONTAINER_CONTROL_SEQUENCE.incrementAndGet()
    val stateFile = File(stateDir, "$containerName-$sequence.json")
    // Evidence is append-only: an existing state file belongs to an earlier
    // stop and must not be overwritten by this one.
    check(!stateFile.exists()) { "A8 container-control state file already exists: $stateFile" }
    logSection("A8 container-control: stop $containerName (state ${stateFile.name})")
    runA8ContainerControl(
        phase = "container-control stop $containerName",
        timeoutSeconds = A8_STOP_PROCESS_TIMEOUT_SECONDS,
        "stop",
        "--container-name", containerName,
        "--run-id", a8RequiredEnv("A8_RUN_ID"),
        "--state-file", stateFile.absolutePath,
    )
    check(stateFile.isFile) {
        "A8 container-control stop of $containerName succeeded without writing $stateFile"
    }
    return A8StoppedApiContainer(this, containerName, stateFile)
}

/**
 * Admin endpoint used for readiness. `api.urls[admin]` is the host-mapped
 * 9200 port of the API container itself (fixed `ADMIN_SERVER_PORT` mapping in
 * upstream `docker-compose-base.yml`), unlike the public URL, which goes through
 * the per-pair proxy that stays up and answers 502 while the API is down.
 */
private fun a8ApiReadyUrl(pair: LocalInferencePair): String {
    val adminUrl = pair.api.urls[SERVER_TYPE_ADMIN]
        ?: error("Testermint pair ${pair.name} exposes no $SERVER_TYPE_ADMIN URL for API readiness")
    return adminUrl.trimEnd('/') + "/admin/v1/config"
}

private fun a8AwaitApiConfig(pair: LocalInferencePair) {
    var lastError: Throwable? = null
    repeat(A8_CONFIG_READY_ATTEMPTS) { attempt ->
        try {
            pair.api.getConfig()
            return
        } catch (error: Exception) {
            lastError = error
            if (attempt + 1 < A8_CONFIG_READY_ATTEMPTS) Thread.sleep(A8_CONFIG_READY_SLEEP_MILLIS)
        }
    }
    throw IllegalStateException(
        "A8 API container for ${pair.name} started but Testermint getConfig() did not answer " +
            "after $A8_CONFIG_READY_ATTEMPTS attempts",
        lastError,
    )
}

private fun runA8ContainerControl(phase: String, timeoutSeconds: Long, vararg args: String) {
    val command = listOf(a8RequiredEnv("A8_PYTHON"), a8RequiredEnv("A8_CONTAINER_CONTROL")) + args
    val stateDir = File(a8RequiredEnv("A8_CONTAINER_CONTROL_STATE_DIR"))
    // A non-zero exit (refusal: not owned, ID mismatch, not ready, ...) fails
    // the scenario with the controller's JSON error line in the message.
    val output = runMarketplaceHarnessProcess(
        command = command,
        directory = stateDir,
        phase = phase,
        timeout = timeoutSeconds,
        timeoutUnit = TimeUnit.SECONDS,
    )
    println(output.trim())
}

private fun a8RequiredEnv(name: String): String =
    System.getenv(name)?.takeIf { it.isNotBlank() }
        ?: error("Required environment variable $name is missing")

private val A8_CONTAINER_CONTROL_SEQUENCE = AtomicInteger(0)

// `docker stop` has a 10 s grace period; the rest is inspect + state write.
private const val A8_STOP_PROCESS_TIMEOUT_SECONDS = 180L

// Controller readiness budget plus headroom for `docker start` and inspect,
// so the controller reports CONTAINER_NOT_READY before this JVM kills it.
private const val A8_READY_TIMEOUT_SECONDS = 180L
private const val A8_START_PROCESS_TIMEOUT_SECONDS = A8_READY_TIMEOUT_SECONDS + 120L

private const val A8_CONFIG_READY_ATTEMPTS = 15
private const val A8_CONFIG_READY_SLEEP_MILLIS = 2_000L
