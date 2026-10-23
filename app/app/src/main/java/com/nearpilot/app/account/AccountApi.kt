package com.nearpilot.app.account

import com.nearpilot.app.beacon.BeaconIdentifier
import org.json.JSONException
import org.json.JSONObject
import java.io.IOException

/** HTTP 요청 하나. 테스트에서는 가짜로 바꾼다. */
interface HttpTransport {
    @Throws(IOException::class)
    fun send(method: String, url: String, body: String?, token: String?): HttpResponse
}

data class HttpResponse(val status: Int, val body: String)

sealed interface ApiResult<out T> {
    data class Success<T>(val value: T) : ApiResult<T>

    /** 서버가 거부했다. `reason`은 서버가 보낸 사유 코드다. */
    data class Rejected(val status: Int, val reason: String) : ApiResult<Nothing>

    /** 서버에 닿지 못했다. */
    data object NetworkError : ApiResult<Nothing>

    /** 서버가 계약과 다른 응답을 보냈다. */
    data class UnexpectedResponse(val status: Int) : ApiResult<Nothing>
}

/** §5 REST "공통 규칙"의 요청과 응답 (#183). 거부 본문은 `{"error": "<사유 코드>"}`다. */
class RestClient(private val baseUrl: String, private val transport: HttpTransport) {

    fun <T> call(
        method: String,
        path: String,
        body: String?,
        token: String?,
        successStatus: Int,
        parse: (String) -> T?,
    ): ApiResult<T> {
        val response = try {
            transport.send(method, baseUrl + path, body, token)
        } catch (e: IOException) {
            return ApiResult.NetworkError
        }
        if (response.status == successStatus) {
            val value = try {
                parse(response.body)
            } catch (e: JSONException) {
                null
            }
            return value?.let { ApiResult.Success(it) } ?: ApiResult.UnexpectedResponse(response.status)
        }
        val reason = try {
            JSONObject(response.body).optString("error")
        } catch (e: JSONException) {
            ""
        }
        return if (reason.isNotEmpty()) ApiResult.Rejected(response.status, reason)
        else ApiResult.UnexpectedResponse(response.status)
    }
}

/** §5 REST "계정"의 두 경로 (#183). */
class AccountApi(baseUrl: String, transport: HttpTransport) {
    private val client = RestClient(baseUrl, transport)

    fun createAccount(name: String): ApiResult<String> {
        val body = JSONObject().put("name", name).toString()
        return client.call("POST", "/accounts", body, token = null, successStatus = 201) { text ->
            JSONObject(text).optString("token").takeIf { it.isNotEmpty() }
        }
    }

    fun putBeacon(token: String, identifier: BeaconIdentifier): ApiResult<Unit> {
        val body = JSONObject()
            .put("uuid", identifier.uuid)
            .put("major", identifier.major)
            .put("minor", identifier.minor)
            .toString()
        return client.call("PUT", "/me/beacon", body, token, successStatus = 200) { text ->
            JSONObject(text)
            Unit
        }
    }

    companion object {
        const val NAME_MAX_LENGTH = 64

        /** 서버와 같은 규칙. 앞뒤 공백을 지운 1~64자다. */
        fun normalizeName(input: String): String? = input.trim().takeIf { it.length in 1..NAME_MAX_LENGTH }
    }
}
