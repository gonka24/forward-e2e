import com.productscience.data.EmergencyTransferExemption
import com.productscience.data.RestrictionsParams

/**
 * Test-harness-only selector for native Bank release failures.
 *
 * It does not instrument the runtime: it translates the already supported
 * restrictions parameters into an address-specific proof that send #1 may
 * pass while send #2 is rejected.  The caller must record the resulting
 * governance proposal and use a fresh, active restriction window.
 */
internal data class BankSendFaultPlan(
    val expectedFailingSend: Int,
    val allowedEarlierRecipient: String?,
    val rejectedRecipient: String,
    val params: RestrictionsParams,
)

internal fun bankSendFaultPlan(
    restrictionEndBlock: Long,
    deal: String,
    buyer: String,
    host: String,
    buyerAmount: Long,
    hostAmount: Long,
    exemptionId: String,
): BankSendFaultPlan {
    require(restrictionEndBlock > 0) { "Native release rollback requires an active restriction window" }
    require(deal.isNotBlank() && buyer.isNotBlank() && host.isNotBlank()) { "Native release rollback requires addresses" }
    require(deal != buyer && deal != host && buyer != host) {
        "Two-recipient native release rollback needs distinct Deal, Buyer and Host addresses"
    }
    require(buyerAmount > 0 && hostAmount > 0) { "Two-recipient native release rollback needs two non-zero GNK sends" }
    require(exemptionId.isNotBlank()) { "Native release rollback needs a recorded exemption id" }

    // The only exemption is Deal -> Buyer, so release send #1 is permitted;
    // the following Deal -> Host send has no matching exemption and is rejected.
    val params = RestrictionsParams(
        restrictionEndBlock = restrictionEndBlock,
        emergencyTransferExemptions = listOf(
            EmergencyTransferExemption(
                exemptionId = exemptionId,
                fromAddress = deal,
                toAddress = buyer,
                maxAmount = buyerAmount.toString(),
                usageLimit = 1,
                expiryBlock = restrictionEndBlock,
                justification = "test-only selected Bank send #1",
            )
        ),
        exemptionUsageTracking = emptyList(),
    )
    return BankSendFaultPlan(2, buyer, host, params)
}

internal fun hostOnlyBankSendFaultPlan(
    restrictionEndBlock: Long,
    host: String,
    hostAmount: Long,
): BankSendFaultPlan {
    require(restrictionEndBlock > 0) { "Host-only release rollback requires an active restriction window" }
    require(host.isNotBlank() && hostAmount > 0) { "Host-only release rollback needs one non-zero Host send" }
    return BankSendFaultPlan(
        expectedFailingSend = 1,
        allowedEarlierRecipient = null,
        rejectedRecipient = host,
        params = RestrictionsParams(
            restrictionEndBlock = restrictionEndBlock,
            emergencyTransferExemptions = emptyList(),
            exemptionUsageTracking = emptyList(),
        ),
    )
}
