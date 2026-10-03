import com.productscience.EpochStage
import com.productscience.LocalInferencePair
import com.productscience.logSection

/*
 * Test-support helpers that upstream Testermint keeps under src/test/kotlin
 * rather than in its main source set.
 *
 * The external harness compiles exactly one upstream test-source file
 * (TestermintTest.kt, copied byte-identically and sha256-checked). Pulling in
 * any further upstream test file would drag the whole upstream test suite's
 * helper graph into the Marketplace build, so the few helpers the scenarios
 * call are reproduced here verbatim instead. Everything they call is upstream
 * *main* API, which required-upstream-api.json pins and the runner checks
 * before any network is created.
 */

/**
 * Verbatim copy of `LocalInferencePair.ensureGenesisSpendableForDevshard` from
 * Gonka c33c9eaa5bc40c53b564159b5e1534bbfdab8a08,
 * `testermint/src/test/kotlin/DevshardTestSupport.kt` lines 340-364.
 *
 * Genesis cold wallet starts with little liquid ngonka; rewards arrive at [EpochStage.CLAIM_REWARDS].
 * Upstream's `waitForNextInferenceWindow` often skips that stage mid-epoch, so fund users only after
 * balance is sufficient.
 */
fun LocalInferencePair.ensureGenesisSpendableForDevshard(
    minBalance: Long,
    maxAttempts: Int = 4,
) {
    repeat(maxAttempts) { attempt ->
        val balance = node.getSelfBalance(config.denom)
        if (balance >= minBalance) {
            return
        }
        logSection(
            "Genesis balance $balance < $minBalance; waiting for CLAIM_REWARDS " +
                "(attempt ${attempt + 1}/$maxAttempts)",
        )
        waitForStage(EpochStage.CLAIM_REWARDS)
        Thread.sleep(2_000)
    }
    val balance = node.getSelfBalance(config.denom)
    check(balance >= minBalance) {
        "Genesis cold account needs at least $minBalance${config.denom} for bank send; balance=$balance"
    }
}
