package com.nearpilot.app.account

import com.nearpilot.app.beacon.BeaconIdentifier
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

class AccountApiTest {

    private class FakeTransport(var response: HttpResponse? = null, var error: IOException? = null) : HttpTransport {
        data class Sent(val method: String, val url: String, val body: String?, val token: String?)

        val sent = mutableListOf<Sent>()

        override fun send(method: String, url: String, body: String?, token: String?): HttpResponse {
            sent += Sent(method, url, body, token)
            error?.let { throw it }
            return response!!
        }
    }

    private val identifier = BeaconIdentifier("e2c56db5-dffb-48d2-b060-d0f5a71096e0", 1, 2)

    @Test
    fun FR01_normalizes_server_address() {
        assertEquals("http://192.168.0.10:8000", ServerAddress.normalize(" 192.168.0.10:8000/ "))
        assertEquals("https://np.local", ServerAddress.normalize("https://np.local"))
        assertEquals("http://192.168.0.10:8000/mcp", ServerAddress.mcpUrl("http://192.168.0.10:8000"))
    }

    @Test
    fun FR01_rejects_invalid_server_address() {
        listOf("", "   ", "http://", "ftp://np.local", "http:// bad host").forEach {
            assertNull("address=$it", ServerAddress.normalize(it))
        }
    }

    @Test
    fun FR01_create_account_posts_name_and_returns_token() {
        val transport = FakeTransport(HttpResponse(201, """{"token":"t-123"}"""))

        val result = AccountApi("http://srv", transport).createAccount("방문자")

        assertEquals(ApiResult.Success("t-123"), result)
        val sent = transport.sent.single()
        assertEquals("POST", sent.method)
        assertEquals("http://srv/accounts", sent.url)
        assertEquals("방문자", JSONObject(sent.body!!).getString("name"))
        assertNull(sent.token)
    }

    @Test
    fun FR01_rejects_invalid_name_before_request() {
        assertNull(AccountApi.normalizeName("   "))
        assertNull(AccountApi.normalizeName("x".repeat(65)))
        assertEquals("방문자", AccountApi.normalizeName("  방문자 "))
    }

    @Test
    fun FR01_create_account_rejection_keeps_reason() {
        val transport = FakeTransport(HttpResponse(409, """{"error":"ACCOUNT_ALREADY_EXISTS"}"""))

        val result = AccountApi("http://srv", transport).createAccount("방문자")

        assertEquals(ApiResult.Rejected(409, "ACCOUNT_ALREADY_EXISTS"), result)
    }

    @Test
    fun FR01_put_beacon_sends_bearer_token() {
        val transport = FakeTransport(HttpResponse(200, """{"uuid":"x","major":1,"minor":2,"is_active":true}"""))

        val result = AccountApi("http://srv", transport).putBeacon("t-123", identifier)

        assertEquals(ApiResult.Success(Unit), result)
        val sent = transport.sent.single()
        assertEquals("PUT", sent.method)
        assertEquals("http://srv/me/beacon", sent.url)
        assertEquals("t-123", sent.token)
        val body = JSONObject(sent.body!!)
        assertEquals(identifier.uuid, body.getString("uuid"))
        assertEquals(1, body.getInt("major"))
        assertEquals(2, body.getInt("minor"))

        transport.response = HttpResponse(401, """{"error":"INVALID_TOKEN"}""")
        assertEquals(ApiResult.Rejected(401, "INVALID_TOKEN"), AccountApi("http://srv", transport).putBeacon("bad", identifier))
    }

    @Test
    fun FR01_network_error_is_reported() {
        val transport = FakeTransport(error = IOException("timeout"))

        assertEquals(ApiResult.NetworkError, AccountApi("http://srv", transport).createAccount("방문자"))
        assertEquals(ApiResult.NetworkError, AccountApi("http://srv", transport).putBeacon("t", identifier))
    }

    @Test
    fun FR01_unexpected_response_is_reported() {
        val cases = listOf(
            HttpResponse(404, "<html>Not Found</html>"),
            HttpResponse(201, """{"other":1}"""),
            HttpResponse(500, """{}"""),
        )
        cases.forEach {
            val result = AccountApi("http://srv", FakeTransport(it)).createAccount("방문자")
            assertEquals("$it", ApiResult.UnexpectedResponse(it.status), result)
        }
    }

    @Test
    fun FR01_reason_messages_tell_what_to_do() {
        val reasons = listOf(
            "INVALID_TOKEN", "BEACON_ALREADY_REGISTERED", "ACCOUNT_ALREADY_EXISTS", "INVALID_INPUT",
            "INVALID_BEACON", "STATE_CONFLICT", "INTERNAL_ERROR",
        )
        reasons.forEach {
            val message = ReasonMessages.ofReason(it)
            assertFalse("$it 는 코드를 그대로 보여 주면 안 된다", message.contains(it))
            assertTrue(message.endsWith("요."))
        }
        assertTrue(ReasonMessages.of(ApiResult.NetworkError).contains("같은 Wi-Fi"))
        assertTrue(ReasonMessages.ofReason("BEACON_ALREADY_REGISTERED").contains("새 값 만들기"))
        assertTrue(ReasonMessages.ofReason("UNKNOWN_CODE").contains("UNKNOWN_CODE"))
    }

    @Test
    fun FR01_beacon_sync_state() {
        assertFalse(BeaconSync.isRegistered(null, identifier))
        assertTrue(BeaconSync.isRegistered(BeaconSync.keyOf(identifier), identifier))
        assertFalse(BeaconSync.isRegistered(BeaconSync.keyOf(identifier), identifier.copy(minor = 3)))
    }
}
