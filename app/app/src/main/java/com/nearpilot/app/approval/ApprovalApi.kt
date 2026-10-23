package com.nearpilot.app.approval

import com.nearpilot.app.account.ApiResult
import com.nearpilot.app.account.HttpTransport
import com.nearpilot.app.account.RestClient
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant

/** 승인 요청 한 건 (§5 REST "승인"). 요청자 정보는 없다 (NFR-08). */
data class ApprovalItem(
    val approvalId: String,
    val nodeId: String,
    val action: String,
    val status: String,
    val createdAt: Instant?,
    val pendingExpiresAt: Instant?,
    val validUntil: Instant?,
)

enum class ApprovalChoice(val wire: String) { APPROVE("approved"), DENY("denied") }

/** §5 REST "승인"의 세 경로 (#183). */
class ApprovalApi(baseUrl: String, transport: HttpTransport) {
    private val client = RestClient(baseUrl, transport)

    fun list(token: String): ApiResult<List<ApprovalItem>> =
        client.call("GET", "/approvals", body = null, token = token, successStatus = 200, parse = ::parseList)

    fun get(token: String, approvalId: String): ApiResult<ApprovalItem> =
        client.call("GET", "/approvals/${encode(approvalId)}", null, token, 200) { parseItem(JSONObject(it)) }

    fun decide(token: String, approvalId: String, choice: ApprovalChoice): ApiResult<ApprovalItem> {
        val body = JSONObject().put("decision", choice.wire).toString()
        return client.call("POST", "/approvals/${encode(approvalId)}/decision", body, token, 200) {
            parseItem(JSONObject(it))
        }
    }

    companion object {
        /** 배열 또는 `{"approvals": [...]}`를 읽는다 (spec 7절 가정). */
        fun parseList(text: String): List<ApprovalItem>? {
            val trimmed = text.trim()
            val array = when {
                trimmed.startsWith("[") -> JSONArray(trimmed)
                trimmed.startsWith("{") -> JSONObject(trimmed).optJSONArray("approvals") ?: return null
                else -> return null
            }
            return (0 until array.length()).map { parseItem(array.getJSONObject(it)) ?: return null }
        }

        fun parseItem(json: JSONObject): ApprovalItem? {
            val id = json.optString("approval_id").takeIf { it.isNotEmpty() } ?: return null
            return ApprovalItem(
                approvalId = id,
                nodeId = json.optString("node_id"),
                action = json.optString("action"),
                status = json.optString("status"),
                createdAt = instant(json, "created_at"),
                pendingExpiresAt = instant(json, "pending_expires_at"),
                validUntil = instant(json, "valid_until"),
            )
        }

        private fun instant(json: JSONObject, key: String): Instant? {
            if (json.isNull(key)) return null
            return runCatching { Instant.parse(json.optString(key)) }
                .recoverCatching { java.time.OffsetDateTime.parse(json.optString(key)).toInstant() }
                .getOrNull()
        }

        private fun encode(id: String): String = java.net.URLEncoder.encode(id, Charsets.UTF_8.name()).replace("+", "%20")
    }
}
