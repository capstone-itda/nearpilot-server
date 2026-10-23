package com.nearpilot.app.approval

import com.nearpilot.app.account.ApiResult
import com.nearpilot.app.account.HttpResponse
import com.nearpilot.app.account.HttpTransport
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.time.Instant

class ApprovalApiTest {

    private class FakeTransport(var response: HttpResponse) : HttpTransport {
        data class Sent(val method: String, val url: String, val body: String?, val token: String?)

        val sent = mutableListOf<Sent>()
        var error: IOException? = null

        override fun send(method: String, url: String, body: String?, token: String?): HttpResponse {
            sent += Sent(method, url, body, token)
            error?.let { throw it }
            return response
        }
    }

    private val itemJson = """
        {"approval_id":"ap-1","node_id":"locker-1","action":"open","status":"pending",
         "created_at":"2026-10-23T10:00:00Z","pending_expires_at":"2026-10-23T10:10:00+00:00","valid_until":null}
    """.trimIndent()

    private fun item(id: String) = ApprovalItem(id, "light-1", "on", "pending", null, null, null)

    @Test
    fun FR03_lists_pending_approvals_with_token() {
        val transport = FakeTransport(HttpResponse(200, "[$itemJson]"))

        val result = ApprovalApi("http://srv", transport).list("owner-token")

        val expected = ApprovalItem(
            "ap-1", "locker-1", "open", "pending",
            Instant.parse("2026-10-23T10:00:00Z"), Instant.parse("2026-10-23T10:10:00Z"), null,
        )
        assertEquals(ApiResult.Success(listOf(expected)), result)
        assertEquals(FakeTransport.Sent("GET", "http://srv/approvals", null, "owner-token"), transport.sent.single())
    }

    @Test
    fun FR03_reads_list_in_both_shapes() {
        assertEquals(1, ApprovalApi.parseList("""{"approvals":[$itemJson]}""")!!.size)
        assertEquals(0, ApprovalApi.parseList("[]")!!.size)
        assertEquals(null, ApprovalApi.parseList("""{"other":[]}"""))
        assertEquals(null, ApprovalApi.parseList("""[{"node_id":"x"}]"""))
    }

    @Test
    fun FR03_decision_posts_choice() {
        val transport = FakeTransport(HttpResponse(200, itemJson.replace("pending\",", "approved\",")))
        val api = ApprovalApi("http://srv", transport)

        val approved = api.decide("owner-token", "ap-1", ApprovalChoice.APPROVE)
        api.decide("owner-token", "ap 2", ApprovalChoice.DENY)

        assertEquals("approved", (approved as ApiResult.Success).value.status)
        val (first, second) = transport.sent
        assertEquals("POST", first.method)
        assertEquals("http://srv/approvals/ap-1/decision", first.url)
        assertEquals("approved", JSONObject(first.body!!).getString("decision"))
        assertEquals("owner-token", first.token)
        assertEquals("http://srv/approvals/ap%202/decision", second.url)
        assertEquals("denied", JSONObject(second.body!!).getString("decision"))
    }

    @Test
    fun FR03_decision_reasons_are_distinguished() {
        val reasons = listOf(
            409 to "ALREADY_DECIDED", 409 to "POLICY_VERSION_MISMATCH", 409 to "REQUEST_EXPIRED",
            403 to "NOT_AUTHORIZED", 404 to "NOT_FOUND",
        )
        val messages = reasons.map { (status, reason) ->
            val result = ApprovalApi("http://srv", FakeTransport(HttpResponse(status, """{"error":"$reason"}""")))
                .decide("t", "ap-1", ApprovalChoice.APPROVE)
            assertEquals(ApiResult.Rejected(status, reason), result)
            ApprovalText.failure(result).also { assertTrue(reason, it.isNotBlank() && !it.contains(reason)) }
        }
        assertEquals(messages.size, messages.toSet().size)
    }

    @Test
    fun FR03_missing_server_feature_is_explained() {
        val result = ApprovalApi("http://srv", FakeTransport(HttpResponse(404, "<html>Not Found</html>"))).list("t")

        assertEquals(ApiResult.UnexpectedResponse(404), result)
        assertTrue(ApprovalText.failure(result).contains("승인 기능이 아직 없어요"))
    }

    @Test
    fun FR03_notifies_each_approval_once() {
        val first = NewApprovalDetector.detect(emptySet(), listOf(item("a"), item("b")))
        assertEquals(listOf("a", "b"), first.toNotify.map { it.approvalId })

        val again = NewApprovalDetector.detect(first.seen, listOf(item("a"), item("b"), item("c")))
        assertEquals(listOf("c"), again.toNotify.map { it.approvalId })

        val afterDecision = NewApprovalDetector.detect(again.seen, listOf(item("c")))
        assertEquals(emptyList<ApprovalItem>(), afterDecision.toNotify)
        assertEquals(setOf("c"), afterDecision.seen)
    }

    @Test
    fun FR03_network_error_keeps_polling() {
        val transport = FakeTransport(HttpResponse(200, "[]")).apply { error = IOException("down") }
        val result = ApprovalApi("http://srv", transport).list("t")

        assertEquals(ApiResult.NetworkError, result)
        assertEquals(PollPolicy.Next.CONTINUE, PollPolicy.after(result))
        assertEquals(PollPolicy.Next.CONTINUE, PollPolicy.after(ApiResult.UnexpectedResponse(404)))
    }

    @Test
    fun FR03_invalid_token_stops_polling() {
        val result = ApprovalApi("http://srv", FakeTransport(HttpResponse(401, """{"error":"INVALID_TOKEN"}"""))).list("bad")

        assertEquals(PollPolicy.Next.STOP, PollPolicy.after(result))
    }

    @Test
    fun FR03_remaining_time_label() {
        val now = Instant.parse("2026-10-23T10:00:00Z")

        assertEquals("9분 30초 남음", ApprovalText.remaining(now.plusSeconds(570), now))
        assertEquals("45초 남음", ApprovalText.remaining(now.plusSeconds(45), now))
        assertEquals("만료됨", ApprovalText.remaining(now, now))
        assertEquals("", ApprovalText.remaining(null, now))
        val expired = item("x").copy(pendingExpiresAt = now.minusSeconds(1))
        assertTrue(ApprovalText.isExpired(expired, now))
        assertNotEquals(true, ApprovalText.isExpired(item("y"), now))
    }

    @Test
    fun FR14_approval_item_has_no_requester() {
        val fields = ApprovalItem::class.java.declaredFields.map { it.name }

        assertTrue(fields.none { it.contains("user", ignoreCase = true) || it.contains("requester", ignoreCase = true) })
        assertEquals("열기", ApprovalText.actionLabel("open"))
        assertEquals("켜기", ApprovalText.actionLabel("on"))
        assertEquals("끄기", ApprovalText.actionLabel("off"))
    }
}
