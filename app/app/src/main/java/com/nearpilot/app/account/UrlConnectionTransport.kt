package com.nearpilot.app.account

import android.util.Log
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/** `HttpURLConnection`으로 보낸다. 메인 스레드에서 부르지 않는다. */
class UrlConnectionTransport(private val timeoutMillis: Int = 5_000) : HttpTransport {
    private companion object {
        const val TAG = "NearPilotHttp"
    }

    override fun send(method: String, url: String, body: String?, token: String?): HttpResponse {
        val connection = try {
            URL(url).openConnection() as HttpURLConnection
        } catch (e: IllegalArgumentException) {
            throw IOException("잘못된 주소", e)
        }
        try {
            connection.requestMethod = method
            connection.connectTimeout = timeoutMillis
            connection.readTimeout = timeoutMillis
            connection.setRequestProperty("Accept", "application/json")
            if (token != null) connection.setRequestProperty("Authorization", "Bearer $token")
            if (body != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                connection.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            }
            val status = connection.responseCode
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
            return HttpResponse(status, text)
        } catch (e: IOException) {
            // 토큰이 들어갈 수 있는 요청 본문과 헤더는 남기지 않는다.
            Log.w(TAG, "$method ${URL(url).path} 실패: ${e.javaClass.simpleName}: ${e.message}")
            throw e
        } finally {
            connection.disconnect()
        }
    }
}
