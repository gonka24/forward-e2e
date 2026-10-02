import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class A8PackageBBankFaultPlanTests {
    @Test
    fun `R7 dot 1 permits only Buyer before rejecting Host as send two`() {
        val plan = a8BankSendFaultPlan(
            restrictionEndBlock = 200,
            deal = "gonka1deal",
            buyer = "gonka1buyer",
            host = "gonka1host",
            buyerAmount = 40,
            hostAmount = 60,
            exemptionId = "a8-r7-send-1",
        )
        assertEquals(2, plan.expectedFailingSend)
        assertEquals("gonka1buyer", plan.allowedEarlierRecipient)
        assertEquals("gonka1host", plan.rejectedRecipient)
        assertEquals(1, plan.params.emergencyTransferExemptions.size)
        val exemption = plan.params.emergencyTransferExemptions.single()
        assertEquals("a8-r7-send-1", exemption.exemptionId)
        assertEquals("gonka1deal", exemption.fromAddress)
        assertEquals("gonka1buyer", exemption.toAddress)
        assertEquals("40", exemption.maxAmount)
        assertEquals(1, exemption.usageLimit)
        assertEquals(200, exemption.expiryBlock)
        assertEquals(200, plan.params.restrictionEndBlock)
    }

    @Test
    fun `R7 dot 1 rejects aliased roles and zero payouts`() {
        assertFailsWith<IllegalArgumentException> {
            a8BankSendFaultPlan(200, "deal", "same", "same", 1, 1, "id")
        }
        assertFailsWith<IllegalArgumentException> {
            a8BankSendFaultPlan(200, "same", "same", "host", 1, 1, "id")
        }
        assertFailsWith<IllegalArgumentException> {
            a8BankSendFaultPlan(200, "same", "buyer", "same", 1, 1, "id")
        }
        assertFailsWith<IllegalArgumentException> {
            a8BankSendFaultPlan(200, "deal", "buyer", "host", 0, 1, "id")
        }
        assertFailsWith<IllegalArgumentException> {
            a8BankSendFaultPlan(200, "deal", "buyer", "host", 1, 0, "id")
        }
    }

    @Test
    fun `R7 dot 2 has no exemption because Host is the first and only send`() {
        val plan = a8HostOnlyBankSendFaultPlan(200, "gonka1host", 100)
        assertEquals(1, plan.expectedFailingSend)
        assertEquals(null, plan.allowedEarlierRecipient)
        assertEquals(0, plan.params.emergencyTransferExemptions.size)
    }
}
