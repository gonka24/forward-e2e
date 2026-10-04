import com.google.gson.JsonParser
import com.productscience.EpochStage
import com.productscience.GENESIS_KEY_NAME
import com.productscience.data.Coin
import com.productscience.data.AppState
import com.productscience.data.BitcoinRewardParams
import com.productscience.data.EpochParams
import com.productscience.data.GovParams
import com.productscience.data.GovState
import com.productscience.data.InferenceParams
import com.productscience.data.InferenceState
import com.productscience.data.MsgTransferWithVesting
import com.productscience.data.RestrictionsParams
import com.productscience.data.RestrictionsState
import com.productscience.data.TokenomicsParams
import com.productscience.data.UpdateRestrictionsParams
import com.productscience.data.UnfundedInferenceParticipant
import com.productscience.data.spec
import com.productscience.inferenceConfig
import com.productscience.initCluster
import com.productscience.logSection
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.Timeout
import java.io.File
import java.time.Duration
import java.util.concurrent.TimeUnit

@Timeout(value = 35, unit = TimeUnit.MINUTES)
class MarketplaceContractAcceptanceTests : TestermintTest() {
    @Test
    fun `marketplace preserves refund boundary and releases a new vested gift`() {
        // One cluster is intentional: R1 and R2 get independent Host/E and Deal
        // fixtures, but share a monotonic epoch schedule and one bootstrap.
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3
        // bootstrap owns join1/targetEpoch; R2 deliberately uses the next
        // epoch, so its Factory (Host,E) key cannot collide with bootstrap.
        val r2Epoch = targetEpoch + 1
        val r1HostKey = createInactiveParticipant(genesis, "a8-package-a-r1")

        bootstrap(targetEpoch)
        prepareDeal("r1-refund-e-plus-5", targetEpoch, funded = true, hostNode = "genesis-node", hostKey = r1HostKey)
        prepareDeal("r2-vested-gift", r2Epoch, funded = true, hostNode = "join1-node", hostKey = "join1")
        genesis.markNeedsReboot()

        while (genesis.getEpochData().latestEpoch.index < r2Epoch) genesis.waitForNextEpoch()
        runHarness("lock-scenario", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r2-vested-gift")
        val rewardSeed = cluster.joinPairs.first().api.getConfig().currentSeed
        check(rewardSeed.epochIndex == r2Epoch) { "R2 reward seed epoch must equal its Deal epoch" }
        val r2StoppedApi = cluster.joinPairs.first().stopOwnedApiContainer()
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        runHarness("claim-scenario", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r2-vested-gift", "--reward-seed", rewardSeed.seed.toString(), "--reward-epoch", rewardSeed.epochIndex.toString())
        runHarness("settle-scenario", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r2-vested-gift")
        r2StoppedApi.startSameContainer()

        // Two original native tranches complete R2 before any new gift is made.
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = -1)
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        genesis.node.waitForNextBlock(2)
        runHarness("release-scenario", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r2-vested-gift")
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        genesis.node.waitForNextBlock(2)
        runHarness("release-scenario", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r2-vested-gift")
        runHarness("r2-gift-checkpoint", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r2-vested-gift", "--stage", "pre_gift")

        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 5) genesis.waitForNextEpoch()
        // Isolated case evidence is written before a failed assertion is surfaced.
        val r1 = runCatching {
            runHarness("refund-e-plus-5-scenario", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r1-refund-e-plus-5", "--gas", "2000000")
        }

        val gift = 10_000_000_001L
        runHarness("snapshot-vesting-scenario", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r2-vested-gift", "--label", "before-gift")
        val governanceAddress = genesis.node.getModuleAccount("gov").account.value.address
        val genesisAddress = genesis.node.getColdAddress()
        genesis.ensureGenesisSpendableForDevshard(gift)
        val fundingTx = genesis.submitTransaction(listOf("bank", "send", genesisAddress, governanceAddress, "$gift${genesis.config.denom}"))
        check(fundingTx.code == 0) { "R2 governance funding failed: ${fundingTx.rawLog}" }
        val proposalId = genesis.runProposal(cluster, MsgTransferWithVesting(
            sender = governanceAddress, recipient = scenarioDeal("r2-vested-gift"),
            amount = listOf(Coin(genesis.config.denom, gift)), vestingEpochs = 2,
        ))
        runHarness("verify-vesting-addition-scenario", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r2-vested-gift", "--before-label", "before-gift", "--amount", gift.toString(), "--vesting-epochs", "2", "--fund-tx-hash", fundingTx.txhash, "--proposal-id", proposalId, "--allow-empty-before")
        runHarness("r2-gift-checkpoint", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r2-vested-gift", "--stage", "fully_locked", "--gift-amount", gift.toString())
        genesis.waitForNextEpoch()
        runHarness("r2-gift-checkpoint", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r2-vested-gift", "--stage", "first_unlocked", "--gift-amount", gift.toString())
        runHarness("release-scenario", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r2-vested-gift")
        genesis.waitForNextEpoch()
        runHarness("release-scenario", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r2-vested-gift")
        runHarness("r2-gift-checkpoint", "--context", requiredEnv("E2E_CONTEXT"), "--name", "r2-vested-gift", "--stage", "final", "--gift-amount", gift.toString())
        r1.getOrThrow()
    }

    @Test
    fun `marketplace funded lock succeeds exactly at E`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }

        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3
        // bootstrap deliberately occupies join1/E. Register a separate native
        // Host so this focused Deal cannot conflict with that permanent pair.
        val hostKey = createInactiveParticipant(genesis, "a8-lock-exact-e")
        bootstrap(targetEpoch)
        // Host=the dedicated participant, Buyer=join2, and Lock fee payer=genesis
        // are distinct addresses.
        prepareDeal(
            "lock-exact-e",
            targetEpoch,
            funded = true,
            hostNode = "genesis-node",
            hostKey = hostKey,
        )

        genesis.markNeedsReboot()
        logSection("Leave the funded Deal unlocked until its exact E boundary")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "lock-exact-e-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "lock-exact-e",
        )
    }

    @Test
    fun `marketplace funded lock succeeds exactly at E plus 4`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }

        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3
        // bootstrap deliberately occupies join1/E. Register a separate native
        // Host so this focused Deal cannot conflict with that permanent pair.
        val hostKey = createInactiveParticipant(genesis, "a8-lock-e-plus-4")
        bootstrap(targetEpoch)
        // Host=the dedicated participant, Buyer=join2, and Lock fee payer=genesis
        // are distinct addresses.
        prepareDeal(
            "lock-e-plus-4",
            targetEpoch,
            funded = true,
            hostNode = "genesis-node",
            hostKey = hostKey,
        )

        genesis.markNeedsReboot()
        logSection("Leave the funded Deal unlocked until its exact E+4 boundary")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 4) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "lock-e-plus-4-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "lock-e-plus-4",
        )
    }

    @Test
    fun `marketplace funded lock rejects exactly at E plus 5`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }

        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3
        // A dedicated native Host avoids the bootstrap join1/E fixture pair.
        val hostKey = createInactiveParticipant(genesis, "a8-lock-e-plus-5")
        bootstrap(targetEpoch)
        // Host, Buyer, and genesis (the fee-paying Lock caller) are distinct.
        prepareDeal(
            "lock-e-plus-5",
            targetEpoch,
            funded = true,
            hostNode = "genesis-node",
            hostKey = hostKey,
        )

        genesis.markNeedsReboot()
        logSection("Leave the funded Deal unlocked until its exact E+5 boundary")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 5) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "lock-e-plus-5-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "lock-e-plus-5",
            "--gas", "2000000",
        )
    }

