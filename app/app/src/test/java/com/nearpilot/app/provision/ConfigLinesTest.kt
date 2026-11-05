package com.nearpilot.app.provision

import com.nearpilot.app.account.ApiResult
import com.nearpilot.app.account.HttpResponse
import com.nearpilot.app.account.HttpTransport
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** 앱 BLE 기기 설정의 줄 형식과 응답 해석 (FR-12A, #234, specs/iot-ble-provisioning/spec.md). */
class ConfigLinesTest {

    private val bundle = ProvisioningBundle(
        "light-1", "light-1", "node-secret-0123", "nearpilot.local", 18883, "light", "shared")
    private val nonce = "0123456789abcdef0123456789abcdef"

    @Test
    fun FR12A_plan_matches_usb_provisioning_lines() {
        val lines = ConfigLines.plan(bundle, "lab-ap", "pw", nonce).map { it.line }

        assertEquals("CONFIG_BEGIN $nonce", lines.first())
        assertEquals("CONFIG_COMMIT $nonce", lines.last())
        assertEquals(
            listOf("node_id", "mqtt_user", "mqtt_password", "wifi_ssid", "wifi_password",
                "mqtt_host", "mqtt_port", "capability", "occupancy"),
            lines.drop(1).dropLast(1).map { it.split(' ')[1] },
        )
        assertTrue("CONFIG_SET wifi_ssid 6c61622d6170" in lines)
        assertTrue("CONFIG_SET mqtt_port 3138383833" in lines)
    }

    @Test
    fun FR12A_hex_encodes_utf8_and_empty_password() {
        assertEquals("ed959c", ConfigLines.hex("한"))
        assertEquals("", ConfigLines.hex(""))
        assertTrue("CONFIG_SET wifi_password " in ConfigLines.plan(bundle, "ap", "", nonce).map { it.line })
    }

    @Test
    fun FR12A_nonce_is_32_hex() {
        val first = ConfigLines.newNonce()
        assertTrue(Regex("[0-9a-f]{32}").matches(first))
        assertTrue(first != ConfigLines.newNonce())
    }

    @Test
    fun FR12A_longest_values_fit_one_line() {
        val ssid = "s".repeat(ConfigLines.SSID_MAX_BYTES)
        val password = "p".repeat(ConfigLines.WIFI_PASSWORD_MAX_BYTES)
        val steps = ConfigLines.plan(bundle.copy(mqttPassword = "m".repeat(64)), ssid, password, nonce)
        assertTrue(steps.all { it.line.length <= ConfigLines.MAX_LINE_BYTES })
        assertNull(ConfigLines.wifiProblem(ssid, password))
        assertNotNull(ConfigLines.wifiProblem("", "x"))
        assertNotNull(ConfigLines.wifiProblem(ssid + "s", "x"))
        assertNotNull(ConfigLines.wifiProblem("ap", password + "p"))
    }

    @Test
    fun FR12A_steps_accept_only_the_expected_reply() {
        val steps = ConfigLines.plan(bundle, "lab-ap", "pw", nonce)

        assertTrue(steps[0].accepts(ConfigLines.parse("CONFIG_READY nonce=$nonce")))
        assertFalse(steps[0].accepts(ConfigLines.parse("CONFIG_READY nonce=ffff")))
        assertTrue(steps[4].accepts(ConfigLines.parse("CONFIG_STAGED key=wifi_ssid bytes=6")))
        assertFalse(steps[4].accepts(ConfigLines.parse("CONFIG_STAGED key=wifi_ssid bytes=5")))
        assertTrue(steps.last().accepts(ConfigLines.parse("CONFIG_SAVED generation=3 node=light-1 restarting=1")))
        assertFalse(steps.last().accepts(ConfigLines.parse("CONFIG_SAVED generation=3 node=light-2 restarting=1")))
    }

    @Test
    fun FR12A_parses_replies() {
        assertEquals(ConfigReply.Rejected("invalid", "mqtt_host"),
            ConfigLines.parse("CONFIG_REJECTED reason=invalid key=mqtt_host"))
        assertEquals(ConfigReply.Rejected("line_too_long", null), ConfigLines.parse("CONFIG_REJECTED reason=line_too_long"))
        assertEquals(ConfigReply.Saved(2, "locker-1"), ConfigLines.parse("CONFIG_SAVED generation=2 node=locker-1 restarting=1"))
        assertEquals(ConfigReply.Other("CLOCK_SYNCED offset_ms=3"), ConfigLines.parse("CLOCK_SYNCED offset_ms=3"))
        assertEquals(ConfigReply.Other("CONFIG_STAGED key=x"), ConfigLines.parse("CONFIG_STAGED key=x"))
    }

    @Test
    fun FR12A_step_label_hides_value() {
        val label = ConfigLines.plan(bundle, "lab-ap", "pw", nonce)[3].label
        assertEquals("CONFIG_SET mqtt_password", label)
    }

    @Test
    fun FR12A_joiner_rebuilds_lines_from_20_byte_chunks() {
        val text = "CONFIG_STAGED key=mqtt_password bytes=16\nCONFIG_READY nonce=$nonce\n한글 줄\n"
        val bytes = text.toByteArray(Charsets.UTF_8)
        val joiner = LineJoiner()
        val lines = bytes.toList().chunked(20).flatMap { joiner.feed(it.toByteArray()) }
        assertEquals(listOf("CONFIG_STAGED key=mqtt_password bytes=16", "CONFIG_READY nonce=$nonce", "한글 줄"), lines)
        assertEquals(emptyList<String>(), joiner.feed("CONFIG_SAVED".toByteArray()))
        assertEquals(listOf("CONFIG_SAVED"), joiner.feed("\r\n".toByteArray()))
    }

    @Test
    fun NFR08_bundle_string_hides_password() {
        assertFalse("node-secret-0123" in bundle.toString())
    }

    private class FakeTransport(val response: HttpResponse) : HttpTransport {
        val urls = mutableListOf<Pair<String, String?>>()
        override fun send(method: String, url: String, body: String?, token: String?): HttpResponse {
            urls += "$method $url" to token
            return response
        }
    }

    @Test
    fun FR12A_api_reads_bundle_with_owner_token() {
        val transport = FakeTransport(HttpResponse(200, """
            {"node_id":"light-1","mqtt_user":"light-1","mqtt_password":"node-secret-0123",
             "mqtt_host":"nearpilot.local","mqtt_port":18883,"capability":"light","occupancy":"shared"}
        """.trimIndent()))

        val result = ProvisioningApi("http://srv", transport).bundle("owner-token", "light-1")

        assertEquals(ApiResult.Success(bundle), result)
        assertEquals(listOf("GET http://srv/admin/nodes/light-1/provisioning" to "owner-token"), transport.urls)
    }

    @Test
    fun FR12A_api_maps_rejections() {
        val rejected = ProvisioningApi("http://srv", FakeTransport(HttpResponse(403, """{"error":"NOT_OWNER"}""")))
            .bundle("t", "light-1")
        assertEquals(ApiResult.Rejected(403, "NOT_OWNER"), rejected)
        assertTrue(ProvisioningApi.failureText(rejected).contains("소유자"))
        val broken = ProvisioningApi("http://srv", FakeTransport(HttpResponse(200, """{"node_id":"light-1"}""")))
            .bundle("t", "light-1")
        assertEquals(ApiResult.UnexpectedResponse(200), broken)
    }
}
