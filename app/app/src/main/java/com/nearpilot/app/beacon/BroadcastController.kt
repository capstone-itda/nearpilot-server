package com.nearpilot.app.beacon

/** 송출 상태. Android가 광고 시작을 확인한 뒤에만 [Broadcasting]이 된다. */
sealed interface BroadcastState {
    data object Stopped : BroadcastState
    data object Starting : BroadcastState
    data object Broadcasting : BroadcastState

    /** 송출을 시작하지 못했다. */
    data class Blocked(val reason: AdvertiserAvailability) : BroadcastState

    /** 시작했던 송출이 끊겼다. 자동으로 다시 시작하지 않는다. */
    data class Interrupted(val reason: Interruption) : BroadcastState

    val isActive: Boolean get() = this == Starting || this == Broadcasting
}

sealed interface Interruption {
    data object BluetoothOff : Interruption
    data class StartFailed(val errorCode: Int) : Interruption
    data object ServiceStopped : Interruption
}

enum class AdvertiserAvailability { READY, BLUETOOTH_OFF, PERMISSION_MISSING, UNSUPPORTED }

/** BLE 광고를 실제로 다루는 쪽. 테스트에서는 fake로 바꾼다. */
interface BeaconAdvertiser {
    fun availability(): AdvertiserAvailability
    fun start(identifier: BeaconIdentifier, callback: Callback)
    fun stop()

    interface Callback {
        fun onStarted()
        fun onStartFailed(errorCode: Int)
    }
}

/** 송출 상태 전이. 광고는 한 번에 하나만 유지한다. */
class BroadcastController(
    private val advertiser: BeaconAdvertiser,
    private val onStateChanged: (BroadcastState) -> Unit,
) {
    var state: BroadcastState = BroadcastState.Stopped
        private set

    // 중지한 뒤에 늦게 도착한 시작 콜백을 무시하려고 시도마다 번호를 붙인다.
    private var attempt = 0

    fun start(identifier: BeaconIdentifier) {
        if (state.isActive) return

        val availability = advertiser.availability()
        if (availability != AdvertiserAvailability.READY) {
            update(BroadcastState.Blocked(availability))
            return
        }

        val current = ++attempt
        update(BroadcastState.Starting)
        advertiser.start(identifier, object : BeaconAdvertiser.Callback {
            override fun onStarted() {
                if (current == attempt && state == BroadcastState.Starting) {
                    update(BroadcastState.Broadcasting)
                }
            }

            override fun onStartFailed(errorCode: Int) {
                if (current == attempt && state == BroadcastState.Starting) {
                    update(BroadcastState.Interrupted(Interruption.StartFailed(errorCode)))
                }
            }
        })
    }

    fun stop() {
        if (state.isActive) stopAdvertiser()
        update(BroadcastState.Stopped)
    }

    fun onBluetoothOff() = interrupt(Interruption.BluetoothOff)

    fun onServiceStopping() = interrupt(Interruption.ServiceStopped)

    private fun interrupt(reason: Interruption) {
        if (!state.isActive) return
        stopAdvertiser()
        update(BroadcastState.Interrupted(reason))
    }

    private fun stopAdvertiser() {
        attempt++
        runCatching { advertiser.stop() }
    }

    private fun update(next: BroadcastState) {
        state = next
        onStateChanged(next)
    }
}
