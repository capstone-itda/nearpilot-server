package com.nearpilot.app.account

import android.content.Context
import com.nearpilot.app.beacon.BeaconIdentifier

/** 서버 주소, 토큰, 서버에 마지막으로 등록한 비콘. 앱 전용 저장소라 다른 앱은 읽지 못한다. */
class AccountStore(context: Context) {
    private val prefs = context.getSharedPreferences("account", Context.MODE_PRIVATE)

    var serverUrl: String?
        get() = prefs.getString(KEY_SERVER, null)
        set(value) = prefs.edit().putString(KEY_SERVER, value).apply()

    var token: String?
        get() = prefs.getString(KEY_TOKEN, null)
        set(value) = prefs.edit().putString(KEY_TOKEN, value).remove(KEY_REGISTERED).apply()

    var registeredBeacon: String?
        get() = prefs.getString(KEY_REGISTERED, null)
        private set(value) = prefs.edit().putString(KEY_REGISTERED, value).apply()

    fun markRegistered(identifier: BeaconIdentifier) {
        registeredBeacon = BeaconSync.keyOf(identifier)
    }

    private companion object {
        const val KEY_SERVER = "server_url"
        const val KEY_TOKEN = "token"
        const val KEY_REGISTERED = "registered_beacon"
    }
}

/** 서버에 등록한 비콘과 지금 송출하는 비콘의 비교. */
object BeaconSync {
    fun keyOf(identifier: BeaconIdentifier): String = "${identifier.uuid}|${identifier.major}|${identifier.minor}"

    fun isRegistered(registeredKey: String?, current: BeaconIdentifier): Boolean = registeredKey == keyOf(current)
}
