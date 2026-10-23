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

    private companion object {
        const val KEY_UUID = "uuid"
        const val KEY_MAJOR = "major"
        const val KEY_MINOR = "minor"
    }
}
