package com.nearpilot.app.ble

import com.nearpilot.app.beacon.BroadcastState
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** 서비스의 송출 상태를 화면에 전달한다. */
object BroadcastStatus {
    private val mutableState = MutableStateFlow<BroadcastState>(BroadcastState.Stopped)
    val state: StateFlow<BroadcastState> = mutableState.asStateFlow()

    fun update(state: BroadcastState) {
        mutableState.value = state
    }
}