    @Test
    fun `marketplace zero unclaimed summary refunds only at claim expiry`() {
        // This is a special test-network genesis setting, not a production default.
        // Native Params.Validate accepts uint64 zero, and the unchanged settlement
        // keeper still writes an EpochPerformanceSummary for the active participant.
        val config = fastMarketplaceConfig(initialEpochReward = 0L)
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }

        val unclaimedHost = cluster.joinPairs.first()
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3

        logSection("Deploy Marketplace under the zero-subsidy test genesis")
        bootstrap(
            targetEpoch,
            hostNode = "genesis-node",
            hostKey = "genesis",
            expectedInitialEpochReward = 0L,
        )
        prepareDeal(
            "claim-expiry-zero",
            targetEpoch,
            funded = true,
            hostNode = "join1-node",
            hostKey = "join1",
        )

        genesis.markNeedsReboot()
        logSection("Reach E-1 and preserve the ordinary active Host snapshot")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch - 1) {
            genesis.waitForNextEpoch()
        }
        genesis.waitForStage(EpochStage.END_OF_POC_VALIDATION, offset = 0)
        unclaimedHost.stopOwnedApiContainer()

        logSection("Enter E=$targetEpoch and lock the exact native recipient")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "lock-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "claim-expiry-zero",
        )

        logSection("At E+1 the exact zero summary exists but Refund is too early")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 1) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "verify-unclaimed-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "claim-expiry-zero",
            "--require-zero",
        )
        runHarness(
            "refund-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "claim-expiry-zero",
            "--expect", "failure",
            "--reason", "too_early",
        )

        logSection("At E+2 the same exact zero summary remains unclaimed")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 2) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "verify-unclaimed-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "claim-expiry-zero",
            "--require-zero",
        )
        runHarness(
            "refund-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "claim-expiry-zero",
            "--expect", "success",
            "--reason", "claim_expiry",
        )
    }

    @Test
    fun `marketplace positive unclaimed summary refunds only at claim expiry`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }

        // join1 is a real active participant with PoC weight. Its DAPI stays up
        // through target-E PoC validation so native settlement can assign a
        // positive reward, then stops before the following CLAIM_REWARDS stage.
        val unclaimedHost = cluster.joinPairs.first()
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3

        logSection("Deploy Marketplace and fund an isolated claim-expiry Deal")
        bootstrap(
            targetEpoch,
            hostNode = "genesis-node",
            hostKey = "genesis",
        )
        prepareDeal(
            "claim-expiry-positive",
            targetEpoch,
            funded = true,
            hostNode = "join1-node",
            hostKey = "join1",
        )

        genesis.markNeedsReboot()
        logSection("Reach E-1 and keep Host active through target-E settlement")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch - 1) {
            genesis.waitForNextEpoch()
        }
        genesis.waitForStage(EpochStage.END_OF_POC_VALIDATION, offset = 0)
        unclaimedHost.stopOwnedApiContainer()

        logSection("Enter E=$targetEpoch and lock the exact native recipient")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "lock-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "claim-expiry-positive",
        )

        logSection("At E+1 the settled summary must be positive and Refund must fail closed")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 1) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "verify-unclaimed-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "claim-expiry-positive",
            "--require-positive",
        )
        runHarness(
            "refund-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "claim-expiry-positive",
            "--expect", "failure",
            "--reason", "too_early",
        )

        logSection("At E+2 the same positive summary must remain unclaimed")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 2) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "verify-unclaimed-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "claim-expiry-positive",
            "--require-positive",
        )
        runHarness(
            "refund-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "claim-expiry-positive",
            "--expect", "success",
            "--reason", "claim_expiry",
        )
    }

    @Test
    fun `marketplace absent native summary refunds only at emergency deadline`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }

        refundAtEmergencyDeadline(genesis, cluster.joinPairs[1])
    }

    @Test
    fun `marketplace emergency refund host only release rolls back and retries`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }

        refundAtEmergencyDeadline(genesis, cluster.joinPairs[1])
        runHarness(
            "donate-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "network-unconfirmed",
            "--label", "after_terminal_emergency_refund",
            "--amount", "11",
        )

        val restrictionEndBlock = genesis.node.queryRestrictionsStatus().currentBlockHeight + 50
        val restrictionProposalId = genesis.runProposal(
            cluster,
            UpdateRestrictionsParams(
                params = RestrictionsParams(
                    restrictionEndBlock = restrictionEndBlock,
                    emergencyTransferExemptions = emptyList(),
                    exemptionUsageTracking = emptyList(),
                )
            ),
        )
        check(genesis.node.queryRestrictionsStatus().isActive) {
            "transfer restrictions did not become active after proposal $restrictionProposalId"
        }
        val scenario = JsonParser.parseString(File(requiredEnv("E2E_CONTEXT")).readText())
            .asJsonObject["scenarios"].asJsonObject["network-unconfirmed"].asJsonObject
        val host = scenario["accounts"].asJsonObject["host"].asString
        runHarness(
            "bank-release-rollback-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "network-unconfirmed",
            "--proposal-id", restrictionProposalId,
            "--expected-send-index", "1",
            "--rejected-recipient", host,
        )

        genesis.node.waitForMinimumBlock(restrictionEndBlock + 1, "restriction expiry")
        check(!genesis.node.queryRestrictionsStatus().isActive) {
            "transfer restrictions remained active after block $restrictionEndBlock"
        }
        runHarness(
            "bank-release-retry-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "network-unconfirmed",
            "--expected-send-index", "1",
            "--rejected-recipient", host,
        )
    }

    @Test
    fun `marketplace terminal release repeat is rejected without payout`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }

        val participant = cluster.joinPairs.first()
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3

        logSection("Deploy one funded G3 Deal with independent financial roles")
        bootstrap(targetEpoch)

        genesis.markNeedsReboot()
        logSection("Reach E=$targetEpoch and lock the exact native recipient")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch) {
            genesis.waitForNextEpoch()
        }
        runHarness("lock", "--context", requiredEnv("E2E_CONTEXT"))
        val rewardSeed = participant.api.getConfig().currentSeed
        check(rewardSeed.epochIndex == targetEpoch) {
            "Testermint reward seed epoch ${rewardSeed.epochIndex} != Deal epoch $targetEpoch"
        }

        val stoppedApi = participant.stopOwnedApiContainer()
        logSection("Claim the positive native reward and settle the funded Deal")
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        runHarness(
            "claim-settle",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--reward-seed", rewardSeed.seed.toString(),
            "--reward-epoch", rewardSeed.epochIndex.toString(),
        )

        stoppedApi.startSameContainer()
        genesis.node.waitForNextBlock(2)
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = -1)
        logSection("Wait for both native vesting tranches and release through Completed")
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        genesis.node.waitForNextBlock(2)
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 2) {
            genesis.waitForNextEpoch()
        }
        genesis.node.waitForNextBlock(2)
        runHarness("release", "--context", requiredEnv("E2E_CONTEXT"))

        logSection("Unlock and release the second native tranche through Completed")
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        genesis.node.waitForNextBlock(2)
        // A slow first release may already drain all unlocked tranches.
        // Require the remaining payout or a verified Completed/NothingToRelease repeat.
        runHarness(
            "release-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "bootstrap",
        )

        logSection("Broadcast a real zero-balance ReleaseUnlockedGnk from an independent caller")
        runHarness("terminal-release-repeat", "--context", requiredEnv("E2E_CONTEXT"))
    }

    @Test
    fun `marketplace native release rejects selected second Bank send then retries once`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }
        val participant = cluster.joinPairs.first()
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3

        bootstrap(targetEpoch)
        genesis.markNeedsReboot()
        while (genesis.getEpochData().latestEpoch.index < targetEpoch) genesis.waitForNextEpoch()
        runHarness("lock", "--context", requiredEnv("E2E_CONTEXT"))
        val rewardSeed = participant.api.getConfig().currentSeed
        check(rewardSeed.epochIndex == targetEpoch)
        val stoppedApi = participant.stopOwnedApiContainer()
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        runHarness(
            "claim-settle", "--context", requiredEnv("E2E_CONTEXT"),
            "--reward-seed", rewardSeed.seed.toString(), "--reward-epoch", rewardSeed.epochIndex.toString(),
        )
        stoppedApi.startSameContainer()
        genesis.node.waitForNextBlock(2)
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = -1)
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        genesis.node.waitForNextBlock(2)
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 2) genesis.waitForNextEpoch()
        genesis.node.waitForNextBlock(2)

        // Governance may cross an epoch. Freeze the payout calculation only
        // after BOTH original vesting tranches are liquid, and prove it natively.
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 3) genesis.waitForNextEpoch()
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        runHarness("bank-release-fault-plan", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "bootstrap", "--require-fully-vested")
        val phases = JsonParser.parseString(File(requiredEnv("E2E_CONTEXT")).readText())
            .asJsonObject["phases"].asJsonArray
        val plan = phases.last { it.asJsonObject["name"].asString == "native_bank_release_fault_plan" }
            .asJsonObject["plan"].asJsonObject
        val restrictionEndBlock = genesis.node.queryRestrictionsStatus().currentBlockHeight + 50
        val firstProposalId = genesis.runProposal(
            cluster,
            UpdateRestrictionsParams(
                params = RestrictionsParams(
                    restrictionEndBlock = restrictionEndBlock,
                    emergencyTransferExemptions = emptyList(),
                    exemptionUsageTracking = emptyList(),
                )
            ),
        )
        check(genesis.node.queryRestrictionsStatus().isActive)
        runHarness(
            "bank-release-rollback-scenario", "--context", requiredEnv("E2E_CONTEXT"), "--name", "bootstrap",
            "--proposal-id", firstProposalId, "--expected-send-index", "1",
            "--rejected-recipient", plan["buyer"].asString,
        )
        val restrictions = bankSendFaultPlan(
            restrictionEndBlock, plan["deal"].asString, plan["buyer"].asString, plan["host"].asString,
            plan["buyer_amount"].asLong, plan["host_amount"].asLong, "a8-r7-1-buyer-first",
        )
        val proposalId = genesis.runProposal(cluster, UpdateRestrictionsParams(params = restrictions.params))
        check(genesis.node.queryRestrictionsStatus().isActive)
        runHarness(
            "bank-release-rollback-scenario", "--context", requiredEnv("E2E_CONTEXT"), "--name", "bootstrap",
            "--proposal-id", proposalId, "--expected-send-index", "2",
            "--allowed-earlier-recipient", restrictions.allowedEarlierRecipient!!,
            "--rejected-recipient", restrictions.rejectedRecipient,
            "--exemption-id", "a8-r7-1-buyer-first",
        )
        genesis.node.waitForMinimumBlock(restrictionEndBlock + 1, "native release restriction expiry")
        check(!genesis.node.queryRestrictionsStatus().isActive)
        runHarness(
            "bank-release-retry-scenario", "--context", requiredEnv("E2E_CONTEXT"), "--name", "bootstrap",
            "--expected-send-index", "2", "--rejected-recipient", restrictions.rejectedRecipient,
        )
    }

    @Test
    fun `marketplace settlement commits once and each rejected USDT withdrawal rolls back atomically`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }
        val participant = cluster.joinPairs.first()
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3
        // Price below the observed positive reward makes Host net, fee, and
        // Buyer refund all non-zero; the Python oracle rejects another shape.
        bootstrap(targetEpoch, price = 1000L)
        genesis.markNeedsReboot()
        while (genesis.getEpochData().latestEpoch.index < targetEpoch) genesis.waitForNextEpoch()
        runHarness("lock", "--context", requiredEnv("E2E_CONTEXT"))
        val rewardSeed = participant.api.getConfig().currentSeed
        check(rewardSeed.epochIndex == targetEpoch)
        participant.stopOwnedApiContainer()
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        runHarness(
            "claim-settle", "--context", requiredEnv("E2E_CONTEXT"),
            "--reward-seed", rewardSeed.seed.toString(), "--reward-epoch", rewardSeed.epochIndex.toString(),
            "--cw20-fault-positions", "1,2,3",
        )
    }

    @Test
    fun `marketplace late liquid donations after Completed use cumulative GNK rounding`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }

        val participant = cluster.joinPairs.first()
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3

        logSection("Deploy one funded B2 Deal with independent financial roles")
        bootstrap(targetEpoch)

        genesis.markNeedsReboot()
        while (genesis.getEpochData().latestEpoch.index < targetEpoch) {
            genesis.waitForNextEpoch()
        }
        runHarness("lock", "--context", requiredEnv("E2E_CONTEXT"))
        val rewardSeed = participant.api.getConfig().currentSeed
        check(rewardSeed.epochIndex == targetEpoch) {
            "Testermint reward seed ${rewardSeed.epochIndex} != B2 Deal epoch $targetEpoch"
        }

        val stoppedApi = participant.stopOwnedApiContainer()
        logSection("Claim the positive native reward and settle the funded Deal")
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        runHarness(
            "claim-settle",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--reward-seed", rewardSeed.seed.toString(),
            "--reward-epoch", rewardSeed.epochIndex.toString(),
        )

        stoppedApi.startSameContainer()
        genesis.node.waitForNextBlock(2)
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = -1)
        logSection("Release both original native tranches through Completed")
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        genesis.node.waitForNextBlock(2)
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 2) {
            genesis.waitForNextEpoch()
        }
        genesis.node.waitForNextBlock(2)
        runHarness("release", "--context", requiredEnv("E2E_CONTEXT"))

        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        genesis.node.waitForNextBlock(2)
        // A slow first release may already drain all unlocked tranches.
        // Require the remaining payout or a verified Completed/NothingToRelease repeat.
        runHarness(
            "release-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "bootstrap",
        )

        logSection("Donate liquid GNK twice after Completed and prove cumulative rounding")
        runHarness(
            "late-donation",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--amount", "1423",
            "--second-amount", "1423",
        )
    }

    @Test
    fun `marketplace successful release preserves foreign native denom`() {
        val config = fastMarketplaceConfig(enableB3ForeignNativeFixture = true)
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }

        val participant = cluster.joinPairs.first()
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3
        importB3ForeignNativeKey(genesis)
        check(genesis.node.getBalance(B3_FOREIGN_ADDRESS, B3_FOREIGN_DENOM).balance.amount == B3_FOREIGN_AMOUNT) {
            "B3 genesis fixture did not preserve its exact foreign native balance"
        }

        logSection("Deploy one funded B3 Deal with independent financial roles")
        bootstrap(targetEpoch)

        genesis.markNeedsReboot()
        while (genesis.getEpochData().latestEpoch.index < targetEpoch) {
            genesis.waitForNextEpoch()
        }
        runHarness("lock", "--context", requiredEnv("E2E_CONTEXT"))
        val rewardSeed = participant.api.getConfig().currentSeed
        check(rewardSeed.epochIndex == targetEpoch) {
            "Testermint reward seed ${rewardSeed.epochIndex} != B3 Deal epoch $targetEpoch"
        }

        val stoppedApi = participant.stopOwnedApiContainer()
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        runHarness(
            "claim-settle",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--reward-seed", rewardSeed.seed.toString(),
            "--reward-epoch", rewardSeed.epochIndex.toString(),
        )

        stoppedApi.startSameContainer()
        genesis.node.waitForNextBlock(2)
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = -1)
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        genesis.node.waitForNextBlock(2)
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 2) {
            genesis.waitForNextEpoch()
        }
        genesis.node.waitForNextBlock(2)
        runHarness(
            "b3-foreign-native-release",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--foreign-key", B3_FOREIGN_KEY,
            "--foreign-address", B3_FOREIGN_ADDRESS,
            "--foreign-denom", B3_FOREIGN_DENOM,
            "--foreign-amount", B3_FOREIGN_AMOUNT.toString(),
        )
    }

    @Test
    fun `marketplace funded claim settles and releases on real Gonka`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }
        val participant = cluster.joinPairs.first()
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3

        // Keep the observed positive reward below the funded capacity so the
        // Host, fee recipient, and Buyer refund all have real CW20 payouts.
        bootstrap(targetEpoch, price = 1000L)
        genesis.markNeedsReboot()
        while (genesis.getEpochData().latestEpoch.index < targetEpoch) {
            genesis.waitForNextEpoch()
        }
        check(genesis.getEpochData().latestEpoch.index == targetEpoch) {
            "funded lifecycle missed its exact Lock epoch"
        }
        runHarness("lock", "--context", requiredEnv("E2E_CONTEXT"))
        val rewardSeed = participant.api.getConfig().currentSeed
        check(rewardSeed.epochIndex == targetEpoch) {
            "reward seed epoch ${rewardSeed.epochIndex} != Deal epoch $targetEpoch"
        }

        val stoppedApi = participant.stopOwnedApiContainer()
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        runHarness(
            "claim-settle", "--context", requiredEnv("E2E_CONTEXT"),
            "--reward-seed", rewardSeed.seed.toString(),
            "--reward-epoch", rewardSeed.epochIndex.toString(),
            "--claim-only",
        )
        stoppedApi.startSameContainer()
        genesis.node.waitForNextBlock(2)
        runHarness(
            "claim-settle", "--context", requiredEnv("E2E_CONTEXT"),
            "--reward-seed", rewardSeed.seed.toString(),
            "--reward-epoch", rewardSeed.epochIndex.toString(),
            "--resume-claim", "--fault-retry",
        )

        // Synchronize at the same native stages used by the proven terminal
        // lifecycle test.  Merely observing an epoch index is not enough:
        // vesting updates are written during CLAIM_REWARDS and can lag the
        // first block that reports the new epoch.
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = -1)
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        genesis.node.waitForNextBlock(2)
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 2) {
            genesis.waitForNextEpoch()
        }
        genesis.node.waitForNextBlock(2)
        runHarness("release", "--context", requiredEnv("E2E_CONTEXT"))
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        genesis.node.waitForNextBlock(2)
        // Slow settlement can leave all tranches unlocked at the first release.
        // The second attempt must prove either the remaining payout or an unchanged
        // Completed Deal with the expected NothingToRelease rejection.
        runHarness(
            "release-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "bootstrap",
        )
    }

    @Test
    fun `marketplace funded routing refunds are isolated and atomic`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3

        // Bootstrap supplies the shared Factory and test contracts.  Both
        // routing histories below are separate Deals in that same Factory so
        // the final assertion proves index/state isolation, not merely clean
        // state obtained from two unrelated networks.
        bootstrap(targetEpoch)
        prepareDeal(
            "routing-mismatch", targetEpoch, funded = true,
            hostNode = "genesis-node", hostKey = "genesis",
        )
        runHarness(
            "set-scenario-routing", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "routing-mismatch", "--recipient", "buyer",
        )
        val routingMissingEpoch = targetEpoch + 1
        prepareDeal(
            "routing-missing", routingMissingEpoch, funded = true,
            hostNode = "genesis-node", hostKey = "genesis",
        )
        runHarness(
            "set-scenario-routing", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "routing-missing", "--recipient", "missing",
        )

        genesis.markNeedsReboot()
        while (genesis.getEpochData().latestEpoch.index < targetEpoch) {
            genesis.waitForNextEpoch()
        }
        check(genesis.getEpochData().latestEpoch.index == targetEpoch) {
            "routing fixtures missed their exact target epoch"
        }
        runHarness(
            "refund-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "routing-mismatch", "--expect", "success",
            "--reason", "routing_mismatch", "--fault-cw20",
        )
        while (genesis.getEpochData().latestEpoch.index < routingMissingEpoch) {
            genesis.waitForNextEpoch()
        }
        check(genesis.getEpochData().latestEpoch.index == routingMissingEpoch) {
            "routing-missing fixture missed its exact target epoch"
        }
        runHarness(
            "refund-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "routing-missing", "--expect", "success",
            "--reason", "routing_missing", "--fault-cw20",
        )
        runHarness("verify-factory-isolation", "--context", requiredEnv("E2E_CONTEXT"))
    }

    @Test
    fun `marketplace unfunded lock boundaries preserve buyer absence`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }
        val ePlus4Host = createInactiveParticipant(genesis, "a8-unfunded-e-plus-4")
        val ePlus5Host = createInactiveParticipant(genesis, "a8-unfunded-e-plus-5")
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3

        bootstrap(targetEpoch)
        prepareDeal(
            "lock-e-plus-4", targetEpoch, funded = false,
            hostNode = "genesis-node", hostKey = ePlus4Host,
        )
        prepareDeal(
            "lock-e-plus-5", targetEpoch, funded = false,
            hostNode = "genesis-node", hostKey = ePlus5Host,
        )
        genesis.markNeedsReboot()

        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 4) {
            genesis.waitForNextEpoch()
        }
        check(genesis.getEpochData().latestEpoch.index == targetEpoch + 4) {
            "unfunded E+4 fixture missed its exact Lock boundary"
        }
        runHarness(
            "lock-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "lock-e-plus-4",
        )

        genesis.waitForNextEpoch()
        check(genesis.getEpochData().latestEpoch.index == targetEpoch + 5) {
            "unfunded E+5 fixture missed its exact rejection boundary"
        }
        runHarness(
            "lock-rejected-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "lock-e-plus-5", "--routing", "pruned",
        )
    }

    @Test
    fun `marketplace claimed refund gas sweep is isolated`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3
        bootstrap(targetEpoch)
        prepareDeal(
            "gas-claimed", targetEpoch, funded = true,
            hostNode = "genesis-node", hostKey = "genesis",
        )
        genesis.markNeedsReboot()
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 2) {
            genesis.waitForNextEpoch()
        }
        genesis.node.waitForNextBlock(2)
        runHarness(
            "verify-claimed-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "gas-claimed", "--require-positive",
        )
        runHarness(
            "lock-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "gas-claimed",
        )
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 3) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "gas-sweep-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "gas-claimed",
        )
    }

    @Test
    fun `marketplace no buyer claim expiry preserves buyer absence`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }
        val unclaimedHost = cluster.joinPairs.first()
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3
        bootstrap(
            targetEpoch,
            hostNode = "genesis-node",
            hostKey = "genesis",
        )
        prepareDeal(
            "no-buyer-expired", targetEpoch, funded = false,
            hostNode = "join1-node", hostKey = "join1",
        )
        genesis.markNeedsReboot()
        while (genesis.getEpochData().latestEpoch.index < targetEpoch - 1) {
            genesis.waitForNextEpoch()
        }
        genesis.waitForStage(EpochStage.END_OF_POC_VALIDATION, offset = 0)
        unclaimedHost.stopOwnedApiContainer()
        while (genesis.getEpochData().latestEpoch.index < targetEpoch) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "lock-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-buyer-expired",
        )
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 1) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "verify-unclaimed-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-buyer-expired", "--require-positive",
        )
        runHarness(
            "refund-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-buyer-expired", "--expect", "failure", "--reason", "too_early",
        )
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 2) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "verify-unclaimed-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-buyer-expired", "--require-positive",
        )
        runHarness(
            "refund-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-buyer-expired", "--expect", "success", "--reason", "claim_expiry",
        )
    }

    @Test
    @Timeout(value = 55, unit = TimeUnit.MINUTES)
    fun `marketplace no sale vesting lifecycle preserves every asset`() {
        val config = fastMarketplaceConfig()
        val (cluster, genesis) = initCluster(config = config, reboot = true)
        cluster.allPairs.forEach { it.waitForMlNodesToLoad() }
        val participant = cluster.joinPairs.first()
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 3
        bootstrap(
            targetEpoch,
            hostNode = "genesis-node",
            hostKey = "genesis",
        )
        prepareDeal(
            "no-sale", targetEpoch, funded = false,
            hostNode = "join1-node", hostKey = "join1",
        )
        genesis.markNeedsReboot()
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 1) {
            genesis.waitForNextEpoch()
        }
        genesis.node.waitForNextBlock(2)
        runHarness(
            "verify-claimed-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-sale", "--require-positive",
        )
        participant.stopOwnedApiContainer()
        runHarness(
            "lock-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-sale",
        )
        runHarness(
            "donate-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-sale", "--label", "before_settlement", "--amount", "3",
        )
        runHarness(
            "contaminate-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-sale", "--amount", "7",
        )
        runHarness(
            "settle-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-sale",
        )
        // Liquid donations are spendable even while native rewards remain
        // vested. Release them before testing NothingToRelease at zero balance.
        runHarness(
            "release-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-sale",
        )
        runHarness(
            "assert-early-release-unavailable", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-sale",
        )

        val vestingDonation = 10_000_000_001L
        val governanceAddress = genesis.node.getModuleAccount("gov").account.value.address
        val genesisAddress = genesis.node.getColdAddress()
        genesis.ensureGenesisSpendableForDevshard(vestingDonation)
        val vestingFundingTx = genesis.submitTransaction(
            listOf(
                "bank", "send", genesisAddress, governanceAddress,
                "$vestingDonation${genesis.config.denom}",
            )
        )
        check(vestingFundingTx.code == 0) {
            "governance funding failed: ${vestingFundingTx.rawLog}"
        }
        runHarness(
            "snapshot-vesting-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-sale", "--label", "before-additional-vesting",
            "--require-non-empty",
        )
        val vestingProposalId = genesis.runProposal(
            cluster,
            MsgTransferWithVesting(
                sender = governanceAddress,
                recipient = scenarioDeal("no-sale"),
                amount = listOf(Coin(genesis.config.denom, vestingDonation)),
                vestingEpochs = 2,
            ),
        )
        runHarness(
            "verify-vesting-addition-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-sale", "--before-label", "before-additional-vesting",
            "--amount", vestingDonation.toString(), "--vesting-epochs", "2",
            "--fund-tx-hash", vestingFundingTx.txhash,
            "--proposal-id", vestingProposalId,
        )
        runHarness(
            "donate-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-sale", "--label", "after_settlement", "--amount", "2",
        )
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        genesis.node.waitForNextBlock(2)
        runHarness(
            "release-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-sale",
        )
        genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)
        genesis.node.waitForNextBlock(2)
        runHarness(
            "release-scenario", "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "no-sale",
        )
    }

    // Each caller owns its cluster; sharing this sequence keeps the native
    // absence and deadline assertions identical before exercising release rollback.
    private fun refundAtEmergencyDeadline(
        genesis: com.productscience.LocalInferencePair,
        absentSummaryHost: com.productscience.LocalInferencePair,
    ) {
        // Configure a real recipient while join2 is registered, then keep its DAPI
        // offline for five complete epochs before E. Native settlement only writes
        // summaries for the active participant snapshot, so exact Host/E is absent.
        val targetEpoch = genesis.getEpochData().latestEpoch.index + 5

        logSection("Deploy and fund an isolated emergency-refund Deal")
        bootstrap(
            targetEpoch,
            hostNode = "genesis-node",
            hostKey = "genesis",
        )
        prepareDeal(
            "network-unconfirmed",
            targetEpoch,
            funded = true,
            hostNode = "join2-node",
            hostKey = "join2",
        )

        genesis.markNeedsReboot()
        absentSummaryHost.stopOwnedApiContainer()
        logSection("Keep Host offline through E=$targetEpoch and lock its exact recipient")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "lock-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "network-unconfirmed",
        )

        logSection("At E+2 exact native NotFound still fails closed")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 2) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "verify-missing-summary-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "network-unconfirmed",
            "--expected-offset", "2",
        )
        runHarness(
            "refund-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "network-unconfirmed",
            "--expect", "failure",
            "--reason", "network_unconfirmed_too_early",
        )

        logSection("At E+3 the same native NotFound permits NetworkUnconfirmed")
        while (genesis.getEpochData().latestEpoch.index < targetEpoch + 3) {
            genesis.waitForNextEpoch()
        }
        runHarness(
            "verify-missing-summary-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "network-unconfirmed",
            "--expected-offset", "3",
        )
        runHarness(
            "refund-scenario",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", "network-unconfirmed",
            "--expect", "success",
            "--reason", "network_unconfirmed",
        )
    }

    private fun bootstrap(
        targetEpoch: Long,
        hostNode: String = "join1-node",
        hostKey: String = "join1",
        expectedInitialEpochReward: Long? = null,
        price: Long? = null,
    ) {
        val args = mutableListOf(
            "bootstrap",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--run-id", requiredEnv("E2E_RUN_ID"),
            "--target-epoch", targetEpoch.toString(),
            "--deal-wasm", requiredEnv("E2E_DEAL_WASM"),
            "--factory-wasm", requiredEnv("E2E_FACTORY_WASM"),
            "--cw20-wasm", requiredEnv("E2E_CW20_WASM"),
            "--caller-wasm", requiredEnv("E2E_CALLER_WASM"),
            "--host-node", hostNode,
            "--host-key", hostKey,
        )
        if (expectedInitialEpochReward != null) {
            args += listOf("--expected-initial-epoch-reward", expectedInitialEpochReward.toString())
        }
        if (price != null) args += listOf("--price", price.toString())
        runHarness(*args.toTypedArray())
    }

    private fun fastMarketplaceConfig(
        initialEpochReward: Long? = null,
        enableB3ForeignNativeFixture: Boolean = false,
    ): com.productscience.ApplicationConfig {
        val fastSpec = spec {
            this[AppState::inference] = spec<InferenceState> {
                this[InferenceState::params] = spec<InferenceParams> {
                    if (initialEpochReward != null) {
                        this[InferenceParams::bitcoinRewardParams] = spec<BitcoinRewardParams> {
                            this[BitcoinRewardParams::initialEpochReward] = initialEpochReward
                        }
                    }
                    this[InferenceParams::tokenomicsParams] = spec<TokenomicsParams> {
                        this[TokenomicsParams::workVestingPeriod] = 2L
                        this[TokenomicsParams::rewardVestingPeriod] = 2L
                    }
                    this[InferenceParams::epochParams] = spec<EpochParams> {
                        this[EpochParams::epochLength] = 25L
                    }
                }
            }
            this[AppState::restrictions] = spec<RestrictionsState> {
                this[RestrictionsState::params] = spec<RestrictionsParams> {
                    this[RestrictionsParams::restrictionEndBlock] = 0L
                }
            }
            if (enableB3ForeignNativeFixture) {
                // The stock test override has a 24h expedited voting period
                // alongside a 30s regular period.  B3 does not exercise
                // governance, but its genesis preflight must validate.
                this[AppState::gov] = spec<GovState> {
                    this[GovState::params] = spec<GovParams> {
                        this[GovParams::expeditedVotingPeriod] = Duration.ofSeconds(15)
                    }
                }
            }
        }
        val config = inferenceConfig.copy(
            genesisSpec = inferenceConfig.genesisSpec?.merge(fastSpec) ?: fastSpec,
            additionalDockerFilesByKeyName = additionalComposeFiles(enableB3ForeignNativeFixture),
        )
        return config
    }

    // Runner-generated Compose overlays under $GONKA_REPO_ROOT/local-test-net
    // (the per-run network root, never the Gonka source snapshot). Every pair
    // gets the run-id ownership label (so the container controller and cleanup
    // only ever touch this run's containers) and the NATS bind layout that the
    // runner prepares. Only B3 adds the genesis-provisioner overlay that
    // installs its foreign native balance with the standard genesis command.
    private fun additionalComposeFiles(enableB3ForeignNativeFixture: Boolean): Map<String, List<String>> {
        val common = listOf(OWNERSHIP_COMPOSE_FILE, NATS_COMPOSE_FILE)
        return mapOf(
            GENESIS_KEY_NAME to if (enableB3ForeignNativeFixture) common + FOREIGN_NATIVE_GENESIS_COMPOSE_FILE else common,
            // Upstream createDockerGroup names join pairs "join$joinIter";
            // initCluster's default joinCount = 2 yields join1 and join2.
            "join1" to common,
            "join2" to common,
        )
    }

    private fun importB3ForeignNativeKey(genesis: com.productscience.LocalInferencePair) {
        genesis.node.exec(
            listOf(
                genesis.node.config.execName,
                "keys",
                "add",
                B3_FOREIGN_KEY,
                "--recover",
                "--output",
                "json",
            ) + genesis.node.config.keychainParams,
            stdin = B3_FOREIGN_MNEMONIC + "\n",
        )
    }

    private fun runHarness(vararg args: String) {
        val command = listOf(requiredEnv("E2E_PYTHON"), requiredEnv("E2E_HARNESS")) + args
        val output = runMarketplaceHarnessProcess(
            command = command,
            directory = File(requiredEnv("E2E_MARKETPLACE_DIR")),
            phase = args.firstOrNull(),
            timeout = 8,
            timeoutUnit = TimeUnit.MINUTES,
        )
        println(output.trim())
    }

    private fun prepareDeal(
        name: String,
        targetEpoch: Long,
        funded: Boolean,
        hostNode: String = "join1-node",
        hostKey: String = "join1",
    ) {
        val args = mutableListOf(
            "create-deal",
            "--context", requiredEnv("E2E_CONTEXT"),
            "--name", name,
            "--target-epoch", targetEpoch.toString(),
            "--host-node", hostNode,
            "--host-key", hostKey,
            "--route-exact",
        )
        if (funded) args += "--fund"
        runHarness(*args.toTypedArray())
    }

    private fun scenarioDeal(name: String): String {
        val root = JsonParser.parseString(File(requiredEnv("E2E_CONTEXT")).readText()).asJsonObject
        return root.getAsJsonObject("scenarios")
            .getAsJsonObject(name)
            .getAsJsonObject("contracts")
            .get("deal")
            .asString
    }

    private fun createInactiveParticipant(genesis: com.productscience.LocalInferencePair, prefix: String): String {
        val key = genesis.node.createKey("$prefix-${System.currentTimeMillis()}")
        genesis.api.addUnfundedInferenceParticipant(
            UnfundedInferenceParticipant(
                url = "",
                models = listOf(),
                validatorKey = "",
                pubKey = key.pubkey.key,
                address = key.address,
            ),
        )
        genesis.node.waitForNextBlock(2)
        return key.name
    }

    private fun requiredEnv(name: String): String = requiredHarnessEnv(name)

    private companion object {
        const val B3_FOREIGN_KEY = "a8-b3-foreign"
        const val B3_FOREIGN_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        const val B3_FOREIGN_ADDRESS = "gonka1k4swv40ur28fvu54p8mskjj4lxkgsj07u9f8ny"
        const val B3_FOREIGN_DENOM = "ua8b3foreign"
        const val B3_FOREIGN_AMOUNT = 12_345L
        const val OWNERSHIP_COMPOSE_FILE = "ownership.yml"
        const val NATS_COMPOSE_FILE = "nats.yml"
        const val FOREIGN_NATIVE_GENESIS_COMPOSE_FILE = "foreign-native-genesis.yml"
    }
}
