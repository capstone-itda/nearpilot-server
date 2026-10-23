package com.nearpilot.app.beacon

import android.content.Context

/** 비콘 식별자를 폰 안에만 저장한다. 서버와 주고받지 않는다. */
class IdentifierStore(context: Context) {
    private val prefs = context.getSharedPreferences("beacon_identifier", Context.MODE_PRIVATE)

    fun load(): BeaconIdentifier? {
        val parsed = BeaconIdentifier.parse(
            prefs.getString(KEY_UUID, null) ?: return null,
            prefs.getInt(KEY_MAJOR, -1).toString(),
            prefs.getInt(KEY_MINOR, -1).toString(),
        )
        return (parsed as? IdentifierParseResult.Valid)?.identifier
    }

    fun save(identifier: BeaconIdentifier) {
        prefs.edit()
            .putString(KEY_UUID, identifier.uuid)
            .putInt(KEY_MAJOR, identifier.major)
            .putInt(KEY_MINOR, identifier.minor)
            .apply()
    }

    /** 저장한 값이 없으면 새로 만들어 저장한다. */
    fun loadOrCreate(): BeaconIdentifier = load() ?: BeaconIdentifier.generate().also(::save)

    var introSeen: Boolean
        get() = prefs.getBoolean(KEY_INTRO_SEEN, false)
        set(value) = prefs.edit().putBoolean(KEY_INTRO_SEEN, value).apply()

    /** 권한을 한 번이라도 물었는지. 묻기 전에는 '다시 묻지 않음'과 구분할 수 없다. */
    var permissionAsked: Boolean
        get() = prefs.getBoolean(KEY_PERMISSION_ASKED, false)
        set(value) = prefs.edit().putBoolean(KEY_PERMISSION_ASKED, value).apply()

    private companion object {
        const val KEY_PERMISSION_ASKED = "permission_asked"
        const val KEY_INTRO_SEEN = "intro_seen"
        const val KEY_UUID = "uuid"
        const val KEY_MAJOR = "major"
        const val KEY_MINOR = "minor"
    }
}
