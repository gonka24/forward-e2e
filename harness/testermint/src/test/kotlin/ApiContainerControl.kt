import com.productscience.LocalInferencePair
import com.productscience.SERVER_TYPE_ADMIN
import com.productscience.logSection
import java.io.File
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * Stops and restarts a pair's dAPI container through the runner-owned
 * controller (`harness/container_control.py`) instead of through
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
internal class StoppedApiContainer internal constructor(
    val pair: LocalInferencePair,
    val containerName: String,
    val stateFile: File,
) {
    @Volatile
    private var started = false

    /**
     * Starts the very container [LocalInferencePair.stopApiContainer]
     * stopped (by its saved ID) and waits until its admin API answers.
     */
    fun startSameContainer() {
        check(!started) { "API container $containerName was already restarted from $stateFile" }
        check(stateFile.isFile) {
            "container-control state file for $containerName is missing: $stateFile"
        }
        val readyUrl = apiReadyUrl(pair)
        logSection("container-control: start $containerName (state ${stateFile.name})")
        runContainerControl(
            phase = "container-control start $containerName",
            timeoutSeconds = START_PROCESS_TIMEOUT_SECONDS,
            "start",
            "--state-file", stateFile.absolutePath,
            "--run-id", requiredHarnessEnv("E2E_RUN_ID", "A8_RUN_ID"),
            "--ready-url", readyUrl,
            "--ready-timeout-seconds", READY_TIMEOUT_SECONDS.toString(),
        )
        started = true
        // The controller already proved HTTP 2xx on the admin config endpoint.
        // Read it once more through Testermint's own client so the scenario
        // continues only when the same call path it uses afterwards works.
        awaitApiConfig(pair)
    }
}

/**
 * Stops this pair's `<pair>-api` container through the external controller and
 * returns the handle that [StoppedApiContainer.startSameContainer] must use.
 * Scenarios that never restart simply drop the handle; the container is torn
 * down with the rest of the run by the runner's ownership cleanup.
 */
internal fun LocalInferencePair.stopApiContainer(): StoppedApiContainer {
    // Same derivation as upstream TestermintContainers.getApi(): "$name-api"
    // with Docker's leading '/' removed.
    val containerName = "${name.trimStart('/')}-api"
    val stateDir = File(requiredHarnessEnv("E2E_CONTAINER_CONTROL_STATE_DIR", "A8_CONTAINER_CONTROL_STATE_DIR"))
    check(stateDir.isDirectory || stateDir.mkdirs()) {
        "E2E_CONTAINER_CONTROL_STATE_DIR cannot be created: $stateDir"
    }
    val sequence = CONTAINER_CONTROL_SEQUENCE.incrementAndGet()
    val stateFile = File(stateDir, "$containerName-$sequence.json")
    // Evidence is append-only: an existing state file belongs to an earlier
    // stop and must not be overwritten by this one.
    check(!stateFile.exists()) { "container-control state file already exists: $stateFile" }
    logSection("container-control: stop $containerName (state ${stateFile.name})")
    runContainerControl(
        phase = "container-control stop $containerName",
        timeoutSeconds = STOP_PROCESS_TIMEOUT_SECONDS,
        "stop",
        "--container-name", containerName,
        "--run-id", requiredHarnessEnv("E2E_RUN_ID", "A8_RUN_ID"),
        "--state-file", stateFile.absolutePath,
    )
    check(stateFile.isFile) {
        "container-control stop of $containerName succeeded without writing $stateFile"
    }
    return StoppedApiContainer(this, containerName, stateFile)
}

/**
 * Admin endpoint used for readiness. `api.urls[admin]` is the host-mapped
 * 9200 port of the API container itself (fixed `ADMIN_SERVER_PORT` mapping in
 * upstream `docker-compose-base.yml`), unlike the public URL, which goes through
 * the per-pair proxy that stays up and answers 502 while the API is down.
 */
private fun apiReadyUrl(pair: LocalInferencePair): String {
    val adminUrl = pair.api.urls[SERVER_TYPE_ADMIN]
        ?: error("Testermint pair ${pair.name} exposes no $SERVER_TYPE_ADMIN URL for API readiness")
    return adminUrl.trimEnd('/') + "/admin/v1/config"
}

private fun awaitApiConfig(pair: LocalInferencePair) {
    var lastError: Throwable? = null
    repeat(CONFIG_READY_ATTEMPTS) { attempt ->
        try {
            pair.api.getConfig()
            return
        } catch (error: Exception) {
            lastError = error
            if (attempt + 1 < CONFIG_READY_ATTEMPTS) Thread.sleep(CONFIG_READY_SLEEP_MILLIS)
        }
    }
    throw IllegalStateException(
        "API container for ${pair.name} started but Testermint getConfig() did not answer " +
            "after $CONFIG_READY_ATTEMPTS attempts",
        lastError,
    )
}

private fun runContainerControl(phase: String, timeoutSeconds: Long, vararg args: String) {
    val command = listOf(
        requiredHarnessEnv("E2E_PYTHON", "A8_PYTHON"),
        requiredHarnessEnv("E2E_CONTAINER_CONTROL", "A8_CONTAINER_CONTROL"),
    ) + args
    val stateDir = File(requiredHarnessEnv("E2E_CONTAINER_CONTROL_STATE_DIR", "A8_CONTAINER_CONTROL_STATE_DIR"))
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

internal fun requiredHarnessEnv(canonicalName: String, legacyName: String): String {
    val canonicalValue = System.getenv(canonicalName)?.takeIf { it.isNotBlank() }
    val legacyValue = System.getenv(legacyName)?.takeIf { it.isNotBlank() }
    if (canonicalValue != null && legacyValue != null && canonicalValue != legacyValue) {
        error(
            "Conflicting environment variables $canonicalName and $legacyName: " +
                "$canonicalName=$canonicalValue != $legacyName=$legacyValue",
        )
    }
    return canonicalValue ?: legacyValue
        ?: error("Required environment variable $canonicalName (or legacy alias $legacyName) is missing")
}

private val CONTAINER_CONTROL_SEQUENCE = AtomicInteger(0)

// `docker stop` has a 10 s grace period; the rest is inspect + state write.
private const val STOP_PROCESS_TIMEOUT_SECONDS = 180L

// Controller readiness budget plus headroom for `docker start` and inspect,
// so the controller reports CONTAINER_NOT_READY before this JVM kills it.
private const val READY_TIMEOUT_SECONDS = 180L
private const val START_PROCESS_TIMEOUT_SECONDS = READY_TIMEOUT_SECONDS + 120L

private const val CONFIG_READY_ATTEMPTS = 15
private const val CONFIG_READY_SLEEP_MILLIS = 2_000L
