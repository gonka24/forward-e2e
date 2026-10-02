import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assumptions.assumeFalse
import org.junit.jupiter.api.Test
import java.io.File
import java.util.concurrent.TimeUnit
import kotlin.system.measureTimeMillis

class MarketplaceHarnessProcessTests {
    @Test
    fun `timeout kills a process whose output remains open`() {
        val process = hangingProcess()

        val elapsedMillis = measureTimeMillis {
            val error = assertThrows(IllegalStateException::class.java) {
                waitForMarketplaceHarnessProcess(
                    process = process,
                    phase = "hanging-regression",
                    timeout = 200,
                    timeoutUnit = TimeUnit.MILLISECONDS,
                )
            }
            assertTrue(error.message.orEmpty().contains("timed out: hanging-regression"))
        }

        assertFalse(process.isAlive, "timed-out process must be dead before the runner returns")
        assertTrue(elapsedMillis < 5_000, "timeout path took ${elapsedMillis}ms")
    }

    @Test
    fun `timeout kills descendants that inherit the output pipe`() {
        assumeFalse(System.getProperty("os.name").startsWith("Windows", ignoreCase = true))
        val childPidFile = File.createTempFile("a8-harness-child-", ".pid").apply {
            deleteOnExit()
            delete()
        }
        val process = ProcessBuilder(
            "/bin/sh",
            "-c",
            "sleep 30 & echo \$! > \$A8_TEST_CHILD_PID_FILE; wait",
        ).apply {
            environment()["A8_TEST_CHILD_PID_FILE"] = childPidFile.absolutePath
        }.redirectErrorStream(true).start()

        val pidDeadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2)
        while (!childPidFile.isFile && System.nanoTime() < pidDeadline) Thread.sleep(10)
        assertTrue(childPidFile.isFile, "child process did not record its PID")
        val childPid = childPidFile.readText().trim().toLong()
        val child = ProcessHandle.of(childPid).orElseThrow()

        val error = assertThrows(IllegalStateException::class.java) {
            waitForMarketplaceHarnessProcess(
                process = process,
                phase = "descendant-timeout-regression",
                timeout = 200,
                timeoutUnit = TimeUnit.MILLISECONDS,
            )
        }

        assertTrue(error.message.orEmpty().contains("timed out: descendant-timeout-regression"))
        assertFalse(process.isAlive, "timed-out parent must be dead")
        assertFalse(child.isAlive, "timed-out descendant must be dead")
    }

    private fun hangingProcess(): Process {
        val windows = System.getProperty("os.name").startsWith("Windows", ignoreCase = true)
        val command = if (windows) {
            listOf(
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Write-Output started; Start-Sleep -Seconds 30",
            )
        } else {
            listOf("/bin/sh", "-c", "printf started; exec sleep 30")
        }
        return ProcessBuilder(command).redirectErrorStream(true).start()
    }
}
