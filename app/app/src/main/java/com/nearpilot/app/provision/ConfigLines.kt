package com.nearpilot.app.provision

import java.security.SecureRandom

/**
 * 서버가 주는 기기 접속 설정 묶음 (#234, §5 REST `GET /admin/nodes/{node_id}/provisioning`).
 * 기기 비밀번호를 담으므로 저장하지 않고 화면을 떠나면 버린다. 문자열로 바꿀 때 비밀번호를 쓰지 않는다 (NFR-08).
 */
data class ProvisioningBundle(
    val nodeId: String,
    val mqttUser: String,
    val mqttPassword: String,
    val mqttHost: String,
    val mqttPort: Int,
    val capability: String,
    val occupancy: String,
) {
    override fun toString(): String =
        "ProvisioningBundle(nodeId=$nodeId, mqttUser=$mqttUser, mqttHost=$mqttHost, mqttPort=$mqttPort, " +
            "capability=$capability, occupancy=$occupancy)"
}

/** 기기 응답 줄 하나 (USB 시리얼 주입과 같은 형식, architecture.md §5 "BLE 기기 설정"). */
sealed interface ConfigReply {
    data class Ready(val nonce: String) : ConfigReply
    data class Staged(val key: String, val bytes: Int) : ConfigReply
    data class Rejected(val reason: String, val key: String?) : ConfigReply
    data class Saved(val generation: Long, val nodeId: String) : ConfigReply
    data class Other(val line: String) : ConfigReply
}

/** 보낼 줄과, 다음 줄로 넘어가도 되는 응답. */
class ConfigStep(val line: String, val accepts: (ConfigReply) -> Boolean) {
    /** 로그나 화면에 쓸 때 값(16진수)을 지운다. */
    val label: String = line.split(' ').take(2).joinToString(" ")
}

/** 설정 줄 만들기와 응답 해석. 기기 펌웨어 `serialCommand`와 같은 규칙이다. */
object ConfigLines {
    /** 펌웨어가 BLE로 받는 한 줄의 최대 길이. 넘으면 `line_too_long`으로 거부한다. */
    const val MAX_LINE_BYTES = 200
    const val SSID_MAX_BYTES = 32
    const val WIFI_PASSWORD_MAX_BYTES = 64

    fun hex(value: String): String =
        value.toByteArray(Charsets.UTF_8).joinToString("") { "%02x".format(it.toInt() and 0xff) }

    /** `CONFIG_BEGIN`의 32자리 16진수 nonce. */
    fun newNonce(random: SecureRandom = SecureRandom()): String =
        ByteArray(16).also(random::nextBytes).joinToString("") { "%02x".format(it.toInt() and 0xff) }

    /** Wi-Fi 입력이 펌웨어 규칙에 맞지 않으면 사유를 돌려준다. 맞으면 null. */
    fun wifiProblem(ssid: String, password: String): String? {
        val ssidBytes = ssid.toByteArray(Charsets.UTF_8).size
        val passwordBytes = password.toByteArray(Charsets.UTF_8).size
        return when {
            ssidBytes !in 1..SSID_MAX_BYTES -> "Wi-Fi 이름은 1~${SSID_MAX_BYTES}바이트예요."
            passwordBytes > WIFI_PASSWORD_MAX_BYTES -> "Wi-Fi 비밀번호는 ${WIFI_PASSWORD_MAX_BYTES}바이트 이하예요."
            else -> null
        }
    }

    /** NVS 설정 키 9개. USB 주입(`nearpilot provision`)과 같은 값이다. */
    fun values(bundle: ProvisioningBundle, ssid: String, password: String): List<Pair<String, String>> = listOf(
        "node_id" to bundle.nodeId,
        "mqtt_user" to bundle.mqttUser,
        "mqtt_password" to bundle.mqttPassword,
        "wifi_ssid" to ssid,
        "wifi_password" to password,
        "mqtt_host" to bundle.mqttHost,
        "mqtt_port" to bundle.mqttPort.toString(),
        "capability" to bundle.capability,
        "occupancy" to bundle.occupancy,
    )

    /** BEGIN, SET 9개, COMMIT 순서. 각 줄은 기대한 응답을 받은 뒤에 보낸다. */
    fun plan(bundle: ProvisioningBundle, ssid: String, password: String, nonce: String): List<ConfigStep> {
        val sets = values(bundle, ssid, password).map { (key, value) ->
            val bytes = value.toByteArray(Charsets.UTF_8).size
            ConfigStep("CONFIG_SET $key ${hex(value)}") { it == ConfigReply.Staged(key, bytes) }
        }
        val steps = listOf(ConfigStep("CONFIG_BEGIN $nonce") { it == ConfigReply.Ready(nonce) }) + sets +
            ConfigStep("CONFIG_COMMIT $nonce") { it is ConfigReply.Saved && it.nodeId == bundle.nodeId }
        require(steps.all { it.line.toByteArray(Charsets.UTF_8).size <= MAX_LINE_BYTES }) { "line_too_long" }
        return steps
    }

    fun parse(line: String): ConfigReply {
        val words = line.trim().split(' ')
        val fields = words.drop(1).mapNotNull { word ->
            word.indexOf('=').takeIf { it > 0 }?.let { word.substring(0, it) to word.substring(it + 1) }
        }.toMap()
        return when (words.first()) {
            "CONFIG_READY" -> fields["nonce"]?.let { ConfigReply.Ready(it) }
            "CONFIG_STAGED" -> {
                val key = fields["key"]
                val bytes = fields["bytes"]?.toIntOrNull()
                if (key != null && bytes != null) ConfigReply.Staged(key, bytes) else null
            }
            "CONFIG_REJECTED" -> fields["reason"]?.let { ConfigReply.Rejected(it, fields["key"]) }
            "CONFIG_SAVED" -> {
                val generation = fields["generation"]?.toLongOrNull()
                val node = fields["node"]
                if (generation != null && node != null) ConfigReply.Saved(generation, node) else null
            }
            else -> null
        } ?: ConfigReply.Other(line)
    }

    /** 사용자에게 보여 줄 거부 사유. */
    fun rejectionText(reply: ConfigReply.Rejected): String = when (reply.reason) {
        "invalid" -> "기기가 설정 값(${reply.key ?: "?"})을 받지 않았어요."
        "no_session" -> "기기의 설정 세션이 끊겼어요. 다시 시도해 주세요."
        "line_too_long" -> "설정 줄이 너무 길어요."
        "write_failed", "storage_unavailable" -> "기기 저장소에 쓰지 못했어요."
        else -> "기기가 설정을 거부했어요 (${reply.reason})."
    }
}

/** 20바이트 조각으로 오는 알림을 줄바꿈 기준으로 다시 잇는다. */
class LineJoiner {
    // 바이트로 모은다. 조각 경계가 UTF-8 문자 가운데에 걸려도 줄을 깨지 않는다.
    private val buffer = java.io.ByteArrayOutputStream()

    fun feed(chunk: ByteArray): List<String> {
        val lines = mutableListOf<String>()
        for (byte in chunk) {
            if (byte == '\n'.code.toByte()) {
                val line = String(buffer.toByteArray(), Charsets.UTF_8).trimEnd('\r')
                buffer.reset()
                if (line.isNotEmpty()) lines += line
            } else {
                buffer.write(byte.toInt())
            }
        }
        return lines
    }
}
