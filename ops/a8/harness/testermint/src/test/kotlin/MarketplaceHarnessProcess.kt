import java.io.File
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

internal fun runMarketplaceHarnessProcess(
    command: List<String>,
    directory: File,
    phase: String?,
    timeout: Long,
    timeoutUnit: TimeUnit,
): String {
    val process = ProcessBuilder(command)
        .directory(directory)
        .redirectErrorStream(true)
        .start()

    return waitForMarketplaceHarnessProcess(process, phase, timeout, timeoutUnit)
}

internal fun waitForMarketplaceHarnessProcess(
    process: Process,
    phase: String?,
    timeout: Long,
    timeoutUnit: TimeUnit,
): String {
    val output = StringBuffer()
    val readFailure = AtomicReference<Throwable?>()
    val outputReader = Thread({
        try {
            process.inputStream.bufferedReader().use { reader ->
                val buffer = CharArray(DEFAULT_BUFFER_SIZE)
                while (true) {
                    val count = reader.read(buffer)
                    if (count < 0) break
                    output.append(buffer, 0, count)
                }
            }
        } catch (error: Throwable) {
            readFailure.set(error)
        }
    }, "marketplace-harness-output-${phase ?: "unknown"}").apply {
        isDaemon = true
        start()
    }

    val completed = try {
        process.waitFor(timeout, timeoutUnit)
    } catch (error: InterruptedException) {
        stopProcess(process)
        Thread.currentThread().interrupt()
        throw IllegalStateException("Interrupted while waiting for Marketplace harness: $phase", error)
    }

    if (!completed) {
        stopProcess(process)
        outputReader.join(TimeUnit.SECONDS.toMillis(5))
        check(!outputReader.isAlive) {
            "Marketplace harness output reader did not stop after process termination: $phase"
        }
        throw IllegalStateException("Marketplace harness timed out: $phase\n$output")
    }

    outputReader.join(TimeUnit.SECONDS.toMillis(5))
    check(!outputReader.isAlive) {
        "Marketplace harness output reader did not finish: $phase"
    }
    readFailure.get()?.let { error ->
        throw IllegalStateException("Failed to read Marketplace harness output: $phase", error)
    }
    check(process.exitValue() == 0) {
        "Marketplace harness failed ($phase):\n$output"
    }
    return output.toString()
}

private fun stopProcess(process: Process) {
    // The harness process can itself be waiting on a CLI or another child.
    // Killing only its direct process would leave that work running and may
    // keep the inherited stdout pipe open forever. Snapshot and stop the tree
    // before closing our streams so the output reader can finish as well.
    val descendants = process.toHandle().descendants().toList().asReversed()
    try {
        descendants.forEach { it.destroyForcibly() }
        process.destroyForcibly()
        check(process.waitFor(5, TimeUnit.SECONDS)) {
            "Marketplace harness process did not stop after destroyForcibly()"
        }
        descendants.forEach { descendant ->
            if (descendant.isAlive) {
                runCatching { descendant.onExit().get(5, TimeUnit.SECONDS) }
            }
            check(!descendant.isAlive) {
                "Marketplace harness descendant ${descendant.pid()} did not stop after destroyForcibly()"
            }
        }
    } finally {
        runCatching { process.outputStream.close() }
        runCatching { process.inputStream.close() }
        runCatching { process.errorStream.close() }
    }
}
