package com.nearpilot.app.beacon

import org.junit.Assert.assertEquals
import org.junit.Test

class BroadcastControllerTest {

    private val identifier = BeaconIdentifier("e2c56db5-dffb-48d2-b060-d0f5a71096e0", 1, 1)
    private val advertiser = FakeAdvertiser()
    private val states = mutableListOf<BroadcastState>()
    private val controller = BroadcastController(advertiser) { states += it }

    @Test
    fun FR02_start_success_marks_broadcasting() {
        controller.start(identifier)
        assertEquals(BroadcastState.Starting, controller.state)

        advertiser.lastCallback!!.onStarted()

        assertEquals(listOf(BroadcastState.Starting, BroadcastState.Broadcasting), states)
        assertEquals(identifier, advertiser.startedWith.single())
    }

    @Test
    fun FR02_start_failure_marks_interrupted() {
        controller.start(identifier)

        advertiser.lastCallback!!.onStartFailed(2)

        assertEquals(BroadcastState.Interrupted(Interruption.StartFailed(2)), controller.state)
    }

    @Test
    fun FR02_bluetooth_off_marks_interrupted() {
        controller.start(identifier)
        advertiser.lastCallback!!.onStarted()

        controller.onBluetoothOff()

        assertEquals(BroadcastState.Interrupted(Interruption.BluetoothOff), controller.state)
        assertEquals(1, advertiser.stopCount)
    }

    @Test
    fun FR02_start_twice_keeps_single_advertisement() {
        controller.start(identifier)
        controller.start(identifier)
        advertiser.lastCallback!!.onStarted()
        controller.start(identifier)

        assertEquals(1, advertiser.startedWith.size)
        assertEquals(BroadcastState.Broadcasting, controller.state)
    }

    @Test
    fun FR02_unavailable_advertiser_blocks_start() {
        AdvertiserAvailability.entries.filter { it != AdvertiserAvailability.READY }.forEach {
            advertiser.availability = it

            controller.start(identifier)

            assertEquals(BroadcastState.Blocked(it), controller.state)
        }
        assertEquals(0, advertiser.startedWith.size)
    }

    @Test
    fun FR02_late_start_callback_after_stop_is_ignored() {
        controller.start(identifier)
        val staleCallback = advertiser.lastCallback!!
        controller.stop()

        staleCallback.onStarted()

        assertEquals(BroadcastState.Stopped, controller.state)
    }

    @Test
    fun FR02_service_stop_while_broadcasting_marks_interrupted() {
        controller.start(identifier)
        advertiser.lastCallback!!.onStarted()

        controller.onServiceStopping()

        assertEquals(BroadcastState.Interrupted(Interruption.ServiceStopped), controller.state)
    }

    @Test
    fun FR02_bluetooth_off_while_stopped_changes_nothing() {
        controller.onBluetoothOff()

        assertEquals(BroadcastState.Stopped, controller.state)
        assertEquals(emptyList<BroadcastState>(), states)
    }

    private class FakeAdvertiser : BeaconAdvertiser {
        var availability = AdvertiserAvailability.READY
        val startedWith = mutableListOf<BeaconIdentifier>()
        var lastCallback: BeaconAdvertiser.Callback? = null
        var stopCount = 0

        override fun availability() = availability

        override fun start(identifier: BeaconIdentifier, callback: BeaconAdvertiser.Callback) {
            startedWith += identifier
            lastCallback = callback
        }

        override fun stop() {
            stopCount++
        }
    }
}
