package com.nearpilot.app.beacon

/**
 * iBeacon 제조사 데이터. 회사 ID(Apple 0x004C)는 Android가 따로 붙이고,
 * 본문은 `02 15` + UUID 16바이트 + major 2바이트 + minor 2바이트 + 측정 전력 1바이트다.
 * major와 minor는 빅엔디언이다.
 */
object IBeaconPacket {
    const val APPLE_COMPANY_ID = 0x004C
    const val DATA_LENGTH = 23

    fun manufacturerData(identifier: BeaconIdentifier, measuredPower: Int): ByteArray {
        require(measuredPower in Byte.MIN_VALUE..Byte.MAX_VALUE) { "measuredPower must fit in a signed byte" }

        val data = ByteArray(DATA_LENGTH)
        data[0] = 0x02
        data[1] = 0x15
        identifier.uuid.replace("-", "").chunked(2).forEachIndexed { i, hex ->
            data[2 + i] = hex.toInt(16).toByte()
        }
        data[18] = (identifier.major shr 8).toByte()
        data[19] = identifier.major.toByte()
        data[20] = (identifier.minor shr 8).toByte()
        data[21] = identifier.minor.toByte()
        data[22] = measuredPower.toByte()
        return data
    }
}
