package com.nearpilot.app.provision

import com.nearpilot.app.account.ApiResult
import com.nearpilot.app.account.HttpTransport
import com.nearpilot.app.account.RestClient
import org.json.JSONObject
import java.net.URLEncoder

/** §5 REST `GET /admin/nodes/{node_id}/provisioning` (#234). 소유자 토큰이 필요하다. */
class ProvisioningApi(baseUrl: String, transport: HttpTransport) {
    private val client = RestClient(baseUrl, transport)

    fun bundle(token: String, nodeId: String): ApiResult<ProvisioningBundle> {
        val path = "/admin/nodes/${URLEncoder.encode(nodeId, "UTF-8").replace("+", "%20")}/provisioning"
        return client.call("GET", path, body = null, token = token, successStatus = 200) { parse(it) }
    }

    companion object {
        fun parse(text: String): ProvisioningBundle? {
            val json = JSONObject(text)
            fun text(key: String) = json.optString(key).takeIf { it.isNotEmpty() }
            val port = json.optInt("mqtt_port", 0).takeIf { it in 1..65535 } ?: return null
            return ProvisioningBundle(
                nodeId = text("node_id") ?: return null,
                mqttUser = text("mqtt_user") ?: return null,
                mqttPassword = text("mqtt_password") ?: return null,
                mqttHost = text("mqtt_host") ?: return null,
                mqttPort = port,
                capability = text("capability") ?: return null,
                occupancy = text("occupancy") ?: return null,
            )
        }

        /** 거부 사유를 사용자 문장으로 바꾼다. */
        fun failureText(result: ApiResult<*>): String = when (result) {
            is ApiResult.Rejected -> when (result.reason) {
                "NOT_OWNER" -> "이 기기의 소유자 토큰이 아니에요."
                "NOT_FOUND" -> "서버의 기기 목록(fleet.json)에 이 기기의 접속 정보가 없어요."
                "INVALID_TOKEN" -> "토큰이 맞지 않아요. 계정 탭에서 확인해 주세요."
                else -> "서버가 거부했어요 (${result.reason})."
            }
            ApiResult.NetworkError -> "서버에 연결하지 못했어요. 같은 Wi-Fi인지 확인해 주세요."
            is ApiResult.UnexpectedResponse -> "서버 응답이 예상과 달라요 (${result.status})."
            is ApiResult.Success -> ""
        }
    }
}
