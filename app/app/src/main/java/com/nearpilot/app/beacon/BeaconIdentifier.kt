package com.nearpilot.app.beacon

/** iBeacon 식별자 `(uuid, major, minor)`. uuid는 하이픈을 포함한 소문자 문자열이다. */
data class BeaconIdentifier(val uuid: String, val major: Int, val minor: Int) {
    init {
        require(UUID_PATTERN.matches(uuid) && uuid == uuid.lowercase()) { "uuid must be lowercase hyphenated" }
        require(major in ID_RANGE) { "major out of range" }
        require(minor in ID_RANGE) { "minor out of range" }
    }

    companion object {
        private val UUID_PATTERN =
            Regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
        private val ID_RANGE = 0..65535

        /** 처음 켰을 때 쓸 값. 무작위 UUID, major 1, 무작위 minor(1~65535)다. */
        fun generate(
            newUuid: () -> java.util.UUID = java.util.UUID::randomUUID,
            random: kotlin.random.Random = kotlin.random.Random.Default,
        ): BeaconIdentifier = BeaconIdentifier(newUuid().toString().lowercase(), 1, random.nextInt(1, 65536))

        /** 입력 문자열을 검증한다. 틀린 칸을 모두 돌려준다. */
        fun parse(uuid: String, major: String, minor: String): IdentifierParseResult {
            val normalizedUuid = uuid.trim().lowercase()
            val majorValue = major.trim().toIntOrNull()?.takeIf { it in ID_RANGE }
            val minorValue = minor.trim().toIntOrNull()?.takeIf { it in ID_RANGE }

            val errors = buildSet {
                if (!UUID_PATTERN.matches(normalizedUuid)) add(IdentifierField.UUID)
                if (majorValue == null) add(IdentifierField.MAJOR)
                if (minorValue == null) add(IdentifierField.MINOR)
            }
            if (errors.isNotEmpty()) return IdentifierParseResult.Invalid(errors)
            return IdentifierParseResult.Valid(BeaconIdentifier(normalizedUuid, majorValue!!, minorValue!!))
        }
    }
}

enum class IdentifierField { UUID, MAJOR, MINOR }

sealed interface IdentifierParseResult {
    data class Valid(val identifier: BeaconIdentifier) : IdentifierParseResult
    data class Invalid(val errors: Set<IdentifierField>) : IdentifierParseResult
}
