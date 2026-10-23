package com.nearpilot.app.beacon

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class BeaconIdentifierTest {

    @Test
    fun FR02_builds_ibeacon_manufacturer_data() {
        val identifier = valid("E2C56DB5-DFFB-48D2-B060-D0F5A71096E0", "258", "65535")

        val data = IBeaconPacket.manufacturerData(identifier, measuredPower = -59)

        val expected = byteArrayOf(
            0x02, 0x15,
            0xe2.toByte(), 0xc5.toByte(), 0x6d, 0xb5.toByte(), 0xdf.toByte(), 0xfb.toByte(), 0x48, 0xd2.toByte(),
            0xb0.toByte(), 0x60, 0xd0.toByte(), 0xf5.toByte(), 0xa7.toByte(), 0x10, 0x96.toByte(), 0xe0.toByte(),
            0x01, 0x02, // major 258, 빅엔디언
            0xff.toByte(), 0xff.toByte(), // minor 65535
            (-59).toByte(),
        )
        assertEquals(0x004C, IBeaconPacket.APPLE_COMPANY_ID)
        assertArrayEquals(expected, data)
    }

    @Test
    fun FR02_normalizes_uuid_to_lowercase() {
        val identifier = valid(" E2C56DB5-DFFB-48D2-B060-D0F5A71096E0 ", "0", "0")

        assertEquals("e2c56db5-dffb-48d2-b060-d0f5a71096e0", identifier.uuid)
    }

    @Test
    fun FR02_rejects_invalid_uuid() {
        listOf("", "not-a-uuid", "e2c56db5dffb48d2b060d0f5a71096e0", "e2c56db5-dffb-48d2-b060-d0f5a71096eg").forEach {
            assertEquals(
                "uuid=$it",
                IdentifierParseResult.Invalid(setOf(IdentifierField.UUID)),
                BeaconIdentifier.parse(it, "1", "1"),
            )
        }
    }

    @Test
    fun FR02_rejects_out_of_range_major_minor() {
        val uuid = "e2c56db5-dffb-48d2-b060-d0f5a71096e0"

        listOf("-1", "65536", "1.5", "abc", "").forEach {
            assertEquals(
                "value=$it",
                IdentifierParseResult.Invalid(setOf(IdentifierField.MAJOR, IdentifierField.MINOR)),
                BeaconIdentifier.parse(uuid, it, it),
            )
        }
        assertEquals(
            IdentifierParseResult.Valid(BeaconIdentifier(uuid, 0, 65535)),
            BeaconIdentifier.parse(uuid, "0", "65535"),
        )
    }

    private fun valid(uuid: String, major: String, minor: String): BeaconIdentifier =
        (BeaconIdentifier.parse(uuid, major, minor) as IdentifierParseResult.Valid).identifier
}
