package com.nearpilot.app.beacon

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.UUID
import kotlin.random.Random

class StatusPresentationTest {

    @Test
    fun FR02_broadcasting_is_shown_as_ok() {
        val p = BroadcastState.Broadcasting.presentation()

        assertEquals(StatusTone.OK, p.tone)
        assertEquals("송출 중", p.title)
        assertEquals(StatusAction.NONE, p.action)
    }

    @Test
    fun FR02_only_confirmed_broadcast_is_ok() {
        val notOk = listOf(
            BroadcastState.Stopped,
            BroadcastState.Starting,
            BroadcastState.Blocked(AdvertiserAvailability.BLUETOOTH_OFF),
            BroadcastState.Interrupted(Interruption.ServiceStopped),
        )
        notOk.forEach { assertTrue("$it", it.presentation().tone != StatusTone.OK) }
    }

    @Test
    fun FR02_every_problem_offers_a_fix_or_explanation() {
        val expected = mapOf(
            BroadcastState.Blocked(AdvertiserAvailability.BLUETOOTH_OFF) to StatusAction.OPEN_BLUETOOTH_SETTINGS,
            BroadcastState.Blocked(AdvertiserAvailability.PERMISSION_MISSING) to StatusAction.REQUEST_PERMISSION,
            BroadcastState.Blocked(AdvertiserAvailability.UNSUPPORTED) to StatusAction.NONE,
            BroadcastState.Interrupted(Interruption.BluetoothOff) to StatusAction.OPEN_BLUETOOTH_SETTINGS,
            BroadcastState.Interrupted(Interruption.StartFailed(2)) to StatusAction.RETRY,
            BroadcastState.Interrupted(Interruption.ServiceStopped) to StatusAction.RETRY,
        )
        expected.forEach { (state, action) ->
            val p = state.presentation()
            assertEquals("$state", StatusTone.PROBLEM, p.tone)
            assertEquals("$state", action, p.action)
            assertTrue("$state", p.message.isNotBlank())
        }
    }

    @Test
    fun FR02_start_failure_shows_error_code() {
        val p = BroadcastState.Interrupted(Interruption.StartFailed(4)).presentation()

        assertTrue(p.message.contains("오류 코드 4"))
    }

    @Test
    fun FR02_generated_identifier_is_valid() {
        val fixed = UUID.fromString("E2C56DB5-DFFB-48D2-B060-D0F5A71096E0")

        val identifier = BeaconIdentifier.generate({ fixed }, Random(7))

        assertEquals("e2c56db5-dffb-48d2-b060-d0f5a71096e0", identifier.uuid)
        assertEquals(1, identifier.major)
        assertTrue(identifier.minor in 1..65535)
        assertEquals(
            IdentifierParseResult.Valid(identifier),
            BeaconIdentifier.parse(identifier.uuid, identifier.major.toString(), identifier.minor.toString()),
        )
    }
}
