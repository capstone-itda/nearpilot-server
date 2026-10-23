package com.nearpilot.app.account

import java.net.URI

/** 서버 주소. `http://` 또는 `https://`로 시작하고 끝에 `/`가 없다. */
object ServerAddress {
    /** 입력을 정리한다. 쓸 수 없는 주소면 null이다. */
    fun normalize(input: String): String? {
        var text = input.trim()
        if (text.isEmpty()) return null
        if (!text.contains("://")) text = "http://$text"
        text = text.trimEnd('/')
        val uri = runCatching { URI(text) }.getOrNull() ?: return null
        if (uri.scheme !in setOf("http", "https")) return null
        if (uri.host.isNullOrBlank()) return null
        return text
    }

    fun mcpUrl(baseUrl: String): String = "$baseUrl/mcp"
}
